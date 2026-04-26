"""Re-aggregate a 3-seed × 3-transport × 3-drop P1 matrix using last-50 mean.

Replaces the runner's parse_cell() raw step-499 extraction with last-50
mean to get a stable convergence estimator (typical SGD smoothing window).

Usage:
  python matrix_aggregate.py /tmp/p0_3seed_ref_<TS>
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

SEEDS = [42, 123, 7]
DROPS = ["0", "0.01", "0.05"]
TRANSPORTS = ["rc_rdma", "rc_lossy", "semirdma"]
TRANSPORT_IDX = {t: i for i, t in enumerate(TRANSPORTS)}
DROP_OFFSET = {"0": 0, "0.01": 3, "0.05": 6}


def load_last_n(p: Path, n: int) -> list[float] | None:
    if not p.exists() or p.stat().st_size == 0:
        return None
    losses: list[float] = []
    with open(p) as f:
        next(f)  # header
        for line in f:
            _, loss = line.strip().split(",")
            losses.append(float(loss))
    if not losses:
        return None
    return losses[-n:]


def cell_dir(root: Path, seed: int, drop: str, transport: str) -> Path:
    idx = DROP_OFFSET[drop] + TRANSPORT_IDX[transport]
    return root / f"seed{seed}" / f"cell_{idx:02d}_drop{drop}_{transport}_t200"


def fmt_cell(val: float | None, crashed: bool) -> str:
    if crashed:
        return "    CRASH"
    if val is None:
        return "      N/A"
    return f"{val:>9.4f}"


def main() -> None:
    root = Path(sys.argv[1])

    print(f"matrix root: {root}")
    print(f"seeds: {SEEDS}  drops: {DROPS}  transports: {TRANSPORTS}")
    print(f"metric: mean of loss over last 50 steps")
    print()

    h = f"{'drop':>5} {'transport':>10}"
    for s in SEEDS:
        h += f" {'s=' + str(s):>10}"
    h += f"  {'mean3':>7} {'std3':>7}  rc"
    print(h)
    print("-" * len(h))

    for drop in DROPS:
        for transport in TRANSPORTS:
            cells = [cell_dir(root, s, drop, transport) for s in SEEDS]
            losses_per_seed = [load_last_n(c / "loss_per_step.csv", 50) for c in cells]
            rcs = []
            for c in cells:
                summary = c.parent / "MATRIX_SUMMARY.csv"
                if not summary.exists():
                    rcs.append("?")
                    continue
                with open(summary) as f:
                    for row in csv.DictReader(f):
                        if row["cell_dir"].endswith(c.name):
                            rcs.append(row["rc"])
                            break
                    else:
                        rcs.append("?")

            means = [statistics.mean(ls) if ls else None for ls in losses_per_seed]
            crashed = [ls is None for ls in losses_per_seed]

            row = f"{drop:>5} {transport:>10}"
            for m, cr in zip(means, crashed):
                row += f" {fmt_cell(m, cr):>10}"

            good = [m for m, cr in zip(means, crashed) if not cr and m is not None]
            if len(good) >= 2:
                mean3 = statistics.mean(good)
                std3 = statistics.stdev(good)
                row += f"  {mean3:>7.4f} {std3:>7.4f}"
            elif len(good) == 1:
                row += f"  {good[0]:>7.4f} {'(n=1)':>7}"
            else:
                row += f"  {'CRASH':>7} {'':>7}"
            row += f"  {','.join(rcs)}"
            print(row)


if __name__ == "__main__":
    main()
