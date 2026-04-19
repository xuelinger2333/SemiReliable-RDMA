"""Loopback end-to-end tests for SemiRDMATransport.

Build two transports on the same host, exchange QP info over TCP,
send bytes, verify byte-for-byte equality.  Covers:

  - happy path (loss_rate=0): every byte must survive
  - lossy path (loss_rate=0.25): ~25% chunks zeroed by GhostMask,
    remaining chunks must still match exactly.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest


def _pair_up(tx, rx, port: int) -> None:
    """Bootstrap tx -> rx and bring both QPs to RTS."""
    from semirdma._bootstrap import exchange_qp_info

    done = {}

    def server_side() -> None:
        q, m = exchange_qp_info(
            True, "127.0.0.1", port, rx.local_qp_info, rx.local_mr_info
        )
        rx.bring_up(q, m)
        done["rx"] = True

    t = threading.Thread(target=server_side, daemon=True)
    t.start()
    time.sleep(0.2)

    q, m = exchange_qp_info(
        False, "127.0.0.1", port, tx.local_qp_info, tx.local_mr_info
    )
    tx.bring_up(q, m)
    t.join(timeout=10)
    assert done.get("rx"), "rx bring-up did not complete"


def test_loopback_64k_no_loss(rxe_device) -> None:
    from semirdma import SemiRDMATransport, TransportConfig
    from semirdma._semirdma_ext import ChunkSet

    cfg = TransportConfig(
        dev_name=rxe_device,
        buffer_bytes=1 << 20,
        chunk_bytes=16 * 1024,
        sq_depth=16,
        rq_depth=64,
    )
    tx = SemiRDMATransport(cfg)
    rx = SemiRDMATransport(cfg)
    _pair_up(tx, rx, port=20001)

    N = 64 * 1024
    payload = (np.arange(N, dtype=np.uint32) ^ 0xDEADBEEF).tobytes()
    tx.post_gradient(payload)

    cs_recv = ChunkSet(0, N, cfg.chunk_bytes)
    stats = rx.await_gradient(cs_recv, timeout_ms=5000)
    assert stats["ok"], f"await timed out: {stats}"
    assert stats["completed"] == cs_recv.size()

    recv = np.frombuffer(rx.buffer_view(), dtype=np.uint8)[:N]
    expected = np.frombuffer(payload, dtype=np.uint8)
    assert np.array_equal(recv, expected)


def test_loopback_with_software_loss(rxe_device) -> None:
    """With loss_rate=0.25, some chunks are deliberately dropped by the
    sender.  Surviving chunks must match bytewise; dropped chunks must be
    zeroed by GhostMask (not filled with stale buffer data).

    Receiver-side buffer is pre-filled with 0xFF so we can detect any
    stale-read leak.
    """
    from semirdma import SemiRDMATransport, TransportConfig
    from semirdma._semirdma_ext import ChunkSet

    cfg_send = TransportConfig(
        dev_name=rxe_device,
        buffer_bytes=1 << 20,
        chunk_bytes=16 * 1024,
        sq_depth=16,
        rq_depth=64,
        loss_rate=0.25,
        loss_seed=1234,
    )
    cfg_recv = TransportConfig(
        dev_name=rxe_device,
        buffer_bytes=1 << 20,
        chunk_bytes=16 * 1024,
        sq_depth=16,
        rq_depth=64,
    )
    tx = SemiRDMATransport(cfg_send)
    rx = SemiRDMATransport(cfg_recv)
    _pair_up(tx, rx, port=20002)

    # Poison receiver buffer with 0xFF before the write so any ghost bytes
    # show up immediately.
    N = 16 * 16 * 1024   # 16 chunks
    rx_mv = rx.buffer_view()
    rx_bytes = memoryview(rx_mv).cast("B")
    for i in range(N):
        rx_bytes[i] = 0xFF

    payload = np.random.default_rng(42).integers(0, 255, size=N, dtype=np.uint8).tobytes()
    tx.post_gradient(payload)

    cs_recv = ChunkSet(0, N, cfg_recv.chunk_bytes)
    # Generous timeout so slow SoftRoCE paths aren't flaky here — the point
    # of this test is correctness, not latency.
    stats = rx.await_gradient(cs_recv, ratio=0.5, timeout_ms=3000)
    assert stats["ok"], stats
    # Some chunks should be missing due to loss_rate=0.25.
    assert stats["completed"] < cs_recv.size()

    recv = np.frombuffer(rx.buffer_view(), dtype=np.uint8)[:N]
    expected = np.frombuffer(payload, dtype=np.uint8)

    chunk = cfg_recv.chunk_bytes
    n_match = 0
    n_zero = 0
    n_stale = 0
    for i in range(cs_recv.size()):
        region_actual = recv[i * chunk : (i + 1) * chunk]
        region_expect = expected[i * chunk : (i + 1) * chunk]
        if np.array_equal(region_actual, region_expect):
            n_match += 1
        elif (region_actual == 0).all():
            n_zero += 1
        else:
            n_stale += 1
    assert n_stale == 0, (
        f"ghost-gradient leak: {n_stale} chunks differ from both expected "
        f"and zero (matched={n_match}, zeroed={n_zero})"
    )
    assert n_match + n_zero == cs_recv.size()
    # Sanity: at the 0.25 rate and seed=1234 some chunks should drop.
    assert n_zero > 0, "loss_rate=0.25 somehow dropped no chunks"
