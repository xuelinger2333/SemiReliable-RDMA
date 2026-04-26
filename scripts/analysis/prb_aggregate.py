"""Aggregate the PR-B v3 head-to-head matrix.

Reads {ROOT}/seed{S}/MATRIX_SUMMARY.csv for each seed, recomputes
final_loss as the last-50-step mean (instead of the runner's raw
step-499 sample), and prints a 3-seed mean/std table per (drop, transport).

Usage:
  python prb_aggregate.py /tmp/p0_prb_v3_<TS>
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

SEEDS = [42, 123, 7]
DROPS = ["0", "0.01", "0.05"]
TRANSPORTS = ["semirdma", "semirdma_layer_aware"]
TR_IDX = {t: i for i, t in enumerate(TRANSPORTS)}
DR_OFF = {"0": 0, "0.01": 2, "0.05": 4}


def cell_dir(root: Path, seed: int, drop: str, transport: str) -> Path:
    idx = DR_OFF[drop] + TR_IDX[transport]
    return root / f"seed{seed}" / f"cell_{idx:02d}_drop{drop}_{transport}_t200"


def last_n_mean(p: Path, n: int = 50) -> float | None:
    if not p.exists() or p.stat().st_size == 0:
        return None
    losses: list[float] = []
    with open(p) as f:
        next(f)
        for line in f:
            _, v = line.strip().split(",")
            losses.append(float(v))
    if len(losses) < n:
        return None
    return statistics.mean(losses[-n:])


def cell_meta(d: Path) -> tuple[float | None, str]:
    s = d.parent / "MATRIX_SUMMARY.csv"
    if not s.exists():
        return None, "?"
    with open(s) as f:
        for row in csv.DictReader(f):
            if row["cell_dir"].endswith(d.name):
                try:
                    it = float(row["mean_iter_ms"])
                except (ValueError, TypeError):
                    it = None
                return it, row["rc"]
    return None, "?"


def main() -> None:
    root = Path(sys.argv[1])
    print(f"matrix root: {root}")
    print()

    h_drop, h_tr = "drop", "transport"
    h_s42, h_s123, h_s7 = "s=42", "s=123", "s=7"
    h_mean, h_std, h_it = "mean", "std", "iter_ms"
    print(f"{h_drop:>5} {h_tr:>22} | {h_s42:>9} {h_s123:>9} {h_s7:>9} | {h_mean:>7} {h_std:>7} {h_it:>8} | rc")
    print("-" * 105)

    for drop in DROPS:
        for tr in TRANSPORTS:
            cells = [cell_dir(root, s, drop, tr) for s in SEEDS]
            means = [last_n_mean(c / "loss_per_step.csv", 50) for c in cells]
            metas = [cell_meta(c) for c in cells]
            iters = [m[0] for m in metas]
            rcs = [m[1] for m in metas]

            good = [m for m in means if m is not None]
            cells_str = " ".join(
                f"{m:>9.4f}" if m is not None else "    CRASH" for m in means
            )

            if len(good) >= 2:
                mean3 = statistics.mean(good)
                std3 = statistics.stdev(good)
                ig = [i for i in iters if i is not None]
                it_mean = statistics.mean(ig) if ig else 0.0
                print(
                    f"{drop:>5} {tr:>22} | {cells_str} | "
                    f"{mean3:.4f}  {std3:.4f}   {it_mean:>6.1f} | {','.join(rcs)}"
                )
            elif len(good) == 1:
                ig = [i for i in iters if i is not None]
                it_mean = statistics.mean(ig) if ig else 0.0
                print(
                    f"{drop:>5} {tr:>22} | {cells_str} | "
                    f"{good[0]:.4f}   (n=1)    {it_mean:>6.1f} | {','.join(rcs)}"
                )
            else:
                print(f"{drop:>5} {tr:>22} | {cells_str} | ALL CRASHED rc={rcs}")


if __name__ == "__main__":
    main()
