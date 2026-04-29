"""W2.3f — multi-process 2-rank ClearHookState E2E test.

Spawns two worker processes that each call
``ClearHookState.for_rank`` with a TCP-bootstrap port, exchange QP
info over loopback TCP, and run a synthetic bucket through
``_run_clear_bucket``. Verifies the averaged output equals
``(G_0 + G_1) / 2`` on both ranks.

Unlike ``test_clear_hook_e2e.py`` (single-process, two threads) this
test exercises the real production bring-up path that DDP will use:
each rank owns its own RDMA context, exchanges (qpn, gid, addr, rkey)
over plain TCP, and brings up four QPs (UC tx + RC tx.cp + UC rx +
RC rx.cp) before the first bucket.

RDMA-gated: skipped unless ``RDMA_LOOPBACK_DEVICE`` is set. Requires a
NIC that allows two QP contexts on the same device (CX-5/CX-6 do).
"""

from __future__ import annotations

import os
import socket

import numpy as np
import pytest


def _dev() -> str:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        pytest.skip("RDMA_LOOPBACK_DEVICE unset; skipping RDMA-gated test")
    return dev


def _gid() -> int:
    return int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _worker(rank: int, port_base: int, dev: str, gid: int,
            n_floats: int, out_q) -> None:
    """Run as a separate process via mp.spawn."""
    try:
        # Local imports so spawn doesn't drag torch into the parent.
        from semirdma.clear.hook import ClearHookState, _run_clear_bucket
        from semirdma.clear.transport import ClearTransportConfig

        cfg = ClearTransportConfig(
            dev_name=dev, gid_index=gid,
            buffer_bytes=8 * 1024 * 1024,
            sq_depth=256, rq_depth=2048,
            chunk_bytes=4096,
            cp_recv_slots=32, cp_send_slots=8,
        )
        state = ClearHookState.for_rank(
            rank=rank, world_size=2,
            peer_host="127.0.0.1", port=port_base,
            cfg=cfg,
        )

        # Per-rank distinct gradient pattern, same byte size.
        if rank == 0:
            grad = np.linspace(-1.0, 1.0, n_floats, dtype=np.float32)
        else:
            grad = np.linspace(2.0, -3.0, n_floats, dtype=np.float32)

        # Instrument bg callback firings so we can see what arrived.
        diag = {"begin_rx": 0, "finalize_tx": 0, "retire_tx": 0}
        orig_on_begin = state.rx.cp.on_begin
        orig_on_finalize = state.tx.cp.on_finalize
        orig_on_retire = state.tx.cp.on_retire
        # Re-wrap each callback by reading the current ones from hook.py
        # — we can't intercept the C++-side ones, so just count via finalize_event.
        # Easier: poll counters from the lease tables.

        try:
            avg = _run_clear_bucket(
                state, bucket_bytes=grad.tobytes(), bucket_seq=0,
                chunk_bytes=4096, ratio=1.0,
                timeout_ms=2000, drain_timeout_ms=5000,
            )
        except Exception as e:
            # Capture transport state at failure.
            try:
                rx_recv_outstanding = state.rx.engine.outstanding_recv()
                tx_recv_outstanding = state.tx.engine.outstanding_recv()
                def _stat(s):
                    return {k: getattr(s, k) for k in dir(s)
                            if not k.startswith("_") and isinstance(getattr(s, k), int)}
                cp_rx_stats = _stat(state.rx.cp.stats)
                cp_tx_stats = _stat(state.tx.cp.stats)
                rx_lease_pressure = "n/a"
                tx_lease_pressure = state.tx.sender_leases.pressure().in_use
                sync_keys = list(state._sync.keys())
                sync_states = {hex(k): {
                    "begin_set": s.begin_event.is_set(),
                    "finalize_set": s.finalize_event.is_set(),
                } for k, s in state._sync.items()}
                diag = {
                    "rx_recv_outstanding": rx_recv_outstanding,
                    "tx_recv_outstanding": tx_recv_outstanding,
                    "cp_rx_stats": cp_rx_stats,
                    "cp_tx_stats": cp_tx_stats,
                    "rx_lease_size": rx_lease_pressure,
                    "tx_lease_in_use": tx_lease_pressure,
                    "sync_states": sync_states,
                }
            except Exception as ee:
                diag = {"diag_err": str(ee)}
            raise RuntimeError(f"{e}\nDIAG: {diag}") from e
        finally:
            state.shutdown()

        out_q.put((rank, avg, grad.tobytes(), None))
    except Exception as e:
        import traceback
        out_q.put((rank, None, None,
                   f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


@pytest.mark.timeout(60)
def test_clear_hook_multiproc_averages_bidirectional():
    """Two processes, real TCP bootstrap, real RDMA, agree on
    (G_0 + G_1) / 2."""
    _dev()  # gate
    mp = pytest.importorskip("multiprocessing")
    ctx = mp.get_context("spawn")

    port_base = _free_port()
    n_floats = 1024

    out_q = ctx.Queue()
    procs = [
        ctx.Process(target=_worker,
                    args=(r, port_base, _dev(), _gid(), n_floats, out_q))
        for r in range(2)
    ]
    for p in procs:
        p.start()
    # Drain queue first (workers always put a result, even on exception)
    # so we can see BOTH ranks' status before any exitcode assertion.
    results: dict = {}
    import queue as _q
    for _ in range(2):
        try:
            r, avg_bytes, grad_bytes, err = out_q.get(timeout=58)
        except _q.Empty:
            break
        results[r] = (avg_bytes, grad_bytes, err)
    for p in procs:
        p.join(timeout=5)

    if not results:
        pytest.fail("no results from either worker (both timed out)")
    errs = {r: v[2] for r, v in results.items() if v[2]}
    if errs:
        msg = "\n".join(f"--- rank {r} ---\n{e}" for r, e in errs.items())
        pytest.fail(f"worker(s) raised:\n{msg}")

    assert set(results.keys()) == {0, 1}, results.keys()

    g0 = np.frombuffer(results[0][1], dtype=np.float32)
    g1 = np.frombuffer(results[1][1], dtype=np.float32)
    expected = ((g0 + g1) / 2).astype(np.float32)

    avg_0 = np.frombuffer(results[0][0], dtype=np.float32)
    avg_1 = np.frombuffer(results[1][0], dtype=np.float32)
    np.testing.assert_array_equal(avg_0, expected)
    np.testing.assert_array_equal(avg_1, expected)
    np.testing.assert_array_equal(avg_0, avg_1)
