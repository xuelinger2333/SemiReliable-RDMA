"""W2.3c — RDMA-gated ControlPlane Python loopback test.

Two ControlPlane instances on the same NIC, exchanging every CLEAR
message type through the Python binding. Verifies:
  - construct + bring_up via Python
  - send_begin / send_witness / send_repair_req / send_finalize /
    send_retire / send_backpressure all reach the peer
  - Python on_* callbacks fire with the correct uid / fields / body
  - body bytes are correctly copied (non-owning C++ pointers don't leak)
  - stats counters coherent after a full round of exchanges
  - ClearTransport skeleton constructs end-to-end against a peer

Opt in: RDMA_LOOPBACK_DEVICE=mlx5_X (mlx5_2 on amd247 cluster).
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

# These C++ symbols only exist on machines where _semirdma_ext was built
# *with* CLEAR support; gate import so unbuilt environments skip cleanly.
clear_mod = pytest.importorskip("semirdma._semirdma_ext.clear")

from semirdma._semirdma_ext.clear import (  # noqa: E402
    BackpressurePayload,
    BeginPayload,
    ControlPlane,
    ControlPlaneConfig,
    FinalizeDecision,
    Policy,
    RetirePayload,
    WitnessEncoding,
    encode_witness,
)
from semirdma.clear.transport import ClearTransport, ClearTransportConfig  # noqa: E402


def _dev() -> str:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        pytest.skip("RDMA_LOOPBACK_DEVICE unset; skipping RDMA-gated test")
    return dev


def _gid() -> int:
    return int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))


def _wait_for(predicate, *, cps, timeout_s=2.0, slice_ms=10):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for cp in cps:
            cp.poll_once(64, slice_ms)
        if predicate():
            return True
    return False


@pytest.fixture
def cps():
    """Two ControlPlanes on the same NIC, brought up against each other."""
    cfg_a = ControlPlaneConfig(dev_name=_dev(), gid_index=_gid(),
                               recv_slots=32, send_slots=8)
    cfg_b = ControlPlaneConfig(dev_name=_dev(), gid_index=_gid(),
                               recv_slots=32, send_slots=8)
    cp_a = ControlPlane(cfg_a)
    cp_b = ControlPlane(cfg_b)
    cp_a.bring_up(cp_b.local_qp_info())
    cp_b.bring_up(cp_a.local_qp_info())
    return cp_a, cp_b


def test_send_begin_callback(cps):
    cp_a, cp_b = cps
    seen = []
    cp_b.on_begin(lambda *args: seen.append(args))
    cp_a.send_begin(
        uid=0xCAFE,
        payload=BeginPayload(
            slot_id=7, gen=3, phase_id=1, policy=Policy.REPAIR_FIRST,
            peer_edge=0xABCD, step_seq=100, bucket_seq=42,
            n_chunks=1024, deadline_us=200000, chunk_bytes=4096,
            checksum_seed=0x12345678,
        ),
    )
    assert _wait_for(lambda: len(seen) == 1, cps=cps)
    args = seen[0]
    # callback signature: (uid, slot, gen, phase, policy, peer_edge,
    # step_seq, bucket_seq, n_chunks, deadline_us, chunk_bytes,
    # checksum_seed)
    assert args[0] == 0xCAFE
    assert args[1] == 7
    assert args[2] == 3
    assert args[7] == 42  # bucket_seq
    assert args[8] == 1024
    assert args[10] == 4096


def test_send_witness_with_range_body(cps):
    cp_a, cp_b = cps
    seen = []
    cp_b.on_witness(
        lambda uid, recv_count, encoding, body: seen.append(
            (uid, recv_count, encoding, bytes(body))))

    n = 1024
    bm = np.full((n + 7) // 8, 0xFF, dtype=np.uint8)
    bm[10] = 0  # 8 missing chunks
    enc = encode_witness(bitmap=bm, n_chunks=n)
    cp_a.send_witness(uid=0xBEEF, recv_count=enc["recv_count"],
                      encoding=enc["encoding"],
                      body=np.frombuffer(enc["body"], dtype=np.uint8))
    assert _wait_for(lambda: len(seen) == 1, cps=cps)
    uid, rc, encoding, body = seen[0]
    assert uid == 0xBEEF
    assert rc == enc["recv_count"]
    assert encoding == WitnessEncoding.RANGE_MISSING
    assert body == bytes(enc["body"])


def test_send_repair_req(cps):
    cp_a, cp_b = cps
    seen = []
    cp_b.on_repair_req(lambda uid, rs: seen.append((uid, list(rs))))
    cp_a.send_repair_req(uid=0xABBA, ranges=[(10, 5), (500, 20)])
    assert _wait_for(lambda: len(seen) == 1, cps=cps)
    uid, rs = seen[0]
    assert uid == 0xABBA
    assert sorted(rs) == [(10, 5), (500, 20)]


def test_send_finalize_with_mask_body(cps):
    cp_a, cp_b = cps
    seen = []
    cp_b.on_finalize(
        lambda uid, decision, enc, body: seen.append(
            (uid, decision, enc, bytes(body))))
    n = 256
    bm = np.full((n + 7) // 8, 0xFF, dtype=np.uint8)
    bm[5] &= 0x0F  # clear some bits
    enc = encode_witness(bitmap=bm, n_chunks=n)
    cp_a.send_finalize(uid=0xDEAD, decision=FinalizeDecision.MASKED,
                       mask_encoding=enc["encoding"],
                       mask_body=np.frombuffer(enc["body"], dtype=np.uint8))
    assert _wait_for(lambda: len(seen) == 1, cps=cps)
    uid, decision, encoding, body = seen[0]
    assert uid == 0xDEAD
    assert decision == FinalizeDecision.MASKED
    assert body == bytes(enc["body"])


def test_send_retire_and_backpressure(cps):
    cp_a, cp_b = cps
    retired, backpressured = [], []
    cp_b.on_retire(
        lambda uid, slot, gen: retired.append((uid, slot, gen)))
    cp_b.on_backpressure(
        lambda uid, peer_edge, k: backpressured.append((uid, peer_edge, k)))
    cp_a.send_retire(uid=1, payload=RetirePayload(slot_id=13, gen=7))
    cp_a.send_backpressure(uid=2, payload=BackpressurePayload(
        peer_edge=0x1234, requested_credits=64))
    assert _wait_for(
        lambda: len(retired) == 1 and len(backpressured) == 1, cps=cps)
    assert retired[0] == (1, 13, 7)
    assert backpressured[0] == (2, 0x1234, 64)


def test_stats_after_full_exchange(cps):
    cp_a, cp_b = cps
    cp_b.on_begin(lambda *a: None)
    cp_a.send_begin(uid=0x1, payload=BeginPayload(slot_id=0, gen=0, n_chunks=8))
    cp_a.send_begin(uid=0x2, payload=BeginPayload(slot_id=0, gen=1, n_chunks=8))
    cp_a.send_begin(uid=0x3, payload=BeginPayload(slot_id=0, gen=2, n_chunks=8))
    assert _wait_for(lambda: cp_b.stats.recv_total >= 3, cps=cps)
    sa = cp_a.stats
    sb = cp_b.stats
    assert sa.sent_total >= 3
    assert sa.sent_by_type["BEGIN"] >= 3
    assert sb.recv_by_type["BEGIN"] >= 3
    assert sb.recv_decode_errors == 0
    assert sa.send_completion_errors == 0


def test_send_ring_recycles_under_pressure(cps):
    cp_a, cp_b = cps
    received = []
    cp_b.on_retire(lambda *args: received.append(args))
    n = 100  # > send_slots=8
    sent = 0
    for i in range(n):
        # Spin briefly if the ring is full.
        for _ in range(50):
            if cp_a.send_retire(
                uid=i,
                payload=RetirePayload(slot_id=i & 0xFF, gen=(i & 0x0F)),
            ):
                sent += 1
                break
            cp_a.poll_once(16, 1)
            cp_b.poll_once(16, 0)
    assert sent == n
    assert _wait_for(lambda: len(received) == n, cps=cps, timeout_s=4.0)


# --------------- ClearTransport skeleton smoke ----------------------------


def test_clear_transport_constructs_and_brings_up():
    """Two ClearTransports on the same NIC; bring up data + control planes
    against each other. Doesn't run a transfer (that's W2.3d) — just
    verifies the skeleton wires both planes correctly."""
    cfg = ClearTransportConfig(
        dev_name=_dev(), gid_index=_gid(),
        buffer_bytes=16 * 1024 * 1024,
        sq_depth=128, rq_depth=2048,
        cp_recv_slots=32, cp_send_slots=8,
    )
    a = ClearTransport(cfg)
    b = ClearTransport(cfg)
    assert not a.is_up and not b.is_up

    # Data plane handshake
    a.bring_up_data(b.data_qp_info, b.data_mr_info)
    b.bring_up_data(a.data_qp_info, a.data_mr_info)
    # Control plane handshake
    a.bring_up_control(b.control_qp_info)
    b.bring_up_control(a.control_qp_info)

    assert a.is_up and b.is_up

    # Wire callbacks; verify finalizer's send_* path threads through cp
    delivered = []
    a.wire_default_callbacks(
        apply_mask_cb=lambda uid, d, mask, n: delivered.append((uid, d)))
    # If we registered on_send_finalize, sending one through the finalizer
    # should produce a FINALIZE on cp.stats.sent_by_type["FINALIZE"].
    n_chunks = 8
    chunk_bytes = 256
    a.finalizer.track(uid=42, slot=0, gen=0,
                      n_chunks=n_chunks, chunk_bytes=chunk_bytes,
                      policy=Policy.MASK_FIRST)
    bm = bytearray((n_chunks + 7) // 8)
    bm[0] = 0xFF  # all present (full mask) — DELIVERED path
    decision = a.finalizer.on_witness(uid=42, recv_bitmap=bm)
    assert decision == FinalizeDecision.DELIVERED
    assert a.cp.stats.sent_by_type["FINALIZE"] >= 1
    assert a.cp.stats.sent_by_type["RETIRE"] >= 1
    assert delivered[0][0] == 42
