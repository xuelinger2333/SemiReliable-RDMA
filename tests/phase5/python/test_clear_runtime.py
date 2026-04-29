"""Phase 5 W2.3 — apply_finalize tests."""

from __future__ import annotations

import numpy as np
import pytest

from semirdma.clear.policy import FinalizeDecision
from semirdma.clear.runtime import apply_finalize


def _bitmap(n_chunks: int, present_idx) -> bytearray:
    bm = bytearray((n_chunks + 7) // 8)
    for i in present_idx:
        bm[i >> 3] |= 1 << (i & 7)
    return bm


# ---------- no-op decisions -------------------------------------------------


def test_delivered_is_noop():
    flat = bytearray(b"\xAA" * 32)
    bm = _bitmap(8, range(8))
    s = apply_finalize(FinalizeDecision.DELIVERED,
                       mask_bitmap=bm, n_chunks=8, chunk_bytes=4,
                       flat=flat)
    assert s["applied_chunks"] == 0
    assert s["bytes_written"] == 0
    assert flat == bytearray(b"\xAA" * 32)


def test_repaired_is_noop():
    flat = bytearray(b"\x55" * 32)
    bm = _bitmap(8, range(8))
    apply_finalize(FinalizeDecision.REPAIRED,
                   mask_bitmap=bm, n_chunks=8, chunk_bytes=4,
                   flat=flat)
    assert flat == bytearray(b"\x55" * 32)


def test_fallback_rc_is_noop():
    flat = bytearray(b"\x33" * 32)
    bm = _bitmap(8, range(8))
    apply_finalize(FinalizeDecision.FALLBACK_RC,
                   mask_bitmap=bm, n_chunks=8, chunk_bytes=4,
                   flat=flat)
    assert flat == bytearray(b"\x33" * 32)


# ---------- MASKED ----------------------------------------------------------


def test_masked_zeros_only_missing_chunks():
    n_chunks = 8
    chunk_bytes = 4
    flat = bytearray(b"\xFF" * (n_chunks * chunk_bytes))
    # Chunks 2 and 5 missing; the rest present.
    present = [0, 1, 3, 4, 6, 7]
    bm = _bitmap(n_chunks, present)
    s = apply_finalize(FinalizeDecision.MASKED,
                       mask_bitmap=bm, n_chunks=n_chunks,
                       chunk_bytes=chunk_bytes, flat=flat)
    assert s["applied_chunks"] == 2
    assert s["bytes_written"] == 8

    # Chunks 0,1,3,4,6,7 still 0xFF; chunks 2 and 5 zeroed.
    for i in range(n_chunks):
        start = i * chunk_bytes
        sl = bytes(flat[start:start + chunk_bytes])
        if i in (2, 5):
            assert sl == b"\x00" * chunk_bytes, f"chunk {i} not zeroed"
        else:
            assert sl == b"\xFF" * chunk_bytes, f"chunk {i} corrupted"


def test_masked_with_short_last_chunk():
    """Last chunk shorter than chunk_bytes (DDP padding case)."""
    n_chunks = 4
    chunk_bytes = 4
    flat_size = n_chunks * chunk_bytes - 2  # last chunk only 2 bytes
    flat = bytearray(b"\xAA" * flat_size)
    bm = _bitmap(n_chunks, [0, 1, 2])  # chunk 3 missing
    s = apply_finalize(FinalizeDecision.MASKED,
                       mask_bitmap=bm, n_chunks=n_chunks,
                       chunk_bytes=chunk_bytes, flat=flat)
    # chunk 3 occupies bytes [12, flat_size=14)
    assert s["applied_chunks"] == 1
    assert s["bytes_written"] == 2  # only 2 bytes touched
    assert flat[:12] == bytearray(b"\xAA" * 12)
    assert flat[12:] == bytearray(b"\x00" * 2)


def test_masked_all_present_writes_nothing():
    flat = bytearray(b"\x77" * 16)
    bm = _bitmap(4, range(4))
    s = apply_finalize(FinalizeDecision.MASKED,
                       mask_bitmap=bm, n_chunks=4, chunk_bytes=4,
                       flat=flat)
    assert s["applied_chunks"] == 0
    assert flat == bytearray(b"\x77" * 16)


# ---------- STALE -----------------------------------------------------------


def test_stale_copies_prev_for_missing():
    n_chunks = 4
    chunk_bytes = 4
    flat = bytearray(b"\x11" * (n_chunks * chunk_bytes))
    prev = bytearray(b"\x99" * (n_chunks * chunk_bytes))
    bm = _bitmap(n_chunks, [0, 2])  # chunks 1 and 3 missing
    s = apply_finalize(FinalizeDecision.STALE,
                       mask_bitmap=bm, n_chunks=n_chunks,
                       chunk_bytes=chunk_bytes, flat=flat,
                       prev_flat=prev)
    assert s["applied_chunks"] == 2
    assert s["bytes_written"] == 8
    # chunks 0, 2 unchanged (0x11), chunks 1, 3 from prev (0x99)
    assert flat[0:4]  == b"\x11" * 4
    assert flat[4:8]  == b"\x99" * 4
    assert flat[8:12] == b"\x11" * 4
    assert flat[12:]  == b"\x99" * 4


def test_stale_without_prev_raises():
    flat = bytearray(b"\x00" * 16)
    bm = _bitmap(4, [0])
    with pytest.raises(ValueError):
        apply_finalize(FinalizeDecision.STALE,
                       mask_bitmap=bm, n_chunks=4, chunk_bytes=4,
                       flat=flat)


def test_stale_with_short_prev_raises():
    flat = bytearray(b"\x00" * 16)
    prev = bytearray(b"\xFF" * 8)
    bm = _bitmap(4, [0])
    with pytest.raises(ValueError):
        apply_finalize(FinalizeDecision.STALE,
                       mask_bitmap=bm, n_chunks=4, chunk_bytes=4,
                       flat=flat, prev_flat=prev)


# ---------- numpy buffer interop --------------------------------------------


def test_accepts_numpy_arrays():
    flat = np.full(16, 0xAB, dtype=np.uint8)
    bm = _bitmap(4, [0, 2])
    apply_finalize(FinalizeDecision.MASKED,
                   mask_bitmap=bm, n_chunks=4, chunk_bytes=4,
                   flat=flat)
    assert (flat[0:4] == 0xAB).all()
    assert (flat[4:8] == 0).all()
    assert (flat[8:12] == 0xAB).all()
    assert (flat[12:16] == 0).all()


def test_rejects_immutable_bytes_target():
    flat = bytes(b"\x00" * 16)  # immutable
    bm = _bitmap(4, [0])
    with pytest.raises(TypeError):
        apply_finalize(FinalizeDecision.MASKED,
                       mask_bitmap=bm, n_chunks=4, chunk_bytes=4,
                       flat=flat)


# ---------- buffer-size validation ------------------------------------------


def test_rejects_undersized_flat():
    flat = bytearray(8)
    bm = _bitmap(4, [0, 1, 2, 3])
    # Need at least (4-1)*8 + 1 = 25 bytes for n_chunks=4, chunk=8.
    with pytest.raises(ValueError):
        apply_finalize(FinalizeDecision.MASKED,
                       mask_bitmap=bm, n_chunks=4, chunk_bytes=8,
                       flat=flat)


def test_rejects_undersized_bitmap():
    flat = bytearray(16)
    bm = bytearray(0)
    with pytest.raises(ValueError):
        apply_finalize(FinalizeDecision.MASKED,
                       mask_bitmap=bm, n_chunks=4, chunk_bytes=4,
                       flat=flat)
