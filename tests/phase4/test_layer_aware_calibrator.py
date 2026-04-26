"""Unit tests for WireCalibrator.

Pure-Python tests — feed synthetic (n_completed, n_total, latency_ms,
n_bytes) tuples and assert EMA convergence + bootstrap fallback.
"""

from __future__ import annotations

import math

from semirdma.config import TransportConfig
from semirdma.layer_aware.calibrator import WireCalibrator


def _make_calibrator(**overrides):
    """Construct a calibrator with TransportConfig defaults plus overrides."""
    base = dict(
        layer_aware=True,
        # Fast bootstrap so tests can verify both phases quickly.
        calibration_alpha=0.2,
        calibration_window=10,
        calibration_bootstrap_buckets=5,
        t_max_jitter_k=5,
        t_max_min_ms=5,
        ratio=0.95,
        timeout_ms=200,
    )
    base.update(overrides)
    cfg = TransportConfig(**base)
    return WireCalibrator.from_config(cfg), cfg


# ---- bootstrap ----

def test_bootstrap_returns_fallback_ratio_and_timeout():
    cal, cfg = _make_calibrator()
    assert not cal.is_bootstrapped()
    # During bootstrap, ratio_for_p ignores p and returns cfg.ratio
    assert cal.ratio_for_p(0.05) == cfg.ratio
    # And t_max returns cfg.timeout_ms regardless of n_chunks
    assert cal.t_max_for_bucket(n_chunks=1000, chunk_bytes=4096) == cfg.timeout_ms


def test_bootstrap_completes_after_n_samples():
    cal, cfg = _make_calibrator()
    for _ in range(cfg.calibration_bootstrap_buckets - 1):
        cal.update(n_completed=1000, n_total=1000, latency_ms=10.0, n_bytes=4_000_000)
        assert not cal.is_bootstrapped()
    cal.update(n_completed=1000, n_total=1000, latency_ms=10.0, n_bytes=4_000_000)
    assert cal.is_bootstrapped()


# ---- EMA convergence ----

def test_epsilon_ema_converges_to_observed_loss_rate():
    cal, _ = _make_calibrator(calibration_alpha=0.3)
    # 10% loss every bucket
    for _ in range(50):
        cal.update(n_completed=900, n_total=1000, latency_ms=10.0, n_bytes=4_000_000)
    assert abs(cal.epsilon_ema - 0.10) < 0.01


def test_bandwidth_ema_converges_to_input():
    cal, _ = _make_calibrator(calibration_alpha=0.3)
    # 10 ms for 10 MB → 1e9 bytes/s = 1 Gbps
    target_bps = 10_000_000 / (10e-3)
    for _ in range(50):
        cal.update(n_completed=1000, n_total=1000, latency_ms=10.0,
                   n_bytes=10_000_000)
    assert abs(cal.bandwidth_bps - target_bps) / target_bps < 0.05


def test_sigma_jitter_tracks_window_stdev():
    cal, _ = _make_calibrator()
    # Push 10 alternating 5ms / 15ms latencies
    for i in range(10):
        lat = 5.0 if i % 2 == 0 else 15.0
        cal.update(n_completed=1000, n_total=1000, latency_ms=lat,
                   n_bytes=1_000_000)
    # Stdev of [5,15,5,15,...] over 10 samples is ~5.27
    assert 4.5 < cal.sigma_jitter_ms < 6.0


# ---- ratio_for_p post-bootstrap ----

def test_ratio_for_p_uses_one_minus_p_after_bootstrap():
    cal, _ = _make_calibrator()
    for _ in range(20):
        cal.update(n_completed=1000, n_total=1000, latency_ms=10.0, n_bytes=4_000_000)
    assert cal.ratio_for_p(0.0) == 1.0
    assert cal.ratio_for_p(0.05) == 0.95
    assert cal.ratio_for_p(0.10) == 0.90


def test_ratio_for_p_clamps_at_extremes():
    cal, _ = _make_calibrator()
    for _ in range(20):
        cal.update(n_completed=1000, n_total=1000, latency_ms=10.0, n_bytes=4_000_000)
    # p == 1 would give ratio == 0; calibrator must clamp to small > 0
    r = cal.ratio_for_p(0.999)
    assert 0.0 < r <= 0.001 + 1e-9
    # p > 1 should never happen but clamp anyway
    r2 = cal.ratio_for_p(1.5)
    assert 0.0 < r2 <= 1.0


# ---- T_max derivation ----

def test_t_max_post_bootstrap_uses_physics():
    cal, cfg = _make_calibrator()
    # Drive bandwidth to a known value: 10 MB in 80 ms → 1.25e8 B/s ≈ 1 Gbps
    for _ in range(20):
        cal.update(n_completed=1000, n_total=1000, latency_ms=80.0,
                   n_bytes=10_000_000)
    # 1000-chunk bucket of 4096B ≈ 4 MB. 4 MB / 1.25e8 B/s ≈ 32 ms
    t_max = cal.t_max_for_bucket(n_chunks=1000, chunk_bytes=4096)
    # No jitter (all same latency) → T_max ≈ ceil(T_min)
    assert 30 <= t_max <= 35


def test_t_max_floors_at_t_max_min_ms():
    cal, cfg = _make_calibrator()
    # Big bandwidth, tiny bucket → T_min near zero
    for _ in range(20):
        cal.update(n_completed=1000, n_total=1000, latency_ms=1.0,
                   n_bytes=100_000_000)  # 100 GB/s effective
    t_max = cal.t_max_for_bucket(n_chunks=1, chunk_bytes=4096)
    assert t_max >= cfg.t_max_min_ms


def test_t_max_includes_jitter_term():
    """K * sigma_jitter must show up in T_max.

    Two separate calibrators with the same mean latency but different
    intra-window variance: the jittery one's T_max should be strictly
    greater because sigma_jitter_ms > 0 contributes K*sigma to T_max.
    """
    cal_flat, _ = _make_calibrator(t_max_jitter_k=10)
    cal_jit, _ = _make_calibrator(t_max_jitter_k=10)
    # Both: same mean latency (~10 ms) and same n_bytes per call → same B_ema.
    # cal_flat sees constant 10 ms; cal_jit alternates 5 / 15 ms within
    # the rolling window so sigma_jitter_ms > 0.
    for i in range(20):
        cal_flat.update(n_completed=1000, n_total=1000, latency_ms=10.0,
                        n_bytes=1_000_000)
        lat = 5.0 if i % 2 == 0 else 15.0
        cal_jit.update(n_completed=1000, n_total=1000, latency_ms=lat,
                       n_bytes=1_000_000)
    assert cal_flat.sigma_jitter_ms == 0.0
    assert cal_jit.sigma_jitter_ms > 1.0
    # Use a large bucket so T_min isn't dwarfed by t_max_min_ms floor.
    t_max_flat = cal_flat.t_max_for_bucket(n_chunks=10_000, chunk_bytes=4096)
    t_max_jit = cal_jit.t_max_for_bucket(n_chunks=10_000, chunk_bytes=4096)
    assert t_max_jit > t_max_flat


# ---- snapshot serializes cleanly ----

def test_snapshot_returns_finite_numbers():
    cal, _ = _make_calibrator()
    for _ in range(10):
        cal.update(n_completed=995, n_total=1000, latency_ms=8.0, n_bytes=4_000_000)
    snap = cal.snapshot()
    for k in ("epsilon_ema", "sigma_jitter_ms", "bandwidth_mbps"):
        assert math.isfinite(snap[k])
    assert isinstance(snap["bootstrapped"], bool)
    assert snap["n_samples"] == 10
