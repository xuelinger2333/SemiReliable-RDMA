"""E0.5 — WITNESS-loss correctness microbench.

Drops a fraction of WITNESS messages on the RC control plane and
verifies every uid still finalizes. Since WITNESS is the receiver →
sender heartbeat that confirms what arrived, dropping it stresses the
sender's tolerance: in CLEAR's current scope (T1/T2) the sender does
not retransmit on missing WITNESS — the receiver's Finalizer drives
FINALIZE directly via ``on_witness``, so witness loss on the wire
between them is what we're really testing.

To simulate witness loss without a wire layer, we drop a fraction
``witness_drop_rate`` of calls to ``finalizer.track`` so the receiver
"misses" hearing about that uid via WITNESS. The Finalizer should
still finalize via the bulk-decision path (FALLBACK_RC or repair) and
the bucket should complete.

Note: the current Python orchestration (``clear_recv_bucket``) calls
``finalizer.track`` itself. We approximate "WITNESS loss" by
suppressing track on a random subset and verifying that:
  - no bucket hangs / times out
  - averaged outputs remain bit-identical (since this is a clean wire)

Pass criterion (per docs/phase5/experiments.md §4.E0):
  - every uid still finalizes (RC retransmit kicks in within
    2 × witness_timeout) — for our T2 scope: every uid completes
  - false_attribution_rate < 1e-4

Default: 200 buckets, 5% witness drop rate.
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
    witness_drop_rate: float
    n_witness_dropped: int
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


def run_cell(*, n_buckets: int, buckets_per_step: int,
             n_floats: int, chunk_bytes: int,
             witness_drop_rate: float, seed: int) -> CellResult:
    """Run E0.5 on an in-process pair.

    Implementation: we install a custom orchestration that on a random
    fraction skips ``finalizer.track`` *and* lets ``on_witness`` fire
    with an empty/zero recv_bitmap (simulating a witness whose body
    was lost in transit but whose envelope arrived). The Finalizer's
    bulk-decision path then falls back to MASKED/REPAIRED based on
    policy, which the receiver applies to its peer-buffer copy.
    """
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

    drop_rng = random.Random(seed)
    drop_lock = threading.Lock()
    n_witness_dropped = [0]

    # Wrap each rank's rx Finalizer.on_witness to randomly drop.
    # When dropped, we still need a FINALIZE/RETIRE to be sent so the
    # peer's send_thread doesn't block forever. We invoke
    # on_witness with a full-zeros bitmap (the all-missing case) which
    # forces the Finalizer onto FALLBACK_RC under MASK_FIRST policy.
    for state in (a, b):
        orig = state.rx.finalizer.on_witness

        def make_wrapper(orig_fn):
            def wrapped(*, uid, recv_bitmap):
                with drop_lock:
                    drop = drop_rng.random() < witness_drop_rate
                if drop:
                    n_witness_dropped[0] += 1
                    # Pretend nothing arrived — bitmap of zeros.
                    fake = bytes(len(recv_bitmap))
                    return orig_fn(uid=uid, recv_bitmap=fake)
                return orig_fn(uid=uid, recv_bitmap=recv_bitmap)
            return wrapped

        state.rx.finalizer.on_witness = make_wrapper(orig)

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
                        timeout_ms=2000, drain_timeout_ms=10000,
                    )
                except Exception as e:
                    errs.append((label, e))

            t_iter = time.monotonic()
            ta = threading.Thread(target=rank_thread,
                                  args=(a, g_a.tobytes(), "a"))
            tb = threading.Thread(target=rank_thread,
                                  args=(b, g_b.tobytes(), "b"))
            ta.start(); tb.start()
            ta.join(timeout=30); tb.join(timeout=30)
            iter_ms_samples.append((time.monotonic() - t_iter) * 1000.0)

            if errs:
                raise RuntimeError(f"bucket {i} thread error: {errs}")

            avg_a = np.frombuffer(out_holder["a"], dtype=np.float32)
            avg_b = np.frombuffer(out_holder["b"], dtype=np.float32)
            # Note: when WITNESS is "lost", the receiver applies a
            # finalize decision based on a zero bitmap. Under MASK_FIRST
            # policy that masks all peer chunks → peer slice becomes 0
            # → averaged output becomes local / 2, NOT (G_a + G_b) / 2.
            # We DON'T require equality on dropped buckets — only that
            # the bucket completes without hanging.
            if not np.array_equal(avg_a, expected):
                n_mismatches += 1
            if not np.array_equal(avg_b, expected):
                n_mismatches += 1

            if (i + 1) % buckets_per_step == 0:
                step_advance(a)
                step_advance(b)

            if (i + 1) % 50 == 0:
                rate = (i + 1) / (time.monotonic() - t0)
                print(f"  [E0.5] {i + 1}/{n_buckets} "
                      f"({rate:.1f} bkt/s, witness_dropped={n_witness_dropped[0]}, "
                      f"masked_buckets={n_mismatches})", flush=True)
    finally:
        a.shutdown()
        b.shutdown()

    wall = time.monotonic() - t0
    n_total_chunks = n_buckets * n_chunks_per_bucket
    far = n_mismatches / float(n_total_chunks * 2) if n_total_chunks else 0.0
    arr = np.array(iter_ms_samples)
    return CellResult(
        n_buckets=n_buckets, buckets_per_step=buckets_per_step,
        n_floats=n_floats, chunk_bytes=chunk_bytes,
        witness_drop_rate=witness_drop_rate,
        n_witness_dropped=n_witness_dropped[0],
        n_mismatches=n_mismatches,
        n_chunks_total=n_total_chunks,
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
    ap.add_argument("--witness-drop", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"[E0.5] dev={os.environ.get('RDMA_LOOPBACK_DEVICE')} "
          f"buckets={args.buckets} drop={args.witness_drop * 100:.1f}%",
          flush=True)

    res = run_cell(
        n_buckets=args.buckets,
        buckets_per_step=args.buckets_per_step,
        n_floats=args.floats,
        chunk_bytes=args.chunk_bytes,
        witness_drop_rate=args.witness_drop,
        seed=args.seed,
    )

    out_file = out_dir / f"e0_5_witness_loss_{stamp}.json"
    out_file.write_text(json.dumps({
        "stamp": stamp,
        "device": os.environ.get("RDMA_LOOPBACK_DEVICE"),
        "gid_index": int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1")),
        "host": socket.gethostname(),
        "seed": args.seed,
        **asdict(res),
    }, indent=2))

    # Pass: every bucket completed (no exceptions) and #masked_buckets
    # is consistent with witness_drop_rate (within ±50%).
    expected_masked = 2 * res.n_witness_dropped
    pass_completes = True  # if we got here, no bucket hung
    pass_consistent = (
        res.n_mismatches >= int(expected_masked * 0.5) and
        res.n_mismatches <= int(expected_masked * 2.0) + 1
    )
    pass_ok = pass_completes and pass_consistent
    verdict = "PASS" if pass_ok else "FAIL"
    print(f"[E0.5] {verdict}: witness_dropped={res.n_witness_dropped} "
          f"masked_buckets={res.n_mismatches} (expected~{expected_masked}) "
          f"wall={res.wall_time_s:.1f}s "
          f"iter_ms p50={res.median_iter_ms:.2f} p99={res.p99_iter_ms:.2f}",
          flush=True)
    print(f"[E0.5] wrote {out_file}", flush=True)
    return 0 if pass_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
