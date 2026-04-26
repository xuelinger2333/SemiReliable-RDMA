# PR-B Real-NIC Validation Results

> **Date:** 2026-04-26 / 27
> **Platform:** CloudLab amd203 + amd196 (CX-5 25 GbE) + amd186 (XDP middlebox)
> **Matrix:** 3 seeds × 2 transports × 3 drop rates × STEPS=500 × timeout=200ms
> **Layer-aware mode:** uniform `default_p=0.10`, `safety_margin=0.005`
> **Raw data:** `/tmp/p0_prb_v3_20260426_100315/` (amd203)

## Bugs found and fixed during validation

### Bug 1: cross-rank routing divergence (commit `967703e`)

The dispatcher used local `eps_ema` to decide RC vs SemiRDMA per bucket. When
rank 0's eps climbed above `p − margin` while rank 1's hadn't, ranks routed
the same bucket to different transports. Rank 0's RC `await_bucket` then
deadlocked at 30 s waiting for chunks rank 1 sent via UC. Fixed by gloo
all-reducing `eps_ema` to its mean across ranks before each routing decision
(one float, ~µs over gloo TCP).

### Bug 2: calibrator metric source (commit `9e18230`)

Calibrator was fed `stats["completed"]` (pre-drain at threshold exit). With
ratio = 1 − p_bucket, this read converges to ~ratio_threshold by construction —
not actual wire loss. eps_ema thus tracked the budget threshold not the wire,
trippling the safety check on every bucket and forcing all traffic to RC.
Surfaced post-drain count via `stats["completed_post_drain"]`; dispatcher now
reads that. Verified: pre_drain ≈ 9822/10913 (95% — threshold), post_drain ≈
10800/10913 (99% — actual wire) at drop=0.01.

## Final results (3-seed last-50-mean, after P1 substitution)

| drop | transport | s=42 | s=123 | s=7 | mean | std | iter_ms | rc |
|---:|---|---:|---:|---:|---:|---:|---:|:---:|
| 0    | semirdma             | 1.0832 | 1.0302 | 1.0541 | 1.0558 | 0.0265 | 853.6 | 0,0,0 |
| 0    | semirdma_layer_aware | 1.0948 | 0.9921 | 1.0963 | 1.0611 | 0.0597 | 757.8 | 0,0,0 |
| 0.01 | semirdma             | 1.1113 | 0.9823 | 1.0664 | 1.0533 | 0.0655 | 963.4 | 0,0,0 |
| 0.01 | semirdma_layer_aware | 1.1699 | 1.1733 | 1.1254 | 1.1562 | 0.0267 | 772.4 | 0,0,0 |
| 0.05 | semirdma             | 0.9533 | 1.0316 | 1.0336 | 1.0062 | 0.0458 | 944.0 | 0,0,0 |
| 0.05 | semirdma_layer_aware | 1.0858 | 1.2289 | 1.1756 | 1.1634 | 0.0723 | 773.8 | 0,0,0 |

Throughput: layer-aware is **−11% to −20% iter_ms** across all drop rates.
Convergence: layer-aware shows **+0.10 final_loss penalty at drop > 0**.
Both effects within ~1.5σ (small-n caveat), but consistent direction.

## P1: SEED=123 drop=0.05 layer_aware crash classification

**Verdict:** TRANSIENT, not systematic.

Cell 5 of the original v3 run crashed with bucket-1 delivery 6635/10913
(39% loss vs configured 5% wire) and cascading RC await timeout. We then
re-ran the same configuration three times in succession on the same nodes:

| run | rc | final_loss | iter_ms |
|---:|:---:|---:|---:|
| 1 | 0 | 1.3898 | 812.6 |
| 2 | 0 | 1.5065 | 748.3 |
| 3 | 0 | 1.4472 | 754.6 |

3/3 reruns succeeded. The crashed cell was archived as `cell_05_*.crashed_orig/`
and run 1 was substituted into the v3 layout for the head-to-head table above.
Likely cause: NIC tail variance in a regime where bursty UC drops compound
with ratio-threshold timing. Same-config run-to-run variance was already
documented in [scripts/analysis/loss_trajectory.py](../scripts/analysis/loss_trajectory.py).

## Limitation

Default `bucket_cap_mb=512` makes ResNet-18 (~47 MiB) fit in one bucket per
step, so per-bucket routing reduces to one decision per step. The full
heterogeneous-budget design (BN p=0 → RC, conv p=0.05 → SemiRDMA, fc p=0.01 →
borderline) cannot be exercised at this configuration. To unlock per-layer
routing, two things are needed:

1. Smaller `bucket_cap_mb` (e.g., 1 MiB → ~50 buckets / step for ResNet-18).
2. Bucket_id encoded in `imm_data` so concurrent buckets across ranks don't
   alias each other's chunk identifiers.

This is the PR-C scope, deferred. PR-B's value is validating the dispatcher
+ calibrator + cross-rank sync at uniform-p_L; PR-C will demonstrate the
per-layer benefit on top.
