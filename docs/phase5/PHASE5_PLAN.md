# Phase 5 — CLEAR: Witnessed Erasure Semantics on RoCE UC

> **Status:** Planning. Triggered by [deep-research-report.md](../../deep-research-report.md) (2026-04-29).
> **Predecessor:** Phase 4 (PR-A/B/C — layer-aware dispatcher + per-bucket imm_data routing). Demoted from headline novelty to scaffolding / ablation.
> **Target venue:** unchanged — INFOCOM 2027 (abstract 2026-07-17, full 2026-07-24); SoCC 2026 R2 backup (2026-07-14).

---

## 0. Why a new phase

The deep-research review found that Phase 4's headline ("different layers tolerate different loss") is **already published prior art** (DLCP'20, MLT NSDI'24, PLOT TNSM'24). Submitting on that thesis invites desk-reject by overlap.

The publishable gap that **no public system covers simultaneously** is:

> RDMA UC + RDMA Write-with-Immediate as fast-path attribution carrier + PyTorch DDP **concurrent** buckets + explicit, witnessed, cross-rank-consistent erasure semantics.

MLT has bitmap witness but uses UDP shim (no UC, no imm_data constraints). OptiReduce has bounded-time collective but its public implementation is hard-capped at **2 concurrent buckets** with `bucket_cap_mb=1350`. Octopus showed `imm_data` as a semantic tag but doesn't handle training silent loss. CLEAR fills the intersection.

## 1. Core thesis (paper headline)

**SemiRDMA's contribution is not "we tolerate loss on UC". It is: we turn UC's silent, locally-inferred loss into an attributable, witnessed, deadline-bounded, cross-rank-consistent erasure object that DDP can finalize.**

Concretely, four design goals (from report §"设计目标"):

1. **Accurate attribution** — every chunk maps to exactly one `(step, bucket, phase, peer-edge, chunk)`; survives DDP bucket rebuild and `bucket_cap_mb=1` concurrency.
2. **Explicit witness** — replace ghost-mask local inference with receiver bitmap returned over an RC control plane; "未到 != 协议事实".
3. **Unified interpretation** — every rank sees the same finalize decision per bucket (`present | repaired | masked | stale-fill`); no silent divergence (PAFT'25 motivation).
4. **Bounded-time recoverable** — within `deadline`, choose deliver / selective repair / semantic mask per `policy`; do not collapse to RC.

## 2. CLEAR at a glance

```
                      RC control plane
   sender ───── BEGIN(uid, slot, gen, n_chunks, policy, deadline) ─────► receiver
   sender ─────── UC Write-with-Imm × n_chunks (imm = slot:8 | chunk:20 | gen:4) ──►
   receiver ───── WITNESS(uid, recv_bitmap, recv_count) ─────► finalizer
              ┌── REPAIR_REQ(uid, ranges) ──► sender (RC) ──► receiver
   finalizer ─┤
              └── FINALIZE(uid, semantic_mask) ──► all ranks (uniform)
   sender/receiver ── RETIRE(uid, slot, gen) ──► slot reusable
```

Detailed design: [clear-design.md](clear-design.md).

## 3. Relationship to Phase 4 work (what survives, what changes)

| Phase 4 component | Phase 5 fate |
|---|---|
| `uc_qp_engine` (UC QP lifecycle, Write-with-Imm) | **Keeps**. Underlies CLEAR data plane. |
| `chunk_manager` (chunk bitmap, ghost mask) | **Keeps as primitive**, but `recv_bitmap` is now exported via WITNESS, not consumed only locally. Ghost mask becomes one of three finalize modes. |
| `ratio_controller` (CQE polling + ratio exit) | **Keeps**, generalized: still drives `last-chunk-or-deadline`, but exit triggers WITNESS instead of immediate finalize. |
| PR-C `imm_data = bucket_id:8 \| chunk_id:24` | **Replaced** by `slot_id:8 \| chunk_idx:20 \| gen:4`. Slot is a *short lease* indirecting to RC-installed `uid`, not a raw bucket id. Solves PR-C's `bucket_id mod 256` wrap risk. |
| `LossToleranceRegistry` + `WireCalibrator` | **Demoted from headline to ablation lever**. Drives `policy` selection per bucket (`repair-first` vs `mask-first`), no longer the paper's central contribution. |
| `layer_aware_dispatcher_hook` | **Subsumed**. Routing decision becomes one input to CLEAR's per-bucket policy, alongside witness outcome. |
| Hybrid (gloo correctness-safeguard) | **Stays deleted** (Phase 4 negative result). |

## 4. New components Phase 5 must add

| Module | Language | Purpose |
|---|---|---|
| `src/transport/lease_table.{h,cpp}` | C++ | `(peer_qp, slot_id, gen) → uid` hash; pre-begin pending CQE queue; lease lifecycle. |
| `src/transport/control_plane.{h,cpp}` | C++ | RC QP for BEGIN/WITNESS/RETIRE/REPAIR_REQ/FINALIZE messages. Separate from data UC QP. |
| `src/transport/witness.{h,cpp}` | C++ | Compress `recv_bitmap` (raw / RLE / range-list); choose encoding by density; threshold-driven fallback. |
| `src/transport/finalizer.{h,cpp}` | C++ | Per-uid state machine: aggregate witness → decide repair vs mask → publish FINALIZE. Repair budget bookkeeping. |
| `python/semirdma/clear/`               | Python | Hook integration: warm-up RC pass, transport manifest builder (stable `bucket_seq`), shadow-RC oracle for false-attribution measurement. |
| `tests/integration/clear_*`            | C++/Py | Slot wrap, prebegin pending, RQ low-watermark, mask consistency across ranks, WITNESS loss/late. |

Detailed code reorganization plan: [code-reorg.md](code-reorg.md).

## 5. Experiment plan (high level)

Order matters — protocol correctness → flat regression → bucket concurrency → end-to-end convergence. Full matrix in [experiments.md](experiments.md).

| Stage | Question | Headline metric |
|---|---|---|
| **E0 protocol microbench** | Does CLEAR keep correct attribution under slot wrap, pre-begin races, RC/UC reorder, RQ pressure? | `false_attribution_rate` < 1e-4 |
| **E1 flat-path regression** | `bucket_cap_mb=512`: does CLEAR's control plane impose >5% overhead on the existing flat semirdma path? | `iter_ms` Δ vs Phase 4 baseline |
| **E2 bucket concurrency stress** | `bucket_cap_mb=1` (~50 buckets/step on ResNet-18): does CLEAR maintain attribution where vanilla PR-C aliases? | `false_attribution_rate`, `semantic_mismatch_rate` (target ≈ 0) |
| **E3 end-to-end convergence** | drop ∈ {0, 0.01, 0.05, 0.1}, burst & uniform: does witnessed erasure beat ghost-mask on TTA / final loss? | TTA, final loss, P99 step time |
| **E4 ablation: repair budget** | Does selective repair within budget improve loss vs mask-only? | repair_yield, mask_density, accuracy |
| **E5 ablation: layer-aware policy** | Phase 4 PR-A/B/C re-cast: does heterogeneous `policy` (BN repair-first / conv mask-first) beat uniform? | per-class accuracy |

Comparators: RC-baseline, RC-lossy, **Phase 4 SemiRDMA (PR-C ghost-mask)**, **CLEAR**, CLEAR + repair-budget-off. Optional MLT-style UDP comparator if engineering bandwidth allows.

Key new metric (mandatory for paper): **`false_attribution_rate`** measured via sampled shadow-RC oracle. This is what the headline rests on.

## 6. Timeline (12 weeks to INFOCOM full-paper deadline)

| Weeks | Milestone |
|---|---|
| W0 (now → 2026-05-03) | Plan freeze; user review + sign-off on [clear-design.md](clear-design.md) and [code-reorg.md](code-reorg.md). |
| W1–W2 (→ 2026-05-17) | C++ scaffolding: `lease_table`, `control_plane`, RC QP wiring, new `imm_data` layout. Unit tests on loopback + SoftRoCE. |
| W3 (→ 2026-05-24) | `witness` + `finalizer`; Python hook integration; warm-up manifest; shadow-RC oracle path. |
| W4 (→ 2026-05-31) | E0 protocol microbench on amd247/amd245 cluster; close attribution-correctness bugs. |
| W5 (→ 2026-06-07) | E1 flat regression + E2 bucket concurrency stress. Reuses XDP middlebox from Phase 4. |
| W6–W7 (→ 2026-06-21) | E3 end-to-end convergence sweep (3 seeds × 5 drops × 4 transports). |
| W8 (→ 2026-06-28) | E4 + E5 ablations; large-model hero cell (ResNet-50 or GPT-2-small). |
| W9–W10 (→ 2026-07-12) | Paper draft. |
| W11 (→ 2026-07-17) | Polish for INFOCOM abstract; SoCC backup decision. |
| W12 (→ 2026-07-24) | INFOCOM full submission. |

Slack: ~2 weeks across W4–W8; if E0 reveals an unexpected wire-level UC/imm_data behavior, fall back to "CLEAR-as-PR-C-extension" (slot-lease only, no witness) and ship a SoCC-scope paper.

## 7. Risks specific to Phase 5

| Risk | P | Impact | Mitigation |
|---|:-:|:-:|---|
| RC control plane adds tail latency that erases UC fast-path benefit | M | H | Bound control-plane bytes ≤ 1% of payload (E1); keep RC QP separate so RC retries don't HoL-block UC. |
| `gen:4` wraps in long runs and aliases stale packets | L | H | Slot quarantine when gen near wrap; verified in E0 long-run wrap test. |
| WITNESS bitmap too large at `bucket_cap_mb=1` × ~1024 chunks | L | M | RLE / range-list encoding; threshold fallback to "all-mask" if loss too dense. |
| Receive queue starvation kills Write-with-Imm (DOCA fatal) | M | H | Per-peer RQ low-watermark monitor; auto-fallback to RC for that bucket; tested in E0. |
| Shadow-RC oracle perturbs measurement | L | M | Sample only; document overhead; report metrics with oracle off as headline, with oracle on as validation. |
| 12 weeks too tight for full witness + repair + masking | M | H | Tiered scope: T1 = slot-lease + WITNESS + mask-only finalize (publishable). T2 = + selective repair. T3 = + heterogeneous policy ablation. |

## 8. Out of scope (explicit)

- Multi-NIC / multi-rail CLEAR (stays single QP-per-peer).
- Dynamic `gen` width or adaptive slot count.
- CUDA RDMA / GPUDirect.
- Kernel-bypass control plane (RC ibverbs is sufficient).
- Replacing flat-mode hook entirely; flat mode survives as ablation row.

## 9. Decisions (locked 2026-04-29)

1. **Numbering** — **Phase 5** (this doc). Phase 4 archives untouched.
2. **Scope tier** — **T2 must-ship**: slot-lease + WITNESS + mask-only finalize **+ selective repair**. T3 (heterogeneous-policy ablation) remains a stretch goal for E5 only.
3. **Ratio controller** — **amend in place** with `(slot, gen)` overload behind a feature flag; old `(bucket_id)` path stays callable so PR-C regression tests pass.
4. **Cluster** — **stay on amd247/amd245/amd264 CX-5 25 GbE** for the entire Phase 5. No d7525 100 GbE migration.
5. **MLT-style UDP comparator** — **default cite-only** for INFOCOM submission (no engineering budget). Revisit for camera-ready if accepted.

Implication for timeline: T2 floor pulls ~1 week of selective-repair work into the W1–W3 budget. Updated breakdown:

| Block | Estimate (rev) |
|---|---|
| W1–W3 base (lease, control plane, witness, mask-only finalize, hooks) | 12 d |
| **W3.5 selective repair (T2 add-on)** | +5 d |
| **Subtotal W1–W3.5** | **~17 days** (≈ 3.5 calendar weeks; ends ~2026-05-24) |

E0 starts ~2026-05-25 (1 week later than initial plan). Slack still adequate vs INFOCOM 2026-07-24 deadline.

---

**Files:**
- [clear-design.md](clear-design.md) — protocol details, imm_data layout, control plane state machine
- [code-reorg.md](code-reorg.md) — directory + module changes, migration steps, test plan
- [experiments.md](experiments.md) — full experiment matrix and metric definitions
