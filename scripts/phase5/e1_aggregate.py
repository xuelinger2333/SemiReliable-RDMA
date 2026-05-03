"""Phase 5 E1 grid result aggregation.

Walks ``docs/phase5/results/raw/``, parses (transport, drop, seed) from
Hydra dir names, picks the latest run per cell, and emits a tidy CSV
plus a per-(transport, drop) summary table.

Usage:
    python scripts/phase5/e1_aggregate.py
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from collections import defaultdict
from statistics import mean, median, stdev
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "docs" / "phase5" / "results" / "raw"
OUT_DIR = ROOT / "docs" / "phase5" / "results"

# Hydra dir name: HH-MM-SS_<transport>_loss<d>_seed<s>
DIR_RE = re.compile(
    r"(?P<time>\d{2}-\d{2}-\d{2})_"
    r"(?P<transport>rc_baseline|semirdma|clear_t1)_"
    r"loss(?P<drop>[\d.]+)_seed(?P<seed>\d+)"
)

# Steps to use for steady-state iter_ms (skip warmup).
WARMUP_STEPS = 50
TOTAL_STEPS_REQUIRED = 200
# Final-loss window (mean over last K steps).
FINAL_LOSS_K = 20


def normalize_drop(s: str) -> float:
    return float(s)


def parse_date_from_path(p: Path) -> str:
    """Extract YYYY-MM-DD from a Hydra parent dir."""
    for part in p.parts:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            return part
    return "?"


def read_iter_csv(p: Path) -> list[dict]:
    rows = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({k: float(v) if k != "step" else int(v) for k, v in r.items()})
    return rows


def read_loss_csv(p: Path) -> list[float]:
    out = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            out.append(float(r["loss"]))
    return out


def collect_runs() -> list[dict]:
    """Return list of cell-runs with metadata + paths."""
    runs = []
    for d in RAW.rglob("*"):
        if not d.is_dir():
            continue
        m = DIR_RE.match(d.name)
        if not m:
            continue
        iter_csv = d / "iter_time.csv"
        loss_csv = d / "loss_per_step.csv"
        if not iter_csv.exists() or not loss_csv.exists():
            continue
        # Identify node from path.
        node = "amd247" if "amd247" in str(d) else "amd245"
        date = parse_date_from_path(d)
        runs.append({
            "node":      node,
            "date":      date,
            "time":      m.group("time"),
            "transport": m.group("transport"),
            "drop":      normalize_drop(m.group("drop")),
            "seed":      int(m.group("seed")),
            "dir":       d,
            "iter_csv":  iter_csv,
            "loss_csv":  loss_csv,
            "ts":        f"{date} {m.group('time')}",
        })
    return runs


def select_latest_per_cell(runs: list[dict]) -> dict:
    """Return dict keyed by (node, transport, drop, seed) → latest run.

    For 'semirdma' transport, each (drop, seed) appears on BOTH nodes
    (one as phase4_flat, one as phase4_prc). We keep both — keying by
    node distinguishes them.
    """
    out: dict = {}
    for r in runs:
        # Filter: only complete 200-step runs.
        rows = read_iter_csv(r["iter_csv"])
        if len(rows) < TOTAL_STEPS_REQUIRED:
            continue
        key = (r["node"], r["transport"], r["drop"], r["seed"])
        if key not in out or r["ts"] > out[key]["ts"]:
            out[key] = r
    return out


def summarize_run(r: dict) -> dict:
    rows = read_iter_csv(r["iter_csv"])[:TOTAL_STEPS_REQUIRED]
    iter_ms = [row["total_ms"] for row in rows[WARMUP_STEPS:]]
    losses = read_loss_csv(r["loss_csv"])[:TOTAL_STEPS_REQUIRED]
    final_loss = mean(losses[-FINAL_LOSS_K:])
    return {
        "node":        r["node"],
        "transport":   r["transport"],
        "drop":        r["drop"],
        "seed":        r["seed"],
        "iter_ms_med": median(iter_ms),
        "iter_ms_p99": sorted(iter_ms)[int(len(iter_ms) * 0.99)],
        "iter_ms_mean": mean(iter_ms),
        "final_loss":  final_loss,
        "ts":          r["ts"],
    }


def group_label(transport: str, drop: float) -> tuple:
    """For aggregation, treat 'semirdma' as 'phase4' (both phase4_flat
    and phase4_prc cells used transport=semirdma with bucket_cap_mb=512)."""
    label = {"semirdma": "phase4"}.get(transport, transport)
    return (label, drop)


def aggregate(summaries: list[dict]) -> list[dict]:
    """Group by (transport_label, drop), aggregate across nodes/seeds."""
    groups = defaultdict(list)
    for s in summaries:
        key = group_label(s["transport"], s["drop"])
        groups[key].append(s)

    out = []
    for (label, drop), items in sorted(groups.items()):
        iter_meds = [it["iter_ms_med"] for it in items]
        finals = [it["final_loss"] for it in items]
        out.append({
            "transport":     label,
            "drop":          drop,
            "n":             len(items),
            "iter_ms_med":   mean(iter_meds),
            "iter_ms_std":   stdev(iter_meds) if len(iter_meds) > 1 else 0.0,
            "iter_ms_min":   min(iter_meds),
            "iter_ms_max":   max(iter_meds),
            "final_loss":    mean(finals),
            "final_loss_std": stdev(finals) if len(finals) > 1 else 0.0,
            "samples":       items,
        })
    return out


def write_tidy(summaries: list[dict], path: Path) -> None:
    keys = ["node", "transport", "drop", "seed",
            "iter_ms_med", "iter_ms_p99", "iter_ms_mean", "final_loss", "ts"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for s in summaries:
            w.writerow({k: s[k] for k in keys})


def write_summary(agg: list[dict], path: Path) -> None:
    keys = ["transport", "drop", "n", "iter_ms_med", "iter_ms_std",
            "iter_ms_min", "iter_ms_max", "final_loss", "final_loss_std"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for s in agg:
            w.writerow({k: s[k] for k in keys})


def render_pass_criteria(agg: list[dict]) -> str:
    """Compute pass criteria per E1 plan: clear_t1 within +5% iter_ms of
    phase4, final_loss within 1 sigma."""
    by = {(g["transport"], g["drop"]): g for g in agg}
    lines = ["| drop | phase4 iter_ms | clear_t1 iter_ms | Δ% | phase4 final_loss | clear_t1 final_loss | Δσ | iter_ms PASS | final_loss PASS |",
             "|---|---|---|---|---|---|---|---|---|"]
    for drop in sorted({g["drop"] for g in agg}):
        p4 = by.get(("phase4", drop))
        ct = by.get(("clear_t1", drop))
        if not p4 or not ct:
            continue
        pct = (ct["iter_ms_med"] - p4["iter_ms_med"]) / p4["iter_ms_med"] * 100
        d_loss = ct["final_loss"] - p4["final_loss"]
        sigma = max(p4["final_loss_std"], 1e-6)
        d_sigma = d_loss / sigma
        iter_pass = "PASS" if pct <= 5.0 else "FAIL"
        loss_pass = "PASS" if abs(d_sigma) <= 1.0 else "FAIL"
        lines.append(
            f"| {drop:.2f} | {p4['iter_ms_med']:.1f} | {ct['iter_ms_med']:.1f} | "
            f"{pct:+.2f}% | {p4['final_loss']:.4f} | {ct['final_loss']:.4f} | "
            f"{d_sigma:+.2f} | {iter_pass} | {loss_pass} |"
        )
    return "\n".join(lines)


def main():
    runs = collect_runs()
    print(f"Found {len(runs)} run dirs (incl. duplicates)")
    selected = select_latest_per_cell(runs)
    print(f"Selected {len(selected)} latest 200-step runs")

    summaries = [summarize_run(r) for r in selected.values()]
    summaries.sort(key=lambda s: (s["transport"], s["drop"], s["seed"], s["node"]))

    agg = aggregate(summaries)

    # Detect any missing cells.
    expected = {("rc_baseline", d, s): None
                for d in [0.0, 0.01, 0.05] for s in [41, 42, 43]}
    expected.update({("clear_t1", d, s): None
                     for d in [0.0, 0.01, 0.05] for s in [41, 42, 43]})
    expected.update({("semirdma", d, s): None
                     for d in [0.0, 0.01, 0.05] for s in [41, 42, 43]})
    have = {(s["transport"], s["drop"], s["seed"]) for s in summaries}
    print(f"\nCoverage:")
    for tr in ["rc_baseline", "semirdma", "clear_t1"]:
        for drop in [0.0, 0.01, 0.05]:
            n_seeds = sum(1 for s in summaries
                          if s["transport"] == tr and s["drop"] == drop)
            print(f"  {tr:14s} drop={drop:.2f}: {n_seeds} runs (across nodes)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_tidy(summaries, OUT_DIR / "e1_per_cell.csv")
    write_summary(agg, OUT_DIR / "e1_summary.csv")

    print(f"\nWrote:")
    print(f"  {OUT_DIR/'e1_per_cell.csv'}")
    print(f"  {OUT_DIR/'e1_summary.csv'}")

    print("\nSummary table (transport × drop):")
    print(f"{'transport':<14s} {'drop':>5s} {'n':>3s} {'iter_med':>10s} {'iter_std':>10s} {'final_loss':>12s} {'loss_std':>10s}")
    for g in agg:
        print(f"{g['transport']:<14s} {g['drop']:>5.2f} {g['n']:>3d} "
              f"{g['iter_ms_med']:>10.2f} {g['iter_ms_std']:>10.3f} "
              f"{g['final_loss']:>12.4f} {g['final_loss_std']:>10.4f}")

    print("\nPass-criteria table:")
    print(render_pass_criteria(agg))


if __name__ == "__main__":
    main()
