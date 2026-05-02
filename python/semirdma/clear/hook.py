"""DDP communication hook backed by CLEAR transport.

Phase 5 W2.3e: bidirectional 2-rank allreduce via CLEAR. Each rank
owns:
  - ``tx`` ClearTransport — outbound UC + control plane (paired with
    peer's rx)
  - ``rx`` ClearTransport — inbound UC + control plane (paired with
    peer's tx)
  - one background poll thread per control plane that drains
    BEGIN/WITNESS/FINALIZE/RETIRE callbacks asynchronously

Per bucket, the hook spawns two worker threads — one driving
``clear_send_bucket`` on tx, one driving ``clear_recv_bucket`` on rx —
because UC is one-directional and both directions must run in parallel
to avoid the deadlock where both ranks await FINALIZE without ever
draining peer's incoming UC writes.

World size is 2 (matching Phase 4 SemiRDMAHookState scope). Multi-rank
ring allreduce is future work.

The hook returns a torch.futures.Future that resolves to
``(local_bucket + peer_bucket_after_mask) / world_size``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

from semirdma._semirdma_ext.clear import (
    BeginPayload,
    FinalizeDecision,
    Policy,
    RetirePayload,
)
from .manifest import BucketManifest, canonical_rank_pair, uid_hash
from .policy import PolicyRegistry
from .protocol import (
    chunkset_to_recv_bitmap,
    clear_recv_bucket,
    clear_send_bucket,
)
from .runtime import apply_finalize as py_apply_finalize
from .transport import ClearTransport, ClearTransportConfig

logger = logging.getLogger(__name__)


@dataclass
class _PerUidSync:
    """Per-uid synchronization primitives populated by the bg poll thread."""

    finalize_event: threading.Event = field(default_factory=threading.Event)
    finalize_decision: Optional[FinalizeDecision] = None
    begin_event: threading.Event = field(default_factory=threading.Event)
    begin_slot: int = 0
    begin_gen: int = 0


@dataclass
class ClearHookState:
    """Hook state for one rank in a 2-rank CLEAR allreduce."""

    rank: int
    world_size: int
    cfg: ClearTransportConfig

    tx: ClearTransport
    rx: ClearTransport

    manifest: BucketManifest = field(default_factory=BucketManifest)
    policy_registry: PolicyRegistry = field(default_factory=PolicyRegistry)
    default_policy: Policy = Policy.MASK_FIRST

    # Per-uid sync state. Bg thread populates; foreground hook awaits.
    _sync: Dict[int, _PerUidSync] = field(default_factory=dict)
    _sync_lock: threading.Lock = field(default_factory=threading.Lock)

    # Foreground bucket-exchange lock — DDP fires buckets in serial on
    # one thread, but multiple bucket calls can otherwise race the lease
    # tables.
    _bucket_lock: threading.Lock = field(default_factory=threading.Lock)

    # Background poll thread.
    _poll_stop: threading.Event = field(default_factory=threading.Event)
    _poll_thread: Optional[threading.Thread] = None

    # Per-step counter for uid construction.
    step_seq: int = 0

    # Cache of canonical rank_pair (depends only on rank + peer_rank).
    _rank_pair: int = 0

    # Optional per-call timing log. When non-None, ``_run_clear_bucket``
    # appends one dict per invocation with stage-by-stage ms. Off by
    # default; trainer enables by setting state.perf_log = [].
    perf_log: Optional[list] = None

    def _get_sync(self, uid: int) -> _PerUidSync:
        with self._sync_lock:
            s = self._sync.get(uid)
            if s is None:
                s = _PerUidSync()
                self._sync[uid] = s
            return s

    def _drop_sync(self, uid: int) -> None:
        with self._sync_lock:
            self._sync.pop(uid, None)

    @property
    def peer_rank(self) -> int:
        return 1 - self.rank

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def for_rank(
        cls,
        *,
        rank: int,
        world_size: int,
        peer_host: str,
        port: int,
        cfg: ClearTransportConfig,
        connect_timeout_s: float = 30.0,
    ) -> "ClearHookState":
        """Build one rank's hook state via TCP bootstrap with the peer.

        World size 2 only (matching ``for_in_process_pair`` scope). The
        bootstrap uses **4 sequential TCP ports** starting at ``port``:

          - port+0: my.tx.data ↔ peer.rx.data
          - port+1: my.tx.cp   ↔ peer.rx.cp   (control plane MR is unused)
          - port+2: my.rx.data ↔ peer.tx.data
          - port+3: my.rx.cp   ↔ peer.tx.cp

        Rank 0 listens on each port; rank 1 connects. Both sides bring
        up afterwards, with the rx data plane pre-posting recv WRs to
        avoid the symmetric UC bring-up race that bit ``for_in_process_pair``.
        """
        if world_size != 2:
            raise NotImplementedError(
                f"ClearHookState.for_rank: world_size=2 only "
                f"(got {world_size}); ring allreduce is future work")
        if rank not in (0, 1):
            raise ValueError(f"rank must be 0 or 1, got {rank}")

        from semirdma._bootstrap import exchange_qp_info

        is_server = (rank == 0)

        tx = ClearTransport(cfg)
        rx = ClearTransport(cfg)

        # The 4 ports establish 4 directional links:
        #   port+0  rank0.tx.data → rank1.rx.data
        #   port+1  rank0.tx.cp   ↔ rank1.rx.cp
        #   port+2  rank1.tx.data → rank0.rx.data
        #   port+3  rank1.tx.cp   ↔ rank0.rx.cp
        # Per port, the two ranks must send DIFFERENT sides of their
        # transport; otherwise both sides ship the same QP info and the
        # tx-side ends up wired to peer's tx (UC traffic blackholed).
        if rank == 0:
            # port+0 — we ship tx.data, peer ships rx.data → we recv peer.rx.data
            peer_rx_data_qp, peer_rx_data_mr = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 0,
                local_qp=tx.data_qp_info, local_mr=tx.data_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
            peer_rx_cp_qp, _ = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 1,
                local_qp=tx.control_qp_info, local_mr=tx.control_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
            # port+2 — we ship rx.data, peer ships tx.data → we recv peer.tx.data
            peer_tx_data_qp, peer_tx_data_mr = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 2,
                local_qp=rx.data_qp_info, local_mr=rx.data_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
            peer_tx_cp_qp, _ = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 3,
                local_qp=rx.control_qp_info, local_mr=rx.control_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
        else:
            # rank 1 — mirror image: ship rx.data on port+0, tx.data on port+2.
            peer_tx_data_qp, peer_tx_data_mr = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 0,
                local_qp=rx.data_qp_info, local_mr=rx.data_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
            peer_tx_cp_qp, _ = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 1,
                local_qp=rx.control_qp_info, local_mr=rx.control_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
            peer_rx_data_qp, peer_rx_data_mr = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 2,
                local_qp=tx.data_qp_info, local_mr=tx.data_mr_info,
                connect_timeout_s=connect_timeout_s,
            )
            peer_rx_cp_qp, _ = exchange_qp_info(
                is_server=is_server, host=peer_host, port=port + 3,
                local_qp=tx.control_qp_info, local_mr=tx.control_mr_info,
                connect_timeout_s=connect_timeout_s,
            )

        # Pre-post a deep recv-WR pool on rx so peer's first UC writes
        # never hit an empty RQ during the post-handshake race window.
        rx_pool = max(0, cfg.rq_depth - 16)

        tx.bring_up_data(peer_rx_data_qp, peer_rx_data_mr)
        tx.bring_up_control(peer_rx_cp_qp)
        rx.bring_up_data(peer_tx_data_qp, peer_tx_data_mr,
                         pre_post_recv=rx_pool)
        rx.bring_up_control(peer_tx_cp_qp)

        state = cls(rank=rank, world_size=world_size, cfg=cfg, tx=tx, rx=rx)
        state._rank_pair = canonical_rank_pair(0, 1)
        state._wire_callbacks_and_start_poller()
        return state

    @classmethod
    def for_in_process_pair(
        cls,
        cfg: ClearTransportConfig,
    ) -> Tuple["ClearHookState", "ClearHookState"]:
        """Build two hook states wired to each other in one process.

        Used by integration tests that simulate two ranks as threads
        within the same Python process. Production code uses
        ``for_rank`` (TCP bootstrap, future work).
        """
        # Build all four transports.
        a_tx = ClearTransport(cfg)
        a_rx = ClearTransport(cfg)
        b_tx = ClearTransport(cfg)
        b_rx = ClearTransport(cfg)

        # Pair UC data planes:
        #   A.tx ─UC→ B.rx     B.tx ─UC→ A.rx
        # Pre-post a large recv-WR pool on each rx engine so peer's UC
        # writes never hit an empty RQ during the symmetric bring-up
        # race. cfg.rq_depth - 16 leaves headroom for top-up posts.
        rx_pool = max(0, cfg.rq_depth - 16)
        a_tx.bring_up_data(b_rx.data_qp_info, b_rx.data_mr_info)
        b_rx.bring_up_data(a_tx.data_qp_info, a_tx.data_mr_info,
                           pre_post_recv=rx_pool)
        b_tx.bring_up_data(a_rx.data_qp_info, a_rx.data_mr_info)
        a_rx.bring_up_data(b_tx.data_qp_info, b_tx.data_mr_info,
                           pre_post_recv=rx_pool)

        # Pair RC control planes (bidirectional):
        #   A.tx.cp ⟷ B.rx.cp     B.tx.cp ⟷ A.rx.cp
        a_tx.bring_up_control(b_rx.control_qp_info)
        b_rx.bring_up_control(a_tx.control_qp_info)
        b_tx.bring_up_control(a_rx.control_qp_info)
        a_rx.bring_up_control(b_tx.control_qp_info)

        a_state = cls(rank=0, world_size=2, cfg=cfg, tx=a_tx, rx=a_rx)
        b_state = cls(rank=1, world_size=2, cfg=cfg, tx=b_tx, rx=b_rx)
        a_state._rank_pair = canonical_rank_pair(0, 1)
        b_state._rank_pair = canonical_rank_pair(0, 1)

        a_state._wire_callbacks_and_start_poller()
        b_state._wire_callbacks_and_start_poller()
        return a_state, b_state

    # ------------------------------------------------------------------
    # Internal: callback wiring + bg poller
    # ------------------------------------------------------------------

    def _wire_callbacks_and_start_poller(self) -> None:
        """Register all 6 ControlPlane callbacks on both transports and
        wire the rx Finalizer's send_* methods to the rx control plane.
        Then start the bg poll thread."""

        # ---- tx.cp callbacks (peer's responses to our pushes) -------
        def on_finalize_tx(uid, decision, mask_encoding, body):
            s = self._get_sync(uid)
            s.finalize_decision = decision
            s.finalize_event.set()

        def on_retire_tx(uid, slot, gen):
            self.tx.sender_leases.release(uid)

        self.tx.cp.on_finalize(on_finalize_tx)
        self.tx.cp.on_retire(on_retire_tx)

        # ---- rx.cp callbacks (peer's announcements of incoming pushes) ----
        def on_begin_rx(uid, slot, gen, *_args):
            self.rx.receiver_leases.install(uid=uid, slot_id=slot, gen=gen)
            s = self._get_sync(uid)
            s.begin_slot = slot
            s.begin_gen = gen
            s.begin_event.set()

        self.rx.cp.on_begin(on_begin_rx)

        # ---- rx finalizer → rx.cp wire (delivers FINALIZE/RETIRE back to
        # peer's tx.cp) ----------------------------------------------------
        def rx_send_finalize(uid, decision, mask_encoding, body):
            self.rx.cp.send_finalize(uid, decision, mask_encoding, body)

        def rx_send_retire(uid, slot, gen):
            self.rx.cp.send_retire(uid, RetirePayload(slot_id=slot, gen=gen))
            self.rx.receiver_leases.retire(uid)

        # apply_mask callback is a no-op; the foreground recv path applies
        # the mask via apply_finalize() against the rx data buffer.
        def rx_apply_mask(uid, decision, mask, n_chunks):
            return None

        self.rx.finalizer.on_send_finalize(rx_send_finalize)
        self.rx.finalizer.on_send_retire(rx_send_retire)
        self.rx.finalizer.on_apply_mask(rx_apply_mask)

        # ---- bg poll thread -----------------------------------------
        def poll_loop():
            while not self._poll_stop.is_set():
                self.tx.cp.poll_once(64, 1)
                self.rx.cp.poll_once(64, 1)

        self._poll_thread = threading.Thread(
            target=poll_loop, name=f"clear_poll_rank{self.rank}", daemon=True)
        self._poll_thread.start()

    def shutdown(self) -> None:
        """Stop the bg poll thread. Safe to call multiple times."""
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

    def __del__(self):  # best effort
        try:
            self.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bucket exchange
# ---------------------------------------------------------------------------


def _run_clear_bucket(
    state: ClearHookState,
    *,
    bucket_bytes: bytes,
    bucket_seq: int,
    base_offset: int = 0,
    chunk_bytes: Optional[int] = None,
    policy: Optional[Policy] = None,
    ratio: float = 1.0,
    timeout_ms: int = 1000,
    drain_timeout_ms: int = 5000,
) -> bytes:
    """Core CLEAR bucket exchange. Returns averaged bytes.

    Operates on a single bucket for one rank. Spawns parallel send +
    recv threads on tx / rx transports, awaits both, computes
    (local + peer_after_mask) / world_size, returns as bytes.
    """
    import time as _time
    chunk_bytes = chunk_bytes or state.cfg.chunk_bytes
    policy = policy or state.default_policy
    nbytes = len(bucket_bytes)
    n_chunks = (nbytes + chunk_bytes - 1) // chunk_bytes
    _t_enter = _time.perf_counter()
    _t_send = _t_recv = _t_finalize = _t_avg = 0.0

    # Recv WRs were pre-posted on each rx engine during bring_up_data
    # (pre_post_recv pool sized at cfg.rq_depth - 16). Top up if the pool
    # has drained below 2× this bucket's chunk count, but never exceed
    # the QP's RQ capacity (rq_depth - outstanding) — otherwise
    # ibv_post_recv aborts with ENOMEM.
    outstanding = state.rx.engine.outstanding_recv()
    if outstanding < 2 * n_chunks:
        rq_depth = state.cfg.rq_depth
        headroom = max(0, rq_depth - outstanding - 8)  # safety margin
        want = min(n_chunks * 4, headroom)
        if want > 0:
            rx_wr_base = (state.step_seq * 0x100000) | (bucket_seq & 0xFFFF)
            state.rx.engine.post_recv_batch(want, base_wr_id=rx_wr_base)

    # Stage local bucket bytes into tx data MR.
    tx_buf = np.frombuffer(state.tx.engine.local_buf_view(), dtype=np.uint8)
    tx_buf[base_offset : base_offset + nbytes] = np.frombuffer(
        bucket_bytes, dtype=np.uint8)

    # Compute uids — both ranks must agree per direction.
    rank_pair = state._rank_pair
    # peer_edge=0: traffic from rank-min to rank-max
    # peer_edge=1: traffic from rank-max to rank-min
    # Whoever sends "from min to max" gets peer_edge=0.
    we_send_min_to_max = (state.rank == 0)
    uid_we_send  = uid_hash(rank_pair=rank_pair, step_seq=state.step_seq,
                             bucket_seq=bucket_seq, phase_id=0,
                             peer_edge=0 if we_send_min_to_max else 1)
    uid_we_recv  = uid_hash(rank_pair=rank_pair, step_seq=state.step_seq,
                             bucket_seq=bucket_seq, phase_id=0,
                             peer_edge=1 if we_send_min_to_max else 0)

    # ---- App-level chunk drop (zero-overhead at loss_rate=0) ----------
    # Mirrors phase4 transport.py: sender drops chunk with prob loss_rate
    # before post_write. Same loss_seed → same RNG sequence → same dropped
    # indices on both ranks (apples-to-apples with phase4_flat). At
    # loss_rate=0 the entire block is skipped — no RNG, no set alloc.
    drop_chunks = None
    if state.cfg.loss_rate > 0.0:
        rng = getattr(state, "_loss_rng", None)
        if rng is None:
            import random as _r
            rng = _r.Random(state.cfg.loss_seed)
            state._loss_rng = rng
        drop_chunks = set()
        for _i in range(n_chunks):
            if rng.random() < state.cfg.loss_rate:
                drop_chunks.add(_i)

    # ---- Send + Recv threads ----
    send_err: list = []
    recv_err: list = []
    recv_decision_holder: list = []
    recv_bitmap_holder: list = []

    _send_started = [0.0]
    _send_done = [0.0]
    _recv_started = [0.0]
    _recv_done = [0.0]

    def send_thread():
        _send_started[0] = _time.perf_counter()
        try:
            sync = state._get_sync(uid_we_send)
            send_res = clear_send_bucket(
                state.tx,
                uid=uid_we_send,
                bucket_seq=bucket_seq, step_seq=state.step_seq,
                base_offset=base_offset, remote_base_offset=base_offset,
                nbytes=nbytes, chunk_bytes=chunk_bytes,
                peer_data_mr=state.tx._peer_data_mr,
                policy=policy,
                drop_chunks=drop_chunks,
                finalize_event=sync.finalize_event,
                finalize_holder=[sync],   # we just need wait-side; decision
                                          # is already inside the sync object
                                          # via tx.on_finalize callback
                drain_timeout_ms=drain_timeout_ms,
            )
            if not send_res.finalize_received:
                send_err.append(
                    RuntimeError(f"FINALIZE not received for uid={uid_we_send:x}"))
        except Exception as e:
            send_err.append(e)
        finally:
            _send_done[0] = _time.perf_counter()

    peer_slice_holder: list = []  # snapshot of peer bytes captured atomically

    def recv_thread():
        _recv_started[0] = _time.perf_counter()
        try:
            sync = state._get_sync(uid_we_recv)
            # Wait for BEGIN to install the lease.
            if not sync.begin_event.wait(timeout=drain_timeout_ms / 1000.0):
                raise TimeoutError(
                    f"BEGIN not received for uid={uid_we_recv:x}")
            recv_res = clear_recv_bucket(
                state.rx,
                uid=uid_we_recv,
                slot=sync.begin_slot, gen=sync.begin_gen,
                n_chunks=n_chunks, base_offset=base_offset,
                chunk_bytes=chunk_bytes,
                ratio=ratio, timeout_ms=timeout_ms,
                policy=policy,
            )
            # Snapshot peer bytes IMMEDIATELY after wait_for_ratio_clear
            # exits — before peer's next bucket can overwrite this offset.
            # Reads via the existing rx_buf view (no extra import).
            local_rx = np.frombuffer(state.rx.engine.local_buf_view(),
                                     dtype=np.uint8)
            peer_slice_holder.append(
                bytearray(local_rx[base_offset : base_offset + nbytes].tobytes()))
            recv_decision_holder.append(recv_res.decision)
            recv_bitmap_holder.append(recv_res.recv_bitmap)
        except Exception as e:
            recv_err.append(e)
        finally:
            _recv_done[0] = _time.perf_counter()

    _t_threads_start = _time.perf_counter()
    st = threading.Thread(target=send_thread, name="clear_tx")
    rt = threading.Thread(target=recv_thread, name="clear_rx")
    st.start(); rt.start()
    st.join(); rt.join()
    _t_threads_end = _time.perf_counter()

    if send_err: raise send_err[0]
    if recv_err: raise recv_err[0]

    decision = recv_decision_holder[0]
    recv_bitmap = recv_bitmap_holder[0]

    # ---- Apply mask to peer bytes (snapshot captured inside recv_thread
    # to avoid the race where peer's NEXT bucket overwrites this offset
    # before we read it).
    _t_fin_start = _time.perf_counter()
    peer_slice = peer_slice_holder[0]
    py_apply_finalize(
        decision,
        mask_bitmap=recv_bitmap,
        n_chunks=n_chunks, chunk_bytes=chunk_bytes,
        flat=peer_slice,
    )
    _t_fin_end = _time.perf_counter()

    # ---- Average locally ---------------------------------------------
    _t_avg_start = _time.perf_counter()
    local_arr = np.frombuffer(bucket_bytes, dtype=np.uint8)
    peer_arr = np.frombuffer(bytes(peer_slice), dtype=np.uint8)
    # Reinterpret as float32 for arithmetic if size aligns; otherwise
    # uint8 average. The hook layer (clear_allreduce_hook) reinterprets
    # back to the original tensor dtype.
    if nbytes % 4 == 0:
        a32 = local_arr.view(np.float32)
        b32 = peer_arr.view(np.float32)
        avg = (a32 + b32) / float(state.world_size)
        out = avg.tobytes()
    else:
        # Byte-level average (rare; for non-fp32 buckets).
        avg = (local_arr.astype(np.uint16) + peer_arr.astype(np.uint16)) \
              // state.world_size
        out = avg.astype(np.uint8).tobytes()

    # Cleanup sync state for these uids — they won't repeat for the
    # same (step, bucket).
    state._drop_sync(uid_we_send)
    state._drop_sync(uid_we_recv)
    _t_avg_end = _time.perf_counter()

    if state.perf_log is not None:
        # Count actually-received chunks for diagnosis (1 bit per chunk).
        recv_count = sum(bin(b).count("1") for b in recv_bitmap)
        state.perf_log.append({
            "step_seq": state.step_seq,
            "bucket_seq": bucket_seq,
            "n_chunks": n_chunks,
            "recv_count": recv_count,
            "decision": int(decision),
            "nbytes": nbytes,
            "stage_ms": (_t_threads_start - _t_enter) * 1000.0,
            "threads_ms": (_t_threads_end - _t_threads_start) * 1000.0,
            "send_ms": (_send_done[0] - _send_started[0]) * 1000.0
                       if _send_done[0] else 0.0,
            "recv_ms": (_recv_done[0] - _recv_started[0]) * 1000.0
                       if _recv_done[0] else 0.0,
            "finalize_ms": (_t_fin_end - _t_fin_start) * 1000.0,
            "average_ms": (_t_avg_end - _t_avg_start) * 1000.0,
            "total_ms": (_t_avg_end - _t_enter) * 1000.0,
        })
    return out


def step_advance(state: ClearHookState) -> None:
    """Advance the per-rank step counter. Caller (the trainer) invokes
    once at the end of every step so subsequent buckets get fresh uids.
    Also refills the rx Finalizer's repair budget."""
    state.step_seq += 1
    state.rx.finalizer.on_step_boundary()


# ---------------------------------------------------------------------------
# DDP-facing hook
# ---------------------------------------------------------------------------


def clear_allreduce_hook(state, bucket):
    """DDP communication hook returning a Future of averaged bucket.

    Imports torch lazily so this module loads in non-torch environments
    (the W2.3a pure-Python tests run on Windows where torch may be absent).
    """
    import time as _time
    import torch
    from torch import futures

    _t0 = _time.perf_counter()
    flat = bucket.buffer()
    if flat.device.type != "cpu":
        raise RuntimeError(
            f"clear_allreduce_hook: bucket must be on CPU, got {flat.device}")
    if not flat.is_contiguous():
        flat = flat.contiguous()

    nbytes = flat.numel() * flat.element_size()
    bucket_id = state.manifest.observe(_signature_from_bucket(bucket))

    with state._bucket_lock:
        _t_to_bytes_start = _time.perf_counter()
        bucket_bytes = bytes(flat.numpy().tobytes())
        _t_to_bytes_end = _time.perf_counter()
        # Under app-level chunk drop, dropped chunks NEVER arrive at the
        # receiver. Holding ratio=1.0 (default) makes wait_for_ratio_clear
        # block until timeout_ms — wasting ~5 s per bucket whenever
        # cfg.loss_rate > 0 (measured +5000 ms in clear_perf send_ms /
        # recv_ms; see docs/phase5/results/e1_clear_perf_decode.md).
        # Lower the target ratio so the wait exits via RATIO_MET as soon
        # as the expected fraction is delivered. Drop count per bucket
        # follows Binomial(n_chunks, loss_rate); a 2× margin on loss_rate
        # (i.e. ratio = 1 - 2 * loss_rate) covers the variance for
        # n_chunks ~ 2729 with high confidence (~3 sigma). Floor at 0.5
        # so extreme loss_rate still produces a meaningful wait.
        lr = float(state.cfg.loss_rate)
        target_ratio = max(0.5, 1.0 - 2.0 * lr) if lr > 0.0 else 1.0
        avg_bytes = _run_clear_bucket(
            state,
            bucket_bytes=bucket_bytes,
            bucket_seq=bucket_id,
            ratio=target_ratio,
            timeout_ms=5000,         # ratio_clear deadline per bucket
            drain_timeout_ms=10000,  # SQ-drain + FINALIZE/BEGIN waits
        )
        # Auto-advance step after each bucket so consecutive hook calls
        # never reuse a uid. With bucket_cap_mb=512 (1 bucket/step) this
        # is exactly per-step boundary; with smaller bucket_cap_mb it
        # advances more often than DDP "steps" but uids stay unique.
        step_advance(state)
    _t_from_start = _time.perf_counter()
    avg_arr = np.frombuffer(avg_bytes, dtype=np.uint8).view(
        flat.numpy().dtype).reshape(flat.shape).copy()
    out_t = torch.from_numpy(avg_arr)
    _t_from_end = _time.perf_counter()

    if state.perf_log is not None and state.perf_log:
        # Annotate the most recent _run_clear_bucket entry with the
        # outer-hook stages.
        state.perf_log[-1]["to_bytes_ms"] = (_t_to_bytes_end - _t_to_bytes_start) * 1000.0
        state.perf_log[-1]["from_numpy_ms"] = (_t_from_end - _t_from_start) * 1000.0
        state.perf_log[-1]["hook_total_ms"] = (_t_from_end - _t0) * 1000.0

    fut: "futures.Future[torch.Tensor]" = futures.Future()
    fut.set_result(out_t)
    return fut


def _signature_from_bucket(bucket):
    """Build a ParamSignature from a torch GradBucket. Lazy torch import."""
    from .manifest import param_signature_from_shapes
    try:
        params = bucket.parameters()
    except AttributeError:
        params = bucket.get_per_parameter_tensors()
    shapes = [tuple(p.shape) for p in params]
    dtypes = [str(p.dtype) for p in params]
    sizes = [int(p.numel() * p.element_size()) for p in params]
    return param_signature_from_shapes(shapes, dtypes, sizes)


__all__ = [
    "ClearHookState",
    "_run_clear_bucket",
    "clear_allreduce_hook",
    "step_advance",
]
