"""Phase 5 W2.3 — BucketManifest + uid_hash + canonical_rank_pair tests."""

from __future__ import annotations

import pytest

from semirdma.clear.manifest import (
    BucketManifest,
    canonical_rank_pair,
    param_signature_from_shapes,
    uid_hash,
)


# ---------- BucketManifest --------------------------------------------------


def _sig(*triples):
    return param_signature_from_shapes(
        shapes=[t[2] for t in triples],
        dtypes=[t[1] for t in triples],
        sizes_bytes=[t[0] for t in triples],
    )


def test_observe_assigns_increasing_seq():
    m = BucketManifest()
    a = _sig((128, "float32", (32,)))
    b = _sig((256, "float32", (64,)))
    assert m.observe(a) == 0
    assert m.observe(b) == 1
    assert m.observe(a) == 0  # idempotent
    assert len(m) == 2


def test_freeze_rejects_unknown_signature():
    m = BucketManifest()
    a = _sig((128, "float32", (32,)))
    m.observe(a)
    m.freeze()
    assert m.frozen
    # Re-observing a known sig is fine.
    assert m.observe(a) == 0
    # Observing a new sig raises.
    new_sig = _sig((256, "float32", (64,)))
    with pytest.raises(ValueError):
        m.observe(new_sig)


def test_lookup_unknown_raises():
    m = BucketManifest()
    with pytest.raises(KeyError):
        m.lookup(_sig((1, "float32", (1,))))


def test_param_signature_is_order_independent():
    """DDP may reorder parameters across rebuild; signature must match."""
    a = _sig((128, "float32", (32,)),
             (256, "float32", (64,)))
    b = _sig((256, "float32", (64,)),
             (128, "float32", (32,)))
    assert a == b


def test_param_signature_distinguishes_dtype():
    a = _sig((128, "float32", (32,)))
    b = _sig((128, "float16", (32,)))
    assert a != b


def test_param_signature_distinguishes_size():
    a = _sig((128, "float32", (32,)))
    b = _sig((256, "float32", (32,)))
    assert a != b


def test_param_signature_validates_input_lengths():
    with pytest.raises(ValueError):
        param_signature_from_shapes(
            shapes=[(1,)], dtypes=["float32", "float16"], sizes_bytes=[1, 2]
        )


# ---------- uid_hash --------------------------------------------------------


def test_uid_hash_deterministic():
    a = uid_hash(rank_pair=0xABCD, step_seq=10, bucket_seq=5,
                phase_id=1, peer_edge=0xCAFE)
    b = uid_hash(rank_pair=0xABCD, step_seq=10, bucket_seq=5,
                phase_id=1, peer_edge=0xCAFE)
    assert a == b
    assert 0 <= a < (1 << 64)


def test_uid_hash_changes_with_each_field():
    base = uid_hash(rank_pair=1, step_seq=2, bucket_seq=3, phase_id=4, peer_edge=5)
    # Each varied field must produce a different uid (hash collisions are
    # vanishingly rare for a 64-bit truncated SHA-256).
    assert uid_hash(rank_pair=1, step_seq=99, bucket_seq=3, phase_id=4, peer_edge=5) != base
    assert uid_hash(rank_pair=2, step_seq=2, bucket_seq=3, phase_id=4, peer_edge=5) != base
    assert uid_hash(rank_pair=1, step_seq=2, bucket_seq=99, phase_id=4, peer_edge=5) != base
    assert uid_hash(rank_pair=1, step_seq=2, bucket_seq=3, phase_id=99, peer_edge=5) != base
    assert uid_hash(rank_pair=1, step_seq=2, bucket_seq=3, phase_id=4, peer_edge=99) != base


def test_uid_hash_handles_large_values():
    # Values near uint64 max must not crash via struct.pack overflow.
    big = (1 << 64) - 1
    h = uid_hash(rank_pair=big, step_seq=big, bucket_seq=big,
                 phase_id=0xFFFF, peer_edge=0xFFFF)
    assert 0 <= h < (1 << 64)


def test_uid_hash_distribution_on_bucket_seq():
    """5000 unique bucket_seq values produce 5000 unique uids in expectation."""
    seen = {uid_hash(rank_pair=0, step_seq=0, bucket_seq=i,
                    phase_id=0, peer_edge=0) for i in range(5000)}
    assert len(seen) == 5000  # collision in 64-bit space at this scale ≈ 0


# ---------- canonical_rank_pair ---------------------------------------------


def test_canonical_rank_pair_symmetric():
    assert canonical_rank_pair(3, 7) == canonical_rank_pair(7, 3)


def test_canonical_rank_pair_distinct_pairs_distinct_keys():
    assert canonical_rank_pair(3, 7) != canonical_rank_pair(3, 8)
    assert canonical_rank_pair(0, 0) != canonical_rank_pair(0, 1)
