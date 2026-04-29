"""W2.3d — RDMA-gated end-to-end CLEAR protocol test (single direction).

One process, two threads. Rank A pushes a bucket of 64 chunks to rank B
through the full CLEAR protocol:

    A.tx_clear  ──BEGIN──►  B.rx_clear     (RC control)
    A.tx_clear  ─UC×N───►   B.rx_clear     (UC writes-with-imm)
    B.rx_clear  ──FINALIZE──► A.tx_clear   (RC control, after on_witness)
    B.rx_clear  ──RETIRE────► A.tx_clear   (RC control, slot release)

Two scenarios:
  - Clean wire: every chunk is posted. Receiver finalizes as DELIVERED.
  - Lossy wire: sender drops ``drop_chunks`` from the post loop. Receiver
    finalizes as MASKED with the ghost mask zeroing missing chunks.

Both scenarios verify byte-level correctness on the receiver buffer
post-mask-application via ``apply_finalize``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

import numpy as np
import pytest

clear_mod = pytest.importorskip("semirdma._semirdma_ext.clear")

from semirdma._semirdma_ext.clear import (  # noqa: E402
    FinalizeDecision,
    Policy,
    RetirePayload,
)
from semirdma.clear.policy import (  # noqa: E402
    FinalizeDecision as PyFinalizeDecision,
)
from semirdma.clear.protocol import (  # noqa: E402
    clear_recv_bucket,
    clear_send_bucket,
)
from semirdma.clear.runtime import apply_finalize  # noqa: E402
from semirdma.clear.transport import (  # noqa: E402
    ClearTransport,
    ClearTransportConfig,
)


def _dev() -> str:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        pytest.skip("RDMA_LOOPBACK_DEVICE unset; skipping RDMA-gated test")
    return dev


def _gid() -> int:
    return int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))


# ---------- transport pair fixture ------------------------------------------


def _make_transport_pair() -> Tuple[ClearTransport, ClearTransport]:
    """Bring up two ClearTransports against each other on the same NIC."""
    cfg = ClearTransportConfig(
        dev_name=_dev(), gid_index=_gid(),
        buffer_bytes=8 * 1024 * 1024,
        sq_depth=256, rq_depth=2048,
        chunk_bytes=4096,
        cp_recv_slots=32, cp_send_slots=8,
    )
    a = ClearTransport(cfg)
    b = ClearTransport(cfg)

    # Data plane handshake
    a.bring_up_data(b.data_qp_info, b.data_mr_info)
    b.bring_up_data(a.data_qp_info, a.data_mr_info)
    # Control plane handshake
    a.bring_up_control(b.control_qp_info)
    b.bring_up_control(a.control_qp_info)
    return a, b


def _poll_cp_loop(transports, stop: threading.Event) -> None:
    """Background poller — drives every transport's control plane so
    on_begin / on_witness / on_finalize / on_retire callbacks fire."""
    while not stop.is_set():
        for t in transports:
            t.cp.poll_once(64, 1)


# ---------- happy path ------------------------------------------------------


@pytest.mark.timeout(30)
def test_clear_e2e_clean_wire_delivers_all_chunks():
    a, b = _make_transport_pair()

    # ---- Sender (A) wiring ---------------------------------------------
    finalize_event = threading.Event()
    finalize_holder: list = []

    def on_finalize_a(uid, decision, mask_encoding, body):
        # A's tx side receives FINALIZE from B; record + signal sender.
        finalize_holder.append(decision)
        finalize_event.set()

    a.cp.on_finalize(on_finalize_a)

    def on_retire_a(uid, slot, gen):
        # B sent RETIRE → release slot on our sender table.
        a.sender_leases.release(uid)

    a.cp.on_retire(on_retire_a)

    # ---- Receiver (B) wiring -------------------------------------------
    # B's finalizer needs to:
    #  - send FINALIZE to A on terminal decision
    #  - apply mask locally (we'll capture it in a probe)
    #  - send RETIRE to A
    apply_calls: list = []

    def b_send_finalize(uid, decision, mask_encoding, body):
        b.cp.send_finalize(uid, decision, mask_encoding, body)

    def b_send_retire(uid, slot, gen):
        b.cp.send_retire(uid, RetirePayload(slot_id=slot, gen=gen))
        # B also retires its own receiver lease.
        b.receiver_leases.retire(uid)

    def b_apply_mask(uid, decision, mask, n_chunks):
        apply_calls.append((uid, decision, bytes(mask), n_chunks))

    b.finalizer.on_send_finalize(b_send_finalize)
    b.finalizer.on_send_retire(b_send_retire)
    b.finalizer.on_apply_mask(b_apply_mask)

    # On BEGIN, install B's receiver lease.
    def on_begin_b(uid, slot, gen, *_args):
        b.receiver_leases.install(uid=uid, slot_id=slot, gen=gen)

    b.cp.on_begin(on_begin_b)

    # Pre-post recv WRs on B's UC QP so Write-with-Imm can land.
    n_chunks = 64
    chunk_bytes = 4096
    nbytes = n_chunks * chunk_bytes
    b.engine.post_recv_batch(n_chunks, base_wr_id=10_000)

    # Stage A's source bytes; predictable pattern so we can byte-compare.
    src_view = np.frombuffer(a.engine.local_buf_view(), dtype=np.uint8)
    pattern = (np.arange(nbytes, dtype=np.uint8) ^ 0x5A)
    src_view[:nbytes] = pattern

    # Zero B's receive buffer to prove the writes actually deliver.
    dst_view = np.frombuffer(b.engine.local_buf_view(), dtype=np.uint8)
    dst_view[:nbytes] = 0

    # Background poll loop on B's control plane.
    stop = threading.Event()
    poller = threading.Thread(target=_poll_cp_loop, args=([a, b], stop))
    poller.start()
    try:
        # ---- Receiver thread runs in parallel ---------------------------
        recv_holder: list = []
        recv_done = threading.Event()

        def recv_thread():
            # We don't know slot/gen yet; the sender thread will tell us via
            # the BEGIN callback's lease install. Spin briefly until B has
            # installed something matching uid 0xCAFE.
            uid = 0xCAFE
            while not recv_done.is_set():
                # Look for an installed lease for this uid by trying every
                # (slot, gen) — easier: drain BEGIN via on_begin callback
                # which installed it. Then we just need slot/gen from peek.
                # Use the receiver lease table's known mapping: install
                # populated uid→slot. To recover that, we reuse ControlPlane
                # poll output — but we don't have it back. Simplest: peek
                # the receiver_leases via a lookup probe over slots 0..255.
                # In practice, B's on_begin handler stores (slot, gen) in a
                # shared dict; do that here.
                if uid in begin_holder:
                    slot, gen = begin_holder[uid]
                    res = clear_recv_bucket(
                        b, uid=uid, slot=slot, gen=gen,
                        n_chunks=n_chunks, base_offset=0,
                        chunk_bytes=chunk_bytes,
                        ratio=1.0, timeout_ms=2000,
                        policy=Policy.MASK_FIRST,
                    )
                    recv_holder.append(res)
                    recv_done.set()
                    return
                time.sleep(0.001)

        # Use a dict to capture slot/gen from the BEGIN callback for the
        # recv thread's benefit.
        begin_holder: dict = {}
        original_on_begin = on_begin_b

        def on_begin_b_v2(uid, slot, gen, *_args):
            original_on_begin(uid, slot, gen)
            begin_holder[uid] = (slot, gen)

        b.cp.on_begin(on_begin_b_v2)

        # Start receiver thread.
        rt = threading.Thread(target=recv_thread)
        rt.start()

        try:
            # Sender side.
            send_res = clear_send_bucket(
                a,
                uid=0xCAFE,
                bucket_seq=1, step_seq=1,
                base_offset=0, remote_base_offset=0,
                nbytes=nbytes, chunk_bytes=chunk_bytes,
                peer_data_mr=a._peer_data_mr,
                policy=Policy.MASK_FIRST,
                finalize_event=finalize_event,
                finalize_holder=finalize_holder,
                drain_timeout_ms=5000,
            )
            assert send_res.n_posted == n_chunks
            assert send_res.finalize_received, \
                "FINALIZE did not arrive at sender"
            assert send_res.finalize_decision == FinalizeDecision.DELIVERED

            rt.join(timeout=5.0)
            assert not rt.is_alive(), "recv_thread did not terminate"
            assert recv_holder, "recv_thread produced no result"
            recv_res = recv_holder[0]
            assert recv_res.decision == FinalizeDecision.DELIVERED
            assert recv_res.recv_count == n_chunks

            # Bytes match: B's recv buffer should equal A's pattern.
            assert np.array_equal(dst_view[:nbytes], pattern), \
                "B's recv buffer doesn't match A's source pattern"
            assert len(apply_calls) == 1
            assert apply_calls[0][1] == FinalizeDecision.DELIVERED
        finally:
            recv_done.set()
            rt.join(timeout=2.0)
    finally:
        stop.set()
        poller.join(timeout=2.0)


# ---------- lossy path ------------------------------------------------------


@pytest.mark.timeout(30)
def test_clear_e2e_lossy_wire_masks_missing_chunks():
    a, b = _make_transport_pair()

    finalize_event = threading.Event()
    finalize_holder: list = []
    a.cp.on_finalize(
        lambda uid, d, e, body: (finalize_holder.append(d),
                                  finalize_event.set()))
    a.cp.on_retire(
        lambda uid, slot, gen: a.sender_leases.release(uid))

    # B side
    begin_holder: dict = {}

    def on_begin_b(uid, slot, gen, *_args):
        b.receiver_leases.install(uid=uid, slot_id=slot, gen=gen)
        begin_holder[uid] = (slot, gen)

    b.cp.on_begin(on_begin_b)
    b.finalizer.on_send_finalize(
        lambda uid, d, e, body: b.cp.send_finalize(uid, d, e, body))
    b.finalizer.on_send_retire(
        lambda uid, slot, gen: (b.cp.send_retire(uid, RetirePayload(
            slot_id=slot, gen=gen)),
                                b.receiver_leases.retire(uid)))
    b.finalizer.on_apply_mask(lambda *args: None)

    n_chunks = 64
    chunk_bytes = 4096
    nbytes = n_chunks * chunk_bytes
    b.engine.post_recv_batch(n_chunks, base_wr_id=20_000)

    # A's source pattern.
    src_view = np.frombuffer(a.engine.local_buf_view(), dtype=np.uint8)
    pattern = (np.arange(nbytes, dtype=np.uint8) ^ 0x33).copy()
    src_view[:nbytes] = pattern

    # Pre-fill B's recv with sentinel so missing-chunk regions can be
    # detected; mask should overwrite them with zeros.
    SENTINEL = 0xCC
    dst_view = np.frombuffer(b.engine.local_buf_view(), dtype=np.uint8)
    dst_view[:nbytes] = SENTINEL

    stop = threading.Event()
    poller = threading.Thread(target=_poll_cp_loop, args=([a, b], stop))
    poller.start()

    try:
        recv_holder: list = []
        recv_done = threading.Event()
        DROPPED = {5, 17, 42}

        def recv_thread():
            uid = 0xBEEF
            while not recv_done.is_set():
                if uid in begin_holder:
                    slot, gen = begin_holder[uid]
                    res = clear_recv_bucket(
                        b, uid=uid, slot=slot, gen=gen,
                        n_chunks=n_chunks, base_offset=0,
                        chunk_bytes=chunk_bytes,
                        ratio=1.0, timeout_ms=300,  # short → DEADLINE
                        policy=Policy.MASK_FIRST,
                    )
                    recv_holder.append(res)
                    recv_done.set()
                    return
                time.sleep(0.001)

        rt = threading.Thread(target=recv_thread)
        rt.start()

        try:
            send_res = clear_send_bucket(
                a,
                uid=0xBEEF,
                bucket_seq=2, step_seq=1,
                base_offset=0, remote_base_offset=0,
                nbytes=nbytes, chunk_bytes=chunk_bytes,
                peer_data_mr=a._peer_data_mr,
                policy=Policy.MASK_FIRST,
                drop_chunks=DROPPED,
                finalize_event=finalize_event,
                finalize_holder=finalize_holder,
                drain_timeout_ms=5000,
            )
            assert send_res.n_posted == n_chunks - len(DROPPED)
            assert send_res.finalize_received
            assert send_res.finalize_decision == FinalizeDecision.MASKED

            rt.join(timeout=5.0)
            recv_res = recv_holder[0]
            assert recv_res.decision == FinalizeDecision.MASKED
            assert recv_res.recv_count == n_chunks - len(DROPPED)

            # Apply the mask locally to B's buffer. After this, dropped
            # chunk regions must be zero, present chunk regions must
            # match A's pattern.
            b_local = bytearray(dst_view[:nbytes].tobytes())
            apply_finalize(
                PyFinalizeDecision.MASKED,
                mask_bitmap=recv_res.recv_bitmap,
                n_chunks=n_chunks, chunk_bytes=chunk_bytes,
                flat=b_local,
            )
            for i in range(n_chunks):
                start = i * chunk_bytes
                end = start + chunk_bytes
                got = bytes(b_local[start:end])
                if i in DROPPED:
                    # Sentinel was zeroed by mask.
                    assert got == b"\x00" * chunk_bytes, \
                        f"chunk {i} expected zero, got {got[:4]!r}"
                else:
                    expected = bytes(pattern[start:end])
                    assert got == expected, \
                        f"chunk {i} mismatch (delivered chunk corrupted)"
        finally:
            recv_done.set()
            rt.join(timeout=2.0)
    finally:
        stop.set()
        poller.join(timeout=2.0)
