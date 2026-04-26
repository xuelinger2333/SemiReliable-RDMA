"""Unit tests for LossToleranceRegistry.

Pure-Python tests — no RDMA / no torch.distributed. Use a small toy
``torch.nn.Module`` to exercise model binding and bucket resolution; the
``GradBucket`` is mocked since these are unit-level behaviors.
"""

from __future__ import annotations

import pytest
import torch
import torch.nn as nn

from semirdma.layer_aware.registry import LossToleranceRegistry


class _FakeBucket:
    """Minimal stand-in for ``torch.distributed.GradBucket`` for unit tests."""
    def __init__(self, params):
        self._params = list(params)

    def parameters(self):
        return self._params


# ---- registration / lookup ----

def test_default_p_is_zero_when_unregistered():
    reg = LossToleranceRegistry()
    assert reg.get("anything") == 0.0
    assert LossToleranceRegistry.DEFAULT_P_L == 0.0


def test_register_then_get_roundtrips():
    reg = LossToleranceRegistry()
    reg.register("conv1", 0.05)
    reg.register("layer1.0.bn1", 0.0)
    reg.register("fc", 0.01)
    assert reg.get("conv1") == 0.05
    assert reg.get("layer1.0.bn1") == 0.0
    assert reg.get("fc") == 0.01
    assert reg.get("unregistered") == 0.0


def test_register_rejects_out_of_range():
    reg = LossToleranceRegistry()
    with pytest.raises(ValueError):
        reg.register("conv1", -0.01)
    with pytest.raises(ValueError):
        reg.register("conv1", 1.0)
    with pytest.raises(ValueError):
        reg.register("conv1", 1.5)


def test_register_rejects_empty_name():
    reg = LossToleranceRegistry()
    with pytest.raises(ValueError):
        reg.register("", 0.05)


def test_update_bulk():
    reg = LossToleranceRegistry()
    reg.update({"a": 0.0, "b": 0.05, "c": 0.01})
    assert reg.get("a") == 0.0
    assert reg.get("b") == 0.05
    assert reg.get("c") == 0.01


# ---- model binding ----

def _make_toy_model():
    """A small Module with named children spanning conv / bn / fc."""
    return nn.Sequential(
        nn.Conv2d(3, 8, 3),     # name "0"  (Conv2d)
        nn.BatchNorm2d(8),      # name "1"  (BatchNorm2d)
        nn.Conv2d(8, 16, 3),    # name "2"
        nn.Flatten(),           # name "3"  (no params)
        nn.Linear(16, 4),       # name "4"
    )


def test_bind_assigns_p_per_named_module():
    reg = LossToleranceRegistry()
    reg.register("0", 0.05)   # first Conv2d
    reg.register("1", 0.0)    # BN
    reg.register("2", 0.05)   # second Conv2d
    reg.register("4", 0.01)   # Linear

    model = _make_toy_model()
    reg.bind(model)
    assert reg.is_bound()

    # Each parameter should resolve to its module's p
    for name, p in model.named_parameters():
        # name is like "0.weight" / "1.bias" / "4.weight" — module name
        # is the prefix before the last dot
        mod_name = name.rsplit(".", 1)[0]
        expected = reg.get(mod_name, default=0.0)
        param = model.get_parameter(name)
        assert reg.p_for_param(param) == pytest.approx(expected)


def test_bind_warns_on_unmatched_names(caplog):
    """Registering a name that doesn't match any module should warn, not crash."""
    reg = LossToleranceRegistry()
    reg.register("nonexistent_layer_xyz", 0.05)
    model = _make_toy_model()
    with caplog.at_level("WARNING", logger="semirdma.layer_aware.registry"):
        reg.bind(model)
    assert any("did not match" in r.message for r in caplog.records)


def test_p_for_param_before_bind_raises():
    reg = LossToleranceRegistry()
    p = nn.Parameter(torch.zeros(1))
    with pytest.raises(RuntimeError):
        reg.p_for_param(p)


# ---- resolve_for_bucket ----

def test_resolve_for_bucket_takes_min_across_layers():
    reg = LossToleranceRegistry()
    reg.register("0", 0.05)
    reg.register("1", 0.0)    # this should dominate
    reg.register("2", 0.05)
    reg.register("4", 0.01)

    model = _make_toy_model()
    reg.bind(model)

    # Bucket containing params from module 0 (Conv) and 1 (BN)
    params_mixed = list(model[0].parameters()) + list(model[1].parameters())
    bucket_mixed = _FakeBucket(params_mixed)
    assert reg.resolve_for_bucket(bucket_mixed) == 0.0  # min(0.05, 0.0)

    # Bucket containing only Conv params → 0.05
    bucket_conv = _FakeBucket(list(model[0].parameters()))
    assert reg.resolve_for_bucket(bucket_conv) == 0.05

    # Bucket containing only Linear params → 0.01
    bucket_fc = _FakeBucket(list(model[4].parameters()))
    assert reg.resolve_for_bucket(bucket_fc) == 0.01


def test_resolve_for_bucket_unregistered_param_pulls_min_to_zero():
    """An unregistered param has p=0 → forces bucket to 0 → routes to RC."""
    reg = LossToleranceRegistry()
    reg.register("0", 0.05)   # only register one module
    # modules 1, 2, 4 are NOT registered → default 0.0

    model = _make_toy_model()
    reg.bind(model)

    bucket = _FakeBucket(list(model[0].parameters()) + list(model[1].parameters()))
    assert reg.resolve_for_bucket(bucket) == 0.0


def test_resolve_for_bucket_empty_returns_default():
    reg = LossToleranceRegistry()
    model = _make_toy_model()
    reg.bind(model)
    bucket = _FakeBucket([])
    assert reg.resolve_for_bucket(bucket) == 0.0
