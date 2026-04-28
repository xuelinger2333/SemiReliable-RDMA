"""LayerAwareHookState — wraps a UC sub-state + RC sub-state + registry + calibrator.

The dispatcher selects between the two sub-states per bucket based on the
registered loss tolerance and the current epsilon EMA. Both sub-states are
brought up together at hook installation; we burn 4 TCP ports per training
process (P..P+3) and two QPs per direction (UC tx/rx + RC tx/rx).

This is opt-in: only the ``transport=semirdma_layer_aware`` Hydra branch
constructs a LayerAwareHookState. All existing transports (semirdma,
rc_rdma, rc_lossy, gloo) keep their current single-hook plumbing.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Optional

# How often to all_reduce ``epsilon_ema`` across ranks. Pre-2026-04-28 the
# dispatcher sync'd per bucket; on the amd247/amd245/amd264 cluster this
# turned out to be the dominant cause of LA-through-middlebox 50% delivery
# regression (DEBUG_LOG.md 2026-04-28: gloo TCP via ARP-spoofed
# experiment LAN routes through amd264's kernel ip_forward, adding
# 50-150 ms per bucket → both ranks unblock with sigma jitter that
# consumes most of the 200 ms `t_max` window).
#
# Both ranks see the same wire so each rank's local ``epsilon_ema``
# converges to nearly the same value naturally; explicit sync every
# 50 buckets keeps routing decisions aligned without paying gloo RTT
# per bucket. 50 ≈ 2.5× the calibrator's EMA window (alpha=0.05).
DEFAULT_EPS_SYNC_PERIOD = 50

import torch

from semirdma.baselines.rc_rdma_hook import RCRDMAHookState
from semirdma.config import TransportConfig
from semirdma.hooks import SemiRDMAHookState
from semirdma.layer_aware.calibrator import WireCalibrator
from semirdma.layer_aware.registry import LossToleranceRegistry

logger = logging.getLogger(__name__)


@dataclass
class LayerAwareHookState:
    """Holds both UC and RC sub-states plus registry + calibrator.

    Construction goes through ``for_rank_layer_aware`` which brings up
    BOTH SemiRDMA (UC) and RC-RDMA transports against the peer.
    """

    rank: int
    world_size: int
    cfg: TransportConfig
    semi_substate: SemiRDMAHookState
    rc_substate: RCRDMAHookState
    registry: LossToleranceRegistry
    calibrator: WireCalibrator

    # Diagnostics counters; updated by dispatcher per bucket.
    n_buckets: int = 0
    n_routed_rc: int = 0
    n_routed_semi: int = 0
    n_t_max_trips: int = 0

    # Amortized cross-rank ``eps_ema`` sync (PR-C debug 2026-04-28).
    # ``eps_sync_period`` = N → call gloo all_reduce on ``epsilon_ema``
    # once every N buckets and cache the result for the in-between
    # buckets.  ``cached_eps`` holds the last sync output;
    # ``last_eps_sync_at`` is the ``n_buckets`` value at the last sync,
    # or ``None`` before the first sync (forces a sync on bucket #1).
    eps_sync_period: int = DEFAULT_EPS_SYNC_PERIOD
    cached_eps: float = 0.0
    last_eps_sync_at: Optional[int] = None
    n_eps_syncs: int = 0   # diagnostic counter

    @classmethod
    def for_rank_layer_aware(
        cls,
        *,
        rank: int,
        world_size: int,
        peer_host: str,
        port: int,
        cfg: TransportConfig,
        model: torch.nn.Module,
        registry: LossToleranceRegistry,
    ) -> "LayerAwareHookState":
        """Bring up UC + RC sub-transports and bind the registry to the model.

        Port layout::

            port      (P + 0)   UC rank0→rank1 direction
            port + 1  (P + 1)   UC rank1→rank0 direction
            port + 2  (P + 2)   RC rank0→rank1 direction
            port + 3  (P + 3)   RC rank1→rank0 direction

        The caller (e.g. ``run_p1_matrix.sh``) must allocate at least 4
        distinct ports per cell when layer_aware is in use; the cell
        runner already advances port_base by 10 per cell, so this fits.
        """
        if not cfg.layer_aware:
            raise ValueError(
                "LayerAwareHookState.for_rank_layer_aware called with "
                "cfg.layer_aware=False; set the flag or use the regular hook"
            )
        if world_size != 2:
            raise NotImplementedError(
                "Layer-aware hook is 2-worker for now (mirrors existing hooks)"
            )

        # Bind the registry to the model — turns module-name → p map into
        # an id(param) → p lookup so per-bucket resolution is constant time.
        registry.bind(model)

        # The user's cfg has qp_type='uc' (its default). Build an RC variant
        # for the rc_substate. RC requires loss_rate=0.0 and qp_type='rc'.
        uc_cfg = cfg if cfg.qp_type == "uc" else dataclasses.replace(cfg, qp_type="uc")
        rc_cfg = dataclasses.replace(cfg, qp_type="rc", loss_rate=0.0)

        semi = SemiRDMAHookState.for_rank(
            rank=rank, world_size=world_size,
            peer_host=peer_host, port=port,
            cfg=uc_cfg,
        )
        rc = RCRDMAHookState.for_rank(
            rank=rank, world_size=world_size,
            peer_host=peer_host, port=port + 2,
            cfg=rc_cfg,
        )

        cal = WireCalibrator.from_config(cfg)

        logger.info(
            "LayerAwareHookState up: rank=%d, registry=%d entries, "
            "bootstrap_buckets=%d safety_margin=%.4f",
            rank, len(list(registry.names())),
            cfg.calibration_bootstrap_buckets, cfg.loss_safety_margin,
        )
        return cls(
            rank=rank,
            world_size=world_size,
            cfg=cfg,
            semi_substate=semi,
            rc_substate=rc,
            registry=registry,
            calibrator=cal,
        )


__all__ = ["LayerAwareHookState"]
