"""Phase 5 W2.3 — Policy + PolicyRegistry tests."""

from __future__ import annotations

from semirdma.clear.policy import FinalizeDecision, Policy, PolicyRegistry


def test_policy_enum_values_match_cpp():
    # These four values MUST equal clear::Policy in
    # src/transport/clear/messages.h or pybind11 marshalling will break.
    assert int(Policy.REPAIR_FIRST)    == 1
    assert int(Policy.MASK_FIRST)      == 2
    assert int(Policy.STALE_FILL)      == 3
    assert int(Policy.ESTIMATOR_SCALE) == 4


def test_finalize_decision_values_match_cpp():
    assert int(FinalizeDecision.DELIVERED)   == 1
    assert int(FinalizeDecision.REPAIRED)    == 2
    assert int(FinalizeDecision.MASKED)      == 3
    assert int(FinalizeDecision.STALE)       == 4
    assert int(FinalizeDecision.FALLBACK_RC) == 5


def test_default_policy_is_returned_for_unknown():
    r = PolicyRegistry()
    assert r.get(99) == Policy.MASK_FIRST  # default


def test_set_then_get():
    r = PolicyRegistry()
    r.set(0, Policy.REPAIR_FIRST)
    r.set(1, Policy.STALE_FILL)
    assert r.get(0) == Policy.REPAIR_FIRST
    assert r.get(1) == Policy.STALE_FILL
    assert r.has(0) and r.has(1) and not r.has(2)


def test_classify_by_kinds_promotes_to_strictest():
    r = PolicyRegistry()
    # BatchNorm wins over Conv → REPAIR_FIRST.
    out = r.classify_by_kinds(0, ["resnet.conv1", "resnet.bn1"])
    assert out == Policy.REPAIR_FIRST
    assert r.get(0) == Policy.REPAIR_FIRST


def test_classify_by_kinds_no_match_uses_default():
    r = PolicyRegistry(default_policy=Policy.STALE_FILL)
    out = r.classify_by_kinds(7, ["unknown_kind"])
    assert out == Policy.STALE_FILL


def test_classify_kinds_layernorm_routes_repair_first():
    r = PolicyRegistry()
    out = r.classify_by_kinds(0, ["transformer.encoder.layer_norm"])
    assert out == Policy.REPAIR_FIRST


def test_classify_kinds_conv_only_is_mask_first():
    r = PolicyRegistry()
    out = r.classify_by_kinds(0, ["resnet.conv1", "resnet.conv2"])
    assert out == Policy.MASK_FIRST


def test_classify_kinds_embedding_routes_repair_first():
    r = PolicyRegistry()
    out = r.classify_by_kinds(0, ["bert.embeddings.word_embeddings"])
    assert out == Policy.REPAIR_FIRST


def test_classify_kinds_promote_off_uses_first_match():
    r = PolicyRegistry()
    # With promote_strict=False, the first matched kind wins.
    out = r.classify_by_kinds(0, ["resnet.conv1", "resnet.bn1"],
                              promote_strict=False)
    assert out == Policy.MASK_FIRST  # conv matched first


def test_clear_drops_all_entries():
    r = PolicyRegistry()
    r.set(1, Policy.REPAIR_FIRST)
    r.set(2, Policy.MASK_FIRST)
    r.clear()
    assert not r.has(1) and not r.has(2)
