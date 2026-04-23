"""Isolated baselines for RQ6 main-experiment comparison.

This subpackage provides DDP comm hooks for the non-SemiRDMA transports
listed in [design-ddp-integration.md §2.2](../../docs/phase3/design-ddp-integration.md):
RC-Baseline, RC-Lossy, and (later) UD-Naive.  Each hook is a drop-in
replacement for ``semirdma_allreduce_hook`` so that the same training
driver (``experiments/stage_a/train_cifar10.py``) can sweep across
baselines without touching the SemiRDMA core transport in
``python/semirdma/{transport,hooks,config}.py`` or ``src/transport/``.

Rationale for isolation (per 2026-04-23 discussion):
  - Baselines are for RQ6 comparison only; they do not belong in the
    SemiRDMA critical path
  - Keeping them in a subpackage means we can delete the whole directory
    if the paper drops a baseline, without touching main transport code
  - The loss-injection model for non-SemiRDMA baselines is documented in
    docs/phase3/rq6-loss-injection-strategy.md §2

Exports:
  - rc_baseline_hook / RCBaselineState — reliable transport, no loss sim
  - rc_lossy_hook    / RCLossyState / RCLossyConfig — reliable transport
    with post-reduce Bernoulli per-chunk masking to simulate "bytes
    delivered but some chunks declared corrupted"
"""

from semirdma.baselines.rc_hook import RCBaselineState, rc_baseline_hook
from semirdma.baselines.rc_lossy_hook import (
    RCLossyConfig,
    RCLossyState,
    rc_lossy_hook,
)

__all__ = [
    "RCBaselineState",
    "RCLossyConfig",
    "RCLossyState",
    "rc_baseline_hook",
    "rc_lossy_hook",
]
