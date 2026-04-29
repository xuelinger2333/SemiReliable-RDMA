"""Stable bucket → bucket_seq manifest + uid hashing.

PyTorch DDP rebuilds buckets after the first iteration: bucket index is
unstable across iter 0 → iter 1, and the underlying parameter set may also
shift if `bucket_cap_mb` is small enough that DDP is still computing
optimal layout. CLEAR's wire identity must outlive that rebuild — the
receiver has to recognize the same logical bucket across thousands of
steps even though the local Python `GradBucket.index()` may have changed.

Strategy:
  1. Run a short warm-up (1–3 iterations) where the hook records every
     unique parameter list it sees. After warm-up, freeze a manifest:
     param_signature → bucket_seq, assigned in deterministic insertion
     order. `bucket_seq` is then the wire identity for the rest of the run.
  2. Per-step, the hook computes a 64-bit `uid` from
     (rank_pair, step_seq, bucket_seq, phase_id, peer_edge). This is what
     gets passed to BEGIN/WITNESS/RETIRE.

`BucketManifest` is dependency-free (no torch import) so it can be unit-
tested with synthetic param signatures. The optional torch helper
`from_grad_bucket(bucket)` lives below the core class and is only called
when torch is available; it produces the same kind of param_signature
the tests use directly.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# A param_signature is a tuple of (size_in_bytes, dtype_id, shape) per
# parameter, sorted by some stable key. We don't use Python id() because
# tensors get reallocated; we use a content-derived signature.
ParamSignature = Tuple[Tuple[int, str, Tuple[int, ...]], ...]


@dataclass
class BucketManifest:
    """Maps stable param signatures to stable bucket_seq integers.

    Lifecycle:
        m = BucketManifest()
        # warm-up: feed each new bucket
        for step in warmup_steps:
            for bucket in ddp_buckets:
                m.observe(param_signature_of(bucket))
        m.freeze()
        # steady state
        bucket_seq = m.lookup(param_signature_of(bucket))
    """

    _by_sig: Dict[ParamSignature, int] = field(default_factory=dict)
    _frozen: bool = False

    def observe(self, signature: ParamSignature) -> int:
        """Record a signature; return its assigned bucket_seq.

        After ``freeze()``, observing an unknown signature raises ValueError.
        """
        if signature in self._by_sig:
            return self._by_sig[signature]
        if self._frozen:
            raise ValueError(
                "BucketManifest is frozen; unknown param_signature observed. "
                "Increase warm-up length or rebuild the manifest."
            )
        idx = len(self._by_sig)
        self._by_sig[signature] = idx
        return idx

    def freeze(self) -> None:
        self._frozen = True

    def lookup(self, signature: ParamSignature) -> int:
        if signature not in self._by_sig:
            raise KeyError(
                f"param_signature not in manifest "
                f"(frozen={self._frozen}, n_known={len(self._by_sig)})"
            )
        return self._by_sig[signature]

    def __len__(self) -> int:
        return len(self._by_sig)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def known_signatures(self) -> List[ParamSignature]:
        return list(self._by_sig.keys())


# ---------------------------------------------------------------------------
# uid construction
# ---------------------------------------------------------------------------

# 64-bit mix function chosen for cheap deterministic hashing of small
# integer tuples. SHA-256 is overkill for this domain (no adversarial
# inputs) but it's already in stdlib and we don't care about the speed
# difference at hook frequency. Mask down to 64 bits.
def uid_hash(*,
             rank_pair: int,
             step_seq: int,
             bucket_seq: int,
             phase_id: int = 0,
             peer_edge: int = 0) -> int:
    """Combine bucket+step+rank-pair identifiers into a uint64 uid.

    Two ranks computing this for the *same* logical transfer must arrive
    at the same uid: pass the canonicalized rank_pair (e.g. min(a,b) << 16
    | max(a,b)) on both sides.
    """
    payload = struct.pack(
        "<QQQHH",
        rank_pair & ((1 << 64) - 1),
        step_seq & ((1 << 64) - 1),
        bucket_seq & ((1 << 64) - 1),
        phase_id & 0xFFFF,
        peer_edge & 0xFFFF,
    )
    digest = hashlib.sha256(payload).digest()
    # Use first 8 bytes as little-endian uint64.
    return struct.unpack("<Q", digest[:8])[0]


def canonical_rank_pair(a: int, b: int) -> int:
    """Pack two ranks into a single uint32 with stable ordering.

    For directed peer edges, callers may want to keep direction explicit
    via peer_edge; rank_pair is the unordered pair identity.
    """
    lo, hi = (a, b) if a <= b else (b, a)
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


# ---------------------------------------------------------------------------
# Helpers for translating torch GradBucket → ParamSignature.
# Imported lazily so the test suite doesn't pull torch.
# ---------------------------------------------------------------------------

def param_signature_from_shapes(
    shapes: Sequence[Tuple[int, ...]],
    dtypes: Sequence[str],
    sizes_bytes: Sequence[int],
) -> ParamSignature:
    """Build a stable signature from raw per-parameter facts.

    The signature sorts parameters by (size_bytes, dtype, shape) so the
    same logical bucket gets the same signature even if DDP reorders the
    underlying parameter list across rebuild events.
    """
    if not (len(shapes) == len(dtypes) == len(sizes_bytes)):
        raise ValueError("shapes / dtypes / sizes_bytes must have same length")
    triples = sorted(zip(sizes_bytes, dtypes, shapes))
    return tuple((sz, dt, tuple(sh)) for sz, dt, sh in triples)


def from_grad_bucket(bucket) -> ParamSignature:
    """Extract a ParamSignature from a torch dist.GradBucket.

    Defers torch import to call time so non-torch test environments can
    still import this module. Returns the same shape of tuple as
    ``param_signature_from_shapes``.
    """
    # Late import — deliberate.
    try:
        params = bucket.parameters()  # torch.distributed >= 1.10 API
    except AttributeError:
        # Fallback for older torch that exposes per_parameter_tensors.
        params = bucket.get_per_parameter_tensors()
    shapes: List[Tuple[int, ...]] = []
    dtypes: List[str] = []
    sizes: List[int] = []
    for p in params:
        shapes.append(tuple(p.shape))
        dtypes.append(str(p.dtype))
        sizes.append(int(p.numel() * p.element_size()))
    return param_signature_from_shapes(shapes, dtypes, sizes)


__all__ = [
    "BucketManifest",
    "ParamSignature",
    "canonical_rank_pair",
    "from_grad_bucket",
    "param_signature_from_shapes",
    "uid_hash",
]
