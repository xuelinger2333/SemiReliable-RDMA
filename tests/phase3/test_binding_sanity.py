"""Stage A commit 7 — smoke tests for the pybind11 module.

Scope: construct C++ objects, touch their methods, but do NOT require two
QPs to talk to each other.  Loopback / bring-up live in
``test_loopback_transport.py``.  Splitting them means a binding build
regression is visible even without a working TCP + GID stack.
"""

from __future__ import annotations

import numpy as np
import pytest


def test_module_exports_expected_symbols() -> None:
    from semirdma import _semirdma_ext as ext

    syms = {s for s in dir(ext) if not s.startswith("_")}
    assert syms == {
        "ChunkSet",
        "RatioController",
        "RemoteMR",
        "RemoteQpInfo",
        "UCQPEngine",
        "apply_ghost_mask",
    }


def test_chunkset_layout() -> None:
    from semirdma._semirdma_ext import ChunkSet

    cs = ChunkSet(0, 64 * 1024, 16 * 1024)
    assert cs.size() == 4
    assert cs.chunk_bytes == 16 * 1024
    assert cs.total_bytes == 64 * 1024
    assert cs.base_offset == 0
    assert cs.num_completed() == 0
    assert cs.completion_ratio() == 0.0

    for i in range(4):
        c = cs.chunk(i)
        assert c["chunk_id"] == i
        assert c["length"] == 16 * 1024
        assert c["local_offset"] == i * 16 * 1024

    cs.mark_completed(1)
    cs.mark_completed(3)
    assert cs.num_completed() == 2
    assert pytest.approx(cs.completion_ratio()) == 0.5
    assert cs.state(1)["has_cqe"] is True
    assert cs.state(0)["has_cqe"] is False

    cs.reset_states()
    assert cs.num_completed() == 0


def test_remote_structs_roundtrip() -> None:
    from semirdma._semirdma_ext import RemoteMR, RemoteQpInfo

    mr = RemoteMR(addr=0x1122334455667788, rkey=0xCAFEBABE)
    assert mr.addr == 0x1122334455667788
    assert mr.rkey == 0xCAFEBABE

    qpi = RemoteQpInfo(qpn=42, gid=bytes(range(16)))
    assert qpi.qpn == 42
    assert qpi.gid == bytes(range(16))

    # 17-byte gid must be rejected.
    with pytest.raises(Exception):
        RemoteQpInfo(qpn=0, gid=b"\x00" * 17)


def test_apply_ghost_mask_zeros_missing_chunks() -> None:
    from semirdma._semirdma_ext import ChunkSet, apply_ghost_mask

    cs = ChunkSet(0, 64 * 1024, 16 * 1024)
    cs.mark_completed(0)
    cs.mark_completed(2)
    buf = np.full(64 * 1024, 0xAA, dtype=np.uint8)
    apply_ghost_mask(buf, cs)
    assert (buf[0 : 16 * 1024] == 0xAA).all()       # kept
    assert (buf[16 * 1024 : 32 * 1024] == 0).all()  # zeroed
    assert (buf[32 * 1024 : 48 * 1024] == 0xAA).all()
    assert (buf[48 * 1024 : 64 * 1024] == 0).all()


def test_apply_ghost_mask_rejects_wrong_buffer() -> None:
    from semirdma._semirdma_ext import ChunkSet, apply_ghost_mask

    cs = ChunkSet(0, 64 * 1024, 16 * 1024)

    # 2-D buffer — must reject.
    buf_2d = np.zeros((2, 32 * 1024), dtype=np.uint8)
    with pytest.raises(Exception):
        apply_ghost_mask(buf_2d, cs)

    # wrong itemsize — must reject.
    buf_u16 = np.zeros(32 * 1024, dtype=np.uint16)
    with pytest.raises(Exception):
        apply_ghost_mask(buf_u16, cs)

    # too small — must reject.
    buf_short = np.zeros(32 * 1024, dtype=np.uint8)
    with pytest.raises(Exception):
        apply_ghost_mask(buf_short, cs)


def test_engine_construct_requires_device(rxe_device) -> None:
    """Constructing UCQPEngine allocates real RDMA resources — skipped
    elsewhere via the ``rxe_device`` fixture."""
    from semirdma._semirdma_ext import UCQPEngine

    eng = UCQPEngine(rxe_device, 4 * 1024 * 1024, 16, 64)
    assert eng.qpn > 0
    assert eng.buf_bytes >= 4 * 1024 * 1024

    # buffer_view must be writable and correctly sized.
    view = eng.local_buf_view()
    assert len(view) == eng.buf_bytes
    mv = memoryview(view)
    mv[0] = 0xAA
    assert mv[0] == 0xAA

    # poll_cq with 0 timeout on a fresh QP returns no completions (not an error).
    assert eng.poll_cq(8, 0) == []

    # outstanding_recv starts at 0; post_recv_batch bumps it.
    assert eng.outstanding_recv() == 0
    eng.post_recv_batch(32, base_wr_id=1000)
    assert eng.outstanding_recv() == 32

    # local_qp_info / local_mr_info expose the RemoteQpInfo / RemoteMR shapes.
    qpi = eng.local_qp_info()
    mr = eng.local_mr_info()
    assert qpi.qpn == eng.qpn
    assert len(qpi.gid) == 16
    assert mr.rkey != 0


def test_ratio_controller_zero_timeout_fails_fast(rxe_device) -> None:
    """With no CQEs pending, wait_for_ratio(timeout=0) returns timed_out."""
    from semirdma._semirdma_ext import ChunkSet, RatioController, UCQPEngine

    eng = UCQPEngine(rxe_device, 1 * 1024 * 1024, 16, 64)
    rc = RatioController(eng)
    cs = ChunkSet(0, 64 * 1024, 16 * 1024)
    stats = rc.wait_for_ratio(cs, 0.5, 0)
    assert stats["ok"] is False
    assert stats["timed_out"] is True
    assert stats["completed"] == 0
