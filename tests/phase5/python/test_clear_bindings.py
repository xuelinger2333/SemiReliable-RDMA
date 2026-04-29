"""W2.3b — pybind11 bindings smoke test.

Verifies that the C++ types exposed under ``semirdma._semirdma_ext.clear``
are callable from Python and produce the same results as the C++ unit
tests do directly. Also exercises the Python ↔ C++ ↔ Python callback
roundtrip for ``Finalizer.on_send_finalize`` etc.

End-to-end "drive a Finalizer through a full lifecycle from Python" is
the value here — combined with the W2.3a clear/* modules, this is what
``clear_allreduce_hook`` (W2.3c) builds on top of.
"""

from __future__ import annotations

import numpy as np
import pytest

from semirdma._semirdma_ext import RatioExitReason  # noqa: F401  (smoke)
from semirdma._semirdma_ext.clear import (
    FinalizeDecision,
    Finalizer,
    FinalizerConfig,
    LookupOutcome,
    Policy,
    RQMonitor,
    RQMonitorConfig,
    Range,
    ReceiverLeaseTable,
    SenderLeaseTable,
    WitnessEncoding,
    decide_finalize,
    decode_witness,
    encode_imm,
    encode_witness,
    imm_chunk,
    imm_gen,
    imm_slot,
    kImmMaxChunkIdx,
    lease_key,
)
from semirdma.clear.policy import (
    FinalizeDecision as PyFinalizeDecision,
    Policy as PyPolicy,
)


# ---------- enum value parity check ----------------------------------------

def test_policy_enum_values_match_python():
    assert int(Policy.REPAIR_FIRST)    == int(PyPolicy.REPAIR_FIRST)
    assert int(Policy.MASK_FIRST)      == int(PyPolicy.MASK_FIRST)
    assert int(Policy.STALE_FILL)      == int(PyPolicy.STALE_FILL)
    assert int(Policy.ESTIMATOR_SCALE) == int(PyPolicy.ESTIMATOR_SCALE)


def test_finalize_decision_values_match_python():
    assert int(FinalizeDecision.DELIVERED)   == int(PyFinalizeDecision.DELIVERED)
    assert int(FinalizeDecision.REPAIRED)    == int(PyFinalizeDecision.REPAIRED)
    assert int(FinalizeDecision.MASKED)      == int(PyFinalizeDecision.MASKED)
    assert int(FinalizeDecision.STALE)       == int(PyFinalizeDecision.STALE)
    assert int(FinalizeDecision.FALLBACK_RC) == int(PyFinalizeDecision.FALLBACK_RC)


# ---------- imm_codec roundtrip --------------------------------------------

def test_imm_codec_roundtrip():
    imm = encode_imm(slot_id=123, chunk_idx=0xABCDE, gen=9)
    assert imm_slot(imm) == 123
    assert imm_chunk(imm) == 0xABCDE
    assert imm_gen(imm) == 9
    # Constants exported.
    assert kImmMaxChunkIdx == (1 << 20) - 1
    # lease_key uniqueness across (slot, gen) — small sample.
    keys = {lease_key(s, g) for s in range(8) for g in range(16)}
    assert len(keys) == 8 * 16


# ---------- LeaseTable ------------------------------------------------------

def test_sender_lease_table_acquire_release():
    t = SenderLeaseTable(quarantine_ticks=0)
    r = t.acquire(uid=1, slot_pref=42)
    assert r["ok"] is True
    assert r["slot_id"] == 42
    assert r["gen"] == 0
    assert t.peek(1) == (42, 0)
    assert t.release(1) is True
    # After release with quarantine=0 + same slot pref, gen bumps.
    r2 = t.acquire(uid=2, slot_pref=42)
    assert r2["slot_id"] == 42
    assert r2["gen"] == 1


def test_receiver_lease_table_lookup_outcomes():
    t = ReceiverLeaseTable()
    # PRE_BEGIN before install
    res = t.lookup(slot_id=3, gen=5)
    assert res.outcome == LookupOutcome.PRE_BEGIN
    # HIT after install
    t.install(uid=0xAA, slot_id=3, gen=5)
    res = t.lookup(3, 5)
    assert res.outcome == LookupOutcome.HIT
    assert res.uid == 0xAA
    # STALE on wrong gen
    res = t.lookup(3, 4)
    assert res.outcome == LookupOutcome.STALE


def test_pending_drain_through_python():
    t = ReceiverLeaseTable()
    t.enqueue_pending(slot_id=1, gen=2, chunk_idx=10)
    t.enqueue_pending(slot_id=1, gen=2, chunk_idx=11)
    t.enqueue_pending(slot_id=1, gen=3, chunk_idx=99)
    drained = t.drain_pending_for(1, 2)
    assert len(drained) == 2
    assert drained[0].chunk_idx == 10
    assert drained[1].chunk_idx == 11
    assert t.pending_size() == 1


# ---------- decide_finalize -------------------------------------------------

def _bm(n_chunks: int, present_idx) -> bytes:
    bm = bytearray((n_chunks + 7) // 8)
    for i in present_idx:
        bm[i >> 3] |= 1 << (i & 7)
    return bytes(bm)


def test_decide_finalize_delivered():
    n = 64
    bm = _bm(n, range(n))  # all present
    r = decide_finalize(n_chunks=n, recv_bitmap=bm, chunk_bytes=4096,
                        policy=Policy.REPAIR_FIRST,
                        repair_budget_bytes=1 << 30)
    assert r["decision"] == FinalizeDecision.DELIVERED
    assert r["missing_count"] == 0


def test_decide_finalize_repair_emits_ranges():
    n = 64
    chunk = 4096
    bm = _bm(n, [i for i in range(n) if i not in (5, 10, 11)])
    r = decide_finalize(n_chunks=n, recv_bitmap=bm, chunk_bytes=chunk,
                        policy=Policy.REPAIR_FIRST,
                        repair_budget_bytes=1 << 20)
    assert r["decision"] == FinalizeDecision.REPAIRED
    assert r["missing_count"] == 3
    # Two contiguous runs: {5} length 1, {10,11} length 2
    assert sorted(r["repair_ranges"]) == [(5, 1), (10, 2)]
    assert r["budget_consumed_bytes"] == 3 * chunk


def test_decide_finalize_mask_first_never_consumes_budget():
    n = 32
    bm = _bm(n, [0, 1, 2])
    r = decide_finalize(n_chunks=n, recv_bitmap=bm, chunk_bytes=256,
                        policy=Policy.MASK_FIRST,
                        repair_budget_bytes=1 << 30)
    assert r["decision"] == FinalizeDecision.MASKED
    assert r["budget_consumed_bytes"] == 0


# ---------- Finalizer end-to-end via Python callbacks ----------------------

def test_finalizer_full_flow_with_callbacks():
    cfg = FinalizerConfig(repair_budget_bytes_per_step=64 * 1024)
    f = Finalizer(cfg)

    seen_repair, seen_finalize, seen_retire, seen_mask = [], [], [], []

    f.on_send_repair_req(
        lambda uid, ranges: seen_repair.append((uid, ranges)))
    f.on_send_finalize(
        lambda uid, decision, enc, body: seen_finalize.append(
            (uid, decision, enc, bytes(body))))
    f.on_send_retire(
        lambda uid, slot, gen: seen_retire.append((uid, slot, gen)))
    f.on_apply_mask(
        lambda uid, decision, mask, n_chunks: seen_mask.append(
            (uid, decision, bytes(mask), n_chunks)))

    n_chunks = 32
    chunk_bytes = 1024
    assert f.track(uid=42, slot=7, gen=2,
                   n_chunks=n_chunks, chunk_bytes=chunk_bytes,
                   policy=Policy.REPAIR_FIRST)

    # Two missing chunks (5, 10) → REPAIRED pending.
    bm = _bm(n_chunks, [i for i in range(n_chunks) if i not in (5, 10)])
    out = f.on_witness(uid=42, recv_bitmap=bm)
    assert out == FinalizeDecision.REPAIRED
    assert len(seen_repair) == 1
    assert seen_repair[0][0] == 42
    assert sorted(seen_repair[0][1]) == [(5, 1), (10, 1)]
    assert len(seen_finalize) == 0  # not finalized yet — pending repair
    assert f.is_tracked(42)

    # Repair completes.
    assert f.on_repair_complete(uid=42)
    assert len(seen_finalize) == 1
    assert seen_finalize[0][1] == FinalizeDecision.REPAIRED
    assert len(seen_retire) == 1
    assert seen_retire[0] == (42, 7, 2)
    assert len(seen_mask) == 1
    assert seen_mask[0][1] == FinalizeDecision.REPAIRED
    assert not f.is_tracked(42)

    # Stats
    s = f.stats
    assert s.n_finalized == 1
    assert s.total_repair_bytes == 2 * chunk_bytes


def test_finalizer_mask_path_via_callbacks():
    f = Finalizer()
    seen = []
    f.on_send_finalize(
        lambda uid, d, enc, body: seen.append((uid, d, enc)))
    f.on_send_retire(lambda *args: None)
    f.on_apply_mask(lambda *args: None)
    assert f.track(uid=1, slot=0, gen=0, n_chunks=8, chunk_bytes=256,
                   policy=Policy.MASK_FIRST)
    bm = _bm(8, [0, 1, 2])  # 5 missing
    assert f.on_witness(uid=1, recv_bitmap=bm) == FinalizeDecision.MASKED
    assert seen[-1][1] == FinalizeDecision.MASKED
    assert f.stats.budget_underruns == 0  # mask-first never spends


# ---------- RQMonitor through Python callbacks -----------------------------

def test_rq_monitor_low_watermark_via_python():
    cfg = RQMonitorConfig(low_watermark=2, refill_target=5, initial_credits=5)
    m = RQMonitor(cfg)
    low_events = []
    refill_calls = []
    m.on_low_watermark(lambda peer, c: low_events.append((peer, c)))
    m.on_replenish_request(lambda peer, k: refill_calls.append((peer, k)))
    m.register_peer(1)
    m.record_consumed(peer_edge=1, n=4)  # 5→1
    assert len(low_events) == 1
    assert low_events[0] == (1, 1)
    assert refill_calls[0] == (1, 4)  # target 5 - credits 1
    m.record_posted(peer_edge=1, n=4)
    assert m.credits(1) == 5
    assert not m.is_low(1)


# ---------- witness_codec encode/decode through bindings -------------------

def test_witness_codec_roundtrip_via_bindings():
    n = 1024
    arr = np.full((n + 7) // 8, 0xFF, dtype=np.uint8)
    arr[10] = 0  # 8 missing
    enc = encode_witness(bitmap=arr, n_chunks=n)
    assert enc["encoding"] == WitnessEncoding.RANGE_MISSING
    dec = decode_witness(encoding=enc["encoding"],
                          body=np.frombuffer(enc["body"], dtype=np.uint8),
                          n_chunks=n)
    assert dec["ok"] is True
    assert dec["recv_count"] == enc["recv_count"]
    # Roundtrip — every bit matches.
    decoded = np.frombuffer(dec["bitmap"], dtype=np.uint8)
    for i in range(n):
        bit_a = (arr[i >> 3] >> (i & 7)) & 1
        bit_b = (decoded[i >> 3] >> (i & 7)) & 1
        assert bit_a == bit_b, f"bit {i}"
