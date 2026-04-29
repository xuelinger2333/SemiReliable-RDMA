"""E0.1 — Slot-wrap correctness microbench.

Drives N_BUCKETS bucket exchanges through a 2-rank in-process CLEAR
hook with a sender lease table sized at 256 slots × 16 gens. Every
bucket's averaged output must equal ``(G_a + G_b) / 2`` bit-for-bit;
ANY mismatch is a false-attribution event (slot or gen aliased).

Pass criterion (per docs/phase5/experiments.md §4.E0):
  - false_attribution_rate < 1e-4 over all uids
  - zero alias detected (any single mismatch fails the test)

Default: 5000 buckets at chunk_bytes=4096, ~50 buckets/step × 100 steps
so the per-step lease tables drain via on_step_boundary. This forces
slot recycling roughly every 256 buckets — the alias regime CLEAR
specifically defends against.

Outputs JSON to ``--out`` plus a one-line summary to stdout.

Usage (on CloudLab node):
  cd ~/SemiRDMA
  source .venv/bin/activate
  RDMA_LOOPBACK_DEVICE=mlx5_2 RDMA_LOOPBACK_GID_INDEX=1 \\
    python experiments/phase5/e0_1_slot_wrap.py \\
      --buckets 5000 --buckets-per-step 50 \\
      --out experiments/results/phase5/e0_1
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class CellResult:
    n_buckets: int
    buckets_per_step: int
    n_floats: int
    chunk_bytes: int
    n_mismatches: int
    n_chunks_total: int
    false_attribution_rate: float
    wall_time_s: float
    median_iter_ms: float
    p99_iter_ms: float


def _env_or_skip() -> tuple[str, int]:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        raise SystemExit("RDMA_LOOPBACK_DEVICE unset")
    gid = int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))
    return dev, gid


def run_cell(*, n_buckets: int, buckets_per_step: int,
             n_floats: int, chunk_bytes: int) -> CellResult:
    from semirdma.clear.hook import (
        ClearHookState, _run_clear_bucket, step_advance,
    )
    from semirdma.clear.transport import ClearTransportConfig

    dev, gid = _env_or_skip()
    cfg = ClearTransportConfig(
        dev_name=dev, gid_index=gid,
        buffer_bytes=8 * 1024 * 1024,
        sq_depth=256, rq_depth=2048,
        chunk_bytes=chunk_bytes,
        cp_recv_slots=64, cp_send_slots=16,
    )
    a, b = ClearHookState.for_in_process_pair(cfg)

    nbytes = n_floats * 4
    n_chunks_per_bucket = (nbytes + chunk_bytes - 1) // chunk_bytes
    rng = np.random.default_rng(seed=0xC1EA9)

    n_mismatches = 0
    iter_ms_samples: list[float] = []
    t0 = time.monotonic()

    try:
        for i in range(n_buckets):
            # Distinct gradient pattern per bucket so any cross-bucket
            # alias produces a numeric mismatch we can detect.
            g_a = rng.standard_normal(n_floats).astype(np.float32)
            g_b = rng.standard_normal(n_floats).astype(np.float32)
            expected = ((g_a + g_b) / 2).astype(np.float32)

            out_holder: dict = {}
            errs: list = []

            def rank_thread(state, bucket_bytes, label):
                try:
                    out_holder[label] = _run_clear_bucket(
                        state, bucket_bytes=bucket_bytes,
                        bucket_seq=i % buckets_per_step,
                        chunk_bytes=chunk_bytes, ratio=1.0,
                        timeout_ms=2000, drain_timeout_ms=5000,
                    )
                except Exception as e:
                    errs.append((label, e))

            t_iter = time.monotonic()
            ta = threading.Thread(target=rank_thread,
                                  args=(a, g_a.tobytes(), "a"))
            tb = threading.Thread(target=rank_thread,
                                  args=(b, g_b.tobytes(), "b"))
            ta.start(); tb.start()
            ta.join(timeout=15); tb.join(timeout=15)
            iter_ms_samples.append((time.monotonic() - t_iter) * 1000.0)

            if errs:
                raise RuntimeError(f"bucket {i} thread error: {errs}")

            avg_a = np.frombuffer(out_holder["a"], dtype=np.float32)
            avg_b = np.frombuffer(out_holder["b"], dtype=np.float32)
            if not np.array_equal(avg_a, expected):
                n_mismatches += 1
            if not np.array_equal(avg_b, expected):
                n_mismatches += 1
            if not np.array_equal(avg_a, avg_b):
                n_mismatches += 1

            # Step boundary every buckets_per_step buckets — drains
            # the per-step lease quarantine so slots can recycle.
            if (i + 1) % buckets_per_step == 0:
                step_advance(a)
                step_advance(b)

            if (i + 1) % 500 == 0:
                rate = (i + 1) / (time.monotonic() - t0)
                print(f"  [E0.1] {i + 1}/{n_buckets} "
                      f"({rate:.1f} bkt/s, mismatches={n_mismatches})",
                      flush=True)
    finally:
        a.shutdown()
        b.shutdown()

    wall = time.monotonic() - t0
    n_total_chunks = n_buckets * n_chunks_per_bucket
    far = n_mismatches / float(n_total_chunks) if n_total_chunks else 0.0
    arr = np.array(iter_ms_samples)
    return CellResult(
        n_buckets=n_buckets, buckets_per_step=buckets_per_step,
        n_floats=n_floats, chunk_bytes=chunk_bytes,
        n_mismatches=n_mismatches, n_chunks_total=n_total_chunks,
        false_attribution_rate=far, wall_time_s=wall,
        median_iter_ms=float(np.median(arr)) if arr.size else 0.0,
        p99_iter_ms=float(np.percentile(arr, 99)) if arr.size else 0.0,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--buckets", type=int, default=5000)
    ap.add_argument("--buckets-per-step", type=int, default=50)
    ap.add_argument("--floats", type=int, default=1024,
                    help="float32 elements per bucket")
    ap.add_argument("--chunk-bytes", type=int, default=4096)
    ap.add_argument("--out", type=str, required=True,
                    help="output directory (created if missing)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"[E0.1] dev={os.environ.get('RDMA_LOOPBACK_DEVICE')} "
          f"buckets={args.buckets} bps={args.buckets_per_step} "
          f"floats={args.floats} chunk={args.chunk_bytes}", flush=True)

    res = run_cell(
        n_buckets=args.buckets,
        buckets_per_step=args.buckets_per_step,
        n_floats=args.floats,
        chunk_bytes=args.chunk_bytes,
    )

    out_file = out_dir / f"e0_1_slot_wrap_{stamp}.json"
    out_file.write_text(json.dumps({
        "stamp": stamp,
        "device": os.environ.get("RDMA_LOOPBACK_DEVICE"),
        "gid_index": int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1")),
        "host": socket.gethostname(),
        **asdict(res),
    }, indent=2))

    pass_far = res.false_attribution_rate < 1e-4 and res.n_mismatches == 0
    verdict = "PASS" if pass_far else "FAIL"
    print(f"[E0.1] {verdict}: false_attribution_rate={res.false_attribution_rate:.3e} "
          f"mismatches={res.n_mismatches}/{res.n_chunks_total} "
          f"wall={res.wall_time_s:.1f}s "
          f"iter_ms p50={res.median_iter_ms:.2f} p99={res.p99_iter_ms:.2f}",
          flush=True)
    print(f"[E0.1] wrote {out_file}", flush=True)
    return 0 if pass_far else 1


if __name__ == "__main__":
    raise SystemExit(main())
