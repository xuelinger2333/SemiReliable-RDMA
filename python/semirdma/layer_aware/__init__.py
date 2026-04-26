"""Application-layer loss tolerance + counter-driven dispatcher.

Opt-in transport mode (TransportConfig.layer_aware=True): each model layer
registers its acceptable loss budget p_L via LossToleranceRegistry; per
DDP bucket the dispatcher resolves p_bucket = min(p_L for param in bucket),
then either routes to the SemiRDMA UC path (with ratio = 1 - p_bucket and
T_max derived from continuous wire calibration) or falls back to the
RC-RDMA path when p_bucket < epsilon_ema + loss_safety_margin.

Default p_L for unregistered parameters is 0.0 → forces RC routing. This
is deliberately conservative: applications opt-in to lossy training per
layer.
"""

from semirdma.layer_aware.calibrator import WireCalibrator
from semirdma.layer_aware.dispatcher import layer_aware_dispatcher_hook
from semirdma.layer_aware.registry import LossToleranceRegistry
from semirdma.layer_aware.state import LayerAwareHookState

__all__ = [
    "LayerAwareHookState",
    "LossToleranceRegistry",
    "WireCalibrator",
    "layer_aware_dispatcher_hook",
]
