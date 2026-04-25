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

    # Per-WR pacing in C++ post_bucket_chunks fast path.  CX-5 + UC drops
    # ~30% of IB packets when WRs hit the NIC TX scheduler back-to-back at
    # ~1 µs (libmlx5 fast-path rate).  The pre-fix Python loop happened to
    # pace at ~5 µs/WR (interpreter + pybind cost), which empirically
    # matches the NIC's safe submission rate.  Set to 0 on SoftRoCE or
    # future fabrics where back-to-back submission is safe.
    per_wr_pace_us: int = 5

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
    # Only meaningful for qp_type="uc"; must be 0 for RC since HW ACK/retx
    # would paper over any simulated drop before the hook ever sees it.
    loss_rate: float = 0.0

    # RNG seed for the loss-rate Bernoulli sampler.  Fixing this makes the
    # drop pattern reproducible across Gloo / SemiRDMA seed-matched runs.
    loss_seed: int = 0xC1FA  # "CIFAR" -- arbitrary

    # QP reliability mode (added 2026-04-25).
    #   "uc" — Unreliable Connected, semi-reliable app layer (SemiRDMA core)
    #   "rc" — Reliable Connected, HW retransmit + ACK (RC-Baseline at the
    #          training layer; combine with XDP middlebox to show RC崩
    #          under real wire drop without self-building an RC engine).
    qp_type: str = "uc"

    # RC state-transition params (ignored when qp_type == "uc").
    # Defaults match Mellanox OFED conservative values; reviewer will check
    # these against the PRM.
    #   rc_timeout in log2(4.096 µs) units: 14 → 67 ms per retry
    #   rc_retry_cnt: total retransmits before IBV_WC_RETRY_EXC_ERR. 7 = max
    #   rc_rnr_retry: RNR (receiver-not-ready) NAK retries. 7 = infinite
    #   rc_min_rnr_timer: min RNR NAK timer in log2 units. 12 ≈ 0.64 ms
    #   rc_max_rd_atomic: outstanding Reads+atomic (we only Write, but 0 is
    #     rejected by some HW; 1 is the safe floor).
    rc_timeout: int = 14
    rc_retry_cnt: int = 7
    rc_rnr_retry: int = 7
    rc_min_rnr_timer: int = 12
    rc_max_rd_atomic: int = 1

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
        if self.qp_type not in ("uc", "rc"):
            raise ValueError(
                f"qp_type must be 'uc' or 'rc', got {self.qp_type!r}"
            )
        if self.qp_type == "rc" and self.loss_rate > 0.0:
            # App-level drop simulation on top of HW retx is a logic error:
            # either the retx papers over the skipped post (so effective
            # loss rate ≠ loss_rate), or the loss lands on the receiver's
            # RQ refill path (which would time out under HW-reliable QP
            # semantics).  Force the config writer to choose one.
            raise ValueError(
                "qp_type='rc' requires loss_rate=0.0 (RC relies on wire-"
                "level loss via the middlebox, not app-level simulation)"
            )
        if not (0 <= self.rc_timeout <= 31):
            raise ValueError(f"rc_timeout must lie in [0, 31], got {self.rc_timeout}")
        if not (0 <= self.rc_retry_cnt <= 7):
            raise ValueError(f"rc_retry_cnt must lie in [0, 7], got {self.rc_retry_cnt}")


__all__ = ["TransportConfig"]
