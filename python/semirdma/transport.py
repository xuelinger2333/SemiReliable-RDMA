"""SemiRDMATransport — a one-directional semi-reliable RDMA sender/receiver.

The DDP hook (commit 6) constructs *two* transports per worker: one to push
gradient bytes to the peer and one to receive the peer's bytes.  Each
transport owns its own ``UCQPEngine`` (so its own MR, CQ, QP) and its own
``RatioController``.  We keep the two directions in separate QPs because:

  - UC QP is one-sided; Writes flow one way by definition.
  - Two QPs means the sender's CQEs (Write complete) and the receiver's CQEs
    (Recv-with-Imm) never interleave — simpler to poll.
  - It matches how real RDMA collectives (Gloo, NCCL) shape traffic.

This module is *pure Python + pybind11*.  No torch import here — we accept
raw ``bytes`` / ``memoryview`` / numpy arrays so the class stays usable from
unit tests that don't depend on torch.  The torch-to-bytes glue lives in
``hooks.py`` (commit 6).

Stage A scope:
  - ``post_gradient(data)``    : sender-side, fires N Write-with-Imm per bucket
  - ``await_gradient()``       : receiver-side, blocks until ratio or timeout
  - ``loss_rate > 0``          : sender drops a Bernoulli(p) chunk subset,
                                 mimicking Phase 2's software-loss methodology.
  - Stage A explicitly does NOT support GPU tensors (design §1.3); callers
    must stage into a CPU buffer first.
"""

from __future__ import annotations

import logging
import random
from typing import Optional, Union

import numpy as np

from semirdma._semirdma_ext import (
    ChunkSet,
    RatioController,
    RemoteMR,
    RemoteQpInfo,
    UCQPEngine,
    apply_ghost_mask,
)
from semirdma.config import TransportConfig

logger = logging.getLogger(__name__)

# Bytes-like things post_gradient accepts.  We normalize everything to a
# 1-D uint8 numpy view before copying into the MR.
BytesLike = Union[bytes, bytearray, memoryview, np.ndarray]


class SemiRDMATransport:
    """One direction of a 2-worker semi-reliable channel.

    Lifecycle:
        1. ``__init__(cfg)`` — allocates the C++ engine (UC QP in RESET).
        2. ``bring_up(remote_qp, remote_mr)`` — transitions to RTS.  Must be
           called with the peer's (qpn, gid, addr, rkey), typically obtained
           via ``semirdma._bootstrap.exchange_qp_info``.
        3. ``post_gradient`` / ``await_gradient`` can be invoked repeatedly.
        4. Destruction — the C++ destructor tears down the QP / MR / PD.

    After ``await_gradient`` returns the caller can read the receiver-side
    buffer via ``buffer_view()``; regions from chunks without a CQE have
    already been zeroed by ``GhostMask::apply``.
    """

    def __init__(self, cfg: TransportConfig) -> None:
        self._cfg = cfg
        self._engine = UCQPEngine(
            cfg.dev_name, cfg.buffer_bytes, cfg.sq_depth, cfg.rq_depth
        )
        self._ratio = RatioController(self._engine)
        self._wr_seq = 0  # monotonic wr_id for Writes; aids post-mortem debug
        self._brought_up = False
        # Receiver needs an outstanding Recv WR per incoming Write-with-Imm.
        # Pre-post one rq_depth batch here so the *first* await_gradient
        # doesn't race with bring_up.
        self._engine.post_recv_batch(cfg.rq_depth, base_wr_id=0)
        self._recv_base_wr = cfg.rq_depth  # next base for subsequent batches
        self._loss_rng = random.Random(cfg.loss_seed)
        self._remote_mr: Optional[RemoteMR] = None

    # -------- properties (read-only) -----------------------------------

    @property
    def cfg(self) -> TransportConfig:
        return self._cfg

    @property
    def local_qp_info(self) -> RemoteQpInfo:
        return self._engine.local_qp_info()

    @property
    def local_mr_info(self) -> RemoteMR:
        return self._engine.local_mr_info()

    @property
    def qpn(self) -> int:
        return self._engine.qpn

    def buffer_view(self) -> memoryview:
        """Writable memoryview over the whole registered MR (zero-copy).

        Shape: 1-D uint8, length == ``cfg.buffer_bytes``.  The receiver side
        reads from here after ``await_gradient``; the sender side copies
        gradient bytes here before calling ``post_gradient``.
        """
        return self._engine.local_buf_view()

    # -------- lifecycle ------------------------------------------------

    def bring_up(self, remote_qp: RemoteQpInfo, remote_mr: RemoteMR) -> None:
        """Transition the QP to RTS and cache the peer's MR descriptor.

        Idempotent: repeated calls are no-ops.  ``remote_mr`` is stored so
        ``post_gradient`` doesn't need the caller to repeat it.
        """
        if self._brought_up:
            return
        self._engine.bring_up(remote_qp)
        self._remote_mr = remote_mr
        self._brought_up = True
        logger.info(
            "SemiRDMATransport up: local qpn=%d -> remote qpn=%d, remote rkey=0x%x",
            self._engine.qpn, remote_qp.qpn, remote_mr.rkey,
        )

    # -------- data path (sender) --------------------------------------

    def post_gradient(
        self,
        data: BytesLike,
        *,
        base_offset: int = 0,
        remote_base_offset: int = 0,
    ) -> ChunkSet:
        """Copy ``data`` into the local MR and Write it to the peer in chunks.

        Args:
            data: up to ``cfg.buffer_bytes - base_offset`` bytes.  Copied into
                the MR; the caller may reuse its source buffer immediately.
            base_offset: byte offset in the *local* MR to write into.  Stage A
                uses 0 (single bucket per step).
            remote_base_offset: byte offset in the peer's MR.  Kept distinct
                from base_offset so Stage B can map multiple layers into one
                MR.

        Returns:
            ``ChunkSet`` describing the post; pass it to ``await_gradient`` on
            the receiver side.  Sender-side CQEs are drained opportunistically
            by ``drain_send_completions``; Stage A doesn't need them.
        """
        if not self._brought_up:
            raise RuntimeError("post_gradient called before bring_up")
        assert self._remote_mr is not None

        arr = _as_uint8(data)
        total = arr.size
        if total <= 0:
            raise ValueError("post_gradient: empty data")
        if base_offset + total > self._cfg.buffer_bytes:
            raise ValueError(
                f"post_gradient: {total} bytes @ off {base_offset} exceeds "
                f"buffer_bytes={self._cfg.buffer_bytes}"
            )

        mr_view = np.frombuffer(self._engine.local_buf_view(), dtype=np.uint8)
        mr_view[base_offset : base_offset + total] = arr

        cs = ChunkSet(base_offset, total, self._cfg.chunk_bytes)
        n_chunks = cs.size()
        n_posted = 0
        for i in range(n_chunks):
            chunk = cs.chunk(i)
            # Software loss: skip posting, receiver gets no CQE for this chunk,
            # GhostMask::apply zeroes the corresponding buffer region.
            if self._cfg.loss_rate > 0.0 and self._loss_rng.random() < self._cfg.loss_rate:
                continue
            self._wr_seq += 1
            # remote_offset = remote_base + (chunk.local_offset - base_offset)
            remote_off = remote_base_offset + (chunk["local_offset"] - base_offset)
            self._engine.post_write(
                wr_id=self._wr_seq,
                local_offset=chunk["local_offset"],
                remote_offset=remote_off,
                length=chunk["length"],
                remote=self._remote_mr,
                with_imm=True,
                imm_data=chunk["chunk_id"],
            )
            n_posted += 1
        logger.debug(
            "post_gradient: %d/%d chunks posted (loss_rate=%.3f, total=%d B)",
            n_posted, n_chunks, self._cfg.loss_rate, total,
        )
        return cs

    def drain_send_completions(self, max_n: int = 64) -> int:
        """Non-blocking poll of sender-side CQ.  Stage A doesn't act on these
        beyond preventing CQ fill-up; Stage B's flow control will care.

        Returns the number of CQEs drained.
        """
        cqes = self._engine.poll_cq(max_n, 0)
        return len(cqes)

    # -------- data path (receiver) ------------------------------------

    def await_gradient(
        self,
        cs: ChunkSet,
        *,
        ratio: Optional[float] = None,
        timeout_ms: Optional[int] = None,
    ) -> dict:
        """Wait for at least ``ratio`` of ``cs``'s chunks to arrive, then
        apply the ghost mask to the receiver-side buffer.

        Args:
            cs: the *receiver-side* ChunkSet, typically reconstructed from the
                same (base_offset, total_bytes, chunk_bytes) the sender used.
                Must be a fresh instance — its completion bitmap is filled in
                by the RatioController as RECV_RDMA_WITH_IMM CQEs arrive.
            ratio: override cfg.ratio for this single call.
            timeout_ms: override cfg.timeout_ms for this single call.

        Returns:
            stats dict from ``RatioController.wait_for_ratio`` with one extra
            key: ``chunks_total`` — the ``cs.size()`` at entry.  Useful for
            the hook's completion CSV.
        """
        if not self._brought_up:
            raise RuntimeError("await_gradient called before bring_up")

        r = self._cfg.ratio if ratio is None else ratio
        t = self._cfg.timeout_ms if timeout_ms is None else timeout_ms
        stats = self._ratio.wait_for_ratio(cs, r, t)
        stats["chunks_total"] = cs.size()

        # Zero out regions from chunks that never arrived.  Must happen
        # *before* the caller reads the buffer, otherwise stale bytes from
        # a previous step would masquerade as this step's gradient — the
        # "ghost gradient" problem (RQ2).
        buf = np.frombuffer(self._engine.local_buf_view(), dtype=np.uint8)
        apply_ghost_mask(buf, cs)

        # Refill the RQ to keep up with the incoming Write-with-Imm stream.
        # ``outstanding_recv()`` returns the current surplus, so we only
        # re-post what was consumed.
        posted = cs.num_completed()
        if posted > 0:
            self._engine.post_recv_batch(posted, base_wr_id=self._recv_base_wr)
            self._recv_base_wr += posted
        return stats

    def outstanding_recv(self) -> int:
        return self._engine.outstanding_recv()


def _as_uint8(data: BytesLike) -> np.ndarray:
    """Return a 1-D contiguous uint8 view of ``data`` without copying when
    possible.  Caller guarantees the source outlives the copy-into-MR step.
    """
    if isinstance(data, np.ndarray):
        if data.dtype != np.uint8:
            data = data.view(np.uint8)
        if not data.flags["C_CONTIGUOUS"]:
            data = np.ascontiguousarray(data)
        return data.reshape(-1)
    # bytes / bytearray / memoryview — frombuffer is zero-copy for the
    # backing store (bytearray / memoryview); bytes is already immutable.
    return np.frombuffer(data, dtype=np.uint8)


__all__ = ["SemiRDMATransport"]
