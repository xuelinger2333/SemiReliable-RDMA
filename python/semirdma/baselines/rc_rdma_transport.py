"""ReliableRDMATransport — HW-reliable RC QP over the same UCQPEngine.

Reuses the exact C++ engine that powers the SemiRDMA core (UC) but
constructs it with ``qp_type="rc"`` so the NIC performs ACK / retransmit
/ retry-exhausted error handling in hardware.  This is the same thing
NCCL does internally — both call ``ibv_create_qp`` with ``IBV_QPT_RC``
— so the path is HW-official, not a self-built reliability layer.

Why we don't extend SemiRDMATransport instead:
  - SemiRDMATransport bakes in ratio-based early exit and ghost-mask
    zeroing (the point of semi-reliability).  RC wants the opposite:
    every CQE or fatal error; no masking.
  - Keeping the RC path in a separate class makes the baseline isolated
    (can be deleted if the paper drops it) and keeps SemiRDMATransport's
    unit tests focused on semi-reliability semantics.

Data path:
  - ``post_bucket(data)``: copy into MR, post N Write-with-Imm WRs with
    SQ wave throttling, then poll the send CQ until all N send CQEs
    have arrived.  Any IBV_WC_RETRY_EXC_ERR → raise (mimics a real RC
    collapse, what we want to observe under XDP middlebox drops).
  - ``await_bucket(cs)``: poll the recv CQ until all N recv CQEs
    arrive.  With HW-reliable RC every chunk eventually arrives (or the
    peer's send side raised).

No loss_rate simulation, no ghost mask, no RatioController.
"""

from __future__ import annotations

import logging
from typing import Optional, Union

import numpy as np

from semirdma._semirdma_ext import (
    ChunkSet,
    RemoteMR,
    RemoteQpInfo,
    UCQPEngine,
)
from semirdma.config import TransportConfig

logger = logging.getLogger(__name__)

BytesLike = Union[bytes, bytearray, memoryview, np.ndarray]


class ReliableRDMATransport:
    """One direction of a 2-worker reliable RC-RDMA channel.

    Lifecycle mirrors ``SemiRDMATransport`` (__init__ -> bring_up ->
    post_bucket / await_bucket).  Constructor raises if
    ``cfg.qp_type != "rc"``.
    """

    # How long to wait between CQE polls when draining (ms).  Each poll is
    # non-blocking + a 1 ms sleep internally (engine.poll_cq(n, 1)).  We
    # cap total wait via a deadline in the outer loop, not here.
    _POLL_CHUNK_MS = 1

    # Default hard deadline for draining a single bucket's send-CQEs.  RC
    # retry chain = rc_timeout × 2^... × retry_cnt; at rc_timeout=14 /
    # retry_cnt=7 the worst single-chunk retry is ~500 ms.  With thousands
    # of chunks per bucket and wire drop ≥ 1 %, we accept that a single
    # bucket can take several seconds under stress — that IS the RC tail
    # latency story we want to measure.  The deadline is large enough that
    # normal (drop=0) runs never hit it but small enough that a truly hung
    # QP aborts within one smoke cell rather than the whole matrix.
    _SEND_DRAIN_DEADLINE_MS = 30_000

    # Same budget for receive-side waiting.
    _RECV_WAIT_DEADLINE_MS = 30_000

    def __init__(self, cfg: TransportConfig) -> None:
        if cfg.qp_type != "rc":
            raise ValueError(
                "ReliableRDMATransport requires qp_type='rc' in TransportConfig"
            )
        self._cfg = cfg
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
        self._wr_seq = 0
        self._brought_up = False
        # Pre-post one RQ batch so the first await_bucket doesn't race.
        self._engine.post_recv_batch(cfg.rq_depth, base_wr_id=0)
        self._recv_base_wr = cfg.rq_depth
        self._remote_mr: Optional[RemoteMR] = None

    # -------- read-only accessors (mirrors SemiRDMATransport) ---------

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
        return self._engine.local_buf_view()

    # -------- lifecycle -----------------------------------------------

    def bring_up(self, remote_qp: RemoteQpInfo, remote_mr: RemoteMR) -> None:
        if self._brought_up:
            return
        self._engine.bring_up(remote_qp)
        self._remote_mr = remote_mr
        self._brought_up = True
        logger.info(
            "ReliableRDMATransport up: local qpn=%d -> remote qpn=%d, "
            "remote rkey=0x%x (qp_type=rc, timeout=%d, retry_cnt=%d)",
            self._engine.qpn, remote_qp.qpn, remote_mr.rkey,
            self._cfg.rc_timeout, self._cfg.rc_retry_cnt,
        )

    # -------- sender side ---------------------------------------------

    def post_bucket(
        self,
        data: BytesLike,
        *,
        base_offset: int = 0,
        remote_base_offset: int = 0,
    ) -> ChunkSet:
        """Copy ``data`` into MR and post every chunk as Write-with-Imm.

        Blocks until every posted WR has produced a send-CQE.  Raises
        RuntimeError on any non-SUCCESS CQE (RC retry-exhausted shows up
        here as IBV_WC_RETRY_EXC_ERR; that IS the "RC崩" signal the
        baseline is meant to expose).
        """
        if not self._brought_up:
            raise RuntimeError("post_bucket called before bring_up")
        assert self._remote_mr is not None

        arr = _as_uint8(data)
        total = arr.size
        if total <= 0:
            raise ValueError("post_bucket: empty data")
        if base_offset + total > self._cfg.buffer_bytes:
            raise ValueError(
                f"post_bucket: {total} B @ off {base_offset} exceeds "
                f"buffer_bytes={self._cfg.buffer_bytes}"
            )

        mr_view = np.frombuffer(self._engine.local_buf_view(), dtype=np.uint8)
        mr_view[base_offset : base_offset + total] = arr

        cs = ChunkSet(base_offset, total, self._cfg.chunk_bytes)
        n_chunks = cs.size()

        # SQ wave throttling — same pattern as SemiRDMATransport but with
        # NO loss_rate skip branch (this is the reliable path).
        capacity = max(1, self._cfg.sq_depth - 1)
        inflight = 0
        import time
        t_deadline = time.monotonic() + self._SEND_DRAIN_DEADLINE_MS / 1000.0

        for i in range(n_chunks):
            chunk = cs.chunk(i)
            while inflight >= capacity:
                cqes = self._engine.poll_cq(capacity, self._POLL_CHUNK_MS)
                _check_send_cqes(cqes)
                inflight = max(0, inflight - len(cqes))
                if time.monotonic() > t_deadline:
                    raise RuntimeError(
                        f"post_bucket: SQ drain deadline exceeded "
                        f"({self._SEND_DRAIN_DEADLINE_MS} ms) with "
                        f"{inflight} inflight; QP likely hung on "
                        f"retry-exhausted loss"
                    )
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

        # Final drain: keep polling until every posted WR has completed.
        # Unlike SemiRDMATransport we do NOT bail out on empty polls — for
        # RC an empty poll just means retries are in flight.  Instead the
        # hard deadline above is the only way out.
        while inflight > 0:
            cqes = self._engine.poll_cq(capacity, self._POLL_CHUNK_MS)
            _check_send_cqes(cqes)
            inflight = max(0, inflight - len(cqes))
            if time.monotonic() > t_deadline:
                raise RuntimeError(
                    f"post_bucket: final drain deadline exceeded "
                    f"({self._SEND_DRAIN_DEADLINE_MS} ms) with "
                    f"{inflight} inflight; QP likely hung on "
                    f"retry-exhausted loss"
                )

        logger.debug("post_bucket: %d chunks (total=%d B)", n_chunks, total)
        return cs

    # -------- receiver side -------------------------------------------

    def await_bucket(
        self,
        cs: ChunkSet,
    ) -> dict:
        """Wait for every chunk in ``cs`` to produce a recv-with-Imm CQE.

        Returns a stats dict for parity with ``await_gradient``.  Raises
        if any recv-CQE carries a non-SUCCESS status (shouldn't happen
        under HW-reliable RC unless the peer's QP entered ERR state).
        """
        if not self._brought_up:
            raise RuntimeError("await_bucket called before bring_up")

        import time
        n_chunks = cs.size()
        received = 0
        t0 = time.monotonic()
        t_deadline = t0 + self._RECV_WAIT_DEADLINE_MS / 1000.0

        # Drain the recv CQ.  We don't need chunk-ID tracking (RC
        # guarantees in-order delivery for a single QP), so just count
        # recv-CQEs until we've seen them all.
        while received < n_chunks:
            cqes = self._engine.poll_cq(n_chunks - received, self._POLL_CHUNK_MS)
            for c in cqes:
                name = c["opcode_name"]
                if name == "RECV_RDMA_WITH_IMM" or name == "RECV":
                    if c["status"] != 0:
                        raise RuntimeError(
                            f"await_bucket: recv CQE error: "
                            f"status={c['status_name']}, imm={c['imm_data']}"
                        )
                    # Mark chunk as delivered (enables any downstream
                    # debugging that inspects cs state).
                    cs.mark_completed(int(c["imm_data"]))
                    received += 1
            if time.monotonic() > t_deadline:
                raise RuntimeError(
                    f"await_bucket: recv deadline exceeded "
                    f"({self._RECV_WAIT_DEADLINE_MS} ms); "
                    f"received {received}/{n_chunks} chunks"
                )

        latency_ms = (time.monotonic() - t0) * 1000.0

        # Refill the RQ for the next bucket.
        self._engine.post_recv_batch(n_chunks, base_wr_id=self._recv_base_wr)
        self._recv_base_wr += n_chunks

        return {
            "ok": True,
            "latency_ms": latency_ms,
            "completed": n_chunks,
            "chunks_total": n_chunks,
            "timed_out": False,
        }

    def drain_send_completions(self, max_n: int = 64) -> int:
        """Non-blocking poll; mirrors SemiRDMATransport.  Useful if the
        caller wants to flush stale CQEs between buckets."""
        cqes = self._engine.poll_cq(max_n, 0)
        _check_send_cqes(cqes)
        return len(cqes)

    def outstanding_recv(self) -> int:
        return self._engine.outstanding_recv()


# ---------------------------------------------------------------------
# module-private helpers
# ---------------------------------------------------------------------

def _check_send_cqes(cqes) -> None:
    """Raise if any CQE in ``cqes`` is a non-SUCCESS send completion.

    For an RC QP, a send-side error (typically IBV_WC_RETRY_EXC_ERR when
    retries are exhausted on a lossy wire) transitions the QP to ERR
    state — there's no recovery.  Bubbling this up as RuntimeError is
    exactly what the RC-Baseline experiment wants to show: the transport
    gives up rather than silently degrading.
    """
    for c in cqes:
        name = c["opcode_name"]
        if name in ("RDMA_WRITE", "SEND") and c["status"] != 0:
            raise RuntimeError(
                f"RC send CQE error: status={c['status_name']} "
                f"(wr_id={c['wr_id']}).  QP now in ERR; retry-exhausted "
                f"loss on a reliable wire is fatal by design."
            )


def _as_uint8(data: BytesLike) -> np.ndarray:
    if isinstance(data, np.ndarray):
        if data.dtype != np.uint8:
            data = data.view(np.uint8)
        if not data.flags["C_CONTIGUOUS"]:
            data = np.ascontiguousarray(data)
        return data.reshape(-1)
    return np.frombuffer(data, dtype=np.uint8)


__all__ = ["ReliableRDMATransport"]
