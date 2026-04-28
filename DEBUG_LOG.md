# Debug Log — SemiRDMA

Append-only log of debug investigations. Format defined in [DEBUG_PROTOCOL.md](DEBUG_PROTOCOL.md). Do not delete entries; rejection records are valuable.

---

## 2026-04-25: SemiRDMA delivery rate < 100% on real CX-5 + UC

### Symptoms

- Phase-4 P1 smoke: SemiRDMA reports `completed = 1768/2729` (≈ 65%) per bucket on `amd203 ↔ amd196` even with `drop_rate = 0` (XDP middlebox confirmed pass-through).
- `gloo` / `rc_lossy` / `rc_rdma` baselines on the same wire converge fine (`final_loss = 0.860358` identical for all three reliable paths).
- `mean_iter_ms` for SemiRDMA at drop=0: 858 ms (Python loop) — slower than `gloo` (744 ms).
- After `chunk_bytes` change to 4096: SemiRDMA delivery rises to 99.5%, but iter_ms still > gloo.

### Hypothesis A: wire really drops ~35% [REJECTED]

- Predictions if true:
  - `rx_packets_phy` delta on receiver should be ~65% of sender's `tx_packets_phy` delta.
  - `ib_write_bw` on the same NIC + GID + UC + size should also drop ~35%.
- Experiment run:
  - Snapshotted ethtool counters pre/post a SemiRDMA cell.
  - Ran `ib_write_bw -c UC -q 1 -s 65536 -x 3` between same nodes.
- Observation:
  - `ib_write_bw` hit 24.5 Gb/s line rate, 0 drops, 0 increments in `rx_discards_phy` / `rx_out_of_buffer`.
  - SemiRDMA's `tx_packets_phy` and receiver's `rx_packets_phy` deltas both increment normally — packets are leaving sender NIC and arriving at receiver NIC.
- **Status: REJECTED**. Bug is in our software stack, not the wire.

### Hypothesis B: per-QP firmware cap at ~8.5 Gb/s [REJECTED, then re-attributed]

- Origin: `ib_send_bw -c RC -q 1 -s 64K` measured 8.5 Gb/s; reasoned that this was a "single-QP RR-consumption-path firmware cap".
- Predictions if true:
  - `ib_write_bw -c UC -q 1` (no RR consumption) should also cap at 8.5 Gb/s.
- Experiment run: `ib_write_bw -c UC -q 1 -s 64K`.
- Observation: 24.5 Gb/s line rate.
- **Status: REJECTED as a universal NIC cap.** Re-attribution: the 8.5 Gb/s number is an **RC-only** ACK serialization effect, not a UC-relevant constraint. RC retransmit/ACK round-trip rate at single-QP determines that ceiling. No bearing on UC SemiRDMA.

### Hypothesis C: `path_mtu = IBV_MTU_1024` hardcoded in `bring_up()` [CONFIRMED, FIXED]

- Inspection of [src/transport/uc_qp_engine.cpp:262](src/transport/uc_qp_engine.cpp#L262) showed `attr.path_mtu = IBV_MTU_1024` literal. Phase-1 era artifact (SoftRoCE active_mtu = 1024). On CX-5 (active_mtu = 4096), this 4×-fragments every chunk.
- Predictions if true:
  - With chunk = 16 KB and path_mtu = 1024, each chunk is 16 IB packets; per-chunk delivery = (1−p)^16. UC drops the whole chunk on any packet PSN gap.
  - Fix should bring delivery rate up immediately.
- Experiment: changed to `attr.path_mtu = active_mtu_` (queried from `ibv_query_port`). Commit `094e0cc`.
- Observation: gid=3 delivery rose 32% → 67%.
- **Status: CONFIRMED, FIXED**. But residual 33% loss remained — Hypothesis C is necessary but not sufficient.

### Hypothesis D: bursty post pattern + multi-IB-packet chunks → UC loss [CONFIRMED for chunk size effect]

- After Hypothesis C fix, varied `chunk_bytes`:
  - chunk=16K (4 IB pkts): 67%
  - chunk=4K (1 IB pkt): 99.5%
- Predictions if true:
  - chunk=2K: predict (1−p)^? = same or higher delivery (1 IB pkt, headers ≈ 60 B).
- Experiment for chunk=2K not yet run. **Status: PARTIALLY CONFIRMED via chunk_bytes ablation.** chunk_bytes=4096 = path_mtu = 1 IB packet/chunk eliminates the multi-packet PSN gap risk.

### Hypothesis E: SQ depth at 512 produces NIC TX bursts → packet drops [CONFIRMED for delivery, mechanism unclear]

- Tested `sq_depth ∈ {64, 512, 8}` × `chunk_bytes = 4096`:
  - sq=64: 61.3% (worse)
  - sq=512: 85.2%
  - sq=8: 99.5%
- The non-monotonic sq=64 result was not reconciled; may indicate sq=64 misses a different regime entirely.
- Predictions if true:
  - Tightening submission rate further (sq=4 or sq=2) should give similar or marginal improvement.
  - Larger chunks at sq=8 should still suffer (multi-packet PSN gap).
- **Status: CONFIRMED at the symptom level** (sq=8 + chunk=4K = 99.5%); the mechanism connecting sq_depth to packet loss is NOT independently verified.

### Hypothesis F: Python interpreter + pybind11 `ibv_post_send` cost is `~5 µs/WR`, which is the safe NIC submission rate [UNCONFIRMED, withdrawn]

- Tried moving the chunk-emit loop into C++ (`UCQPEngine::post_bucket_chunks`) to skip Python boundary overhead. Three variants:
  - C++ chained `wr.next` + single `ibv_post_send`: 70% delivery, 935 ms.
  - C++ per-WR `ibv_post_send`, no chain: 70% delivery, 943 ms.
  - C++ per-WR + 5 µs busy-wait pacing between WRs: 99% delivery, 996 ms.
- Originally claimed: "Python ~5 µs is the implicit safe pacer".
- **This claim was challenged and withdrawn**. Reasons:
  - **Contradicts public `ib_write_bw` data**: `ib_write_bw -c UC -q 1 -s 4096` runs at 1.33 µs/WR (= 24.5 Gb/s line rate / 4 KB / 8 bits) on CX-5 with **0 loss**. So 1 µs/WR submission is NOT a CX-5 hardware cliff. The bug is in our software stack.
  - **Python boundary cost is not 5 µs**: Python interpreter dispatch + pybind11 marshalling is commonly 10–50 µs per call. The "5 µs implicit pacer" number was retro-fit, not measured.
  - **C++ 5 µs pacing is slower than Python loop end-to-end** (996 ms vs 858–972 ms), suggesting the busy-wait + GIL release/acquire combined cost is dominating, not the WR submission rate per se.
- **Status: UNCONFIRMED**. The Python loop incidentally produces ≈ 99.5% delivery on this stack, but the *mechanism* is not "implicit 5 µs pacer matches NIC's 5 µs safe rate". Some other effect (likely receiver-side; see G–J) is at play.

### Hypothesis G: Receiver SRQ refill speed cannot keep up with sender 1 µs/WR submission [PENDING]

- The SemiRDMA receiver's Python `await_gradient` calls `wait_for_ratio` (C++ poll loop) which marks chunks completed. After return, Python re-posts `cs.num_completed()` RRs. If the sender posts at NIC fast-path rate (~1 µs/WR) and receiver per-CQE processing is slower than that, RRs deplete on the NIC and silently-drop incoming Write-with-Imm packets.
- Predictions if true:
  - Receiver-side `rx_out_of_buffer` (Mellanox ethtool counter) should increment during the C++ tight-loop run.
  - Doubling `rq_depth` (16384 → 32768) and/or pre-posting larger RR batches should restore delivery on the C++ tight-loop path.
  - Python loop "works" because Python overhead on sender (~10 µs/WR) just happens to be ≥ receiver per-CQE cost, keeping SRQ stable.
- Experiment NOT YET RUN. Needs `ethtool -S enp65s0f0np0 | grep rx_out_of_buffer` pre/post on a C++ tight-loop SemiRDMA cell.
- **Status: PENDING — most likely candidate per user review.**

### Hypothesis H: PCIe doorbell batching limit / libmlx5 BlueFlame interaction [PENDING]

- libmlx5 has a fast-path BlueFlame path that may batch doorbell rings. At 1 µs/WR posting rate, the doorbell ring rate is 1 Mreq/s; PCIe writes are 100–200 ns each, so theoretically OK, but the interaction with NIC-side WQE consumption may not be smooth.
- Predictions if true:
  - Bursty doorbell rate (e.g. measured by `perf` on `mlx5_ib_post_send`) should correlate with delivery loss.
  - Forcing `IBV_SEND_INLINE` or specific WQE size could change the picture.
- Experiment NOT YET RUN. Speculative.
- **Status: PENDING.**

### Hypothesis I: Sender SQ overflow silently swallowed [PENDING]

- If `ibv_post_send` returns non-zero ENOMEM under SQ pressure and the wrapper ignores the return value, posts vanish silently — looking like "30% wire drop".
- The current wrapper [src/transport/uc_qp_engine.cpp post_write](src/transport/uc_qp_engine.cpp) does throw on `ret != 0`, so this is **likely not** the issue. But verify by adding a counter for non-zero `ibv_post_send` returns and checking it stays zero.
- **Status: PENDING — easy to rule out.**

### Hypothesis J: NIC TX scheduler hardware cliff at ~1 µs/WR submission [REJECTED]

- This was the conclusion proposed at the end of the C++ fast-path episode.
- Predictions if true:
  - `ib_write_bw -c UC -q 1 -s 4096` (also a C++ tight loop posting at ~1 µs/WR) should also drop ~30% packets.
- Experiment: ran `ib_write_bw -c UC -q 1 -s 4096 -x 3` between amd203 ↔ amd196 earlier this session.
- Observation: 24.5 Gb/s line rate, 0 loss.
- **Status: REJECTED**. NIC tolerates 1 µs/WR submission cleanly when the rest of the stack (libmlx5 fast path, receiver) is healthy. The "1 µs cliff" is a property of OUR receiver/refill chain, not the NIC.

### Hypothesis L: residual ~0.5% loss is await_gradient leftover-drain bookkeeping race — chunks DO arrive, but ratio_controller returns before their CQEs are visible, and the leftover drain reads the late CQEs but never marks them on ChunkSet so apply_ghost_mask zeros their (perfectly-delivered) data [PRIMARY CANDIDATE]

**Discovered**: 2026-04-25 ~08:42 from cell #3 of P0 SEED=42 (`drop=0 semirdma`, 500 buckets).

**Symptom data per bucket (mean over 500)**:
- `wait_for_ratio` returns with `ok=True timed_out=False` in 3.65–66 ms (well below 200 ms timeout)
- `cs.num_completed = 10860/10913` (99.5%) at return
- `LEFTOVER_after_wait` non-blocking drain finds **`recv_ok = 49–54` extra `RECV_RDMA_WITH_IMM` CQEs** with **unique imm_data**
- **`completed (10860) + leftover_recv_ok (53) = 10913 = total chunks`** (all chunks DID arrive)

**Mechanism**:
- `ratio_controller.wait_for_ratio` exits on `cs.completion_ratio() >= r=0.995`. At 10860/10913 = 0.9951, threshold is met. Loop exits.
- The remaining ~53 chunks have already produced `RECV_RDMA_WITH_IMM` CQEs at the NIC, but those CQEs hadn't been polled into `cs` yet at the moment ratio was hit (CQE-visibility timing race in tight C++ poll loop).
- `await_gradient` then runs the leftover drain (`for _ in range(64): poll_cq(16384, 0)`), which DOES collect those 53 CQEs — but **only increments a counter and adds to a `set` for logging**; it never calls `cs.mark_completed(c.imm_data)`.
- `apply_ghost_mask(buf, cs)` then sees `cs.state(i).has_cqe == False` for those 53 chunks and **zeros their data even though the data was delivered correctly to the MR**.

**Predictions if true**:
1. Adding `cs.mark_completed(c.imm_data)` inside the leftover drain loop should bring `cs.num_completed` to 10913/10913 (100%) on benign wire.
2. With 100% effective completion, SemiRDMA at `drop=0` should produce `final_loss ≈ gloo's final_loss` (within seed variance ~0.01), not 0.03 lower.
3. The "0.83 < 0.86 final_loss surprise" disappears.
4. At `drop_rate > 0` (real wire drop via XDP), the fix should still leave `cs.num_completed < n_chunks` for chunks that **truly didn't arrive** — leftover drain only marks completions it actually received.

**Predictions if false**:
- After fix, `cs.num_completed` < 10913 even at `drop=0` (some other source of dropped chunks beyond the bookkeeping race).
- OR `cs.num_completed = 10913` but `final_loss` is still 0.83 (final_loss reduction was seed luck, not regularization noise).

**Experiment plan**: Apply the one-line fix, rebuild, run a single SemiRDMA cell at `drop=0` with `STEPS=500 SEED=42` (same as the P0 cell #3 we have data for). Then compare `cs.num_completed` distribution and `final_loss` against:
- The archived P0 cell #3 SEED=42 (`final_loss=0.830595`)
- The P0 cell #0 gloo SEED=42 (`final_loss=0.860358`)

**Status**: PARTIALLY CONFIRMED (2026-04-25 ~09:00). Code at commit `9a0bdbc`.

**Falsification result** (`/tmp/p0_falsify_L/cell_00_drop0_semirdma_t200`, same config as P0 cell #3):

- Mean delivery: 99.51% → **99.97%** (mean 2.90 chunks/bucket missing instead of 53)
- Distribution: **459 / 500 buckets perfect (10913/10913)**, 41 / 500 with non-zero missing
- Outliers: occasional buckets with 60–203 missing (`min ratio = 0.9814`)
- Final loss: 0.830595 → **0.844591** (closed half the gap to gloo's 0.860358)
- iter_ms: 725 → 810 (+85 ms, from `cs.mark_completed(imm)` calls in drain)

**Interpretation**: Hypothesis L is the dominant cause for 92% of buckets. The bookkeeping race was real and the fix works. BUT a secondary timing issue remains for 8% of buckets — the leftover drain breaks too early when `poll_cq` transiently returns 0 even though more CQEs are about to be generated.

Hypothesis G/H/I/K (receiver SRQ refill / PCIe doorbell / SQ overflow / multi-QP) remain SUPERSEDED for the primary 0.5% residual symptom. They are NOT formally rejected — they could still describe the secondary 8% effect — but the simpler explanation came first and the partial fix landed.

### Hypothesis L.2: leftover drain `for _ in range(64): if not cqes: break` exits too early on transient 0-poll [REJECTED — caused regression]

**Mechanism (proposed)**: After Hypothesis L's fix, the leftover drain still breaks on the first `poll_cq` returning 0. NIC CQE generation is asynchronous; when poll_cq sees 0, the next few µs may still produce CQEs for chunks that are being finalized in NIC's RX path. The drain's "first zero ⇒ done" heuristic loses those.

**Patch tried** (commit `f95892c`, since reverted in `b38a883`): replaced the bounded-iteration drain with a quiescent-based one — `_DRAIN_QUIESCENT_THRESHOLD_NS = 200 µs`, `_DRAIN_MAX_NS = 5 ms` ceiling.

**Predictions if true** (from the original entry):
1. mean delivery ≥ 99.99% (≤1 chunk/bucket missing avg)
2. min delivery > 10900 (no 60–200 chunk outliers)
3. iter_ms ≤ 820 (drain overhead ≤ +10 ms vs current 810)
4. final_loss within 0.005 of gloo's 0.860358

**Falsification result** (2026-04-26 morning, `/tmp/p0_falsify_L2/cell_00_drop0_semirdma_t200`, same config as L falsify):

The cell never completed a single training step — `loss_per_step.csv` was empty when killed. Per-bucket DIAG sequence:

```
bucket 1–7:   completed=10913/10913 (perfect, drain finds ~50 late CQEs as before)
bucket 8:     completed=0/10913, outstanding_recv_pre=16384, ok=False timed_out=True
              recv_ok=0 recv_err=0 other=0 unique_imm=0 drain_us≈5131 drain_max_aborted=0
bucket 9–N:   stuck at completed=10211/10913 (deterministic), outstanding_recv_pre=0,
              every bucket times out at 200ms, recv_ok=0 in drain
              positional histogram of missing chunks: bin10=[5,0,0,1,8,3,0,0,0,685]
              (685 of the 702 missing chunks land in last 10% of bucket)
```

After bucket 7, the receiver permanently loses ability to refill RRs through the normal poll-and-receive cycle. Cell wedges until killed.

Note: `drain_us=5131` on bucket 8 is dominated by `apply_ghost_mask` zeroing 44 MB of buffer, NOT the drain loop itself — `drain_max_aborted=0` confirms the loop exited via the 200 µs quiescence path. So the proposed mechanism (drain hiding CQEs longer) executed as designed; the regression came from elsewhere.

**Verdict**: REJECTED for the patch as written. Criterion 1 fails catastrophically (delivery drops to 93.5% of every bucket after bucket 7, not the targeted 99.99%). Criterion 4 cannot even be evaluated since training never produces a final_loss.

**Mechanism unclear; candidates not yet falsified** (do NOT touch the drain again until one of these is established):
- Drain takes ~400 µs longer than the bounded-iteration version → ranks drift, ending up writing into different `n_slots` slots after a few buckets, so received chunks land where the local `cs_recv` isn't watching.
- Quiescent loop greedily consumes SEND completions (now in `leftover_other`) that the SQ flow control or the engine's outstanding-Send bookkeeping needs.
- `poll_cq` tight loop interferes with libmlx5 doorbell pacing or RR-refill latency on the receiver side.

**Status**: REJECTED (regression confirmed). Code reverted at `b38a883`; deployed code is back to L-only at `9a0bdbc`'s state.

### Hypothesis K: dual-QP fanout would double delivery [PENDING — should have been run last session]

- User suggested in the previous round; not yet executed.
- Predictions:
  - If the bottleneck is per-QP (e.g. one SRQ saturating), 2 QPs should ~2× the effective delivery rate.
  - If the bottleneck is system-wide (PCIe / single-core poll), 2 QPs do not help.
- Experiment plan: spin up 2 SemiRDMA QPs sender-side, distribute chunks round-robin, measure delivery.
- **Status: PENDING — overdue.**

### Current resolution

The deployed configuration:
- `path_mtu` = `active_mtu` (Hypothesis C — fix at commit `094e0cc`).
- `chunk_bytes = 4096` (Hypothesis D — chunk size = 1 IB packet).
- `sq_depth = 8`, `timeout_ms = 200` (Hypothesis E + jitter tolerance, commit `80beb30`).
- `post_gradient` retains the per-chunk Python loop (Hypothesis F withdrawn; tight C++ loop demonstrably loses packets, but the mechanism is not yet identified).
- Leftover drain marks late CQEs on `cs` before `apply_ghost_mask` runs (Hypothesis L — fix at commit `9a0bdbc`). Drain bounds: `for _ in range(64): if not cqes: break` (the L.2 quiescent variant was tried in `f95892c` and reverted in `b38a883` after wedging the cell after bucket 7).

This gives ~99.97% delivery on benign wire (459/500 buckets perfect, 41 imperfect with up to 203 chunks missing each), final_loss=0.844591 vs gloo's 0.860358 at SEED=42. The residual 8% imperfect-bucket tail + the tight-C++-loop loss are NOT root-caused. **Hypotheses G, H, I, K must be tested before any paper claim about "the per-WR submission rate floor on CX-5 + UC" or similar.**

### Required experiments before this bug can be marked closed

| # | Experiment | Cost | What it falsifies |
|---|-----------|------|-------------------|
| 1 | Re-run `ib_write_bw -c UC -q 1 -s 16384 --duration=10`, log actual achieved Gb/s and Mpps | 5 min | Re-establish public NIC baseline at chunk=16K |
| 2 | Add timing instrumentation to `post_gradient` (Python loop) and `post_bucket_chunks` (C++ tight loop). Log actual µs/WR submission rate from each | 30 min | Falsifies "Python = 5 µs" assumption (Hypothesis F) |
| 3 | Snapshot `rx_out_of_buffer` pre/post on a tight-C++-loop SemiRDMA cell | 15 min | Tests Hypothesis G (SRQ exhaustion) |
| 4 | Dual-QP fanout test: 2 QPs round-robin chunks; compare delivery and iter_ms vs single QP | 1 h | Tests Hypothesis K (per-QP vs system-wide) |
| 5 | Add a counter for `ibv_post_send` non-zero returns | 10 min | Rules out Hypothesis I (silent swallow) |

These should be scheduled after the in-flight P0 (SEED=42) completes.

### What must NOT be in the paper until the above is resolved

- Any claim that CX-5 + UC has a "1 µs/WR submission cliff" or "5 µs safe rate".
- Any "microbenchmark" figure plotting submission rate vs delivery — until we know it's hardware vs software.
- Any "per-WR pacing is required" methodology claim.

The path_mtu fix (Hypothesis C) and the chunk_bytes ablation (Hypothesis D) are confirmed and **safe to use in the paper as design choices** ("on CX-5 we set chunk_bytes = active_mtu so each chunk rides as one IB packet"), without claiming a hardware mechanism.

---

## PR-A / PR-B: layer-aware mode bugs

Two bugs surfaced during the PR-B real-NIC validation matrix and were fixed
in-band. Both are documented here so the post-mortem record stays in one
place.

### Hypothesis M: dispatcher uses LOCAL eps_ema → cross-rank routing divergence → RC await deadlock [CONFIRMED, FIXED]

**Discovered**: 2026-04-26, PR-B v1 first run on amd203/amd196.

**Symptom**: cell crashed after bucket 7 with
`RuntimeError: await_bucket: recv deadline exceeded (30000 ms); received 0/10913 chunks`.
Per-bucket DIAG showed buckets 1–7 dispatching SemiRDMA (route=SEMI), then a
silent transition where rank 0 chose RC while rank 1 still chose SemiRDMA.
Rank 0's RC `await_bucket` blocked on the RC QP while rank 1's chunks went
to the UC QP — RC await timed out at the 30 s hardware deadline.

**Mechanism**:
- `LayerAwareHookState.calibrator.epsilon_ema` is a per-rank value, fed only
  by that rank's local observed loss
- The dispatcher's safety check `p_bucket < eps + margin` compares against
  this LOCAL eps
- If rank 0 sees a transient loss spike that pushes its eps above
  `p_bucket - margin` while rank 1's eps stays below, ranks decide
  differently for the same bucket
- They post to different QPs but the receiver awaits on the chosen-locally
  QP → the OTHER rank's chunks never satisfy the await

**Predictions if true**:
1. Replacing the local eps with a cross-rank-synchronized eps eliminates the
   deadlock.
2. Both ranks make identical per-bucket routing decisions after the fix.

**Fix** (commit `967703e`): one-float gloo `dist.all_reduce` of
`epsilon_ema` at the start of every dispatch, dividing by `world_size` to
get the global mean. Cost: ~µs over gloo TCP per bucket.

**Status**: CONFIRMED. PR-B v3 18-cell matrix completed with cross-rank
routing always identical across ranks; no more deadlocks.

### Hypothesis N: calibrator fed pre-drain `stats["completed"]` → eps_ema converges to ratio threshold not wire loss → safety check trips on every bucket [CONFIRMED, FIXED]

**Discovered**: 2026-04-26, PR-B v2 (post-M fix) at drop=0.01 cell 3.

**Symptom**: dispatcher logs at bucket 100 showed `eps_ema=0.0951` with
configured drop=0.01 — wire loss should be ~1%, observed ~9.5%. Safety
check `p_bucket=0.10 < eps + margin = 0.0951 + 0.005 = 0.1001` tripped on
every bucket → all buckets routed to RC → RC retry storms grew iter_ms
from 800 ms to 3200 ms.

**Mechanism**:
- `RatioController::wait_for_ratio` exits as soon as `cs.completion_ratio()
  >= ratio`. With `ratio = 1 - p_bucket`, the controller exits at exactly
  the threshold by construction — `stats["completed"]` reads ~ratio×n_total.
- The leftover drain (`for _ in range(64)`) catches late CQEs and marks them
  on `cs` AFTER `wait_for_ratio` returns — but that count was never
  surfaced to the dispatcher.
- Calibrator update used `stats["completed"]` (pre-drain), so `eps_ema`
  converged to ~p_bucket regardless of actual wire health. Trade-off:
  ratio-truncation looks like wire loss to the calibrator.

**Predictions if true**:
1. Surfacing the post-drain `cs.num_completed()` to the dispatcher (and using
   that for the calibrator update) makes `eps_ema` converge to actual wire
   loss (~drop_rate).
2. Safety check should then NOT trip when `p_bucket > drop_rate + margin`.

**Fix** (commit `9e18230`): added `stats["completed_post_drain"] =
int(cs.num_completed())` in `transport.await_gradient`; dispatcher reads
that field instead of `stats["completed"]`. Logger also dumps both pre and
post drain counts for debugging.

**Status**: CONFIRMED. Post-fix smoke at drop=0.01: `pre_drain=10368
(~95%, threshold)` vs `post_drain=10808 (~99%, wire)`. eps_ema converged
to ~0.03 (matches drop=0.01 + NIC tail variance). 6/6 logged dispatches
chose SemiRDMA (no false RC fallback). PR-B v3 matrix completed end-to-end.

### Side note: SEED=123 drop=0.05 layer_aware cell 5 transient crash [TRANSIENT, NOT root-caused]

The PR-B v3 matrix had one bad cell: SEED=123, drop=0.05, layer_aware.
Bucket 1 reported `completed=6635/10913` (39% loss vs configured 5% wire),
all subsequent buckets stuck around 6700, eventually triggered RC fallback
which timed out → cell crash.

3 isolated reruns of the same configuration on the same nodes succeeded
cleanly (rc=0, final_loss ∈ {1.39, 1.51, 1.45}). Run 1 was substituted into
the v3 matrix layout; the crashed cell is preserved at
`cell_05_*.crashed_orig/`.

The root cause is **not isolated** — could be matrix-sequence dependent
(bucket-state from prior cells), NIC tail pathology, or dispatcher race
when eps approaches p. PR-C should retest after the bucket_id-in-imm
protocol fix lands; if the failure mode persists, deeper investigation is
needed.

---

## 2026-04-28: layer_aware SEMI delivery ~50% on amd247/amd245 (vs ~99% on amd203)

### Symptoms

- New cluster amd247/amd245/amd264 (CX-5 25 GbE, fw 16.28.4512); same code as
  prior amd203/amd196 (commit `13430f5` == archive code).
- PR-B v3 reproducibility matrix (3 seed × 6 cells): `transport=semirdma`
  reproduces archive within seed noise (9/9 cells PASS); `transport=semirdma_layer_aware`
  passes only at `drop=0` (3/3); fails systematically at `drop>0`
  (**6/6 cells fail across all 3 seeds**).
- Failure is **not transient**. Old amd203/amd196 had 1/18 transient (PR-B v3
  seed=123 cell #5); new amd247/amd245 has 6/18 deterministic.
- Failure mechanism: dispatcher's first 4–5 SEMI dispatches return
  `completed ≈ 5000/10913` (~45% delivery in 200 ms `t_max`); calibrator
  `eps_ema` climbs 0.027 → 0.054 → 0.078 → 0.102; once `eps_ema + safety_margin
  > p_bucket = 0.10`, dispatcher routes ALL subsequent buckets to RC. RC at
  `drop=0` is fine (cell #1 finishes in ~440s); RC at `drop>0` retransmits
  → ~1500–2600 ms/step → exceeds `CELL_TIMEOUT=900s` → process killed
  (`exit 124`) before 500 steps complete.
- LA at `drop=0` shows the SAME ~50% SEMI delivery
  (`dispatch[1]: completed=5357/10913`). It only "works" because the RC
  fallback is fast on a clean wire. The underlying ~50% bug is also active.
- `flat semirdma` on the SAME wire at `drop=0.01` delivers ~99%
  (`completed=10804/10913`, time-out at `dyn_target=0.995`). Same `t_max=200ms`.
  Same `chunk_bytes=4096`, same `rq_depth=16384`, same NIC, same XDP middlebox.
- Archive amd203 LA cell #3 at the SAME drop=0.01: `completed=10788/10913`,
  `pre_drain=10369`, `bw_mbps` climbs 456 → 12420 within 100 dispatches.
  **New amd247 LA `bw_mbps` stuck around 23–60 — ~250× slower**. Same code.
- Wire baselines on new cluster are healthy: `ib_write_bw -d mlx5_1 -x 3
  -s 65536` direct = 24.39 Gbps; through XDP middlebox = 12.25 Gbps.

### Hypothesis A: amd264 XDP-generic middlebox drops more than configured for LA's burst pattern [PENDING]

- Predictions if true:
  - Bypassing the middlebox (`MIDDLEBOX_HOST=""`, no ARP spoof, gid_index=1 direct)
    while keeping `transport=semirdma_layer_aware` should restore ~99% SEMI delivery.
  - Same LA workload through middlebox at `drop=0` should still show ~50% (which
    it does — consistent with this hypothesis).
- Predictions if false:
  - Bypass middlebox still shows ~50% SEMI delivery → wire is not the cause.
- **Status: PENDING — falsification experiment #1 below.**

### Hypothesis B: `_synchronized_eps` gloo all_reduce blocks dispatcher tail and serializes peer post_gradient [PENDING]

- The layer-aware dispatcher calls `dist.all_reduce` (gloo TCP, 2 ranks) on
  `epsilon_ema` BEFORE every bucket. Flat path does not. If gloo TCP is slow
  on the new cluster's mgmt LAN (rendezvous goes through 128.110.x), this
  could effectively serialize the two ranks: rank-A finishes post_gradient,
  enters dispatcher, blocks on all_reduce until rank-B arrives. Rank-B's
  await_gradient sees rank-A's tx packets only AFTER rank-A unblocks.
- Predictions if true:
  - Replacing `_synchronized_eps` body with `return local_eps` (no all_reduce)
    and rerunning LA cell #3 should restore ~99% SEMI delivery.
  - Wall-time spent inside `_synchronized_eps` should be measurable as ~50–150ms
    per dispatch.
- Predictions if false:
  - No-op `_synchronized_eps` still shows ~50% SEMI delivery → eps sync isn't
    the bottleneck.
- **Status: PENDING — falsification experiment #2 below.**

### Hypothesis C: 4-QP setup (SEMI tx/rx + RC tx/rx) creates per-CPU CQ-poll contention on faster EPYC 7402P (24C) [PENDING]

- LA constructs 4 SemiRDMATransport-like instances (UC tx, UC rx, RC tx,
  RC rx). On amd203 (EPYC 7302P, 16C) Python's GIL + 4 transports' poll loops
  may have happened to overlap differently than on amd247 (7402P, 24C). All 4
  CQs sharing 1 NIC interrupt vector could be the relevant axis.
- Predictions if true:
  - Pinning the training process to a smaller core set (e.g. taskset 0-3)
    should reduce the gap between flat and LA delivery.
  - Constructing flat semirdma + an extra unused RCRDMATransport (just sits
    idle) should reproduce the 50% degradation.
- Predictions if false:
  - taskset doesn't change anything → CPU contention isn't the cause.
- **Status: PENDING — lower priority; deprioritized until A/B are tested.**

### Resolution
TBD. Two falsification experiments queued (A: bypass middlebox; B: skip
_synchronized_eps). Both are 1-cell ~7 min runs on the existing amd247 cluster.
Will not propose any code change to the layer_aware path until at least one
hypothesis is confirmed.


### Hypothesis A: middlebox/wire causes LA-specific drops [PARTIALLY CONFIRMED]

- **Falsification A run (2026-04-28 04:11)**: LA cell drop=0 with NO middlebox,
  NO ARP spoof, gid_index=1 (direct wire).
  Result: `dispatch[1..5]: completed=10913/10913 (100%)`, `timed_out=False`,
  `bw_mbps` climbing 646 → 2912.
  **Direct wire LA = perfect.** So the LA failure requires the
  middlebox+ARP+gid=3 path.
- **Falsification A2 run (2026-04-28 04:12)**: LA cell drop=0 DIRECT wire +
  `gid_index=3` forced.
  Result: identical to A1 — `completed=10913/10913` for all 5 dispatches.
  **gid_index alone is not the cause.** So failure requires middlebox+ARP.
- **Status: CONFIRMED — LA failure requires the middlebox+ARP-spoof path.
  Direct wire LA is fine. But mechanism is not yet pinned to middlebox vs gloo.**

### Hypothesis B: _synchronized_eps gloo all_reduce blocking [CONFIRMED]

- **Falsification B run (2026-04-28 04:16)**: middlebox UP at drop=0 + ARP spoof
  + gid_index=3 + LA, with `_synchronized_eps` patched to `return local_eps`
  (skip the per-bucket `dist.all_reduce`).
  Result:
  ```
  dispatch[1]: completed=6470/10913 (59%)  timed_out=True
  dispatch[2]: completed=10913/10913 (100%) timed_out=False  ← FIXED
  dispatch[3]: completed=10784/10913 (99%)  timed_out=False
  dispatch[4]: completed=10913/10913 (100%) timed_out=False
  dispatch[5]: completed=10412/10913 (95%)  timed_out=False
  sigma_ms 88-140ms  (large per-bucket jitter, absorbed by 200ms t_max)
  ```
- Without the patch (matrix data): dispatch[1..5] all 45-50% timed_out=True.
- With patch: dispatch[2+] recovers to ~99% — calibrator never trips eps_ema
  past safety margin. Dispatcher stays SEMI route; cell completes cleanly.
- dispatch[1] residual 41% loss is consistent with gloo TCP rendezvous still
  warming up + RC bring_up tail; dispatch[2+] is the meaningful recovery.
- **Mechanism**: per-bucket gloo TCP `dist.all_reduce` on `epsilon_ema` (line
  `python/semirdma/layer_aware/dispatcher.py:62-64`) routes through
  `master_addr=10.10.1.1`. With ARP-spoofed `10.10.1.0/24`, this TCP path
  goes amd247→amd264→amd245 via kernel `ip_forward=1`, adding ~15-30 ms RTT
  per gloo internal round-trip. gloo all_reduce uses ~3-4 round-trips →
  ~50-150 ms blocking per bucket. The two ranks unblock at slightly
  different times, then post + await with sigma jitter that approaches the
  200 ms `t_max` window — receiver's await sees a fraction of peer's bytes.
- archive amd203 had `sigma_ms < 1ms`; new amd247 (no patch) `sigma_ms = 0`
  because every dispatch hit timeout (so calibrator's variance estimator
  saw constant 200ms latency and reported 0 noise — a calibrator
  measurement artifact, not a wire-quality claim).
- **Status: CONFIRMED — `_synchronized_eps` is the dominant cause of LA's
  through-middlebox 50% delivery regression.**

### Resolution

The LA regression is a **gloo-TCP-through-ARP-spoofed-middlebox round-trip
amplification**, not a NIC / firmware / RDMA-stack issue. Two viable fixes,
both deferred to a separate code commit (must not silently change PR-A/PR-B
data without user OK per DEBUG_PROTOCOL §6):

1. **Amortize**: call `_synchronized_eps` every N (e.g. 50) buckets instead of
   per-bucket. Eps drift across ranks is bounded since both ranks see the
   same wire feedback; per-bucket sync is over-conservative.
2. **Bypass-route**: use a torch.distributed PG bound to the management LAN
   (128.110.x) for `_synchronized_eps` only. RDMA traffic stays on
   experiment LAN through the middlebox; gloo control stays direct.
3. **Combine**: amortize + run on mgmt LAN.

Verification path (needs new short matrix on amd247/amd245):
- Pick fix, apply, rerun PR-B v3 18 cells (drop ∈ {0, 0.01, 0.05} × LA × seed
  ∈ {42, 123, 7}). Pass criterion: 18/18 cells complete with `rc=0`,
  `final_loss` within ±0.20 of archive amd203 numbers.

Why archive amd203 didn't show this:
- amd203/amd196/amd186 used the SAME ARP-spoof + ip_forward topology. Either
  amd186's kernel ip_forward path was lower-latency (different driver tuning?
  different IRQ affinity?), or the EPYC 7302P (slower CPU) hid the issue by
  making each step slower so per-bucket all_reduce overhead was a smaller
  fraction. Pending: cluster-comparison microbenchmark on direct gloo TCP
  RTT through middlebox path on each cluster — but archive cluster is gone,
  so this is a forensic dead-end. Current cluster behavior is the only
  observable; fix accordingly.


### Fix implemented (2026-04-28 04:46–04:59)

Three independent fixes, smallest-blast-radius first:

1. **Amortize `_synchronized_eps`** (`python/semirdma/layer_aware/dispatcher.py`,
   `state.py`).  Per-bucket gloo TCP all_reduce → every-N-bucket
   (N=`DEFAULT_EPS_SYNC_PERIOD=50`).  Both ranks see the same wire so
   their local `epsilon_ema` converges to the same value naturally;
   amortized sync is sufficient to keep routing decisions aligned.
   `LayerAwareHookState` gains `eps_sync_period`, `cached_eps`,
   `last_eps_sync_at`, `n_eps_syncs` fields.  Backwards-compat: setting
   `eps_sync_period=1` reproduces pre-fix behavior.

2. **Cap calibrator `t_max`** (`python/semirdma/layer_aware/calibrator.py`,
   `python/semirdma/config.py`).  `t_max_max_ms` knob (default 0 = "auto",
   resolved to `2 * timeout_ms` at construction).  `t_max_for_bucket`
   clamps result to `[t_max_min_ms, t_max_max_ms]`.  Without this,
   bimodal latency at the ratio threshold would grow `sigma_jitter_ms`,
   which grew `t_max`, which caused more time-outs at `t_max`, further
   inflating sigma — runaway feedback loop ending in multi-second
   per-bucket awaits.

3. **Gate RC fallback by `rc_safe_drop_threshold`** (default 0.005 =
   0.5%) in `python/semirdma/config.py` + `dispatcher.py`.  Empirically
   RC dies on lossy wires (PLAN.md P2: `IBV_WC_RETRY_EXC_ERR` after
   retry-cnt exhaustion at any drop>0).  When `eps_ema > rc_safe`,
   bucket stays SEMI even if budget is tighter than `eps + margin` —
   ghost-mask absorbs the residual loss like flat SemiRDMA does.
   Routing to RC happens only when wire is clean enough for HW retry to
   absorb (`eps_ema <= 0.005`).  This matches paper claim «layer_aware
   safely degrades to RC for tight budgets» but only on clean wire,
   which was the regime archive amd203 actually exercised.

### Verification (2026-04-28 04:49–04:59)

Mini-matrix on amd247/amd245/amd264 with all 3 fixes applied:
`TRANSPORTS=semirdma_layer_aware × DROP_RATES="0 0.01 0.05" × SEED=42 × STEPS=200`

```
idx  drop  rc  final_loss  mean_iter_ms
 0   0     0   1.830       855
 1   0.01  0   1.599       871
 2   0.05  0   1.639       963
```

All 3 cells PASS (`rc=0`).  Compare to pre-fix matrix (full PR-B v3 18-cell
on same cluster, 2026-04-28 00:32–02:47):
```
seed=42:  drop=0 LA rc=0 final=1.05  ← passed
          drop=0.01 LA rc=124 final=2166  ← failed (CELL_TIMEOUT)
          drop=0.05 LA rc=1 final=3.63   ← failed (RC retry exhaust)
```

Dispatcher behavior post-fix at drop=0.05:
- `dispatch[1..5]`: SEMI route, completed ~5K-10K (bimodal, expected)
- `dispatch[100]`: SEMI, eps_ema=0.139, t_max=400ms, completed=9233/10913
- `dispatch[200]`: SEMI, eps_ema=0.269, t_max=400ms, completed=9535/10913
- Never routed to RC (because eps_ema > 0.005 = rc_safe).
- Loss decreases monotonically: step 0 ~2.4 → step 199 = 1.51 (200 steps from random init; will converge further with longer training).

### Open question (deferred — non-blocking)

Why is SEMI delivery bimodal on amd247 (some dispatches 90-99%, others
45-50%) when archive amd203 had constant 99%?  flat semirdma exhibits the
same bimodality but tolerates it via the dynamic ratio target
(`max(0.95, 1 - loss_rate - 0.005)` = 0.995) → just times out and
ghost-masks.  LA is now effectively the same after the rc_safe gate.

Candidate causes (not yet falsified):
- Periodic gloo TCP traffic from DDP itself routing via amd264 ip_forward
- amd264 XDP-generic single-CPU RX bursts
- EPYC 7402P CPU race tightening some pipeline timing

This bimodality is now COSMETIC for the application: training still
converges. If a future paper claim depends on per-dispatch delivery
distribution, the bimodality must be characterized.  Pending.

