"""SemiRDMA transport configuration.

Stage A keeps the config small and explicit.  Everything that could vary per
experiment (chunk size, ratio, timeout, loss rate) is a field; everything that
is a device fact (``dev_name``) or a buffer-sizing decision goes through the
same object so there is one authoritative place to read.

The dataclass is ``frozen=True`` so a config handed to a long-running training
job cannot be mutated mid-run by accident — important for reproducibility.
Overrides happen by constructing a new instance (e.g. via Hydra's
``OmegaConf.structured(TransportConfig(**overrides))``).
"""

from __future__ import annotations

from dataclasses import dataclass


# 128 MiB — covers ResNet-18's ~47 MiB fp32 parameter set with room for
# slot partitioning.  The DDP hook splits this MR into n_slots disjoint
# windows (default 2) so back-to-back buckets in one step don't collide;
# 128 MiB / 2 = 64 MiB per slot, enough for the single-bucket first
# iteration where DDP hasn't rebucketed yet.  Stage B can shrink this for
# GPT-2 per-layer chunking.
_DEFAULT_BUFFER_BYTES = 128 * 1024 * 1024

# 16384 bytes — the chunk size that RQ1 identified as the SoftRoCE throughput
# saturation point (see docs/phase2/rq1-results-chunk-sweep.md).  Phase 3
# Stage A inherits this as the default.
_DEFAULT_CHUNK_BYTES = 16 * 1024

# 0.95 / 20 ms — the pair RQ4 picked as the best p99 / completeness trade-off
# in the 16-cell sweep (see docs/phase2/rq4-results-ratio-timeout.md).
_DEFAULT_RATIO = 0.95
_DEFAULT_TIMEOUT_MS = 20


@dataclass(frozen=True)
class TransportConfig:
    """User-visible transport settings for one SemiRDMA endpoint.

    All fields are plain data — no live RDMA handles, no sockets.  Safe to
    pickle, compare, and log.
    """

    # RDMA device name as seen by ibv_devinfo.  "rxe0" is the SoftRoCE device
    # on aliyun; real deployments will pass "mlx5_0" etc.
    dev_name: str = "rxe0"

    # GID table index to pin.  -1 means auto-discover (the C++ engine tries
    # {1, 0, 2, 3} and uses the first non-zero GID).  Use an explicit non-
    # negative value when the choice matters — e.g. gid_index=3 selects
    # RoCE v2 IPv4-mapped (::ffff:10.10.1.x) so kernel ARP is consulted for
    # dst MAC resolution, which is required when routing through an XDP
    # middlebox that relies on ARP-spoof to steer traffic.
    gid_index: int = -1

    # Registered MR size.  Must be >= max bucket size the trainer posts.
    buffer_bytes: int = _DEFAULT_BUFFER_BYTES

    # SQ / RQ depths.  sq_depth bounds how many outstanding Writes the sender
    # can have; rq_depth bounds how many Write-with-Imm the receiver can accept
    # before the RQ drains.  A 64 MiB / 16 KiB bucket is 4096 chunks, so the
    # RQ must pre-post at least that many or the tail chunks hit an empty RQ
    # and are silently dropped by UC.  4096 stays well below SoftRoCE's
    # max_qp_wr=16384 and Mellanox CX-5's 32768.
    sq_depth: int = 128
    rq_depth: int = 4096

    # Write granularity.  post_gradient splits the bucket into
    # ceil(bucket_bytes / chunk_bytes) Writes, one WR per chunk, imm = chunk_id.
    chunk_bytes: int = _DEFAULT_CHUNK_BYTES

    # Forward-progress boundary.  await_completion returns once this fraction
    # of chunks has a CQE, or timeout_ms elapses — whichever comes first.
    ratio: float = _DEFAULT_RATIO
    timeout_ms: int = _DEFAULT_TIMEOUT_MS

    # Synthetic per-chunk loss rate applied by the sender (Phase 2
    # methodology: skip posting the Write for a Bernoulli(p) fraction of
    # chunks).  Stage A uses this to drive RQ5-A2 without touching netem.
    # 0.0 means "post every chunk"; 1.0 would drop everything.
    loss_rate: float = 0.0

    # RNG seed for the loss-rate Bernoulli sampler.  Fixing this makes the
    # drop pattern reproducible across Gloo / SemiRDMA seed-matched runs.
    loss_seed: int = 0xC1FA  # "CIFAR" -- arbitrary

    def __post_init__(self) -> None:
        if self.buffer_bytes <= 0:
            raise ValueError(f"buffer_bytes must be > 0, got {self.buffer_bytes}")
        if self.chunk_bytes <= 0:
            raise ValueError(f"chunk_bytes must be > 0, got {self.chunk_bytes}")
        if self.sq_depth <= 0 or self.rq_depth <= 0:
            raise ValueError(
                f"sq_depth / rq_depth must be > 0, got {self.sq_depth}, {self.rq_depth}"
            )
        if not (0.0 < self.ratio <= 1.0):
            raise ValueError(f"ratio must lie in (0, 1], got {self.ratio}")
        if self.timeout_ms < 0:
            raise ValueError(f"timeout_ms must be >= 0, got {self.timeout_ms}")
        if not (0.0 <= self.loss_rate < 1.0):
            # loss_rate == 1.0 would mean "drop everything" and is useless for
            # training — reject it to catch config typos early.
            raise ValueError(f"loss_rate must lie in [0, 1), got {self.loss_rate}")


__all__ = ["TransportConfig"]
