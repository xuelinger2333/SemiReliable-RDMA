"""W2.3e — single-process 2-rank ClearHookState E2E test.

Builds two ClearHookStates wired to each other via
``ClearHookState.for_in_process_pair``, then runs a synthetic bucket
through ``_run_clear_bucket`` from each "rank" thread in parallel.
Verifies the averaged output equals (G_a + G_b) / 2 on both sides.

This proves the hook's bidirectional bucket exchange works end-to-end:
both ranks simultaneously send + receive over UC + RC, the receivers
finalize, the senders consume FINALIZE/RETIRE, and both ranks land on
identical averaged bytes.

The multi-process DDP test that uses TCP bootstrap is future work
(scope shrink; already proven by this in-process variant + the W2.3d
single-direction E2E).
"""

from __future__ import annotations

import os
import threading

import numpy as np
import pytest

clear_mod = pytest.importorskip("semirdma._semirdma_ext.clear")

from semirdma._semirdma_ext.clear import FinalizeDecision, Policy  # noqa: E402
from semirdma.clear.hook import ClearHookState, _run_clear_bucket, step_advance  # noqa: E402
from semirdma.clear.transport import ClearTransportConfig  # noqa: E402


def _dev() -> str:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        pytest.skip("RDMA_LOOPBACK_DEVICE unset; skipping RDMA-gated test")
    return dev


def _gid() -> int:
    return int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))


def _make_pair():
    cfg = ClearTransportConfig(
        dev_name=_dev(), gid_index=_gid(),
        buffer_bytes=8 * 1024 * 1024,
        sq_depth=256, rq_depth=2048,
        chunk_bytes=4096,
        cp_recv_slots=32, cp_send_slots=8,
    )
    return ClearHookState.for_in_process_pair(cfg)


@pytest.mark.timeout(30)
def test_clear_hook_clean_wire_averages_bidirectional():
    """Both ranks push their gradient to the peer and receive peer's,
    average locally, end up with bit-identical averaged bytes."""
    a, b = _make_pair()
    try:
        # Per-rank gradients: distinct float32 patterns of equal byte size.
        n_floats = 1024
        nbytes = n_floats * 4
        g_a = np.linspace(-1.0, 1.0, n_floats, dtype=np.float32)
        g_b = np.linspace(2.0, -3.0, n_floats, dtype=np.float32)

        out_holder = {}
        errs: list = []

        def rank_thread(state, bucket_bytes, label):
            try:
                avg = _run_clear_bucket(
                    state, bucket_bytes=bucket_bytes, bucket_seq=0,
                    chunk_bytes=4096, ratio=1.0, timeout_ms=2000,
                    drain_timeout_ms=5000)
                out_holder[label] = avg
            except Exception as e:
                errs.append((label, e))

        ta = threading.Thread(target=rank_thread,
                              args=(a, g_a.tobytes(), "a"))
        tb = threading.Thread(target=rank_thread,
                              args=(b, g_b.tobytes(), "b"))
        ta.start(); tb.start()
        ta.join(); tb.join()

        if errs:
            for lbl, e in errs:
                print(f"rank {lbl} error: {e!r}")
            raise errs[0][1]

        # Expected average of float32 element-wise.
        expected = ((g_a + g_b) / 2).astype(np.float32)
        avg_a = np.frombuffer(out_holder["a"], dtype=np.float32)
        avg_b = np.frombuffer(out_holder["b"], dtype=np.float32)

        np.testing.assert_array_equal(avg_a, expected)
        np.testing.assert_array_equal(avg_b, expected)
        # And bit-identical across ranks.
        np.testing.assert_array_equal(avg_a, avg_b)
    finally:
        a.shutdown()
        b.shutdown()


@pytest.mark.timeout(30)
def test_clear_hook_multi_step_advances_uid():
    """Two consecutive steps must use different uids (uid_hash includes
    step_seq) so back-to-back buckets don't alias on the lease tables."""
    a, b = _make_pair()
    try:
        n_floats = 256
        for step in range(3):
            g_a = np.full(n_floats, float(step + 1), dtype=np.float32)
            g_b = np.full(n_floats, float(step + 1) * 2, dtype=np.float32)

            out_holder = {}
            errs: list = []

            def rank_thread(state, bucket_bytes, label):
                try:
                    out_holder[label] = _run_clear_bucket(
                        state, bucket_bytes=bucket_bytes, bucket_seq=0,
                        chunk_bytes=4096, ratio=1.0,
                        timeout_ms=2000, drain_timeout_ms=5000)
                except Exception as e:
                    errs.append((label, e))

            ta = threading.Thread(target=rank_thread,
                                  args=(a, g_a.tobytes(), "a"))
            tb = threading.Thread(target=rank_thread,
                                  args=(b, g_b.tobytes(), "b"))
            ta.start(); tb.start()
            ta.join(); tb.join()
            assert not errs, errs

            expected = ((g_a + g_b) / 2).astype(np.float32)
            np.testing.assert_array_equal(
                np.frombuffer(out_holder["a"], dtype=np.float32), expected)
            np.testing.assert_array_equal(
                np.frombuffer(out_holder["b"], dtype=np.float32), expected)

            step_advance(a)
            step_advance(b)

        # After 3 steps, lease tables should drain to empty. RETIRE
        # arrives async via the bg poll thread, so spin briefly for
        # the last in-flight slot to be released.
        import time
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if (a.tx.sender_leases.pressure().in_use == 0 and
                b.tx.sender_leases.pressure().in_use == 0):
                break
            time.sleep(0.005)
        assert a.tx.sender_leases.pressure().in_use == 0
        assert b.tx.sender_leases.pressure().in_use == 0
    finally:
        a.shutdown()
        b.shutdown()


@pytest.mark.timeout(30)
def test_clear_hook_two_buckets_per_step():
    """Two distinct buckets in one step (different bucket_seq) get
    different uids and don't collide."""
    a, b = _make_pair()
    try:
        n_floats = 128
        # Two buckets per step.
        g_a0 = np.arange(n_floats, dtype=np.float32)
        g_b0 = np.arange(n_floats, dtype=np.float32) * 2
        g_a1 = np.full(n_floats, 7.0, dtype=np.float32)
        g_b1 = np.full(n_floats, -3.0, dtype=np.float32)

        out_holder = {}
        errs: list = []

        def rank_thread(state, label):
            try:
                ga = g_a0 if label.startswith("a") else g_b0
                ga1 = g_a1 if label.startswith("a") else g_b1
                # bucket 0
                out_holder[f"{label}_0"] = _run_clear_bucket(
                    state, bucket_bytes=ga.tobytes(), bucket_seq=0,
                    chunk_bytes=4096, ratio=1.0, timeout_ms=2000,
                    drain_timeout_ms=5000)
                # bucket 1
                out_holder[f"{label}_1"] = _run_clear_bucket(
                    state, bucket_bytes=ga1.tobytes(), bucket_seq=1,
                    chunk_bytes=4096, ratio=1.0, timeout_ms=2000,
                    drain_timeout_ms=5000)
            except Exception as e:
                errs.append((label, e))

        ta = threading.Thread(target=rank_thread, args=(a, "a"))
        tb = threading.Thread(target=rank_thread, args=(b, "b"))
        ta.start(); tb.start()
        ta.join(); tb.join()
        assert not errs, errs

        exp0 = ((g_a0 + g_b0) / 2).astype(np.float32)
        exp1 = ((g_a1 + g_b1) / 2).astype(np.float32)
        np.testing.assert_array_equal(
            np.frombuffer(out_holder["a_0"], dtype=np.float32), exp0)
        np.testing.assert_array_equal(
            np.frombuffer(out_holder["b_0"], dtype=np.float32), exp0)
        np.testing.assert_array_equal(
            np.frombuffer(out_holder["a_1"], dtype=np.float32), exp1)
        np.testing.assert_array_equal(
            np.frombuffer(out_holder["b_1"], dtype=np.float32), exp1)
    finally:
        a.shutdown()
        b.shutdown()
