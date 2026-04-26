"""Correlate per-cell total ghost-masked chunks with last-50-mean final loss.

If hypothesis A ("phantom ghost residual at drop=0 explains the 0.057 gap")
is correct, then within the 3 semirdma drop=0 cells the seed with the most
ghost-masked chunks should also have the highest final_loss.

If A is the ONLY explanation, then the ghost-zero-correlation should also
extend across drop_rate (drop=0 has fewer ghosts than drop=0.01, etc.).

Usage:
  python ghost_vs_loss.py /tmp/p0_3seed_ref_<TS>
"""
from __future__ import annotations

import re
import statistics
import sys
from pathlib import Path

SEEDS = [42, 123, 7]
DROPS = ["0", "0.01", "0.05"]
TRANSPORTS = ["rc_rdma", "rc_lossy", "semirdma"]
TRANSPORT_IDX = {t: i for i, t in enumerate(TRANSPORTS)}
DROP_OFFSET = {"0": 0, "0.01": 3, "0.05": 6}

DIAG_RE = re.compile(r"completed=(\d+)/(\d+)")


def cell_dir(root: Path, seed: int, drop: str, transport: str) -> Path:
    idx = DROP_OFFSET[drop] + TRANSPORT_IDX[transport]
    return root / f"seed{seed}" / f"cell_{idx:02d}_drop{drop}_{transport}_t200"


def extract_ghost(log_path: Path) -> dict | None:
    if not log_path.exists():
        return None
    n_buckets = 0
    perfect = 0
    total_missed = 0
    n_chunks_per_bucket = None
    with open(log_path) as f:
        for line in f:
            if "await_gradient DIAG:" not in line:
                continue
            m = DIAG_RE.search(line)
            if not m:
                continue
            c, t = int(m.group(1)), int(m.group(2))
            if n_chunks_per_bucket is None:
                n_chunks_per_bucket = t
            n_buckets += 1
            missed = t - c
            total_missed += missed
            if missed == 0:
                perfect += 1
    if n_buckets == 0:
        return None
    return {
        "n_buckets": n_buckets,
        "n_chunks_per_bucket": n_chunks_per_bucket,
        "perfect": perfect,
        "perfect_pct": 100 * perfect / n_buckets,
        "total_missed": total_missed,
        "mean_missed_per_bucket": total_missed / n_buckets,
        "effective_loss_pct": 100 * total_missed / (n_buckets * (n_chunks_per_bucket or 1)),
    }


def last50_mean(loss_csv: Path) -> float | None:
    if not loss_csv.exists() or loss_csv.stat().st_size == 0:
        return None
    losses: list[float] = []
    with open(loss_csv) as f:
        next(f)
        for line in f:
            _, v = line.strip().split(",")
            losses.append(float(v))
    if len(losses) < 50:
        return None
    return statistics.mean(losses[-50:])


def main() -> None:
    root = Path(sys.argv[1])
    print(f"matrix root: {root}")
    print()
    print(f"{'drop':>5} {'transport':>10} {'seed':>4} {'final_loss':>11} {'perfect%':>9} {'total_miss':>11} {'eff_loss%':>10}")
    print("-" * 72)

    rows = []
    for drop in DROPS:
        for transport in TRANSPORTS:
            for seed in SEEDS:
                d = cell_dir(root, seed, drop, transport)
                fl = last50_mean(d / "loss_per_step.csv")
                ghost = extract_ghost(d / "train_cifar10.log") if transport == "semirdma" else None
                row = {"drop": drop, "transport": transport, "seed": seed, "final_loss": fl, "ghost": ghost}
                rows.append(row)
                fl_s = f"{fl:>11.4f}" if fl is not None else "      CRASH"
                if ghost:
                    p = f"{ghost['perfect_pct']:>8.2f}%"
                    tm = f"{ghost['total_missed']:>11d}"
                    eff = f"{ghost['effective_loss_pct']:>9.4f}%"
                else:
                    p = tm = eff = "-"
                print(f"{drop:>5} {transport:>10} {seed:>4} {fl_s} {p:>9} {tm:>11} {eff:>10}")

    print()
    print("=== Hypothesis A test: within drop=0 semirdma cells, ghost ↔ final_loss ===")
    semi0 = [r for r in rows if r["drop"] == "0" and r["transport"] == "semirdma"]
    rc0 = [r for r in rows if r["drop"] == "0" and r["transport"] == "rc_rdma"]
    semi0.sort(key=lambda r: r["ghost"]["total_missed"] if r["ghost"] else 0)
    print(f"{'seed':>4} {'total_miss':>11} {'eff_loss%':>10} {'final_loss':>11}")
    for r in semi0:
        if r["ghost"] and r["final_loss"]:
            print(f"{r['seed']:>4} {r['ghost']['total_missed']:>11d} {r['ghost']['effective_loss_pct']:>9.4f}% {r['final_loss']:>11.4f}")

    if len(semi0) >= 3 and all(r["ghost"] and r["final_loss"] for r in semi0):
        # rank correlation
        miss = [r["ghost"]["total_missed"] for r in semi0]
        loss = [r["final_loss"] for r in semi0]
        # Pearson
        n = len(miss)
        mm, ml = statistics.mean(miss), statistics.mean(loss)
        num = sum((m - mm) * (l - ml) for m, l in zip(miss, loss))
        den = (sum((m - mm) ** 2 for m in miss) * sum((l - ml) ** 2 for l in loss)) ** 0.5
        if den > 0:
            r_pearson = num / den
            print(f"\n  Pearson r(total_missed, final_loss) over 3 seeds = {r_pearson:.3f}")
            print(f"  (|r|>0.8 = strong A, |r|<0.4 = A weak/absent)")

    print()
    print("=== RC-baseline (drop=0) cross-seed delta vs SemiRDMA ===")
    rc_losses = [r["final_loss"] for r in rc0 if r["final_loss"]]
    semi_losses = [r["final_loss"] for r in semi0 if r["final_loss"]]
    print(f"  rc_rdma:  mean={statistics.mean(rc_losses):.4f}  std={statistics.stdev(rc_losses):.4f}  n={len(rc_losses)}")
    print(f"  semirdma: mean={statistics.mean(semi_losses):.4f}  std={statistics.stdev(semi_losses):.4f}  n={len(semi_losses)}")
    print(f"  delta:    {statistics.mean(semi_losses) - statistics.mean(rc_losses):+.4f}")
    pooled_se = ((statistics.stdev(rc_losses)**2 / 3) + (statistics.stdev(semi_losses)**2 / 3)) ** 0.5
    delta = statistics.mean(semi_losses) - statistics.mean(rc_losses)
    if pooled_se > 0:
        print(f"  pooled SE (n=3 each): {pooled_se:.4f}")
        print(f"  effect size: {delta / pooled_se:.2f}σ  (|t|>2 ≈ p<0.05)")


if __name__ == "__main__":
    main()
