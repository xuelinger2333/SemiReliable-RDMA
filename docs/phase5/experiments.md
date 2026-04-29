# Phase 5 Experiment Matrix

> **Status:** Plan (2026-04-29). Numbers are targets; cell counts may shrink under cluster constraints.
> **Parent:** [PHASE5_PLAN.md](PHASE5_PLAN.md), [clear-design.md](clear-design.md)

---

## 1. Goals and ordering

The point of Phase 5 experiments is **not** more accuracy curves. It is to defend three claims, in order:

1. **Correctness**: CLEAR's attribution is sound under hostile concurrency, slot wrap, RC/UC reorder, and RQ pressure.
2. **No-regression**: CLEAR's added control plane does not break the flat-path performance Phase 4 already shipped.
3. **Concurrency win**: at `bucket_cap_mb=1` (~50 buckets/step) — where Phase 4 PR-C aliases under `mod 256` — CLEAR keeps `false_attribution_rate ≈ 0` and `semantic_mismatch_rate == 0`.

Only after those three are nailed do we run E3 convergence and E4/E5 ablations. The reviewer-defeating headline is **(2) + (3)**, not "TTA goes down 5 %".

## 2. Comparator set

| Tag | Description | When run |
|---|---|---|
| `rc_baseline` | Full RC, no loss injection. Gold-standard reliability + accuracy. | E1, E3 |
| `rc_lossy` | RC + XDP middlebox drops. Reproduces Phase 4 P2 RC catastrophic-fail story. | E3 |
| `phase4_flat` | Phase 4 SemiRDMA flat hook, ghost-mask only. | E1, E3 |
| `phase4_la` | Phase 4 layer-aware dispatcher (PR-A/B). | E1, E3, E5 |
| `phase4_prc` | Phase 4 + PR-C `bucket_id mod 256` imm encoding. | E2, E3 |
| `clear_t1` | CLEAR scope T1: slot-lease + WITNESS + mask-only finalize. | All |
| `clear_t2` | CLEAR + selective repair (repair-budget on). | E3, E4 |
| `clear_t3` | CLEAR + heterogeneous policy (BN repair-first / conv mask-first / fc estimator-scale). | E5 |
| `mlt_udp_cite` | MLT-style UDP. **Cited only in related work**, not engineered. | — |

## 3. Network / drop matrix

Reused across stages (cells inherit unless overridden):

- `chunk_bytes = 4096`
- `STEPS = 500` for E1/E2, `STEPS = 3000` for E3 hero cells.
- `seed ∈ {41, 42, 43}` (default 3 seeds; 5 for E3 hero cells).
- Drop injection via XDP middlebox `xdp_dropbox` (Phase 4):
  - `drop ∈ {0, 0.001, 0.005, 0.01, 0.05, 0.10}`
  - patterns: `uniform`, `burst (Markov 2-state, p_burst=0.6, mean_run=10)`
- Cluster: amd247 / amd245 (data) + amd264 (XDP middlebox), CX-5 25 GbE.

## 4. Stages

### E0 — Protocol microbench (W4)

**Question:** does CLEAR maintain attribution under adversarial protocol conditions?

| Sub-test | Setup | Pass criterion |
|---|---|---|
| `e0.1 slot_wrap` | 5 000 buckets, force slot recycle every ~256 buckets, fixed gen-bits=4 | `false_attribution_rate < 1e-4` over all uids; zero alias detected |
| `e0.2 prebegin_race` | inject artificial 1–10 ms BEGIN delay; UC writes arrive first | every uid finalizes correctly; PREBEGIN_PENDING drains |
| `e0.3 ucrc_reorder` | randomize per-msg jitter on RC control plane (±5 ms) | no FINALIZE collision; semantic_mismatch_rate == 0 |
| `e0.4 rq_starvation` | receiver intentionally lags RWR replenishment by N | `BACKPRESSURE` fires before fatal; no Write-with-Imm error |
| `e0.5 witness_loss` | drop 5 % of WITNESS messages on the RC plane | every uid still finalizes (RC retransmit kicks in within 2× witness_timeout) |
| `e0.6 long_run_wrap` | 12 h run, ~10⁶ buckets, watch gen wrap quarantine | zero alias; quarantine count and pressure curves recorded |

Only e0.1 and e0.6 require long compute; the rest are minutes-scale.

### E1 — Flat-path regression (W5)

**Question:** does CLEAR's control plane add measurable cost on the existing flat path?

- Workload: ResNet-18 / CIFAR-10 / 2 ranks (Phase 4 stage_a fixture).
- `bucket_cap_mb = 512` (effectively 1 bucket).
- 5 transports: `rc_baseline`, `phase4_flat`, `phase4_prc`, `clear_t1` (mask-only, oracle off), `clear_t1+oracle` (oracle 10 %).
- 3 seeds × 5 transports × 3 drops {0, 0.01, 0.05} = **45 cells**, ~3 h compute.

| Metric | Pass criterion |
|---|---|
| `iter_ms` median | `clear_t1` within +5 % of `phase4_flat` |
| `final_loss` last-50 mean | within 1 σ of `phase4_flat` |
| `control_plane_overhead` | ≤ 1 % of total bytes |

If `clear_t1` adds > 5 % iter_ms, debug before E2.

### E2 — Bucket-concurrency stress (W5–W6)

**Question:** at `bucket_cap_mb=1`, does CLEAR maintain attribution where PR-C aliases?

- Same workload; `bucket_cap_mb = 1` → ~50 buckets/step.
- Transports: `phase4_prc`, `clear_t1`. Shadow-RC oracle on for both at sample rate 0.10.
- 3 seeds × 2 transports × 4 drops {0, 0.005, 0.01, 0.05} = **24 cells**, ~3 h.

| Metric | Pass criterion |
|---|---|
| `false_attribution_rate` (oracle-validated) | `clear_t1` < 1 e-4; `phase4_prc` will be ≥ 1 e-2 (this is the headline plot) |
| `semantic_mismatch_rate` (across ranks) | `clear_t1` == 0 |
| `iter_ms` median | `clear_t1` within +10 % of `phase4_prc` |
| `mask_density` distribution | reported, not gated |

**This is the figure that sells the paper.** A two-curve plot of `false_attribution_rate` vs drop, comparing `phase4_prc` and `clear_t1`, is the reviewer-defeating result.

### E3 — End-to-end convergence (W6–W7)

**Question:** does witnessed erasure beat ghost-mask on TTA / final loss?

- Workload: ResNet-18 / CIFAR-10, then ResNet-50 / ImageNet-subset (hero cell only).
- `bucket_cap_mb = 1` (forces concurrency stress on every step).
- Transports: `rc_baseline`, `rc_lossy`, `phase4_flat`, `phase4_prc`, `clear_t1`, `clear_t2`.
- 3 seeds × 6 transports × 5 drops × 2 patterns (uniform, burst) = **180 cells** at STEPS=500. Trim to subset:
  - Full sweep on ResNet-18 / uniform: 3 × 6 × 5 = **90 cells** (~6 h).
  - Burst pattern on subset of drops {0.01, 0.05}: 3 × 6 × 2 = **36 cells** (~2.5 h).
  - ResNet-50 hero: 2 seeds × 4 transports {`rc_baseline`, `phase4_flat`, `clear_t1`, `clear_t2`} × 2 drops {0.01, 0.05} = **16 cells** at STEPS=3000 (~12 h on CPU; revisit cluster).

| Metric | Pass criterion |
|---|---|
| TTA to fixed accuracy | `clear_t2` ≤ `phase4_flat`; `clear_t1` within 5 % of `phase4_flat` |
| Final loss / accuracy | `clear_t2` within 1 σ of `rc_baseline` at drop ≤ 0.05 |
| P99 step time | `clear_t1`/`clear_t2` < `rc_baseline` at drop ≥ 0.01 |
| `byte_wise_uc_share` | `clear_t1` > 0.95; `clear_t2` > 0.85 |

### E4 — Repair-budget ablation (W8)

**Question:** does selective repair within budget actually pay back its bytes?

- Single workload (ResNet-18, drop=0.05, burst), single seed × 5 budgets = 5 cells × 3 seeds = **15 cells**.
- Sweep `repair_budget_bytes ∈ {0, 1 MB, 4 MB, 16 MB, ∞}` per step.
- Report: `repair_yield`, `mask_density`, final loss, `iter_ms`. Identify the knee.

### E5 — Heterogeneous policy ablation (W8)

**Question:** does per-bucket-class policy beat uniform mask-first?

- ResNet-18, drop ∈ {0.01, 0.05}, 3 seeds.
- Policies:
  - `all-mask`: every bucket mask-first.
  - `bn-repair`: BN/LN/embedding repair-first, rest mask-first.
  - `bn-repair + fc-estimator`: above + fc layer uses estimator-scale.
  - `phase4_la-recast`: re-run Phase 4 PR-A/B layer-aware as a CLEAR policy.
- 3 seeds × 4 policies × 2 drops = **24 cells**, ~2.5 h.

This is where Phase 4's layer-awareness contribution survives: it becomes one ablation row, not the headline.

## 5. Metric definitions (canonical)

These names are used unchanged in code, CSV columns, and paper.

| Metric | Source | Definition |
|---|---|---|
| `iter_ms_p50/p95/p99` | runtime | per-step wall time, last-50 mean of `STEPS` excluding warmup. |
| `final_loss` | runtime | last-50 step mean training loss. |
| `accuracy_top1` | eval | top-1 on held-out validation set after `STEPS`. |
| `tta_to_X` | eval | first step at which `accuracy_top1 ≥ X`. |
| `false_attribution_rate` | shadow oracle | `Σ chunks where post-mask buffer != oracle / Σ chunks sampled`. **Headline new metric.** |
| `semantic_mismatch_rate` | cross-rank diff | `Σ uids where rank0_final_mask != rank1_final_mask / Σ uids`. Target == 0. |
| `byte_wise_uc_share` | metrics counter | `bytes_uc / (bytes_uc + bytes_rc_repair + bytes_rc_fallback + bytes_rc_control)`. |
| `control_plane_overhead` | metrics counter | `bytes_rc_control / total_bytes`. |
| `repair_yield` | metrics counter | `uids_repaired / uids_with_repair_attempted`. |
| `mask_density` | metrics counter | `Σ masked_chunks / Σ total_chunks` per uid; report distribution. |
| `rq_low_watermark_events` | metrics counter | count of times `rq_monitor::on_low_watermark` fired per run. |
| `slot_quarantine_events` | metrics counter | count of times slot recycling was deferred due to gen-near-wrap. |

All metrics are emitted to `experiments/phase5/<stage>/<run_id>/metrics.csv` and aggregated by `scripts/analysis/phase5_aggregate.py` (to be written in W4).

## 6. Cluster fixture

Same as Phase 4. `scripts/cloudlab/bootstrap_fresh_node.sh` brings up amd247/amd245; `middlebox_setup.sh bootstrap` configures amd264 with XDP. **No new cluster work** unless decision-point #4 in the plan flips to d7525 100 GbE.

## 7. Risks specific to experiments

| Risk | Mitigation |
|---|---|
| Shadow oracle adds enough latency that it perturbs `iter_ms` measurement | Run E1/E2 with oracle off, use oracle-on runs only for `false_attribution_rate` reporting. Document in paper as a sampled-validation methodology. |
| `bucket_cap_mb=1` exposes new tail crashes (PR-B v3 had 1/18 NIC tail crash) | Adopt Phase 4 rerun-on-crash policy; document in methodology. Investigate root cause if rate > 5 %. |
| Burst-loss pattern interacts badly with `repair_budget` | Capture burst-vs-uniform side-by-side in E3; if budget exhausted by bursts, raise budget knob and rerun. |
| ResNet-50 STEPS=3000 too slow on CPU-only nodes | Skip large-model E3 hero in T1 cycle; cite as future work and / or revisit when GPU CloudLab profile available. |
| Cluster expiration mid-sweep | Phase 4 archive pipeline (`raw_data/` + remote rsync) extends; document `bootstrap_fresh_node.sh` re-entry. |

## 8. Deliverables (per stage)

Each stage drops one directory `docs/phase5/results/<stage>/` containing:

- `aggregate.csv` (one row per cell)
- `figures/` (rendered plots)
- `findings.md` (≤ 1 page, what we learned, what failed)
- `raw/` (timestamped run dirs, not gitted; rsync to long-term archive)

Aggregator script: `scripts/analysis/phase5_aggregate.py` (mirror of Phase 4's matrix aggregator, with new columns for CLEAR metrics).
