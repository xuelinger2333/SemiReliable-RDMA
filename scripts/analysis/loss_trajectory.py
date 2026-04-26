"""Inspect per-step loss trajectories from a 3-seed × 3-transport × 3-drop matrix.

Reads loss_per_step.csv files from a P1 matrix layout and reports:
  - per-seed trajectory at fixed checkpoints
  - last-50/100-step mean (smoothed alternative to raw step-499 value)
  - max-in-last-100 (catches mid-late divergence spikes)

Usage:
  python loss_trajectory.py /tmp/p0_3seed_ref_<TS> [transport=rc_lossy] [drop=0]
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

SEEDS = [42, 123, 7]


def load_loss(p: Path) -> list[float]:
    out: list[float] = []
    with open(p) as f:
        next(f)  # header
        for line in f:
            _, loss = line.strip().split(",")
            out.append(float(loss))
    return out


def cell_dir(root: Path, seed: int, drop: str, transport: str) -> Path:
    # P1 matrix lays cells out as cell_NN_drop<rate>_<transport>_t<to>/
    # idx scheme: 0..8 with outer=drop, middle=transport
    transport_to_idx = {"rc_rdma": 0, "rc_lossy": 1, "semirdma": 2}
    drop_to_offset = {"0": 0, "0.01": 3, "0.05": 6}
    idx = drop_to_offset[drop] + transport_to_idx[transport]
    return root / f"seed{seed}" / f"cell_{idx:02d}_drop{drop}_{transport}_t200"


def main() -> None:
    root = Path(sys.argv[1])
    transport = sys.argv[2] if len(sys.argv) > 2 else "rc_lossy"
    drop = sys.argv[3] if len(sys.argv) > 3 else "0"

    print("=" * 80)
    print(f"transport={transport}  drop={drop}  seeds={SEEDS}")
    print("=" * 80)

    trajs = {}
    for s in SEEDS:
        p = cell_dir(root, s, drop, transport) / "loss_per_step.csv"
        if not p.exists() or p.stat().st_size == 0:
            print(f"!! seed={s}: missing or empty {p.name}")
            continue
        trajs[s] = load_loss(p)

    if len(trajs) < 2:
        print("not enough data to compare")
        return

    print(f"\n{'step':>5}", end="")
    for s in SEEDS:
        print(f" {('s=' + str(s)):>10}", end="")
    print(f"  {'std3':>7}")

    for step in [0, 50, 100, 200, 300, 400, 450, 470, 480, 490, 495, 496, 497, 498, 499]:
        vals = [trajs[s][step] for s in SEEDS if s in trajs]
        std = statistics.stdev(vals) if len(vals) >= 2 else 0.0
        print(f"{step:>5}", end="")
        for s in SEEDS:
            print(f" {trajs[s][step]:>10.4f}", end="")
        print(f"  {std:>7.4f}")

    print()
    print("=== last 50/100 steps: mean and std per seed ===")
    print(f"{'seed':>5} {'step499':>10} {'last50_mean':>12} {'last50_std':>11} {'last100_mean':>13} {'last100_max':>12}")
    for s in SEEDS:
        if s not in trajs:
            continue
        t = trajs[s]
        l50 = t[-50:]
        l100 = t[-100:]
        print(
            f"{s:>5} {t[-1]:>10.4f} "
            f"{statistics.mean(l50):>12.4f} {statistics.stdev(l50):>11.4f} "
            f"{statistics.mean(l100):>13.4f} {max(l100):>12.4f}"
        )

    print()
    print("=== cross-seed std comparison: raw step499 vs smoothed last50 mean ===")
    raw = [trajs[s][-1] for s in SEEDS if s in trajs]
    smooth = [statistics.mean(trajs[s][-50:]) for s in SEEDS if s in trajs]
    print(f"raw step499:        mean={statistics.mean(raw):.4f}  std={statistics.stdev(raw):.4f}")
    print(f"smoothed last50:    mean={statistics.mean(smooth):.4f}  std={statistics.stdev(smooth):.4f}")
    print(f"smoothed/raw std:   {statistics.stdev(smooth) / statistics.stdev(raw):.2f}x")


if __name__ == "__main__":
    main()
