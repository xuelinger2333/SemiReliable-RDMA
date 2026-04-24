"""RC-Lossy DDP comm hook — drift-free lossy baseline.

Represents "reliable transport + simulated chunk loss, identical across
ranks" in the baseline comparison.  Purpose: isolate "accuracy
degradation from lossy information" from "UC-specific per-rank drift".
If semirdma-UC matches RC-Lossy in final accuracy, the parity comes
from SGD's tolerance to missing chunks (not from UC's error pattern),
and UC's unique contribution is throughput, not accuracy.

  1. A reliable AllReduce runs first (gloo TCP backend, identical to
     RC-Baseline) — all bytes arrive correctly, both ranks hold the
     same averaged gradient.
  2. Both ranks then apply the SAME Bernoulli chunk mask (shared
     ``loss_seed``, identical RNG state across ranks because DDP
     invokes the hook with matched cadence on equal-sized buckets),
     zeroing the same chunk indices on both sides.  Post-mask, both
     ranks still hold bit-identical gradients → no drift.

The HW retry tail-latency that a real RC QP would pay on a lossy wire
is captured separately by the ``ib_write_bw`` RC throughput sweep
(transport-layer evidence); this hook is for training-layer convergence
only.
"""

from dataclasses import dataclass

import numpy as np
import torch
import torch.distributed as dist
import torch.futures as futures
from torch.distributed import GradBucket

from semirdma.baselines._common import apply_chunk_mask


@dataclass
class RCLossyConfig:
    """Parameters that control the loss-simulation layer.

    ``chunk_bytes`` should match ``TransportConfig.chunk_bytes`` (16384 on
    the c240g5 CX-6 Lx setup per stage-b-cloudlab.yaml) so the mask
    granularity is identical to SemiRDMA.
    """

    chunk_bytes: int = 16384
    loss_rate: float = 0.0
    loss_seed: int = 42


class RCLossyState:
    """Holds the per-rank numpy RNG so each step gets a deterministic but
    distinct mask, reproducible from ``loss_seed``."""

    def __init__(self, cfg: RCLossyConfig) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.loss_seed)
        self.bucket_idx = 0
        self.total_chunks_dropped = 0

    @classmethod
    def for_rank(
        cls,
        *,
        rank: int,
        cfg: RCLossyConfig,
    ) -> "RCLossyState":
        """Mirrors ``SemiRDMAHookState.for_rank`` so both baselines have
        the same construction contract in the training driver.

        Note: ``rank`` is intentionally unused — RC-Lossy is the
        drift-free baseline, so every rank must build its RNG from the
        *same* ``loss_seed``.  Matched RNG state + matched hook-call
        cadence across ranks produces bit-identical Bernoulli masks,
        and post-mask the averaged gradient stays identical on all
        ranks (no drift).  Introducing a per-rank offset would make
        ranks drift on every bucket after the first masked step.
        """
        del rank
        return cls(cfg)


def rc_lossy_hook(
    state: RCLossyState, bucket: GradBucket
) -> torch.futures.Future[torch.Tensor]:
    """Reliable AllReduce followed by Bernoulli per-chunk zeroing.

    Matches ``rc_baseline_hook`` when ``loss_rate == 0``.
    """
    state.bucket_idx += 1
    tensor = bucket.buffer()

    world_size = dist.get_world_size()
    work = dist.all_reduce(tensor, op=dist.ReduceOp.SUM, async_op=True)
    fut = work.get_future()

    def _finish(fut: futures.Future) -> torch.Tensor:
        out = fut.value()[0]
        out.div_(world_size)
        if state.cfg.loss_rate > 0.0:
            dropped = apply_chunk_mask(
                out, state.cfg.chunk_bytes, state.cfg.loss_rate, state.rng
            )
            state.total_chunks_dropped += dropped
        return out

    return fut.then(_finish)
