"""Shared helpers for the RQ6 baseline hooks.

The only non-trivial helper is ``apply_chunk_mask`` which zeros ``loss_rate``
fraction of ``chunk_bytes``-sized chunks of a 1-D tensor.  This simulates
"these chunks were lost in transit and replaced by zeros" — the same
error model used by SemiRDMA's GhostMask when a chunk arrives late, so
RC-Lossy and SemiRDMA can be compared on equal error-rate footing per
docs/phase3/rq6-loss-injection-strategy.md §2.

The mask is seeded deterministically (``loss_seed``) so each (rank, seed,
step) combination gets a reproducible drop pattern, matching the
reproducibility contract of SemiRDMA's own per-chunk Bernoulli drop.
"""

from __future__ import annotations

import numpy as np
import torch


def apply_chunk_mask(
    tensor: torch.Tensor,
    chunk_bytes: int,
    loss_rate: float,
    rng: np.random.Generator,
) -> int:
    """Zero a Bernoulli fraction of ``chunk_bytes``-sized chunks in-place.

    Args:
        tensor: 1-D contiguous CPU or CUDA tensor (typically a gradient
            bucket post-allreduce).  Modified in place.
        chunk_bytes: chunk granularity in bytes (matches SemiRDMA
            ``transport_cfg.chunk_bytes``).
        loss_rate: probability each chunk is zeroed, in ``[0.0, 1.0]``.
        rng: seeded ``numpy.random.Generator`` — allows deterministic
            drop patterns across runs with the same seed.

    Returns:
        number of chunks zeroed (for logging / stats).
    """
    if loss_rate <= 0.0:
        return 0
    if not tensor.is_contiguous():
        tensor = tensor.contiguous()

    nbytes = tensor.numel() * tensor.element_size()
    if nbytes == 0:
        return 0
    num_chunks = (nbytes + chunk_bytes - 1) // chunk_bytes

    # Bernoulli draw per chunk.
    drops = rng.random(num_chunks) < loss_rate  # bool array
    if not drops.any():
        return 0

    # Work on a byte view of the tensor so we don't care about dtype.
    # .view(torch.uint8) gives nbytes-length view; we assume CPU tensor for
    # this reshape (bucket is CPU in Stage A/B).
    if tensor.device.type == "cpu":
        byte_view = tensor.view(torch.uint8)
        # Torch does not expose a fast scatter-zero by range set, so fall
        # back to numpy for the per-chunk zeroing — still in-place because
        # .numpy() on a CPU torch tensor shares storage.
        bv = byte_view.numpy()
        dropped = 0
        for c in range(num_chunks):
            if drops[c]:
                start = c * chunk_bytes
                end = min(start + chunk_bytes, nbytes)
                bv[start:end] = 0
                dropped += 1
        return dropped

    # CUDA path: do it with torch slice assignment.
    # Reinterpret as uint8 view via storage-level reshape trick.
    flat_u8 = tensor.view(torch.uint8).reshape(-1) if tensor.dtype != torch.uint8 else tensor
    dropped = 0
    for c in range(num_chunks):
        if drops[c]:
            start = c * chunk_bytes
            end = min(start + chunk_bytes, nbytes)
            flat_u8[start:end].zero_()
            dropped += 1
    return dropped
