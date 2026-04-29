"""E0.3 — UC/RC reorder correctness microbench.

Adds uniform jitter on the RC control plane: every RC receive callback
(``on_begin``, ``on_finalize``, ``on_retire``) sleeps a random
``[0, 2 * jitter_ms]`` before doing its bookkeeping. This stresses the
protocol's tolerance to RC delivery delay variance and (because the bg
poll thread is sequential) cross-message delivery jitter.

Pass criterion (per docs/phase5/experiments.md §4.E0):
  - no FINALIZE collision (no double-set on a single uid)
  - semantic_mismatch_rate == 0 (every bucket's averaged output is
    bit-identical to ``(G_a + G_b) / 2``)

Default: 200 buckets at chunk_bytes=4096, jitter ±5 ms uniform.
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
    jitter_ms: float
    n_mismatches: int
    n_finalize_collisions: int
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


def _install_jittered_callbacks(state, *, jitter_ms: float, rng: random.Random,
                                 rng_lock: threading.Lock,
                                 collision_counter: list):
    """Re-install the 3 hook RC callbacks with uniform [0, 2 * jitter_ms]
    jitter prefixes. Mirrors hook.py:_wire_callbacks_and_start_poller's
    handler logic (cannot intercept the originals since they're closures).

    Also tallies FINALIZE collisions: if on_finalize_tx fires twice for
    the same uid before the foreground send_thread observes / drops the
    sync object, that is a duplicate FINALIZE delivery.
    """

    def _sleep():
        with rng_lock:
            d = rng.uniform(0.0, 2.0 * jitter_ms)
        time.sleep(d / 1000.0)

    def on_begin_rx(uid, slot, gen, *_args):
        _sleep()
        state.rx.receiver_leases.install(uid=uid, slot_id=slot, gen=gen)
        s = state._get_sync(uid)
        s.begin_slot = slot
        s.begin_gen = gen
        s.begin_event.set()

    def on_finalize_tx(uid, decision, mask_encoding, body):
        _sleep()
        s = state._get_sync(uid)
        if s.finalize_event.is_set():
            collision_counter[0] += 1
        s.finalize_decision = decision
        s.finalize_event.set()

    def on_retire_tx(uid, slot, gen):
        _sleep()
        try:
            state.tx.sender_leases.release(uid)
        except Exception:
            # Already released — harmless under reorder.
            pass

    state.rx.cp.on_begin(on_begin_rx)
    state.tx.cp.on_finalize(on_finalize_tx)
    state.tx.cp.on_retire(on_retire_tx)


def run_cell(*, n_buckets: int, buckets_per_step: int,
             n_floats: int, chunk_bytes: int,
             jitter_ms: float, seed: int) -> CellResult:
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

    jitter_rng = random.Random(seed)
    jitter_lock = threading.Lock()
    collision_counter = [0]
    _install_jittered_callbacks(a, jitter_ms=jitter_ms, rng=jitter_rng,
                                rng_lock=jitter_lock,
                                collision_counter=collision_counter)
    _install_jittered_callbacks(b, jitter_ms=jitter_ms, rng=jitter_rng,
                                rng_lock=jitter_lock,
                                collision_counter=collision_counter)

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
                print(f"  [E0.3] {i + 1}/{n_buckets} "
                      f"({rate:.1f} bkt/s, mismatches={n_mismatches}, "
                      f"collisions={collision_counter[0]})", flush=True)
    finally:
        a.shutdown()
        b.shutdown()

    wall = time.monotonic() - t0
    n_total_chunks = n_buckets * n_chunks_per_bucket
    far = n_mismatches / float(n_total_chunks) if n_total_chunks else 0.0
    arr = np.array(iter_ms_samples)
    return CellResult(
        n_buckets=n_buckets, buckets_per_step=buckets_per_step,
        n_floats=n_floats, chunk_bytes=chunk_bytes, jitter_ms=jitter_ms,
        n_mismatches=n_mismatches,
        n_finalize_collisions=collision_counter[0],
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
    ap.add_argument("--jitter-ms", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, required=True)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    print(f"[E0.3] dev={os.environ.get('RDMA_LOOPBACK_DEVICE')} "
          f"buckets={args.buckets} jitter=±{args.jitter_ms}ms",
          flush=True)

    res = run_cell(
        n_buckets=args.buckets,
        buckets_per_step=args.buckets_per_step,
        n_floats=args.floats,
        chunk_bytes=args.chunk_bytes,
        jitter_ms=args.jitter_ms,
        seed=args.seed,
    )

    out_file = out_dir / f"e0_3_ucrc_reorder_{stamp}.json"
    out_file.write_text(json.dumps({
        "stamp": stamp,
        "device": os.environ.get("RDMA_LOOPBACK_DEVICE"),
        "gid_index": int(os.environ.get("RDMA_LOOPBACK_GID_INDEX", "1")),
        "host": socket.gethostname(),
        "seed": args.seed,
        **asdict(res),
    }, indent=2))

    pass_ok = (res.false_attribution_rate < 1e-4 and
               res.n_mismatches == 0 and
               res.n_finalize_collisions == 0)
    verdict = "PASS" if pass_ok else "FAIL"
    print(f"[E0.3] {verdict}: false_attribution_rate={res.false_attribution_rate:.3e} "
          f"mismatches={res.n_mismatches}/{res.n_chunks_total} "
          f"finalize_collisions={res.n_finalize_collisions} "
          f"wall={res.wall_time_s:.1f}s "
          f"iter_ms p50={res.median_iter_ms:.2f} p99={res.p99_iter_ms:.2f}",
          flush=True)
    print(f"[E0.3] wrote {out_file}", flush=True)
    return 0 if pass_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
