"""RC-Lossy DDP comm hook.

Represents "reliable transport + simulated chunk loss" in the RQ6
5-baseline comparison.  We cannot induce real wire loss on CX-6 Lx (tc
netem is bypassed; see rq6-loss-injection-strategy.md §1), so loss is
simulated at the application layer:

  1. A reliable AllReduce runs first (gloo TCP backend, identical to
     RC-Baseline) — all bytes arrive correctly.
  2. We then mask a Bernoulli fraction ``loss_rate`` of
     ``chunk_bytes``-sized chunks in the averaged bucket to zeros — as if
     those chunks had been corrupted on the wire and the reliable layer
     chose not to retransmit (or RC reached its retry limit and gave up).

This error model matches SemiRDMA's own drop+ghost-mask output (the
reduced gradient ends up with the same fraction of zero'd chunks in
both cases), giving an apples-to-apples convergence comparison.  What
it does NOT model is the HW retry tail-latency that a real RC QP would
pay on a lossy wire — the paper's tail-latency discussion handles that
analytically (citing Mellanox retry-timeout configs) rather than via
this simulation.
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
        """Mirrors ``SemiRDMAHookState.for_rank`` so both baselines have the
        same construction contract in the training driver."""
        # Offset seed per rank so rank 0 and rank 1 don't drop the same
        # chunks when running the same bucket — prevents accidentally
        # correlated drop patterns that would under-estimate effective
        # loss rate after AllReduce averaging.
        per_rank_cfg = RCLossyConfig(
            chunk_bytes=cfg.chunk_bytes,
            loss_rate=cfg.loss_rate,
            loss_seed=cfg.loss_seed + rank * 1009,
        )
        return cls(per_rank_cfg)


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
