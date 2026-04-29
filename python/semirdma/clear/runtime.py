"""Apply a CLEAR finalize decision to a flat byte buffer.

When the C++ Finalizer publishes FINALIZE for a uid, every rank must
mutate its local copy of the bucket buffer the same way before the SGD
step. This module is the canonical Python implementation of that
mutation.

Inputs:
    decision      — FinalizeDecision from policy.py
    mask_bitmap   — bit-packed LSB-first; bit i = 1 ⇒ chunk i is "present"
    n_chunks      — total chunks in the bucket (the wire identity uses
                    this to size the bitmap)
    chunk_bytes   — bytes per chunk
    flat          — writable buffer holding the bucket's flat data
    prev_flat     — buffer holding the previous step's value of this
                    bucket; only required for STALE
    recv_count    — populated chunk count; required for ESTIMATOR_SCALE
                    when caller needs in-place rescale

Semantics:
    DELIVERED     — no-op.
    REPAIRED      — no-op (repair already filled the holes on the wire).
    MASKED        — zero every chunk where mask_bitmap[i] == 0.
    STALE         — copy prev_flat[chunk_i] into flat[chunk_i] for each
                    chunk where mask_bitmap[i] == 0. Requires prev_flat.
    FALLBACK_RC   — no-op (RC has resent everything by the time we land
                    here).

Pure-Python; operates on numpy arrays / memoryviews. No torch import.
"""

from __future__ import annotations

from typing import Optional, Union

import numpy as np

from .policy import FinalizeDecision

# Anything castable to a 1-D uint8 numpy view.
ByteBuffer = Union[bytes, bytearray, memoryview, np.ndarray]


def _as_uint8_view(buf: ByteBuffer, *, writable: bool) -> np.ndarray:
    """Best-effort uint8 1-D view. No copy unless the input is bytes."""
    arr = np.frombuffer(buf, dtype=np.uint8)
    if writable:
        try:
            arr.setflags(write=True)
        except ValueError:
            # bytes is immutable; caller must pass bytearray / mutable view.
            raise TypeError(
                "apply_finalize requires a writable buffer for the target; "
                "pass bytearray / np.ndarray / a writable memoryview"
            )
    return arr


def _bit_test(bitmap: np.ndarray, i: int) -> bool:
    return bool(bitmap[i >> 3] & (1 << (i & 7)))


def apply_finalize(
    decision: FinalizeDecision,
    *,
    mask_bitmap: ByteBuffer,
    n_chunks: int,
    chunk_bytes: int,
    flat: ByteBuffer,
    prev_flat: Optional[ByteBuffer] = None,
    recv_count: Optional[int] = None,
) -> dict:
    """Apply a finalize decision in place. Returns a stats dict.

    Stats keys:
        applied_chunks       — chunks the function actually touched
        bytes_written        — total bytes mutated
        decision             — echoed
    """
    decision = FinalizeDecision(decision)
    flat_view = _as_uint8_view(flat, writable=True)
    if flat_view.size < n_chunks * chunk_bytes:
        # The last chunk may legitimately be shorter than chunk_bytes (DDP
        # bucket padding). Allow that, but require at least
        # (n_chunks-1) * chunk_bytes + 1 bytes.
        min_required = max(0, (n_chunks - 1) * chunk_bytes + 1)
        if flat_view.size < min_required:
            raise ValueError(
                f"flat buffer too small: have {flat_view.size}, need at "
                f"least {min_required} (for n_chunks={n_chunks}, "
                f"chunk_bytes={chunk_bytes})"
            )

    if decision in (
        FinalizeDecision.DELIVERED,
        FinalizeDecision.REPAIRED,
        FinalizeDecision.FALLBACK_RC,
    ):
        return {"applied_chunks": 0, "bytes_written": 0, "decision": decision}

    bitmap_arr = _as_uint8_view(mask_bitmap, writable=False)
    if bitmap_arr.size < (n_chunks + 7) // 8:
        raise ValueError(
            f"mask_bitmap too small: have {bitmap_arr.size}, need "
            f"{(n_chunks + 7) // 8} for n_chunks={n_chunks}"
        )

    if decision == FinalizeDecision.MASKED:
        applied = 0
        bytes_written = 0
        for i in range(n_chunks):
            if _bit_test(bitmap_arr, i):
                continue
            start = i * chunk_bytes
            end = min(start + chunk_bytes, flat_view.size)
            flat_view[start:end] = 0
            applied += 1
            bytes_written += end - start
        return {
            "applied_chunks": applied,
            "bytes_written": bytes_written,
            "decision": decision,
        }

    if decision == FinalizeDecision.STALE:
        if prev_flat is None:
            raise ValueError(
                "FinalizeDecision.STALE requires prev_flat for fill-from-prev"
            )
        prev_view = _as_uint8_view(prev_flat, writable=False)
        if prev_view.size < flat_view.size:
            raise ValueError(
                f"prev_flat ({prev_view.size} B) shorter than flat "
                f"({flat_view.size} B)"
            )
        applied = 0
        bytes_written = 0
        for i in range(n_chunks):
            if _bit_test(bitmap_arr, i):
                continue
            start = i * chunk_bytes
            end = min(start + chunk_bytes, flat_view.size)
            flat_view[start:end] = prev_view[start:end]
            applied += 1
            bytes_written += end - start
        return {
            "applied_chunks": applied,
            "bytes_written": bytes_written,
            "decision": decision,
        }

    raise NotImplementedError(f"Unhandled FinalizeDecision: {decision!r}")


__all__ = [
    "apply_finalize",
]
