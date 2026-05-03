"""Tracks 1 + 3: clear_perf.csv decode for E1 grid clear_t1 cells.

Track 1 — control_plane_overhead: what fraction of iter_ms is consumed
by CLEAR's control plane vs the underlying data path? Compare clear_t1
total_ms decomposition against phase4 baseline iter_ms.

Track 3 — +79% regression mechanism: under loss>0, which clear_perf
column (send_ms, recv_ms, threads_ms, finalize_ms, ...) accounts for
the 5 s/step inflation? Pinpoints the optimization target.

Usage:
    python scripts/phase5/e1_clear_perf_analyze.py
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from collections import defaultdict
from statistics import mean, median
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "docs" / "phase5" / "results" / "raw"
OUT = ROOT / "docs" / "phase5" / "results"

DIR_RE = re.compile(
    r"(?P<time>\d{2}-\d{2}-\d{2})_clear_t1_loss(?P<drop>[\d.]+)_seed(?P<seed>\d+)"
)

WARMUP_STEPS = 50
TOTAL_STEPS_REQUIRED = 200


def parse_date(p: Path) -> str:
    for part in p.parts:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            return part
    return "?"


def collect() -> list[dict]:
    runs: list[dict] = []
    for d in RAW.rglob("*"):
        if not d.is_dir():
            continue
        m = DIR_RE.match(d.name)
        if not m:
            continue
        cp = d / "clear_perf.csv"
        it = d / "iter_time.csv"
        if not cp.exists() or not it.exists():
            continue
        runs.append({
            "drop": float(m.group("drop")),
            "seed": int(m.group("seed")),
            "ts":   f"{parse_date(d)} {m.group('time')}",
            "node": "amd247" if "amd247" in str(d) else "amd245",
            "dir":  d,
            "perf": cp,
            "iter": it,
        })
    return runs


def latest_per_cell(runs: list[dict]) -> list[dict]:
    """Pick latest valid 200-step run per (drop, seed)."""
    best: dict = {}
    for r in runs:
        with open(r["iter"]) as f:
            n_rows = sum(1 for _ in f) - 1
        if n_rows < TOTAL_STEPS_REQUIRED:
            continue
        key = (r["drop"], r["seed"])
        if key not in best or r["ts"] > best[key]["ts"]:
            best[key] = r
    return list(best.values())


def load_perf(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "step": int(r["step_seq"]),
                "n_chunks": int(r["n_chunks"]),
                "recv_count": int(r["recv_count"]),
                "decision": int(r["decision"]),
                "to_bytes_ms": float(r["to_bytes_ms"]),
                "stage_ms":    float(r["stage_ms"]),
                "threads_ms":  float(r["threads_ms"]),
                "send_ms":     float(r["send_ms"]),
                "recv_ms":     float(r["recv_ms"]),
                "finalize_ms": float(r["finalize_ms"]),
                "average_ms":  float(r["average_ms"]),
                "from_numpy_ms": float(r["from_numpy_ms"]),
                "total_ms":    float(r["total_ms"]),
                "hook_total_ms": float(r["hook_total_ms"]),
            })
    return rows


def steady(rows: list[dict]) -> list[dict]:
    """Drop warmup; rows that survive are bucket-level entries (1 bucket
    per step in this grid)."""
    return [r for r in rows if r["step"] >= WARMUP_STEPS][:TOTAL_STEPS_REQUIRED - WARMUP_STEPS]


def col_med(rows: list[dict], col: str) -> float:
    return median(r[col] for r in rows)


def col_mean(rows: list[dict], col: str) -> float:
    return mean(r[col] for r in rows)


def render_decomposition(runs: list[dict]) -> str:
    """For each (drop) group, compute median per-column ms and the gap
    vs phase4_iter_ms."""
    by_drop = defaultdict(list)
    for r in runs:
        by_drop[r["drop"]].append(r)

    # phase4 medians from e1_summary.csv (loaded once for delta calc).
    p4_iter = {}
    sum_csv = OUT / "e1_summary.csv"
    if sum_csv.exists():
        with open(sum_csv) as f:
            for row in csv.DictReader(f):
                if row["transport"] == "phase4":
                    p4_iter[float(row["drop"])] = float(row["iter_ms_med"])

    cols = ["to_bytes_ms", "stage_ms", "threads_ms", "send_ms",
            "recv_ms", "finalize_ms", "average_ms", "from_numpy_ms",
            "hook_total_ms"]
    lines = ["",
             "## Per-bucket median ms breakdown (steady-state, last 150 steps)",
             "",
             "Each cell row aggregates 150 buckets; outer column = mean across "
             "n=3 seeds per drop.",
             "",
             "| col / drop |   0.00   |   0.01   |   0.05   |  Δ(0.05−0) |",
             "|---|---|---|---|---|"]
    for c in cols:
        per_drop = {}
        for d, items in by_drop.items():
            cell_meds = []
            for it in items:
                rows = steady(load_perf(it["perf"]))
                if not rows:
                    continue
                cell_meds.append(col_med(rows, c))
            per_drop[d] = mean(cell_meds) if cell_meds else float("nan")
        delta = per_drop.get(0.05, 0) - per_drop.get(0.0, 0)
        lines.append(
            f"| {c:<14s} | {per_drop.get(0.0, 0):>7.2f} | "
            f"{per_drop.get(0.01, 0):>7.2f} | "
            f"{per_drop.get(0.05, 0):>7.2f} | "
            f"{delta:+.2f} |"
        )
    lines.append("")
    lines.append("**Reference: phase4 iter_ms median:**")
    for d in sorted(p4_iter):
        lines.append(f"- drop={d}: {p4_iter[d]:.2f} ms/step")
    return "\n".join(lines)


def render_overhead_table(runs: list[dict]) -> str:
    """Track 1: clear_t1 hook_total_ms vs phase4 iter_ms_med ratio."""
    by_drop = defaultdict(list)
    for r in runs:
        by_drop[r["drop"]].append(r)

    p4_iter = {}
    sum_csv = OUT / "e1_summary.csv"
    if sum_csv.exists():
        with open(sum_csv) as f:
            for row in csv.DictReader(f):
                if row["transport"] == "phase4":
                    p4_iter[float(row["drop"])] = float(row["iter_ms_med"])

    lines = ["",
             "## Track 1: control_plane_overhead",
             "",
             "Defined as `(clear_t1.iter_ms_med − phase4.iter_ms_med) / phase4.iter_ms_med`.",
             "Per E1 plan the pass criterion is overhead ≤ 1%.",
             "",
             "| drop | phase4 iter_ms | clear_t1 iter_ms | overhead | ≤1%? |",
             "|---|---|---|---|---|"]

    # Re-use e1_summary numbers for clear_t1 too.
    ct_iter = {}
    if sum_csv.exists():
        with open(sum_csv) as f:
            for row in csv.DictReader(f):
                if row["transport"] == "clear_t1":
                    ct_iter[float(row["drop"])] = float(row["iter_ms_med"])

    for d in sorted(p4_iter):
        if d not in ct_iter:
            continue
        ovh = (ct_iter[d] - p4_iter[d]) / p4_iter[d] * 100
        verdict = "PASS" if ovh <= 1.0 else "FAIL"
        lines.append(
            f"| {d:.2f} | {p4_iter[d]:.1f} | {ct_iter[d]:.1f} | "
            f"{ovh:+.2f}% | {verdict} |"
        )

    return "\n".join(lines)


def render_repair_attribution(runs: list[dict]) -> str:
    """Track 3: how many bytes / how much time per dropped chunk on
    average. Helps decide whether C++ hot path could close the gap."""
    by_drop = defaultdict(list)
    for r in runs:
        by_drop[r["drop"]].append(r)

    # Compute drop fraction (1 - recv_count/n_chunks) and time per repair.
    lines = ["",
             "## Track 3: repair attribution",
             "",
             "**recv_count / n_chunks** = fraction of chunks delivered (UC drop "
             "is sender-side; this measures effective UC delivery rate). The "
             "complementary set is what CLEAR repair must recover.",
             "",
             "**send_ms + recv_ms per dropped chunk** estimates the per-chunk "
             "repair cost. (send_ms + recv_ms is the data plane + repair plane "
             "wall time.)",
             "",
             "| drop | n_chunks | recv_count (median) | delivery rate | "
             "send_ms (med) | recv_ms (med) | total_ms (med) |",
             "|---|---|---|---|---|---|---|"]

    for d in sorted(by_drop):
        n_chunks_all = []
        recv_all = []
        send_all = []
        recv_ms_all = []
        total_all = []
        for r in by_drop[d]:
            rows = steady(load_perf(r["perf"]))
            if not rows:
                continue
            n_chunks_all.append(median(rr["n_chunks"] for rr in rows))
            recv_all.append(median(rr["recv_count"] for rr in rows))
            send_all.append(median(rr["send_ms"] for rr in rows))
            recv_ms_all.append(median(rr["recv_ms"] for rr in rows))
            total_all.append(median(rr["total_ms"] for rr in rows))

        rate = mean(recv_all) / mean(n_chunks_all) if n_chunks_all else 0
        lines.append(
            f"| {d:.2f} | {mean(n_chunks_all):.0f} | {mean(recv_all):.0f} | "
            f"{rate*100:.1f}% | {mean(send_all):.1f} | "
            f"{mean(recv_ms_all):.1f} | {mean(total_all):.1f} |"
        )

    lines.append("")
    lines.append("Interpretation:")
    lines.append("- delivery rate < 100% under loss>0 → drops are observed at "
                 "receiver, repair is invoked")
    lines.append("- Compare send_ms / recv_ms inflation drop=0 → drop=0.05; "
                 "the column with the largest absolute jump pinpoints the "
                 "repair-traffic bottleneck")

    return "\n".join(lines)


def main():
    runs_all = collect()
    runs = latest_per_cell(runs_all)
    print(f"Found {len(runs_all)} clear_t1 perf dirs; selected {len(runs)} latest 200-step")

    # Coverage check.
    covered = {(r["drop"], r["seed"]) for r in runs}
    expected = {(d, s) for d in [0.0, 0.01, 0.05] for s in [41, 42, 43]}
    missing = expected - covered
    if missing:
        print(f"WARNING: missing {missing}")

    body = []
    body.append("# E1 — clear_perf.csv decode (control_plane_overhead + "
                "regression attribution)")
    body.append("")
    body.append(f"Generated: 2026-05-03 from {len(runs)} clear_t1 cells "
                "(latest 200-step run per (drop, seed)).")
    body.append("")
    body.append(render_overhead_table(runs))
    body.append(render_decomposition(runs))
    body.append(render_repair_attribution(runs))

    out_md = OUT / "e1_clear_perf_decode.md"
    out_md.write_text("\n".join(body), encoding="utf-8")
    print(f"\nWrote {out_md}")
    print("\n" + "\n".join(body))


if __name__ == "__main__":
    main()
