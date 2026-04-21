"""Stage B · single-node CX-6 microbenchmarks.

Measures the HW-local / software-path constants that we CAN get without
a second node:

  M1. engine.poll_cq(max_n, timeout_ms=0) on an empty CQ
      — sweep max_n ∈ {1, 4, 16, 64}; median + P99 ns / call.
      This sets the RatioController busy-loop budget on real NIC.

  M2. engine.post_recv_batch(n) at INIT state (legal per verbs spec)
      — sweep n ∈ {1, 10, 100, 1000, 10000}; total + per-WR cost.
      Sets the warm-up cost of large bucket allreduce.

  M3. UCQPEngine(dev, buffer_bytes, sq_depth, rq_depth) construction
      — sweep buffer_bytes ∈ {1, 4, 16, 64, 256} MiB × 10 trials each.
      Separates the MR-registration cost vs the fixed PD/CQ/QP cost
      by linear regression over size.

  M4. engine.outstanding_recv() — one-int getter; measures the pure
      Python → pybind11 → C++ trampoline latency, no verbs call.
      Amortized over 1M calls.

  M5. apply_ghost_mask(buf, chunkset) — pure-CPU Phase 2 RQ2 code path.
      Sweep buffer_bytes ∈ {1, 16, 256} MiB × loss_rate ∈ {0, 1, 10}%.
      Useful to re-check the CPU bound on d7525's EPYC (vs aliyun ECS).

Outputs (all under results_root/microbench_<timestamp>/):
  environment.json   — uname / cpu / fw_ver / driver / python / torch
  summary.json       — every (bench, cell, median_ns, p99_ns, n_samples)
  m1_poll_cq.csv     — raw per-call ns, long format (max_n, sample_idx, ns)
  m2_post_recv.csv   — (batch_n, trial, total_us, per_wr_ns)
  m3_construct.csv   — (buf_mib, trial, total_us)
  m4_trampoline.csv  — per-call ns (n_samples rows)
  m5_ghost_mask.csv  — (buf_mib, loss_pct, trial, mask_us, throughput_gibps)

Usage (on CloudLab node):
  cd ~/SemiRDMA
  source .venv/bin/activate
  PYTHONPATH=python python experiments/stage_b/microbench_cx6_local.py \
      --dev mlx5_0 --out experiments/results/stage_b/microbench

Re-runnable.  Safe to kill and restart (each run lives in its own
timestamped subdir).
"""
from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# Stage B microbench lives under experiments/stage_b/; the compiled
# pybind ext sits in python/semirdma/_semirdma_ext*.so.  Caller is
# expected to set PYTHONPATH=python, but we also add it defensively.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "python"))

from semirdma._semirdma_ext import (  # noqa: E402
    UCQPEngine,
    ChunkSet,
    apply_ghost_mask,
)

MIB = 1024 * 1024


# ------------------------------------------------------------------
# Environment capture (non-fatal if a command is missing)
# ------------------------------------------------------------------
def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
        return (out.stdout or out.stderr).strip()
    except Exception as e:
        return f"<error: {e}>"


def capture_environment(dev: str) -> dict:
    env = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": platform.node(),
        "uname": platform.uname()._asdict(),
        "python": sys.version.split()[0],
        "cpu_model": run(
            ["bash", "-c", "lscpu | awk -F: '/Model name/ {gsub(/^ +/,\"\",$2); print $2; exit}'"]
        ),
        "cpu_cores": os.cpu_count(),
        "mem_kb": run(["bash", "-c", "awk '/MemTotal/ {print $2}' /proc/meminfo"]),
        "ibv_devinfo": run(["bash", "-c", f"ibv_devinfo -d {dev} | grep -E 'fw_ver|vendor_part_id|state|phys_state|link_layer|active_mtu'"]),
        "driver": run(["bash", "-c", "lsmod | grep -E '^mlx5_(core|ib)' | awk '{print $1\": \"$2\" deps=\"$3}'"]),
    }
    # Try torch version but don't require it
    try:
        import torch
        env["torch"] = torch.__version__
    except Exception:
        env["torch"] = None
    return env


# ------------------------------------------------------------------
# Measurement helpers
# ------------------------------------------------------------------
def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = int(len(s) * p)
    return s[min(k, len(s) - 1)]


@dataclass
class Summary:
    bench: str
    cell: str              # cell parameters serialised as "k1=v1 k2=v2"
    n_samples: int
    median_ns: float
    p99_ns: float
    mean_ns: float
    min_ns: float


# ------------------------------------------------------------------
# M1 — poll_cq empty-CQ latency
# ------------------------------------------------------------------
def bench_poll_cq(engine: UCQPEngine, out_dir: Path, iters: int) -> list[Summary]:
    """Measure engine.poll_cq(max_n, 0) wall-time on an empty CQ.
    Sweeps max_n so we can tell per-call vs per-WC.
    """
    print(f"[M1] poll_cq empty-CQ × {iters} iters per cell …", flush=True)
    results: list[Summary] = []
    csv_path = out_dir / "m1_poll_cq.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["max_n", "sample_idx", "ns"])
        for max_n in [1, 4, 16, 64]:
            # 1k warmup
            for _ in range(1000):
                engine.poll_cq(max_n, 0)
            samples: list[float] = []
            # Time in tight loop; measure each call individually.
            t_perf = time.perf_counter_ns
            for i in range(iters):
                t0 = t_perf()
                engine.poll_cq(max_n, 0)
                ns = t_perf() - t0
                samples.append(ns)
                if i < 10000:   # cap CSV size
                    w.writerow([max_n, i, ns])
            results.append(Summary(
                bench="poll_cq_empty",
                cell=f"max_n={max_n}",
                n_samples=len(samples),
                median_ns=statistics.median(samples),
                p99_ns=percentile(samples, 0.99),
                mean_ns=statistics.mean(samples),
                min_ns=min(samples),
            ))
            print(f"   max_n={max_n:<3d}  median={statistics.median(samples):8.1f} ns  "
                  f"p99={percentile(samples, 0.99):8.1f} ns", flush=True)
    return results


# ------------------------------------------------------------------
# M2 — post_recv_batch throughput
# ------------------------------------------------------------------
def bench_post_recv(engine: UCQPEngine, out_dir: Path, trials: int) -> list[Summary]:
    """engine.post_recv_batch(n) at INIT — legal per verbs spec.
    Sweeps n ∈ {1, 10, 100, 1000, 10000}, trials per cell.
    Note: rq_depth caps n; we use engine with rq_depth=16384 to allow n=10000.
    Between trials we drain by... we can't drain without a peer, so each
    trial's wr_id uses a fresh base to avoid duplicate-wr_id EINVAL.
    """
    print(f"[M2] post_recv_batch × {trials} trials per cell …", flush=True)
    results: list[Summary] = []
    csv_path = out_dir / "m2_post_recv.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["batch_n", "trial", "total_us", "per_wr_ns"])
        for batch_n in [1, 10, 100, 1000]:
            per_wr_samples: list[float] = []
            for t in range(trials):
                base = 1_000_000 + t * batch_n + batch_n * 100 * [0, 1, 10, 100, 1000].index(batch_n)
                t_perf = time.perf_counter_ns
                t0 = t_perf()
                try:
                    engine.post_recv_batch(batch_n, base)
                except Exception as e:
                    # RQ exhaustion — stop this cell, we've got enough data
                    print(f"   batch_n={batch_n} exhausted at trial {t}: {e}", flush=True)
                    break
                ns = t_perf() - t0
                total_us = ns / 1000.0
                per_wr = ns / batch_n
                per_wr_samples.append(per_wr)
                w.writerow([batch_n, t, total_us, per_wr])
            if per_wr_samples:
                results.append(Summary(
                    bench="post_recv_batch",
                    cell=f"batch_n={batch_n}",
                    n_samples=len(per_wr_samples),
                    median_ns=statistics.median(per_wr_samples),
                    p99_ns=percentile(per_wr_samples, 0.99),
                    mean_ns=statistics.mean(per_wr_samples),
                    min_ns=min(per_wr_samples),
                ))
                print(f"   batch_n={batch_n:<5d} median_per_wr={statistics.median(per_wr_samples):8.1f} ns  "
                      f"n_trials={len(per_wr_samples)}", flush=True)
    return results


# ------------------------------------------------------------------
# M3 — UCQPEngine construction cost
# ------------------------------------------------------------------
def bench_construct(dev: str, out_dir: Path, trials: int) -> list[Summary]:
    """Time UCQPEngine(dev, buf_bytes, 16, 320) across buffer sizes.
    Each construct exercises: ibv_open_device + alloc_pd + reg_mr +
    create_cq + create_qp + modify_qp(INIT).  reg_mr is the only
    size-dependent step, so a linear fit size → time isolates reg_mr
    throughput.  Other steps are fixed cost.
    """
    print(f"[M3] UCQPEngine construct × {trials} trials per cell …", flush=True)
    results: list[Summary] = []
    csv_path = out_dir / "m3_construct.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["buf_mib", "trial", "total_us"])
        for mib in [1, 4, 16, 64, 256]:
            size_bytes = mib * MIB
            samples_us: list[float] = []
            for t in range(trials):
                gc.collect()
                t_perf = time.perf_counter_ns
                t0 = t_perf()
                e = UCQPEngine(dev, size_bytes, 16, 320)
                ns = t_perf() - t0
                del e
                gc.collect()
                total_us = ns / 1000.0
                samples_us.append(total_us)
                w.writerow([mib, t, total_us])
            results.append(Summary(
                bench="construct",
                cell=f"buf_mib={mib}",
                n_samples=len(samples_us),
                median_ns=statistics.median(samples_us) * 1000.0,
                p99_ns=percentile(samples_us, 0.99) * 1000.0,
                mean_ns=statistics.mean(samples_us) * 1000.0,
                min_ns=min(samples_us) * 1000.0,
            ))
            print(f"   buf={mib:>4d} MiB  median={statistics.median(samples_us):8.1f} µs  "
                  f"p99={percentile(samples_us, 0.99):8.1f} µs", flush=True)
    return results


# ------------------------------------------------------------------
# M4 — pybind trampoline overhead
# ------------------------------------------------------------------
def bench_trampoline(engine: UCQPEngine, out_dir: Path, iters: int) -> list[Summary]:
    """engine.outstanding_recv() — bare int getter.
    Measures pure Python → pybind → C++ function call cost.
    """
    print(f"[M4] pybind trampoline (outstanding_recv) × {iters} …", flush=True)
    results: list[Summary] = []
    csv_path = out_dir / "m4_trampoline.csv"

    # 1k warmup
    for _ in range(1000):
        engine.outstanding_recv()

    samples: list[float] = []
    t_perf = time.perf_counter_ns
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample_idx", "ns"])
        for i in range(iters):
            t0 = t_perf()
            engine.outstanding_recv()
            ns = t_perf() - t0
            samples.append(ns)
            if i < 10000:
                w.writerow([i, ns])
    results.append(Summary(
        bench="pybind_trampoline",
        cell="outstanding_recv",
        n_samples=len(samples),
        median_ns=statistics.median(samples),
        p99_ns=percentile(samples, 0.99),
        mean_ns=statistics.mean(samples),
        min_ns=min(samples),
    ))
    print(f"   median={statistics.median(samples):6.1f} ns  "
          f"p99={percentile(samples, 0.99):6.1f} ns  "
          f"min={min(samples):6.1f} ns", flush=True)
    return results


# ------------------------------------------------------------------
# M5 — apply_ghost_mask CPU throughput
# ------------------------------------------------------------------
def bench_ghost_mask(out_dir: Path, trials: int) -> list[Summary]:
    """apply_ghost_mask over different buffer sizes × loss rates.
    Simulates marking `(1 - loss_rate)` fraction of chunks as completed,
    then calling ghost_mask which zero-fills the rest.
    """
    print(f"[M5] apply_ghost_mask × {trials} trials per cell …", flush=True)
    results: list[Summary] = []
    csv_path = out_dir / "m5_ghost_mask.csv"
    chunk_bytes = 16384  # RQ1 saturation point
    rng = random.Random(42)

    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["buf_mib", "loss_pct", "trial", "mask_us", "throughput_gibps"])
        for mib in [1, 16, 256]:
            total_bytes = mib * MIB
            buf = bytearray(total_bytes)
            # fill with non-zero pattern so we can detect zero-fill
            for i in range(0, total_bytes, 4096):
                buf[i] = 0xAB

            for loss_pct in [0, 1, 10]:
                samples_us: list[float] = []
                for t in range(trials):
                    cs = ChunkSet(0, total_bytes, chunk_bytes)
                    # mark (1 - loss_pct/100) fraction as completed
                    n_chunks = cs.size()
                    for cid in range(n_chunks):
                        if rng.random() * 100.0 >= loss_pct:
                            cs.mark_completed(cid)
                    t_perf = time.perf_counter_ns
                    t0 = t_perf()
                    apply_ghost_mask(buf, cs)
                    ns = t_perf() - t0
                    total_us = ns / 1000.0
                    # throughput = bytes processed / time
                    throughput_gibps = (total_bytes / (1024**3)) / (ns / 1e9)
                    samples_us.append(total_us)
                    w.writerow([mib, loss_pct, t, total_us, throughput_gibps])
                if samples_us:
                    med_us = statistics.median(samples_us)
                    gibps_at_median = (total_bytes / (1024**3)) / (med_us / 1e6)
                    results.append(Summary(
                        bench="ghost_mask",
                        cell=f"buf_mib={mib} loss_pct={loss_pct}",
                        n_samples=len(samples_us),
                        median_ns=med_us * 1000.0,
                        p99_ns=percentile(samples_us, 0.99) * 1000.0,
                        mean_ns=statistics.mean(samples_us) * 1000.0,
                        min_ns=min(samples_us) * 1000.0,
                    ))
                    print(f"   buf={mib:>4d} MiB loss={loss_pct:>2d}%  median={med_us:8.1f} µs  "
                          f"{gibps_at_median:5.2f} GiB/s", flush=True)
    return results


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default="mlx5_0",
                    help="RDMA device name (default: mlx5_0)")
    ap.add_argument("--out", default="experiments/results/stage_b/microbench",
                    help="Output root directory (a timestamped subdir is created)")
    ap.add_argument("--iters-poll", type=int, default=200_000,
                    help="M1 iters per max_n cell (default: 200k)")
    ap.add_argument("--iters-tramp", type=int, default=1_000_000,
                    help="M4 trampoline iters (default: 1M)")
    ap.add_argument("--trials-recv", type=int, default=100,
                    help="M2 trials per batch_n cell (default: 100)")
    ap.add_argument("--trials-construct", type=int, default=10,
                    help="M3 trials per buffer size (default: 10)")
    ap.add_argument("--trials-mask", type=int, default=20,
                    help="M5 trials per (size, loss) cell (default: 20)")
    args = ap.parse_args()

    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = Path(args.out) / f"microbench_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"results will land in {out_dir}", flush=True)

    # Environment snapshot
    env = capture_environment(args.dev)
    (out_dir / "environment.json").write_text(json.dumps(env, indent=2))
    print(f"  captured env -> environment.json", flush=True)

    all_summaries: list[Summary] = []

    # Engine for M1, M2, M4  (rq_depth=16384 so we can batch-post a lot)
    eng = UCQPEngine(args.dev, 4 * MIB, 16, 16384)
    try:
        all_summaries += bench_poll_cq(eng, out_dir, args.iters_poll)
        all_summaries += bench_post_recv(eng, out_dir, args.trials_recv)
        all_summaries += bench_trampoline(eng, out_dir, args.iters_tramp)
    finally:
        del eng
        gc.collect()

    # M3 constructs its own engines
    all_summaries += bench_construct(args.dev, out_dir, args.trials_construct)

    # M5 is pure-CPU, no engine
    all_summaries += bench_ghost_mask(out_dir, args.trials_mask)

    # Dump summary
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(
        [asdict(s) for s in all_summaries], indent=2))
    print(f"\nsummary -> {summary_path}")

    # Terminal recap
    print("\n" + "=" * 72)
    print("RECAP")
    print("=" * 72)
    for s in all_summaries:
        if s.median_ns >= 1e6:
            fmt = f"{s.median_ns/1e6:8.2f} ms"
        elif s.median_ns >= 1e3:
            fmt = f"{s.median_ns/1e3:8.2f} µs"
        else:
            fmt = f"{s.median_ns:8.1f} ns"
        print(f"  {s.bench:<22s} {s.cell:<30s} median={fmt}  n={s.n_samples}")


if __name__ == "__main__":
    main()
