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
        # Forward qp_type + RC params even though SemiRDMA's primary caller
        # always uses "uc" — lets the baselines subpackage reuse this same
        # transport class with qp_type="rc" at construction time.  RC attrs
        # are no-ops when qp_type="uc".
        self._engine = UCQPEngine(
            cfg.dev_name, cfg.buffer_bytes, cfg.sq_depth, cfg.rq_depth,
            gid_index=cfg.gid_index,
            qp_type=cfg.qp_type,
            rc_timeout=cfg.rc_timeout,
            rc_retry_cnt=cfg.rc_retry_cnt,
            rc_rnr_retry=cfg.rc_rnr_retry,
            rc_min_rnr_timer=cfg.rc_min_rnr_timer,
            rc_max_rd_atomic=cfg.rc_max_rd_atomic,
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
        # SQ flow control: a bucket may need thousands of chunks (ResNet-18's
        # ~44 MB / 16 KB ≈ 2700) while sq_depth is O(16).  Post in waves,
        # draining Write CQEs each time the SQ approaches full.  We leave one
        # slot of slack so a transient spurious inflight count won't trip
        # ibv_post_send.
        capacity = max(1, self._cfg.sq_depth - 1)
        inflight = 0
        n_posted = 0
        for i in range(n_chunks):
            chunk = cs.chunk(i)
            # Software loss: skip posting, receiver gets no CQE for this chunk,
            # GhostMask::apply zeroes the corresponding buffer region.
            if self._cfg.loss_rate > 0.0 and self._loss_rng.random() < self._cfg.loss_rate:
                continue
            while inflight >= capacity:
                cqes = self._engine.poll_cq(capacity, 1)
                inflight = max(0, inflight - len(cqes))
            self._wr_seq += 1
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
            inflight += 1
            n_posted += 1

        # Drain the tail so the next bucket starts clean.  Bound the wait so a
        # lost Write (software loss on the peer's rx path — not possible with
        # our current model, but defensive) can't hang the sender.
        drain_deadline_ms = max(50, self._cfg.timeout_ms)
        n_drained_in_tail = 0
        while inflight > 0:
            cqes = self._engine.poll_cq(capacity, drain_deadline_ms)
            if not cqes:
                break
            inflight = max(0, inflight - len(cqes))
            n_drained_in_tail += len(cqes)

        # DIAG: if the tail drain bailed with inflight>0, those send CQEs are
        # still pending (will get drained on next call's wave-throttle).  Log
        # so we can see whether sender is leaking inflight across buckets.
        if inflight > 0 or n_posted != n_drained_in_tail + (n_posted - inflight - n_drained_in_tail):
            logger.warning(
                "post_gradient DIAG: n_posted=%d tail_drained=%d inflight_left=%d",
                n_posted, n_drained_in_tail, inflight,
            )
        else:
            logger.info(
                "post_gradient DIAG: n_posted=%d all CQEs drained",
                n_posted,
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

        # Receive target is dynamic: the sender skipped ``loss_rate`` of the
        # chunks, so we should wait for ``1 - loss_rate - jitter_slack`` of
        # them before falling through to GhostMask.  The cfg.ratio value
        # acts as a safety floor (e.g. cap at 0.95 so we never wait forever
        # when loss_rate is tiny but the wire is genuinely flaky).
        #
        # Prior to this fix, r was hard-pinned at cfg.ratio (0.95), which
        # meant the effective receive-side drop rate was always
        # max(cfg.loss_rate, 5%) regardless of what the sender actually
        # did — see docs/phase3/rq6-semirdma-effective-loss-analysis.md.
        if ratio is None:
            dyn_target = 1.0 - self._cfg.loss_rate - 0.005   # 0.5% jitter slack
            r = max(self._cfg.ratio, dyn_target)
        else:
            r = ratio
        t = self._cfg.timeout_ms if timeout_ms is None else timeout_ms
        stats = self._ratio.wait_for_ratio(cs, r, t)
        stats["chunks_total"] = cs.size()

        # Zero out regions from chunks that never arrived.  Must happen
        # *before* the caller reads the buffer, otherwise stale bytes from
        # a previous step would masquerade as this step's gradient — the
        # "ghost gradient" problem (RQ2).
        buf = np.frombuffer(self._engine.local_buf_view(), dtype=np.uint8)
        apply_ghost_mask(buf, cs)

        # DIAG: capture per-step receive-side counters BEFORE refill, to
        # diagnose RQ depletion hypothesis.  outstanding_recv() == current
        # number of Recv WRs still posted but not yet consumed by an
        # incoming Write-with-Imm.  Cumulative consumed = (initial rq_depth +
        # all post_recv_batch increments) - outstanding_recv.
        n_completed = cs.num_completed()
        n_expected = cs.size()
        out_recv_pre = self._engine.outstanding_recv()

        # DIAG2: drain whatever CQEs are STILL sitting in the CQ right after
        # wait_for_ratio returned.  This separates "CQEs never arrived"
        # (post-drain count == 0) from "wait_for_ratio undercounted"
        # (post-drain count > 0).  Bucket by status / opcode to catch any
        # error-status CQEs that the C++ ratio loop silently skipped.
        leftover_recv_ok = 0
        leftover_recv_err = 0
        leftover_other = 0
        leftover_imm_unique = set()
        for _ in range(64):  # up to 64 batches × 16384 = 1M CQEs (cap)
            cqes = self._engine.poll_cq(16384, 0)
            if not cqes:
                break
            for c in cqes:
                op = c.get("opcode_name")
                st = c.get("status")
                if op == "RECV_RDMA_WITH_IMM" and st == 0:
                    leftover_recv_ok += 1
                    leftover_imm_unique.add(int(c.get("imm_data", 0)))
                elif op in ("RECV", "RECV_RDMA_WITH_IMM"):
                    leftover_recv_err += 1
                else:
                    leftover_other += 1

        logger.info(
            "await_gradient DIAG: completed=%d/%d outstanding_recv_pre=%d "
            "ok=%s timed_out=%s latency_ms=%.2f "
            "LEFTOVER_after_wait: recv_ok=%d recv_err=%d other=%d unique_imm=%d",
            n_completed, n_expected, out_recv_pre,
            stats.get("ok"), stats.get("timed_out"),
            stats.get("latency_ms", 0.0),
            leftover_recv_ok, leftover_recv_err, leftover_other,
            len(leftover_imm_unique),
        )

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
