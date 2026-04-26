# Layer-Aware SemiRDMA — System Design

> **Status:** PR-A landed (commit `33e7c57` + 2 follow-up fixes); PR-B
> validated end-to-end on real NIC (3 seeds × 2 transports × 3 drops, see
> [phase4/prb-results.md](prb-results.md)); PR-C (per-bucket layer mapping
> via imm_data bucket_id) deferred to next sprint.

## Motivation

Phase 3 + Phase 4 P2 established that pure SemiRDMA (UC + ratio-controlled
counter exit + ghost mask) survives wire drops where RC catastrophically
fails. But the flat-ratio design has two problems:

1. **One global p_L** ignores that different layers have different loss
   tolerance. BatchNorm and embeddings tolerate ~0% loss; conv mid-layers
   tolerate 5%; fc heads sit somewhere in between. Forcing the same ratio
   on all of them either over-protects the lossy-tolerant layers (wasted
   throughput) or under-protects the strict ones (silent drift).
2. **`cfg.timeout_ms = 200` is a magic number** — it must be large enough
   to absorb wire/DDP jitter, but the right value depends on bucket size,
   wire bandwidth, and observed jitter. A single value hard-codes a
   trade-off the runtime should be deriving.

The user's redesign moves the loss budget to the application layer:
each layer registers its own `p_L`, the transport derives a per-bucket
threshold, and the timeout becomes a derived physical bound from
continuous wire calibration.

## Architecture

```
Application                        Transport                        NIC
-----------                        ---------                        ---
register_loss_tolerance         →  LossToleranceRegistry
   ("layer1.0.bn1", 0.0)            (param_id → p_L; default_p)
   ("layer1.0.conv1", 0.05)               │
   ("fc", 0.01)                           ▼
                                  layer_aware_dispatcher_hook
DDP bucket fires                       │
                                       ▼
   bucket.parameters() ────────►  resolve_for_bucket(bucket)
                                       │   (p_bucket = min(p_L over params))
                                       ▼
   gloo all-reduce(eps_ema) ◄────► global eps_ema synchronized across ranks
                                       │
                                       ▼
   if p_bucket < eps_global + safety_margin:
       ───► rc_rdma_allreduce_hook(rc_substate, bucket)  ──► RC QP, HW retx
   else:
       ───► _run_semirdma_bucket(semi_substate, bucket,
                                 ratio = 1 - p_bucket,
                                 timeout_ms = T_max_for_bucket(...))
                                              │
                                              ▼
                                         UC QP, ratio-controlled exit
                                              │
                                              ▼
   stats["completed_post_drain"] ─────► WireCalibrator.update(...)
                                         (ε_ema, σ_jitter, B_ema EMAs)
```

## Components

### LossToleranceRegistry ([python/semirdma/layer_aware/registry.py](../../python/semirdma/layer_aware/registry.py))

Module-name → p_L map with a model-bound `id(param) → p_L` lookup.

- `register("layer1.0.bn1", 0.0)` — explicit per-module p_L
- `default_p` constructor argument — global floor for unregistered modules
  (used by PR-B uniform-budget runs; defaults to 0.0 = "everything strict
  by default, opt-in to lossy")
- `bind(model)` — walks `model.named_modules()`, attributes p_L per
  parameter; warns on registered names that don't match any module
- `resolve_for_bucket(bucket)` — `min(p_L)` over `bucket.parameters()`.
  An unregistered param's default 0.0 forces the entire bucket to RC,
  which is the conservative behavior we want.

### WireCalibrator ([python/semirdma/layer_aware/calibrator.py](../../python/semirdma/layer_aware/calibrator.py))

Continuous EMA fed from per-bucket training traffic (no probe burst).

- `epsilon_ema` ← `α · (1 − n_completed_post_drain / n_total) + (1−α) · ε_ema`
- `sigma_jitter_ms` ← stdev of `latency_ms` over rolling window
- `bandwidth_bps` ← `α · (n_bytes / latency_ms) + (1−α) · B_ema`
- Bootstrap window: first ~20 buckets fall back to `cfg.ratio` /
  `cfg.timeout_ms` while EMAs warm up
- `t_max_for_bucket(n_chunks, chunk_bytes)` →
  `max(t_max_min_ms, ceil((n_chunks·chunk_bytes / B_ema) + K · σ_jitter))`

### LayerAwareHookState ([python/semirdma/layer_aware/state.py](../../python/semirdma/layer_aware/state.py))

Wraps both `SemiRDMAHookState` (UC sub-state) and `RCRDMAHookState` (RC
sub-state) plus the registry and calibrator. Brings up 4 TCP exchange
ports (P..P+3) at construction:

- P, P+1: UC tx and rx
- P+2, P+3: RC tx and rx

Both sub-states are kept alive simultaneously; the dispatcher chooses
which one to call per bucket.

### Dispatcher ([python/semirdma/layer_aware/dispatcher.py](../../python/semirdma/layer_aware/dispatcher.py))

Per-bucket routing logic:

1. `p_bucket = state.registry.resolve_for_bucket(bucket)`
2. **Cross-rank synchronize** `epsilon_ema` via gloo all-reduce
   (Hypothesis M fix — without this, ranks can disagree on routing)
3. If `p_bucket < eps_global + safety_margin`: route to RC sub-hook
4. Otherwise:
   - `ratio = 1 − p_bucket` (or `cfg.ratio` during bootstrap)
   - `t_max = T_max_for_bucket(...)` (or `cfg.timeout_ms` during bootstrap)
   - Call `_run_semirdma_bucket(state.semi_substate, bucket, ratio, t_max)`
   - Feed returned stats back to calibrator (using `completed_post_drain`,
     Hypothesis N fix)

## Decision flow per bucket

```
                    ┌──────────────────────────────────┐
                    │ bucket arrives                    │
                    └──────────────────────────────────┘
                                       │
                                       ▼
                       p_bucket = min(p_L over bucket.parameters)
                                       │
                                       ▼
              ┌─────── all-reduce(eps_ema) ─────────────┐
              │                                          │
              ▼                                          ▼
     eps_global ← shared mean                  eps_global ← shared mean
              │                                          │
              ▼                                          ▼
   p_bucket < eps_global + margin?       p_bucket >= eps_global + margin?
              │                                          │
       (RC route)                                (SemiRDMA route)
              │                                          │
              ▼                                          ▼
   rc_rdma_allreduce_hook                _run_semirdma_bucket
   (HW retx, no ghost mask)              (ratio = 1 − p_bucket,
                                          T_max from calibrator,
                                          ghost mask zeros un-CQE'd
                                          chunks post-drain)
              │                                          │
              ▼                                          ▼
   future = (local + remote) / 2     future = (local + remote_with_ghosts) / 2
              │                                          │
              ▼                                          ▼
              ▲          calibrator.update(            ▲
              │             n_completed_post_drain,    │
              │             n_total,                   │
              │             latency_ms, n_bytes        │
              │          )                              │
              ▲                                          ▲
              └────────── return future ────────────────┘
```

## Two bugs found and fixed during PR-B

See [DEBUG_LOG.md](../../DEBUG_LOG.md) Hypotheses M and N for the full
post-mortems. Summary:

| Bug | Symptom | Root cause | Fix | Commit |
|---|---|---|---|---|
| **M** — cross-rank routing divergence | Cell crashes after ~bucket 7 with `await_bucket: recv deadline exceeded (30000 ms)` | Each rank's eps_ema is local; transient loss spike pushed rank 0's eps above threshold while rank 1's stayed below → rank 0 routed bucket to RC, rank 1 to SemiRDMA, RC await deadlocked | gloo all-reduce eps_ema before each routing decision; cost ~µs per bucket | `967703e` |
| **N** — calibrator measures budget not wire | All buckets routed to RC at drop=0.01 with p=0.10; iter_ms 800→3200 ms (RC retry storm) | `stats["completed"]` is taken at threshold-exit by ratio_controller, so it converges to ~ratio_threshold by construction. Dispatcher fed this to calibrator → eps_ema converged to ~p_bucket → safety check trips on every bucket | Surfaced post-drain `cs.num_completed()` as `stats["completed_post_drain"]`; dispatcher reads that. Now eps_ema tracks WIRE loss | `9e18230` |

## Validated behavior (PR-B v3, 18 cells, 3 seeds)

| drop | flat semirdma | layer_aware | Δ final_loss | Δ iter_ms |
|---:|---:|---:|---:|---:|
| 0.00 | 1.0558 ± 0.027 | 1.0611 ± 0.060 | +0.005 (NS) | **−11%** |
| 0.01 | 1.0533 ± 0.066 | 1.1562 ± 0.027 | +0.103 (~1.5σ) | **−20%** |
| 0.05 | 1.0062 ± 0.046 | 1.1634 ± 0.072 | +0.157 (~1.5σ) | **−20%** |

Layer-aware (uniform p=0.10) trades ~+0.10 final_loss for −20% iter_ms at
drop > 0. The full per-layer benefit (BN→RC, conv→SemiRDMA at p=0.05,
fc→SemiRDMA at p=0.01) requires **PR-C**: imm_data bucket_id encoding
+ smaller `bucket_cap_mb`. See [PLAN.md §1 PR-C](../PLAN.md).

## Known limitations

1. **Single-bucket workloads see a binary routing decision per step.**
   At default `bucket_cap_mb=512`, ResNet-18 fits in one bucket, so the
   "per-bucket" dispatcher reduces to "did min(p_L over the entire model)
   exceed eps + margin? RC vs SemiRDMA". To exercise heterogeneous
   routing, both `bucket_cap_mb` must shrink AND imm_data must encode
   bucket_id (current chunk_id is bucket-local 0..N-1; concurrent
   buckets cross ranks would alias).
2. **NIC tail rare crash.** PR-B v3 had 1/18 cells crash with
   `bucket-1 delivery 60%` at SEED=123 drop=0.05 layer_aware. 3 isolated
   reruns succeeded → classified as transient. Root cause not fully
   isolated; could be matrix-sequence dependent (NIC state from prior
   cell) or dispatcher race when eps approaches p. PR-C should retest.
3. **safety_margin tuning.** Currently 0.005. With NIC tail variance,
   eps_ema can momentarily spike. A larger margin (e.g. 0.01) would be
   more forgiving but pushes more buckets to RC. Empirical value not yet
   ablated.
4. **Per-bucket DIAG only at bucket_idx ≤ 5 or % 100 == 0.** Logging
   gate to keep log size manageable, but means we miss the moment
   eps_ema crosses thresholds. Paper-grade analysis should add a CSV
   sink.

## Why this design (vs. alternatives considered)

- **Probe-based calibration**: rejected — adds dedicated bandwidth, must
  schedule a non-overlapping window. Continuous-from-traffic is free.
- **Per-rank fallback decision (no all-reduce)**: rejected — leads to
  cross-rank routing divergence (Hypothesis M).
- **Replace `cfg.ratio` entirely**: rejected — keeps existing 27-cell
  flat-ratio data + tests valid for paper comparison. Layer-aware mode
  is opt-in via `cfg.layer_aware=True`.
- **Use SRQ instead of separate per-bucket cs**: deferred — would simplify
  imm_data demuxing for PR-C but is invasive. Will reconsider after PR-C
  if performance demands.

## Files

| Path | Purpose |
|---|---|
| [python/semirdma/layer_aware/__init__.py](../../python/semirdma/layer_aware/__init__.py) | Public exports |
| [python/semirdma/layer_aware/registry.py](../../python/semirdma/layer_aware/registry.py) | LossToleranceRegistry |
| [python/semirdma/layer_aware/calibrator.py](../../python/semirdma/layer_aware/calibrator.py) | WireCalibrator (continuous EMA) |
| [python/semirdma/layer_aware/state.py](../../python/semirdma/layer_aware/state.py) | LayerAwareHookState (UC + RC + registry + calibrator) |
| [python/semirdma/layer_aware/dispatcher.py](../../python/semirdma/layer_aware/dispatcher.py) | Per-bucket routing dispatcher |
| [python/semirdma/config.py](../../python/semirdma/config.py) | Config knobs (`layer_aware`, `loss_safety_margin`, `calibration_*`, `t_max_*`) |
| [python/semirdma/hooks.py](../../python/semirdma/hooks.py) | `_run_semirdma_bucket` helper extracted for dispatcher reuse |
| [python/semirdma/transport.py](../../python/semirdma/transport.py) | `await_gradient` returns `stats["completed_post_drain"]` |
| [tests/phase4/](../../tests/phase4/) | 31 unit tests + 1 RDMA-gated E2E test, all passing |
