"""Per-layer loss-tolerance registry.

Application registers p_L per module name (matching ``model.named_modules``
keys), e.g. ``"layer1.0.conv1"``. The registry is then bound to a model so
each ``torch.nn.Parameter`` resolves to the p_L of its owning module.

Conventions:

- Default p_L for unregistered modules is 0.0 → routes the bucket to RC
  via the dispatcher's safety check. Applications must opt-in to lossy
  training per layer.
- p_L is clamped to [0.0, 1.0). p_L = 1.0 would mean "discard everything",
  which is useless for training and rejected.
- ``resolve_for_bucket(bucket)`` returns ``min(p_L for param in bucket)``.
  If ``bucket.parameters()`` is empty (defensive), returns 0.0.

The registry must be ``bind(model)``-ed before being passed to a hook,
which builds a ``id(param) -> p_L`` lookup so the per-bucket resolution
is a constant-time dict lookup per parameter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional

import torch

logger = logging.getLogger(__name__)


@dataclass
class LossToleranceRegistry:
    """Module-name → p_L map with a model-bound id(param) → p_L lookup.

    Typical use::

        reg = LossToleranceRegistry()
        reg.register("conv1", 0.05)
        reg.register("layer1.0.conv1", 0.05)
        reg.register("layer1.0.bn1", 0.0)        # explicit RC route
        # any module not registered also defaults to 0.0 (= RC)
        reg.bind(model)
        p_bucket = reg.resolve_for_bucket(ddp_bucket)

    To set a non-zero global default — e.g. "the whole model tolerates 5%
    unless I explicitly say otherwise" — pass ``default_p`` at construction
    time. Useful for PR-B uniform-budget validation runs and as a quick
    knob in YAML when per-layer registration is more friction than value.
    """

    default_p: float = 0.0
    _module_p: Dict[str, float] = field(default_factory=dict)
    _param_p: Optional[Dict[int, float]] = field(default=None, init=False)

    # Class-level default kept for backward compatibility with code that
    # reads ``LossToleranceRegistry.DEFAULT_P_L`` (e.g. existing tests).
    # Instance-level ``self.default_p`` is what bind/resolve actually use.
    DEFAULT_P_L: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.default_p < 1.0):
            raise ValueError(
                f"default_p must lie in [0, 1), got {self.default_p!r}"
            )

    # ---- registration ----

    def register(self, module_name: str, p: float) -> None:
        """Set p_L for a module by name (as in ``model.named_modules``)."""
        if not isinstance(module_name, str) or not module_name:
            raise ValueError(f"module_name must be a non-empty str, got {module_name!r}")
        if not (0.0 <= p < 1.0):
            raise ValueError(f"p must lie in [0, 1), got {p!r}")
        self._module_p[module_name] = float(p)
        self._param_p = None  # invalidate any prior bind

    def update(self, mapping: Mapping[str, float]) -> None:
        """Bulk-register from a {name: p} mapping."""
        for name, p in mapping.items():
            self.register(name, p)

    def get(self, module_name: str, default: Optional[float] = None) -> float:
        """Return p_L for a registered module name; default if missing.

        ``default=None`` (the typical caller) falls back to the
        instance-level ``self.default_p``.
        """
        if default is None:
            default = self.default_p
        return self._module_p.get(module_name, default)

    def names(self) -> Iterable[str]:
        return self._module_p.keys()

    # ---- model binding ----

    def bind(self, model: torch.nn.Module) -> "LossToleranceRegistry":
        """Build the per-parameter lookup using ``model.named_modules()``.

        For every (name, module) pair, every direct ``module.parameters
        (recurse=False)`` gets ``self.get(name)``. Direct-only iteration
        avoids attributing a parent's p_L to params owned by a child.
        """
        param_p: Dict[int, float] = {}
        seen_names: set[str] = set()
        for mod_name, module in model.named_modules():
            seen_names.add(mod_name)
            p = self.get(mod_name)
            for param in module.parameters(recurse=False):
                # If a parameter shows up under multiple names (rare:
                # parameter sharing), the most-conservative win.
                prev = param_p.get(id(param))
                param_p[id(param)] = p if prev is None else min(prev, p)

        # Surface registry entries that don't match any module name —
        # likely typos. Logged once at bind, not raised, so the user can
        # still recover by adding a module.
        unknown = [name for name in self._module_p if name not in seen_names]
        if unknown:
            logger.warning(
                "LossToleranceRegistry.bind: %d registered name(s) did not "
                "match any module on the bound model: %s",
                len(unknown), unknown[:8],
            )

        self._param_p = param_p
        logger.info(
            "LossToleranceRegistry bound: %d named modules, %d parameters mapped",
            len(seen_names), len(param_p),
        )
        return self

    def is_bound(self) -> bool:
        return self._param_p is not None

    def p_for_param(self, param: torch.nn.Parameter) -> float:
        """Lookup the bound p_L for a single parameter."""
        if self._param_p is None:
            raise RuntimeError(
                "LossToleranceRegistry.p_for_param called before bind(model)"
            )
        return self._param_p.get(id(param), self.default_p)

    # ---- bucket resolution ----

    def resolve_for_bucket(self, bucket) -> float:
        """Return ``min(p_L)`` over the bucket's parameters, or DEFAULT if empty.

        ``bucket`` is a ``torch.distributed.GradBucket``; we use
        ``bucket.parameters()``.
        """
        if self._param_p is None:
            raise RuntimeError(
                "LossToleranceRegistry.resolve_for_bucket called before bind(model)"
            )
        params = bucket.parameters()
        if not params:
            return self.default_p
        return min(self.p_for_param(p) for p in params)


__all__ = ["LossToleranceRegistry"]
