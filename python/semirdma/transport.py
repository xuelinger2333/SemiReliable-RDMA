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

Tunables (DEBUG_LOG.md hypothesis L.2 — quiescent-based leftover drain):
  ``_DRAIN_QUIESCENT_THRESHOLD_US`` — break the post-ratio leftover drain
      once this many microseconds have passed since the last RECV CQE was
      marked on ChunkSet.  Default 200 µs is tuned for ResNet-18 at
      chunk_bytes=4096 (~10913 chunks/bucket).  Larger models with sparser
      CQE generation may need recalibration; document threshold + chunk
      count in any cross-model paper claim.
  ``_DRAIN_MAX_US`` — hard ceiling on total leftover drain time, kept so
      a pathological CQE-generation stall cannot block the bucket
      indefinitely.  If hit, fall through to apply_ghost_mask (the still-
      missing CQEs become ghost chunks).
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional, Union

import numpy as np

# DEBUG_LOG.md hypothesis L.2: drain late RECV CQEs until either no new CQE
# arrives for QUIESCENT_THRESHOLD_US, or MAX_DRAIN_US elapses.  Tuned for
# chunk_bytes=4096 / ResNet-18 (~10913 chunks/bucket) on CX-5 25 GbE; larger
# chunk counts may need recalibration.
_DRAIN_QUIESCENT_THRESHOLD_NS = 200 * 1_000   # 200 µs
_DRAIN_MAX_NS                 = 5_000 * 1_000  # 5 ms hard ceiling

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

        drain_deadline_ms = max(50, self._cfg.timeout_ms)

        # Per-chunk Python loop.  Production path on CX-5.
        #
        # We tried moving this loop to C++ via UCQPEngine::post_bucket_chunks
        # (commits f5466d8 / 4de0c99 / 5e71813 / 389b740) hoping to skip
        # ~30K Python ↔ pybind boundary crossings per bucket.  All three
        # variants (chained-WR / per-WR no chain / per-WR + 5-10 µs
        # busy-wait) ran with WORSE delivery (70%) or worse iter_ms
        # (1000+ ms) than this Python loop's ~99% / 858 ms baseline.
        #
        # Why this Python loop "happens to work" while the C++ tight loop
        # does not is NOT YET ROOT-CAUSED.  Candidate mechanisms (see
        # DEBUG_LOG.md hypotheses G–K):
        #   - receiver SRQ refill cannot keep up with sender at ~1 µs/WR
        #     (most likely; Python sender is slow enough to stay in sync)
        #   - libmlx5 BlueFlame doorbell batching interaction
        #   - sender-side SQ overflow silently swallowed
        #   - per-QP behavior fixable via multi-QP fanout
        # `ib_write_bw -c UC -q 1 -s 4096` runs at 1.33 µs/WR with 0 loss
        # on the same NIC, so this is NOT a CX-5 hardware cliff.
        #
        # The C++ post_bucket_chunks is kept in UCQPEngine for SoftRoCE
        # bring-ups (per_wr_pace_us=0 fine there) and as the future
        # production path once the CX-5-specific cause is identified.
        # See DEBUG_PROTOCOL.md before debugging this further.
        cs = ChunkSet(base_offset, total, self._cfg.chunk_bytes)
        n_chunks = cs.size()
        capacity = max(1, self._cfg.sq_depth - 1)
        inflight = 0
        n_posted = 0
        for i in range(n_chunks):
            chunk = cs.chunk(i)
            # Software loss path (cfg.loss_rate > 0) — used by Phase-2
            # SoftRoCE simulation; real-wire P2 always sets loss_rate=0
            # and the entire bucket goes through.
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

        n_drained_in_tail = 0
        while inflight > 0:
            cqes = self._engine.poll_cq(capacity, drain_deadline_ms)
            if not cqes:
                break
            inflight = max(0, inflight - len(cqes))
            n_drained_in_tail += len(cqes)

        if inflight > 0:
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

        # CRITICAL ORDERING FIX (2026-04-25, DEBUG_LOG.md hypothesis L):
        # Before this fix, apply_ghost_mask ran *immediately* after
        # wait_for_ratio, then a "leftover drain" loop merely *logged*
        # the late CQEs without updating ChunkSet.  Result on benign wire
        # (drop_rate=0) at SEED=42 cell #3:
        #   - wait_for_ratio returned with cs.num_completed = 10860 / 10913
        #   - leftover drain found 53 more RECV_RDMA_WITH_IMM CQEs
        #     (10860 + 53 = 10913 → all chunks DID arrive)
        #   - apply_ghost_mask had already zeroed 53 perfectly-delivered
        #     chunks because cs.state(i).has_cqe was still False
        #   - Effective receive ratio = 99.5% (artificial, software-
        #     introduced; not a wire/NIC property).
        # Fix: drain late CQEs into ChunkSet *before* applying ghost mask.

        # Step 1: drain late CQEs into ChunkSet.
        # ratio_controller exits as soon as the threshold is hit; CQEs
        # whose imm_data corresponds to chunks that arrived in the
        # microseconds AFTER threshold but BEFORE wait_for_ratio observed
        # them are still sitting in the CQ.  Mark them on cs so
        # apply_ghost_mask doesn't zero their (delivered) data.
        #
        # Drain loop: poll-until-quiescent.  Break when no new RECV CQE
        # has arrived for ``_DRAIN_QUIESCENT_THRESHOLD_NS`` (default 200 µs)
        # or total drain time has reached ``_DRAIN_MAX_NS`` (default 5 ms,
        # pathology fallback).  An earlier first-zero-poll heuristic
        # exited too early when the NIC's CQE generation took a brief
        # micro-pause mid-tail (DEBUG_LOG.md L.2).
        leftover_recv_ok = 0
        leftover_recv_err = 0
        leftover_other = 0
        leftover_imm_unique = set()
        drain_start_ns = time.monotonic_ns()
        last_cqe_ns    = drain_start_ns
        drain_aborted_at_max = False
        while True:
            cqes = self._engine.poll_cq(16384, 0)
            if cqes:
                for c in cqes:
                    op = c.get("opcode_name")
                    st = c.get("status")
                    if op == "RECV_RDMA_WITH_IMM" and st == 0:
                        imm = int(c.get("imm_data", 0))
                        cs.mark_completed(imm)
                        leftover_recv_ok += 1
                        leftover_imm_unique.add(imm)
                    elif op in ("RECV", "RECV_RDMA_WITH_IMM"):
                        leftover_recv_err += 1
                    else:
                        leftover_other += 1
                last_cqe_ns = time.monotonic_ns()
                continue   # don't check quiescence on the same iteration
            now_ns = time.monotonic_ns()
            if (now_ns - last_cqe_ns) >= _DRAIN_QUIESCENT_THRESHOLD_NS:
                break  # quiescent — drain complete
            if (now_ns - drain_start_ns) >= _DRAIN_MAX_NS:
                drain_aborted_at_max = True
                break  # pathology fallback

        # Step 2: snapshot completion stats AFTER leftover drain.
        n_completed = cs.num_completed()
        n_expected = cs.size()
        out_recv_pre = self._engine.outstanding_recv()

        # Step 3: ghost-mask only the chunks that truly never arrived.
        # On a benign wire this should now zero zero chunks; ratio threshold
        # + leftover drain together capture the full delivery.
        buf = np.frombuffer(self._engine.local_buf_view(), dtype=np.uint8)
        apply_ghost_mask(buf, cs)

        drain_total_us = (time.monotonic_ns() - drain_start_ns) / 1000.0
        logger.info(
            "await_gradient DIAG: completed=%d/%d outstanding_recv_pre=%d "
            "ok=%s timed_out=%s latency_ms=%.2f "
            "LEFTOVER_after_wait: recv_ok=%d recv_err=%d other=%d unique_imm=%d "
            "drain_us=%.0f drain_max_aborted=%d",
            n_completed, n_expected, out_recv_pre,
            stats.get("ok"), stats.get("timed_out"),
            stats.get("latency_ms", 0.0),
            leftover_recv_ok, leftover_recv_err, leftover_other,
            len(leftover_imm_unique),
            drain_total_us, int(drain_aborted_at_max),
        )

        # DIAG3: positional histogram of MISSING chunk_ids — answers
        # "are the residual losses concentrated at start of bucket
        # (NIC TX cold-start) vs end of bucket (tail race) vs uniform".
        # Only emit when there are actually missing chunks AND missing
        # rate is small enough to make positional analysis cheap.
        if 0 < (n_expected - n_completed) <= 1024:
            missing = [i for i in range(n_expected) if not cs.state(i)["has_cqe"]]
            if missing:
                # 10-bucket histogram across [0, n_expected)
                bins = [0] * 10
                for m in missing:
                    bins[min(9, m * 10 // n_expected)] += 1
                logger.info(
                    "await_gradient DIAG3 missing_pos: count=%d  first=%d  last=%d  "
                    "hist10=[%s]",
                    len(missing), missing[0], missing[-1],
                    ",".join(str(b) for b in bins),
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
