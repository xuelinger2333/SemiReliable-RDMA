# Phase 4 raw experiment data

All directories below were collected on CloudLab Utah (`amd203.utah.cloudlab.us`
+ `amd196.utah.cloudlab.us` for ranks, `amd186.utah.cloudlab.us` for the XDP
middlebox) between 2026-04-25 and 2026-04-26 before the nodes were released.
Filesystem layout for each `p0_*` / `p1_*` folder:

```
<root>/
├── master.log                                    (only multi-seed wrappers)
├── seed{42,123,7}/                               (per-seed sub-matrix)
│   ├── MATRIX.log                                (chronological runner trace)
│   ├── MATRIX_SUMMARY.csv                        (idx, drop, transport, rc, final_loss, mean_iter_ms, dir)
│   └── cell_NN_drop<rate>_<transport>_t<to>/
│       ├── train_cifar10.log                     (full DIAG trace for the cell)
│       ├── loss_per_step.csv                     (501 lines incl. header)
│       ├── iter_time.csv                         (per-step fwd/bwd/opt/total in ms)
│       ├── grad_norm.csv                         (L2 of the gradient tensor per step)
│       └── .hydra/                               (resolved config + overrides)
└── (some single-cell runs flatten the seed dir away)
```

## Contents

| Folder | Purpose | Size |
|---|---|---|
| [p0_3seed_ref_20260425_110928/](p0_3seed_ref_20260425_110928/) | Original 3-seed × 3-transport × 3-drop matrix that surfaced the 0.057 SemiRDMA-vs-RC question. 27 cells. Used as paper-comparison reference vs PR-B v3. | 3.3 M |
| [p0_falsify_L/](p0_falsify_L/) | Single-cell falsification of [DEBUG_LOG.md](../../DEBUG_LOG.md) hypothesis L (drain bookkeeping race). Bridge data: pre-PR-A. | 231 K |
| [p0_sanity_revert/](p0_sanity_revert/) | Same-config rerun after L.2 revert that demonstrated 0.026 same-seed final_loss spread → established the seed-luck noise floor. | 235 K |
| [p0_repro_seed7_073032/](p0_repro_seed7_073032/) | SEED=7 × 3 reruns (NIC tail variance investigation). Showed `total_missed` varies 309 → 898 across same-seed runs. | 689 K |
| [p0_prb_v3_20260426_100315/](p0_prb_v3_20260426_100315/) | **PR-B head-to-head matrix** — 3 seeds × {`semirdma`, `semirdma_layer_aware`} × {0, 0.01, 0.05} drop = 18 cells. SEED=123 cell 5 substituted from P1 run 1; original crashed cell archived under `*.crashed_orig/`. | 5.1 M |
| [p1_repro_122328/](p1_repro_122328/) | P1 verification: 3 isolated reruns of SEED=123 layer_aware drop=0.05 — 3/3 succeeded → original v3 crash classified as transient. | 910 K |

Plus runner logs: `p0_prb_v3_runner.log`, `p1_repro_runner.log`,
`p0_sanity_revert_runner.log`, `p0_repro_seed7_runner.log`,
`p0_3seed_ref_runner.log` — chronological wrapper output for the multi-seed
runs.

## Older Phase-4 archives (pre-existing, kept for reference)

| Folder | Notes |
|---|---|
| [2seed_highdrop/](2seed_highdrop/) | Phase 1 hybrid-vs-semirdma drop ∈ {0, 0.05, 0.1} sweep |
| [3seed_lowdrop/](3seed_lowdrop/) | Phase 1 hybrid-vs-semirdma drop ∈ {0, 0.01} sweep |
| [post_hybrid_smoke/](post_hybrid_smoke/) | Smoke test after hybrid removal |
| [aggregate_final.csv](aggregate_final.csv) | Hybrid-dead-end final aggregate |

## Reproducing analysis

The analysis scripts in [scripts/analysis/](../../../scripts/analysis/) read
these directories directly. Examples:

```bash
# 3-seed mean ± std with last-50 smoothing
python scripts/analysis/matrix_aggregate.py docs/phase4/raw_data/p0_3seed_ref_20260425_110928

# PR-B v3 head-to-head table
python scripts/analysis/prb_aggregate.py docs/phase4/raw_data/p0_prb_v3_20260426_100315

# Per-seed loss trajectory
python scripts/analysis/loss_trajectory.py docs/phase4/raw_data/p0_prb_v3_20260426_100315 semirdma_layer_aware 0

# Ghost-vs-loss correlation
python scripts/analysis/ghost_vs_loss.py docs/phase4/raw_data/p0_3seed_ref_20260425_110928
```

## Why archived in repo

CloudLab nodes are scheduled to be released. `/tmp/p0_*` folders on amd203 do
not survive node release. Archiving here costs ~10 MB and lets the analysis
scripts above run on any clone of the repo. The matrix runner config
(`scripts/cloudlab/run_p1_matrix.sh`) and aggregator scripts let any future
reservation regenerate the same data.
