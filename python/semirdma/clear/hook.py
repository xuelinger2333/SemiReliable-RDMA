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
        a_tx.bring_up_data(b_rx.data_qp_info, b_rx.data_mr_info)
        b_rx.bring_up_data(a_tx.data_qp_info, a_tx.data_mr_info)
        b_tx.bring_up_data(a_rx.data_qp_info, a_rx.data_mr_info)
        a_rx.bring_up_data(b_tx.data_qp_info, b_tx.data_mr_info)

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
    chunk_bytes = chunk_bytes or state.cfg.chunk_bytes
    policy = policy or state.default_policy
    nbytes = len(bucket_bytes)
    n_chunks = (nbytes + chunk_bytes - 1) // chunk_bytes

    # Pre-post recv WRs on rx engine BEFORE the peer can issue UC writes.
    # Use a per-call wr_id offset so consecutive bucket calls don't collide.
    rx_wr_base = (state.step_seq * 0x100000) | (bucket_seq & 0xFFFF)
    state.rx.engine.post_recv_batch(n_chunks, base_wr_id=rx_wr_base)

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

    # ---- Send + Recv threads ----
    send_err: list = []
    recv_err: list = []
    recv_decision_holder: list = []
    recv_bitmap_holder: list = []

    def send_thread():
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

    def recv_thread():
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
            recv_decision_holder.append(recv_res.decision)
            recv_bitmap_holder.append(recv_res.recv_bitmap)
        except Exception as e:
            recv_err.append(e)

    st = threading.Thread(target=send_thread, name="clear_tx")
    rt = threading.Thread(target=recv_thread, name="clear_rx")
    st.start(); rt.start()
    st.join(); rt.join()

    if send_err: raise send_err[0]
    if recv_err: raise recv_err[0]

    decision = recv_decision_holder[0]
    recv_bitmap = recv_bitmap_holder[0]

    # ---- Apply mask to peer bytes (rx data buffer slice) -------------
    rx_buf = np.frombuffer(state.rx.engine.local_buf_view(), dtype=np.uint8)
    peer_slice = bytearray(rx_buf[base_offset : base_offset + nbytes].tobytes())
    py_apply_finalize(
        decision,
        mask_bitmap=recv_bitmap,
        n_chunks=n_chunks, chunk_bytes=chunk_bytes,
        flat=peer_slice,
    )

    # ---- Average locally ---------------------------------------------
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
    import torch
    from torch import futures

    flat = bucket.buffer()
    if flat.device.type != "cpu":
        raise RuntimeError(
            f"clear_allreduce_hook: bucket must be on CPU, got {flat.device}")
    if not flat.is_contiguous():
        flat = flat.contiguous()

    nbytes = flat.numel() * flat.element_size()
    bucket_id = state.manifest.observe(_signature_from_bucket(bucket))

    with state._bucket_lock:
        bucket_bytes = bytes(flat.numpy().tobytes())
        avg_bytes = _run_clear_bucket(
            state,
            bucket_bytes=bucket_bytes,
            bucket_seq=bucket_id,
        )
    avg_arr = np.frombuffer(avg_bytes, dtype=np.uint8).view(
        flat.numpy().dtype).reshape(flat.shape).copy()
    out_t = torch.from_numpy(avg_arr)

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
