"""Continuous wire calibrator: ε, σ_jitter, B updated from training traffic.

After every ``await_gradient`` call the dispatcher feeds the per-bucket
``(n_completed, n_total, latency_ms, n_bytes)`` into the calibrator. The
calibrator maintains:

- ``epsilon_ema``: exponential moving average of observed loss rate
  ``1 - n_completed / n_total``. Used by the dispatcher's safety check
  ``p_L > epsilon + safety_margin``.
- ``sigma_jitter_ms``: standard deviation of ``latency_ms`` over a rolling
  window. Used as the jitter term in ``T_max = T_min + K * sigma``.
- ``bandwidth_bps``: EMA of ``n_bytes / (latency_ms * 1e-3)``. Used as
  the bandwidth term in ``T_min = n_chunks * chunk_bytes / B``.

A bootstrap window allows the dispatcher to fall back to the legacy flat
``cfg.ratio`` and ``cfg.timeout_ms`` until enough samples accumulate.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

logger = logging.getLogger(__name__)


# Floor for B_ema: avoids divide-by-near-zero in T_min computation when the
# very first sample reports a tiny effective bandwidth (e.g. 1-chunk bucket
# with a slow first call). 100 Mbit/s = 12.5 MB/s — well below any sane
# CX-5 25 GbE link, so it only kicks in for bootstrap pathologies.
_MIN_BANDWIDTH_BPS: float = 12.5e6


@dataclass
class WireCalibrator:
    """EMA-based calibrator fed by per-bucket training traffic.

    Construction takes ``cfg`` so the calibrator can read α, window size,
    bootstrap budget, and the T_max / safety-margin knobs in one place.
    """

    alpha: float
    window_size: int
    bootstrap_buckets: int
    t_max_jitter_k: int
    t_max_min_ms: int

    # Fallback values used during bootstrap (mirror cfg.ratio / cfg.timeout_ms).
    fallback_ratio: float
    fallback_timeout_ms: int

    # Live state.
    epsilon_ema: float = 0.0
    bandwidth_bps: float = _MIN_BANDWIDTH_BPS
    _latency_window: Deque[float] = field(default_factory=deque)
    n_samples: int = 0

    # ---- update path ----

    def update(
        self,
        n_completed: int,
        n_total: int,
        latency_ms: float,
        n_bytes: int,
    ) -> None:
        """Fold one bucket's stats into the EMAs.

        Args:
            n_completed: chunks marked has_cqe at await_gradient return
            n_total: total chunks the bucket attempted
            latency_ms: wall-clock latency_ms reported by wait_for_ratio
            n_bytes: byte size of the bucket
        """
        if n_total <= 0:
            return  # defensive: empty bucket, nothing to learn

        obs_loss = 1.0 - (n_completed / n_total)
        self.epsilon_ema = self.alpha * obs_loss + (1.0 - self.alpha) * self.epsilon_ema

        # Bandwidth: ignore non-positive latency to avoid div-by-zero on a
        # cached / zero-elapsed return.
        if latency_ms > 0.0 and n_bytes > 0:
            obs_bw = n_bytes / (latency_ms * 1e-3)
            self.bandwidth_bps = (
                self.alpha * obs_bw + (1.0 - self.alpha) * self.bandwidth_bps
            )

        # Latency rolling window for sigma.
        if math.isfinite(latency_ms) and latency_ms >= 0.0:
            self._latency_window.append(latency_ms)
            while len(self._latency_window) > self.window_size:
                self._latency_window.popleft()

        self.n_samples += 1

    # ---- read path ----

    def is_bootstrapped(self) -> bool:
        return self.n_samples >= self.bootstrap_buckets

    @property
    def sigma_jitter_ms(self) -> float:
        """Stdev of latency_ms over the rolling window; 0 if <2 samples."""
        if len(self._latency_window) < 2:
            return 0.0
        return statistics.stdev(self._latency_window)

    def t_max_for_bucket(self, n_chunks: int, chunk_bytes: int) -> int:
        """Derived T_max in milliseconds for a bucket of given chunks/size.

        Bootstrap returns ``fallback_timeout_ms``; post-bootstrap returns
        ``max(t_max_min_ms, T_min + K * sigma)`` rounded up to int ms.
        """
        if not self.is_bootstrapped():
            return self.fallback_timeout_ms

        bw = max(self.bandwidth_bps, _MIN_BANDWIDTH_BPS)
        t_min_ms = (n_chunks * chunk_bytes) / bw * 1e3
        t_max_ms = t_min_ms + self.t_max_jitter_k * self.sigma_jitter_ms
        return max(self.t_max_min_ms, int(math.ceil(t_max_ms)))

    def ratio_for_p(self, p_bucket: float) -> float:
        """Return the wait_for_ratio threshold for a given bucket budget.

        ``ratio = 1 - p_bucket`` once bootstrapped, else fall back to the
        flat config ratio so early steps don't see anomalous thresholds.
        Also clamps to (0, 1] for ratio_controller's input contract.
        """
        if not self.is_bootstrapped():
            return self.fallback_ratio
        r = 1.0 - p_bucket
        if r <= 0.0:
            r = 1e-3   # ratio_controller validates ratio > 0
        elif r > 1.0:
            r = 1.0
        return r

    def snapshot(self) -> dict:
        """Logger-friendly view of current state."""
        return {
            "epsilon_ema": round(self.epsilon_ema, 6),
            "sigma_jitter_ms": round(self.sigma_jitter_ms, 3),
            "bandwidth_mbps": round(self.bandwidth_bps / 1e6, 2),
            "n_samples": self.n_samples,
            "bootstrapped": self.is_bootstrapped(),
        }

    @classmethod
    def from_config(cls, cfg) -> "WireCalibrator":
        """Construct from a TransportConfig instance."""
        return cls(
            alpha=cfg.calibration_alpha,
            window_size=cfg.calibration_window,
            bootstrap_buckets=cfg.calibration_bootstrap_buckets,
            t_max_jitter_k=cfg.t_max_jitter_k,
            t_max_min_ms=cfg.t_max_min_ms,
            fallback_ratio=cfg.ratio,
            fallback_timeout_ms=cfg.timeout_ms,
        )


__all__ = ["WireCalibrator"]
