# Phase 5 Code Reorganization Plan

> **Status:** Proposal (2026-04-29). **No source changes have been made yet.** This doc is the plan; implementation starts after user sign-off.
> **Parent:** [PHASE5_PLAN.md](PHASE5_PLAN.md), [clear-design.md](clear-design.md)

---

## 1. Guiding rules

1. **Amend, don't rewrite.** PR-A/B/C code paths stay runnable as ablation rows. We add a new transport mode (`mode=clear`) alongside `mode=flat` and `mode=layer_aware`.
2. **One module per CLEAR concept.** Each new C++ file ≤ 400 lines (per coding-style.md). If a concept needs more, split.
3. **C++ owns hot path; Python owns policy.** Lease table, control-plane wire format, witness encoding, finalizer state machine — all C++. Manifest builder, registry, hook orchestration, shadow-RC oracle — Python.
4. **No CLEAR code is reachable unless `cfg.transport.mode == "clear"`.** Existing flat / layer-aware tests must remain green.

## 2. Target directory layout

```
src/transport/
├── uc_qp_engine.{h,cpp}        # unchanged: UC QP + Write-with-Imm
├── chunk_manager.{h,cpp}       # unchanged: bitmap + ghost mask primitive
├── ratio_controller.{h,cpp}    # AMENDED: accepts (slot,gen) and emits ratio-exit event without finalizing
├── ghost_mask.{h,cpp}          # unchanged; ghost mask is now one of the finalize modes
├── clear/                      # NEW — all CLEAR-specific code lives here
│   ├── lease_table.{h,cpp}     # (peer_qp, slot_id, gen) → uid; PREBEGIN_PENDING; lifecycle
│   ├── control_plane.{h,cpp}   # RC QP for BEGIN/WITNESS/RETIRE/REPAIR_REQ/FINALIZE; serialization
│   ├── messages.h              # POD structs + wire-format encode/decode
│   ├── witness_codec.{h,cpp}   # RAW / RLE / RANGE / FULL bitmap encodings + density-driven choice
│   ├── finalizer.{h,cpp}       # per-uid state machine; repair budget bookkeeping
│   ├── rq_monitor.{h,cpp}      # per-peer receive-queue low-watermark + backpressure
│   └── metrics.{h,cpp}         # false_attribution / semantic_mismatch / control_plane_overhead counters
└── (no other changes here)

src/bindings/
└── py_semirdma.cpp             # AMENDED: expose CLEAR types + acquire_lease, post_clear_bucket, await_clear_finalize

python/semirdma/
├── transport.py                # AMENDED: add ClearTransport alongside SemiRdmaTransport
├── hooks.py                    # AMENDED: add clear_allreduce_hook (parallel to flat/layer_aware)
├── clear/                      # NEW
│   ├── __init__.py
│   ├── manifest.py             # post-warmup bucket → bucket_seq stable id; uid hash
│   ├── policy.py               # registry: bucket_seq → policy (repair-first / mask-first / ...)
│   ├── shadow_oracle.py        # sampled RC reference path; computes false_attribution_rate
│   └── runtime.py              # finalize callback orchestration; mask application
├── layer_aware/                # unchanged; demoted to ablation
└── baselines/                  # unchanged

experiments/
├── stage_a/                    # legacy CIFAR-10 driver, unchanged
├── stage_b/                    # legacy real-NIC matrix, unchanged
└── phase5/                     # NEW
    ├── e0_microbench/          # protocol correctness microbench (slot wrap, prebegin, RQ pressure)
    ├── e1_flat_regression/     # bucket_cap_mb=512 baseline-vs-CLEAR overhead
    ├── e2_concurrency/         # bucket_cap_mb=1 attribution stress
    ├── e3_convergence/         # full TTA sweep
    ├── e4_repair_ablation/     # repair budget ablation
    └── e5_policy_ablation/     # heterogeneous policy ablation (PR-A/B/C re-cast)

tests/
├── unit/                       # gtest, existing
│   ├── clear_lease_table_test.cpp        # NEW
│   ├── clear_witness_codec_test.cpp      # NEW
│   ├── clear_finalizer_test.cpp          # NEW
│   └── clear_control_plane_test.cpp      # NEW (loopback)
└── integration/
    ├── clear_loopback_test.py            # NEW: full BEGIN→WRITES→WITNESS→FINALIZE→RETIRE
    ├── clear_slot_wrap_test.py           # NEW: 5000+ buckets, watch for alias
    ├── clear_prebegin_race_test.py       # NEW: forced UC-before-BEGIN ordering
    ├── clear_rank_consistency_test.py    # NEW: 2-rank semantic_mismatch_rate ≈ 0
    └── clear_rq_starvation_test.py       # NEW: receiver lags RWRs

docs/phase5/
├── PHASE5_PLAN.md
├── clear-design.md
├── code-reorg.md
├── experiments.md
└── results/                    # populated as E0–E5 complete
```

## 3. Module-by-module change spec

### 3.1 `ratio_controller.{h,cpp}` — amend in place

Today: `wait_for_ratio(bucket_id, n_chunks, ratio, timeout)` polls CQ, drains foreign-bucket CQEs into a pending queue, returns when ratio met or timeout.

Changes:
- Replace `bucket_id` with `(slot_id, gen)`.
- Add an enum return: `RatioExitReason::DELIVERED | RATIO_MET | DEADLINE`.
- Stop calling `chunk_manager::finalize_with_ghost_mask` internally. Instead, emit a `RatioExitEvent { slot_id, gen, recv_count, recv_bitmap_view }` for the finalizer to consume.
- The pending-queue is moved to `lease_table` (it's really a lease-keyed concept, not a ratio-controller concept).

This is the only edit to existing transport sources. Keeps PR-C's CQE polling loop intact; just severs the "auto-finalize" coupling.

### 3.2 `clear/lease_table.{h,cpp}` — new

```cpp
class LeaseTable {
public:
    // Sender side
    Lease acquire(uint64_t uid, /*hint*/ std::optional<uint8_t> slot_pref = {});
    void release(uint64_t uid);  // on RETIRE

    // Receiver side
    void install(const BeginMsg& begin);             // BEGIN rx
    LookupResult lookup(uint8_t slot, uint8_t gen);  // CQE rx
    void retire(uint64_t uid);                       // RETIRE rx

    // PREBEGIN_PENDING
    void enqueue_pending(uint8_t slot, uint8_t gen, uint32_t chunk_idx);
    std::vector<PendingEntry> drain_pending_for(uint8_t slot, uint8_t gen);

    // Slot-pressure / gen-wrap
    SlotPressure pressure() const;
    void quarantine_if_near_wrap();

private:
    std::array<SlotState, 256> slots_;
    std::unordered_map<uint64_t, uint8_t> uid_to_slot_;
    std::deque<PendingEntry> prebegin_;
};
```

Owns the `(slot, gen, uid)` triple integrity. Unit-tested standalone (no RDMA dependency).

### 3.3 `clear/control_plane.{h,cpp}` — new

Wraps a per-peer RC QP. Provides:

```cpp
class ControlPlane {
public:
    void send_begin(const BeginMsg&);
    void send_witness(const WitnessMsg&);
    void send_repair_req(const RepairReqMsg&);
    void send_finalize(const FinalizeMsg&);
    void send_retire(const RetireMsg&);

    // poll RC CQ; dispatches by message type to registered handlers
    void poll_once();

    void on_begin(std::function<void(const BeginMsg&)>);
    void on_witness(std::function<void(const WitnessMsg&)>);
    // ... etc
};
```

Wire format and (de)serialization in `messages.h`. Hand-written; no protobuf dependency. Messages are tiny (≤ ~256 B except WITNESS payload).

### 3.4 `clear/witness_codec.{h,cpp}` — new

Density-aware bitmap encoding. Inputs: `recv_bitmap` of N bits + `recv_count`. Outputs `(encoding_tag, bytes)`:

- N ≤ 4096 → RAW (512 B).
- N > 4096 and missing density < 5 % → RLE.
- N > 4096 and missing dense → RANGE list.
- recv_count == 0 or recv_count == N → FULL (1-byte tag, no payload).

Pure function, gtest-able with no RDMA.

### 3.5 `clear/finalizer.{h,cpp}` — new

The state machine in §3.3 of [clear-design.md](clear-design.md). One instance per receiver-rank. Inputs: `RatioExitEvent` from `ratio_controller`, `WitnessMsg` from peer (if not co-located), `policy` from Python. Output: `FinalizeMsg` over control plane + a `final_mask` blob handed back to Python via callback.

Owns `repair_budget_bytes` accounting; budget is per-step, refilled at step boundary by the Python hook.

### 3.6 `clear/rq_monitor.{h,cpp}` — new

Maintains `posted_recv_credits[peer]`. Two callbacks:
- `on_low_watermark(peer)` → triggers `BACKPRESSURE` control message OR forces fallback for in-flight buckets to RC.
- `on_replenish(peer, k)` → posts k RWRs to receive QP.

### 3.7 `python/semirdma/clear/manifest.py` — new

Post-warmup `bucket → bucket_seq` stable id. Algorithm:

```python
def build_manifest(model: nn.Module, ddp: DistributedDataParallel) -> Manifest:
    # run a dry forward+backward to let DDP construct buckets
    # then walk bucket -> [param_id, ...] and assign deterministic bucket_seq
    # by sorted tuple of param_ids (cross-rank-stable hash)
    ...
```

`uid` is `hash((rank_pair, step_seq, bucket_seq, phase_id, peer_edge))` truncated to 64 bits.

### 3.8 `python/semirdma/clear/policy.py` — new

Wraps Phase 4's `LossToleranceRegistry`. Adds `policy: Literal["repair-first","mask-first","stale-fill","estimator-scale"]` per `bucket_seq`. Default policy decision rule:

```
if any param in bucket is BN/LN/embedding: policy = repair-first
elif p_L >= 0.05: policy = mask-first
elif p_L >= 0.01: policy = mask-first (or stale-fill if optimizer state)
else: policy = repair-first
```

Tunable via YAML. Also feeds Phase 5 E5 ablation.

### 3.9 `python/semirdma/clear/shadow_oracle.py` — new

Sampling shadow-RC oracle. Per-step, with prob `oracle_sample_rate ∈ [0, 0.1]`, send the same bucket bytes on a parallel RC channel and stash the reference. After FINALIZE, byte-compare the post-mask buffer against the oracle to compute `false_attribution_rate`. Default off in production runs; on for E0–E2.

### 3.10 `python/semirdma/hooks.py` — amend

Add `clear_allreduce_hook(state, bucket) -> Future`. Calls into `clear/runtime.py`. Existing `semirdma_allreduce_hook` and `layer_aware_dispatcher_hook` untouched.

## 4. Migration order (W1–W3)

This sequencing keeps the tree green at every step.

1. **W1.1** Add `clear/messages.h` + `clear/witness_codec.{h,cpp}` + unit tests. No transport touched.
2. **W1.2** Add `clear/lease_table.{h,cpp}` + unit tests. No transport touched.
3. **W1.3** Add `clear/control_plane.{h,cpp}`; wire to a new RC QP allocated alongside the existing UC QP. Add a "CLEAR no-op" mode that brings up the RC QP but does no transfers; verify with `ibv_devinfo`-style diagnostic test.
4. **W2.1** Amend `ratio_controller`: add `(slot,gen)` overload behind a feature flag. Old `(bucket_id)` overload still works, still passes all PR-C tests.
5. **W2.2** Add `clear/finalizer.{h,cpp}` + `clear/rq_monitor.{h,cpp}` + bindings.
6. **W2.3** Python side: `clear/manifest.py`, `clear/policy.py`, `clear/runtime.py`, `clear_allreduce_hook`. Loopback E2E test passes.
7. **W3.1** Two-rank integration test on amd247/amd245. Validate `semantic_mismatch_rate ≈ 0`, `false_attribution_rate < 1e-4`.
8. **W3.2** Shadow-RC oracle path; turn it on in E0 fixture.
9. **W3.3** Tag `v0.5.0-clear-T1` (witness + mask-only finalize, no selective repair). Branch to E0–E3 experiments.

## 5. What we are NOT changing in W1–W3

- `uc_qp_engine.cpp` — keep UC bring-up exactly as-is.
- `chunk_manager.cpp` — keep bitmap + buffer ownership; just expose a const view of `recv_bitmap` to the witness codec.
- Phase 4 hooks (`semirdma_allreduce_hook`, `layer_aware_dispatcher_hook`) — no edits; they remain ablation rows.
- XDP middlebox (`scripts/cloudlab/middlebox_setup.sh`, `xdp_dropbox.bpf.c`) — unchanged. CLEAR experiments use the same drop injection.
- All existing `experiments/stage_a/*` and `experiments/stage_b/*` drivers — unchanged.

## 6. CMake changes

Add a single new library target:

```cmake
add_library(clear_transport
    src/transport/clear/lease_table.cpp
    src/transport/clear/control_plane.cpp
    src/transport/clear/witness_codec.cpp
    src/transport/clear/finalizer.cpp
    src/transport/clear/rq_monitor.cpp
    src/transport/clear/metrics.cpp
)
target_include_directories(clear_transport PUBLIC src/transport src/transport/clear)
target_link_libraries(clear_transport PUBLIC semirdma_transport)
```

`semirdma_transport` (existing target) does not link `clear_transport`; the binding layer pulls both. This keeps the legacy library buildable even if CLEAR is disabled.

## 7. Test plan summary

| Layer | Test | Pass criterion |
|---|---|---|
| Unit (C++) | `clear_witness_codec_test` | RLE / RANGE / FULL roundtrip on 50k random bitmaps |
| Unit (C++) | `clear_lease_table_test` | 1M acquire/release cycles, no double-issued slots; PREBEGIN drain correctness |
| Unit (C++) | `clear_finalizer_test` | All 4 policies × 6 witness scenarios produce expected `FinalizeMsg` |
| Unit (C++) | `clear_control_plane_test` | Loopback RC QP roundtrip on every message type |
| Integration (Py, loopback) | `clear_loopback_test` | 1k buckets, recv_bitmap matches |
| Integration (2-rank) | `clear_rank_consistency_test` | `semantic_mismatch_rate == 0` over 10k buckets |
| Integration (2-rank) | `clear_slot_wrap_test` | 5000 buckets (slot wraps ~20×, gen wraps ~1×); zero alias |
| Integration (2-rank) | `clear_prebegin_race_test` | Inject artificial RC delay; UC-before-BEGIN handled |
| Integration (2-rank) | `clear_rq_starvation_test` | Backpressure triggers; no fatal Write-with-Imm error |
| Regression | Existing PR-C 18-cell matrix | bit-for-bit identical results when `mode != clear` |

## 8. Estimated effort

Numbers below are working days for a single engineer (you), assuming Phase 4 code familiarity. Italicized ranges absorb usual cluster availability slop.

| Block | Estimate |
|---|---|
| 3.1 `ratio_controller` amend + tests | 1 d |
| 3.2 `lease_table` + tests | 1 d |
| 3.3 `control_plane` + tests | 2 d |
| 3.4 `witness_codec` + tests | 0.5 d |
| 3.5 `finalizer` + tests | 1.5 d |
| 3.6 `rq_monitor` + tests | 0.5 d |
| 3.7–3.10 Python side | 2 d |
| pybind11 binding amends | 1 d |
| 2-rank integration + bug closure | *2–4 d* |
| **Subtotal W1–W3** | **~12 days** (≈ 2.5 calendar weeks) |
| E0 microbench | 2 d |
| E1+E2 stress | 2 d |
| E3 convergence sweep | 3 d (mostly compute) |
| **Subtotal W4–W7** | **~7 days** |

Total CLEAR T1 to first publishable data: ~4 calendar weeks. Matches Phase 5 timeline (W1–W7).

## 9. Sign-off questions

Before W1 starts, please confirm:

1. **Numbering** — accept `docs/phase5/` (proposed) or rewrite-in-place over Phase 4? Recommend Phase 5.
2. **T1 scope** — witness + mask-only finalize is acceptable as the publishable floor? (T2 selective repair adds ~1 wk; T3 ablations add ~1–2 wk.)
3. **Single finalizer vs symmetric** — start with single (Option A) and treat symmetric (Option B) as future work? Recommend yes.
4. **Cluster** — stay on amd247/amd245/amd264 (CX-5 25 GbE) for entire Phase 5? Or move to d7525 100 GbE for E3 convergence?
5. **Comparator** — add MLT-style UDP comparator (~1 wk) or only cite in related work? Recommend cite-only for INFOCOM submission; add for camera-ready.
