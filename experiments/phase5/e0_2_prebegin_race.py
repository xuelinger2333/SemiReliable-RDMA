"""E0.2 — Pre-BEGIN race correctness microbench.

Forces UC Write-with-Imm chunks to arrive at the receiver **before**
the BEGIN control message that announces the slot lease. The hook's
recv_thread must still finalize every uid correctly: UC completions
sit in the rx CQ until ``wait_for_ratio_clear`` is called, which only
happens after BEGIN installs the lease.

Mechanism: monkey-patch each rank's ``tx.cp.send_begin`` so it sleeps
``begin_delay_ms`` (uniformly sampled 1–10 ms) before the actual
RC SEND goes out. The sender's UC writes proceed immediately after
``send_begin`` returns, so they hit the wire while the peer's BEGIN
is still in the artificial delay.

Pass criterion (per docs/phase5/experiments.md §4.E0):
  - every uid finalizes correctly (no timeout, no crash)
  - PREBEGIN_PENDING drains: every bucket's averaged output equals
    ``(G_a + G_b) / 2`` bit-for-bit

Default: 200 buckets at chunk_bytes=4096, BEGIN delay 1–10 ms uniform.
"""
from __future__ import annotations

import argparse
import json
import os
import random
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
    delay_ms_min: float
    delay_ms_max: float
    n_mismatches: int
    n_chunks_total: int
    false_attribution_rate: float
    wall_time_s: float
    median_iter_ms: float
    p99_iter_ms: float


def _env() -> tuple[str, int]:
    dev = os.environ.get("RDMA_LOOPBACK_DEVICE")
    if not dev:
        raise SystemExit("RDMA_LOOPBACK_DEVICE unset")
    gid = int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1"))
    return dev, gid


def _wrap_send_begin_with_delay(transport, rng_lock, rng,
                                 delay_ms_min, delay_ms_max):
    """Replace transport.cp.send_begin with a wrapper that sleeps a random
    1-10 ms before delegating. Returns the original for restore."""
    orig = transport.cp.send_begin

    def delayed_send_begin(*args, **kwargs):
        with rng_lock:
            d = rng.uniform(delay_ms_min, delay_ms_max)
        time.sleep(d / 1000.0)
        return orig(*args, **kwargs)

    transport.cp.send_begin = delayed_send_begin
    return orig


def run_cell(*, n_buckets: int, buckets_per_step: int,
             n_floats: int, chunk_bytes: int,
             delay_ms_min: float, delay_ms_max: float,
             seed: int) -> CellResult:
    from semirdma.clear.hook import (
        ClearHookState, _run_clear_bucket, step_advance,
    )
    from semirdma.clear.transport import ClearTransportConfig

    dev, gid = _env()
    cfg = ClearTransportConfig(
        dev_name=dev, gid_index=gid,
        buffer_bytes=8 * 1024 * 1024,
        sq_depth=256, rq_depth=2048,
        chunk_bytes=chunk_bytes,
        cp_recv_slots=64, cp_send_slots=16,
    )
    a, b = ClearHookState.for_in_process_pair(cfg)

    delay_rng = random.Random(seed)
    delay_lock = threading.Lock()
    _wrap_send_begin_with_delay(a.tx, delay_lock, delay_rng,
                                delay_ms_min, delay_ms_max)
    _wrap_send_begin_with_delay(b.tx, delay_lock, delay_rng,
                                delay_ms_min, delay_ms_max)

    nbytes = n_floats * 4
    n_chunks_per_bucket = (nbytes + chunk_bytes - 1) // chunk_bytes
    grad_rng = np.random.default_rng(seed=seed ^ 0xC1EA9)

    n_mismatches = 0
    iter_ms_samples: list[float] = []
    t0 = time.monotonic()

    try:
        for i in range(n_buckets):
            g_a = grad_rng.standard_normal(n_floats).astype(np.float32)
            g_b = grad_rng.standard_normal(n_floats).astype(np.float32)
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

            if (i + 1) % buckets_per_step == 0:
                step_advance(a)
                step_advance(b)

            if (i + 1) % 50 == 0:
                rate = (i + 1) / (time.monotonic() - t0)
                print(f"  [E0.2] {i + 1}/{n_buckets} "
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
        delay_ms_min=delay_ms_min, delay_ms_max=delay_ms_max,
        n_mismatches=n_mismatches, n_chunks_total=n_total_chunks,
        false_attribution_rate=far, wall_time_s=wall,
        median_iter_ms=float(np.median(arr)) if arr.size else 0.0,
        p99_iter_ms=float(np.percentile(arr, 99)) if arr.size else 0.0,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--buckets", type=int, default=200)
    ap.add_argument("--buckets-per-step", type=int, default=20)
    ap.add_argument("--floats", type=int, default=1024)
    ap.add_argument("--chunk-bytes", type=int, default=4096)
    ap.add_argument("--delay-ms-min", type=float, default=1.0)
    ap.add_argument("--delay-ms-max", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"[E0.2] dev={os.environ.get('RDMA_LOOPBACK_DEVICE')} "
          f"buckets={args.buckets} delay={args.delay_ms_min}-{args.delay_ms_max}ms",
          flush=True)

    res = run_cell(
        n_buckets=args.buckets,
        buckets_per_step=args.buckets_per_step,
        n_floats=args.floats,
        chunk_bytes=args.chunk_bytes,
        delay_ms_min=args.delay_ms_min,
        delay_ms_max=args.delay_ms_max,
        seed=args.seed,
    )

    out_file = out_dir / f"e0_2_prebegin_race_{stamp}.json"
    out_file.write_text(json.dumps({
        "stamp": stamp,
        "device": os.environ.get("RDMA_LOOPBACK_DEVICE"),
        "gid_index": int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1")),
        "host": socket.gethostname(),
        "seed": args.seed,
        **asdict(res),
    }, indent=2))

    pass_far = res.false_attribution_rate < 1e-4 and res.n_mismatches == 0
    verdict = "PASS" if pass_far else "FAIL"
    print(f"[E0.2] {verdict}: false_attribution_rate={res.false_attribution_rate:.3e} "
          f"mismatches={res.n_mismatches}/{res.n_chunks_total} "
          f"wall={res.wall_time_s:.1f}s "
          f"iter_ms p50={res.median_iter_ms:.2f} p99={res.p99_iter_ms:.2f}",
          flush=True)
    print(f"[E0.2] wrote {out_file}", flush=True)
    return 0 if pass_far else 1


if __name__ == "__main__":
    raise SystemExit(main())
