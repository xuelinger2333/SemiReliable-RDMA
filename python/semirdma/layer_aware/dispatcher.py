"""Per-bucket dispatcher: routes between RC and SemiRDMA on layer p_L.

For each DDP bucket:

1. Resolve ``p_bucket = min(p_L for p in bucket.parameters())`` from the
   bound ``LossToleranceRegistry``.
2. If ``p_bucket < epsilon_ema + cfg.loss_safety_margin``: route via the
   reliable RC sub-hook. The bucket's loss budget is too tight to risk
   the wire's currently-observed loss rate.
3. Otherwise: route via the SemiRDMA UC sub-hook with overrides
     ratio = 1 - p_bucket   (counter-driven exit at the layer's budget)
     timeout_ms = T_max(L)  (derived from B_ema + K * sigma_jitter)
   Update the calibrator with ``(n_completed, n_total, latency_ms,
   n_bytes)`` from the returned stats so its EMAs track training reality.

During the bootstrap window (before ``calibrator.is_bootstrapped()``) the
calibrator returns the legacy flat ``cfg.ratio`` / ``cfg.timeout_ms`` so
behavior matches the existing transport while the EMAs warm up.
"""

from __future__ import annotations

import logging
import math

import torch
import torch.distributed as dist
from torch import futures

from semirdma.baselines.rc_rdma_hook import rc_rdma_allreduce_hook
from semirdma.hooks import _run_semirdma_bucket
from semirdma.layer_aware.state import LayerAwareHookState

logger = logging.getLogger(__name__)


def layer_aware_dispatcher_hook(
    state: LayerAwareHookState,
    bucket: dist.GradBucket,
) -> futures.Future[torch.Tensor]:
    """Per-bucket dispatcher for the layer-aware transport mode."""
    p_bucket = state.registry.resolve_for_bucket(bucket)
    eps = state.calibrator.epsilon_ema
    margin = state.cfg.loss_safety_margin
    state.n_buckets += 1

    # Safety check: route to RC when the bucket's budget is tighter than
    # the wire's observed loss rate plus a safety margin. Unregistered
    # params produce p_bucket=0 which always trips this check.
    if p_bucket < eps + margin:
        state.n_routed_rc += 1
        if state.n_buckets <= 5 or state.n_buckets % 100 == 0:
            logger.info(
                "dispatch[%d]: RC  p_bucket=%.4f eps_ema=%.4f margin=%.4f",
                state.n_buckets, p_bucket, eps, margin,
            )
        return rc_rdma_allreduce_hook(state.rc_substate, bucket)

    # SemiRDMA route — derive ratio and T_max from the calibrator.
    flat = bucket.buffer()
    n_bytes = flat.numel() * flat.element_size()
    n_chunks = max(1, int(math.ceil(n_bytes / state.cfg.chunk_bytes)))

    ratio = state.calibrator.ratio_for_p(p_bucket)
    t_max = state.calibrator.t_max_for_bucket(n_chunks, state.cfg.chunk_bytes)

    state.n_routed_semi += 1
    fut, stats = _run_semirdma_bucket(
        state.semi_substate, bucket, ratio=ratio, timeout_ms=t_max,
    )

    # Feed the calibrator from this bucket's stats. ``stats`` has the
    # wait_for_ratio fields (ok, latency_ms, completed, timed_out) plus
    # ``chunks_total`` added by transport.await_gradient.
    n_completed = stats.get("completed", 0)
    n_total = stats.get("chunks_total", n_chunks)
    latency_ms = stats.get("latency_ms", 0.0)
    state.calibrator.update(
        n_completed=n_completed,
        n_total=n_total,
        latency_ms=latency_ms,
        n_bytes=n_bytes,
    )
    if stats.get("timed_out", False):
        state.n_t_max_trips += 1

    if state.n_buckets <= 5 or state.n_buckets % 100 == 0:
        logger.info(
            "dispatch[%d]: SEMI p_bucket=%.4f ratio=%.4f t_max=%dms "
            "completed=%d/%d timed_out=%s eps_ema=%.4f sigma_ms=%.2f bw_mbps=%.1f",
            state.n_buckets, p_bucket, ratio, t_max,
            n_completed, n_total, stats.get("timed_out", False),
            state.calibrator.epsilon_ema,
            state.calibrator.sigma_jitter_ms,
            state.calibrator.bandwidth_bps / 1e6,
        )

    return fut


__all__ = ["layer_aware_dispatcher_hook"]
