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

This gives ~99.5% delivery on benign wire and converges. The residual ~0.5% loss + the tight-C++-loop loss are NOT root-caused. **Hypotheses G, H, I, K must be tested before any paper claim about "the per-WR submission rate floor on CX-5 + UC" or similar.**

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
