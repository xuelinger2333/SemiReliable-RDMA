"""RQ5 Stage A sweep analysis.

Reads the 9 cells of experiments/results/stage_a/sweep_2026-04-20/ and emits:
  - A1 max_rel_err (gloo vs semirdma, per seed)
  - A2 semirdma loss=0.01 convergence curve (mean/std across 3 seeds)
  - iter_time summary (fwd/bwd/opt/total ms, post-warmup median)
  - grad_l2 matching diagnostic
"""
from __future__ import annotations

import csv
from pathlib import Path
from statistics import mean, stdev

ROOT = Path("experiments/results/stage_a/sweep_2026-04-20")

SEEDS = [42, 123, 7]

CELL_DIRS = {
    ("gloo", 0.00, 42): "14-28-34_gloo_loss0.0_seed42",
    ("semirdma", 0.00, 42): "14-52-27_semirdma_loss0.0_seed42",
    ("semirdma", 0.01, 42): "15-16-29_semirdma_loss0.01_seed42",
    ("gloo", 0.00, 123): "17-23-51_gloo_loss0.0_seed123",
    ("semirdma", 0.00, 123): "17-49-25_semirdma_loss0.0_seed123",
    ("semirdma", 0.01, 123): "18-15-50_semirdma_loss0.01_seed123",
    ("gloo", 0.00, 7): "20-23-07_gloo_loss0.0_seed7",
    ("semirdma", 0.00, 7): "20-48-40_semirdma_loss0.0_seed7",
    ("semirdma", 0.01, 7): "21-15-01_semirdma_loss0.01_seed7",
}


def load_csv(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def load_loss(cell: tuple) -> list[float]:
    d = ROOT / CELL_DIRS[cell]
    rows = load_csv(d / "loss_per_step.csv")
    return [float(r["loss"]) for r in rows]


def load_grad(cell: tuple) -> list[float]:
    d = ROOT / CELL_DIRS[cell]
    rows = load_csv(d / "grad_norm.csv")
    return [float(r["grad_l2"]) for r in rows]


def load_iter(cell: tuple) -> list[dict]:
    d = ROOT / CELL_DIRS[cell]
    return load_csv(d / "iter_time.csv")


def a1_analysis() -> None:
    print("=" * 72)
    print("A1 · bit-for-bit equivalence (gloo vs semirdma, loss=0, 100 steps)")
    print("=" * 72)
    for seed in SEEDS:
        g = load_loss(("gloo", 0.00, seed))
        s = load_loss(("semirdma", 0.00, seed))
        assert len(g) == len(s) == 100, f"length mismatch seed={seed}: {len(g)} vs {len(s)}"
        rel_errs = [abs(a - b) / max(b, 1e-9) for a, b in zip(s, g)]
        abs_errs = [abs(a - b) for a, b in zip(s, g)]
        print(
            f"  seed={seed:3d}: max|Δloss|={max(abs_errs):.2e}  "
            f"max_rel_err={max(rel_errs) * 100:.4f}%  "
            f"final_loss gloo={g[-1]:.4f} vs semirdma={s[-1]:.4f}"
        )
    # also compare grad_l2
    print()
    for seed in SEEDS:
        g = load_grad(("gloo", 0.00, seed))
        s = load_grad(("semirdma", 0.00, seed))
        diffs = [abs(a - b) for a, b in zip(s, g)]
        print(f"  seed={seed:3d}: max|Δgrad_l2|={max(diffs):.2e}")


def a2_analysis() -> None:
    print()
    print("=" * 72)
    print("A2 · convergence under 1% chunk loss (semirdma loss=0.01, 500 steps)")
    print("=" * 72)
    per_seed = {s: load_loss(("semirdma", 0.01, s)) for s in SEEDS}
    for s, curve in per_seed.items():
        print(f"  seed={s:3d}: loss[0]={curve[0]:.4f}  loss[250]={curve[250]:.4f}  loss[499]={curve[-1]:.4f}")
    # mean / std at select milestones
    print()
    print("  milestone means (n=3 seeds):")
    for step in [0, 50, 100, 200, 300, 400, 499]:
        vals = [per_seed[s][step] for s in SEEDS]
        print(f"    step={step:3d}  mean={mean(vals):.4f}  std={stdev(vals):.4f}  "
              f"vals={[f'{v:.3f}' for v in vals]}")

    # Compare A2 step-100 against A1-gloo step-100 (same seed) as rough anchor
    print()
    print("  A2 vs Gloo(100-step reference) at step 99:")
    for s in SEEDS:
        a2 = per_seed[s][99]
        g100 = load_loss(("gloo", 0.00, s))[-1]
        rel = (a2 - g100) / g100 * 100
        print(f"    seed={s:3d}: a2={a2:.4f}  gloo_100={g100:.4f}  rel_diff={rel:+.2f}%")


def iter_time_summary() -> None:
    print()
    print("=" * 72)
    print("iter_time (median post-warmup, step>=10)")
    print("=" * 72)
    for key, dname in CELL_DIRS.items():
        rows = load_iter(key)
        post = [r for r in rows if int(r["step"]) >= 10]
        if not post:
            continue
        total = sorted(float(r["total_ms"]) for r in post)
        fwd = sorted(float(r["fwd_ms"]) for r in post)
        bwd = sorted(float(r["bwd_ms"]) for r in post)
        opt = sorted(float(r["opt_ms"]) for r in post)

        def med(xs):
            n = len(xs)
            return xs[n // 2]

        print(f"  {key[0]:<8} loss={key[1]:.2f} seed={key[2]:<3d}  "
              f"n={len(post):<3d}  total={med(total):8.1f}ms  "
              f"fwd={med(fwd):6.1f}  bwd={med(bwd):7.1f}  opt={med(opt):5.1f}")


if __name__ == "__main__":
    a1_analysis()
    a2_analysis()
    iter_time_summary()
