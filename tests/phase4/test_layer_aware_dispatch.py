"""Unit tests for layer_aware_dispatcher_hook routing logic.

Mocks both sub-hooks (rc_rdma_allreduce_hook + _run_semirdma_bucket) so
these tests run anywhere — no RDMA, no torch.distributed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import torch
import torch.nn as nn
from torch import futures

from semirdma.config import TransportConfig
from semirdma.layer_aware import dispatcher as dispatcher_mod
from semirdma.layer_aware.calibrator import WireCalibrator
from semirdma.layer_aware.registry import LossToleranceRegistry


class _FakeBucket:
    """Minimal stand-in: parameters() + buffer() only."""
    def __init__(self, params, n_bytes=4_000_000):
        self._params = list(params)
        # Build a flat tensor of the right size; element_size matters for
        # the dispatcher's n_bytes computation.
        elements = max(1, n_bytes // 4)
        self._buf = torch.zeros(elements, dtype=torch.float32)

    def parameters(self):
        return self._params

    def buffer(self):
        return self._buf


@dataclass
class _FakeState:
    cfg: TransportConfig
    registry: LossToleranceRegistry
    calibrator: WireCalibrator
    semi_substate: object   # opaque — mocked
    rc_substate: object     # opaque — mocked
    n_buckets: int = 0
    n_routed_rc: int = 0
    n_routed_semi: int = 0
    n_t_max_trips: int = 0


def _make_state(model, registry_dict, *, alpha=0.2, bootstrap=0):
    cfg = TransportConfig(
        layer_aware=True,
        loss_safety_margin=0.005,
        calibration_alpha=alpha,
        calibration_window=10,
        calibration_bootstrap_buckets=bootstrap,
        t_max_jitter_k=5,
        t_max_min_ms=5,
        ratio=0.95,
        timeout_ms=200,
        chunk_bytes=4096,
    )
    reg = LossToleranceRegistry()
    reg.update(registry_dict)
    reg.bind(model)
    cal = WireCalibrator.from_config(cfg)
    return _FakeState(
        cfg=cfg,
        registry=reg,
        calibrator=cal,
        semi_substate=object(),
        rc_substate=object(),
    )


def _make_toy_model():
    return nn.Sequential(
        nn.Conv2d(3, 8, 3),     # name "0"
        nn.BatchNorm2d(8),      # name "1"
        nn.Linear(72, 4),       # name "2"
    )


# ---- routing decisions ----

def test_unregistered_param_routes_to_rc(monkeypatch):
    """p_bucket=0 (default) < eps + margin (0 + 0.005) → RC."""
    model = _make_toy_model()
    # Don't register anything → all p_L = 0
    state = _make_state(model, {})

    rc_called = []
    def fake_rc_hook(rc_state, bucket):
        rc_called.append((rc_state, bucket))
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f
    semi_called = []
    def fake_semi(semi_state, bucket, *, ratio=None, timeout_ms=None):
        semi_called.append((semi_state, bucket, ratio, timeout_ms))
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f, {"ok": True, "completed": 100, "chunks_total": 100,
                   "latency_ms": 1.0, "timed_out": False}

    monkeypatch.setattr(dispatcher_mod, "rc_rdma_allreduce_hook", fake_rc_hook)
    monkeypatch.setattr(dispatcher_mod, "_run_semirdma_bucket", fake_semi)

    bucket = _FakeBucket(list(model.parameters()))
    fut = dispatcher_mod.layer_aware_dispatcher_hook(state, bucket)
    assert fut.done()
    assert len(rc_called) == 1
    assert len(semi_called) == 0
    assert state.n_routed_rc == 1
    assert state.n_routed_semi == 0


def test_high_p_bucket_routes_to_semi(monkeypatch):
    """p_bucket = 0.05 > eps (0) + margin (0.005) → SemiRDMA."""
    model = _make_toy_model()
    # Register all modules with p=0.05 so min(p_L) = 0.05
    state = _make_state(model, {"0": 0.05, "1": 0.05, "2": 0.05}, bootstrap=0)

    semi_called = []
    def fake_semi(semi_state, bucket, *, ratio=None, timeout_ms=None):
        semi_called.append((ratio, timeout_ms))
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f, {"ok": True, "completed": 950, "chunks_total": 1000,
                   "latency_ms": 5.0, "timed_out": False}
    rc_called = []
    def fake_rc_hook(rc_state, bucket):
        rc_called.append(True)
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f

    monkeypatch.setattr(dispatcher_mod, "rc_rdma_allreduce_hook", fake_rc_hook)
    monkeypatch.setattr(dispatcher_mod, "_run_semirdma_bucket", fake_semi)

    bucket = _FakeBucket(list(model.parameters()))
    dispatcher_mod.layer_aware_dispatcher_hook(state, bucket)
    assert len(semi_called) == 1
    assert len(rc_called) == 0
    # Post-bootstrap (=0), ratio_for_p(0.05) = 0.95
    ratio, t_max = semi_called[0]
    assert ratio == pytest.approx(0.95)
    assert t_max >= state.cfg.t_max_min_ms


def test_dispatcher_updates_calibrator_from_semi_stats(monkeypatch):
    """Each SemiRDMA call should fold its (n_completed, n_total, latency,
    n_bytes) into the calibrator EMAs."""
    model = _make_toy_model()
    state = _make_state(model, {"0": 0.05, "1": 0.05, "2": 0.05}, bootstrap=0)

    def fake_semi(semi_state, bucket, *, ratio=None, timeout_ms=None):
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f, {"ok": True, "completed": 950, "chunks_total": 1000,
                   "latency_ms": 10.0, "timed_out": False}
    monkeypatch.setattr(dispatcher_mod, "_run_semirdma_bucket", fake_semi)

    bucket = _FakeBucket(list(model.parameters()), n_bytes=4_000_000)
    for _ in range(15):
        dispatcher_mod.layer_aware_dispatcher_hook(state, bucket)

    # 5% loss every call → epsilon_ema should converge near 0.05
    assert 0.03 < state.calibrator.epsilon_ema < 0.07
    # Sigma is 0 (constant latency), bandwidth converged
    assert state.calibrator.sigma_jitter_ms == 0.0


def test_timeout_increments_t_max_trips(monkeypatch):
    model = _make_toy_model()
    state = _make_state(model, {"0": 0.05, "1": 0.05, "2": 0.05})

    def fake_semi(semi_state, bucket, *, ratio=None, timeout_ms=None):
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f, {"ok": False, "completed": 800, "chunks_total": 1000,
                   "latency_ms": 200.0, "timed_out": True}
    monkeypatch.setattr(dispatcher_mod, "_run_semirdma_bucket", fake_semi)

    bucket = _FakeBucket(list(model.parameters()))
    for _ in range(3):
        dispatcher_mod.layer_aware_dispatcher_hook(state, bucket)
    assert state.n_t_max_trips == 3


def test_safety_margin_kicks_in_when_eps_climbs(monkeypatch):
    """If eps_ema climbs above p_bucket - margin, the bucket should
    start routing to RC instead of SemiRDMA."""
    model = _make_toy_model()
    # p_L = 0.02 for every layer (tight budget)
    state = _make_state(model, {"0": 0.02, "1": 0.02, "2": 0.02})
    # Manually fast-forward calibrator to eps = 0.05 (above p_L + margin)
    for _ in range(20):
        state.calibrator.update(
            n_completed=950, n_total=1000, latency_ms=5.0, n_bytes=4_000_000,
        )
    assert state.calibrator.epsilon_ema > 0.02 + state.cfg.loss_safety_margin

    rc_called = []
    def fake_rc_hook(rc_state, bucket):
        rc_called.append(True)
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f
    semi_called = []
    def fake_semi(semi_state, bucket, *, ratio=None, timeout_ms=None):
        semi_called.append(True)
        f = futures.Future()
        f.set_result(bucket.buffer())
        return f, {"ok": True, "completed": 1000, "chunks_total": 1000,
                   "latency_ms": 5.0, "timed_out": False}
    monkeypatch.setattr(dispatcher_mod, "rc_rdma_allreduce_hook", fake_rc_hook)
    monkeypatch.setattr(dispatcher_mod, "_run_semirdma_bucket", fake_semi)

    bucket = _FakeBucket(list(model.parameters()))
    dispatcher_mod.layer_aware_dispatcher_hook(state, bucket)
    assert rc_called == [True]
    assert semi_called == []
