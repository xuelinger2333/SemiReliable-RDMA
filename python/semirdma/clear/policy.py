"""CLEAR per-bucket policy registry.

Each `bucket_seq` (from BucketManifest) is associated with one of four
policies that drive the C++ Finalizer's decision:

    REPAIR_FIRST    — critical layers (BatchNorm/LayerNorm/embedding/output
                      projection). Missing chunks consume repair budget;
                      if budget runs out the bucket falls back to MASKED.
    MASK_FIRST      — mid-layer conv / MLP. Missing chunks zeroed.
    STALE_FILL      — optimizer-state-like buffers. Missing chunks reuse
                      the previous iteration's value.
    ESTIMATOR_SCALE — like MASKED at the wire layer; downstream training
                      step rescales aggregated tensor by n_chunks/recv_count
                      to maintain unbiasedness in expectation.

PolicyRegistry holds the bucket_seq → Policy map plus convenience
classifiers from a list of param "kinds". Keeps the C++ Finalizer free
of torch / nn-module knowledge.

The Policy enum values must match src/transport/clear/messages.h
``Policy`` enum so they can be passed across the pybind11 boundary
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Iterable, Optional


class Policy(IntEnum):
    """Mirrors clear::Policy in src/transport/clear/messages.h."""
    REPAIR_FIRST    = 1
    MASK_FIRST      = 2
    STALE_FILL      = 3
    ESTIMATOR_SCALE = 4


class FinalizeDecision(IntEnum):
    """Mirrors clear::FinalizeDecision."""
    DELIVERED   = 1
    REPAIRED    = 2
    MASKED      = 3
    STALE       = 4
    FALLBACK_RC = 5


# Default kind→policy mapping. The strings on the LHS are normalized
# lowercase fragments matched as substrings of the param name or the
# parent module's class name. Override at construction time.
_DEFAULT_KIND_TABLE: Dict[str, Policy] = {
    "batchnorm":    Policy.REPAIR_FIRST,
    "layernorm":    Policy.REPAIR_FIRST,
    "groupnorm":    Policy.REPAIR_FIRST,
    "embedding":    Policy.REPAIR_FIRST,
    "norm":         Policy.REPAIR_FIRST,   # catch-all for *Norm modules
    "lm_head":      Policy.REPAIR_FIRST,
    "classifier":   Policy.REPAIR_FIRST,
    # Conv / Linear default to mask-first.
    "conv":         Policy.MASK_FIRST,
    "linear":       Policy.MASK_FIRST,
    "fc":           Policy.MASK_FIRST,
    # Attention / projection: mid-tolerance.
    "attn":         Policy.MASK_FIRST,
    "mlp":          Policy.MASK_FIRST,
}


@dataclass
class PolicyRegistry:
    default_policy: Policy = Policy.MASK_FIRST
    kind_table: Dict[str, Policy] = field(
        default_factory=lambda: dict(_DEFAULT_KIND_TABLE))

    _by_bucket_seq: Dict[int, Policy] = field(default_factory=dict)

    def set(self, bucket_seq: int, policy: Policy) -> None:
        self._by_bucket_seq[int(bucket_seq)] = Policy(policy)

    def get(self, bucket_seq: int) -> Policy:
        return self._by_bucket_seq.get(int(bucket_seq), self.default_policy)

    def has(self, bucket_seq: int) -> bool:
        return int(bucket_seq) in self._by_bucket_seq

    def clear(self) -> None:
        self._by_bucket_seq.clear()

    def items(self):
        return self._by_bucket_seq.items()

    def classify_by_kinds(
        self,
        bucket_seq: int,
        param_kinds: Iterable[str],
        *,
        promote_strict: bool = True,
    ) -> Policy:
        """Pick the most-restrictive policy that applies to any kind in
        the bucket and store it.

        Strictness order (most → least): REPAIR_FIRST, MASK_FIRST,
        STALE_FILL, ESTIMATOR_SCALE. If no kind matches, falls back to
        ``default_policy``.
        """
        order = [Policy.REPAIR_FIRST, Policy.MASK_FIRST,
                 Policy.STALE_FILL, Policy.ESTIMATOR_SCALE]
        seen: list[Policy] = []
        for kind in param_kinds:
            kind_lc = kind.lower()
            for needle, pol in self.kind_table.items():
                if needle in kind_lc:
                    seen.append(pol)
                    break
        if not seen:
            chosen = self.default_policy
        elif promote_strict:
            chosen = min(seen, key=order.index)
        else:
            chosen = seen[0]
        self.set(bucket_seq, chosen)
        return chosen


__all__ = [
    "FinalizeDecision",
    "Policy",
    "PolicyRegistry",
]
