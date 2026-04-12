# Phase 1 Analysis Report: UC QP Validation on SoftRoCE

**Date:** 2026-04-12
**Environment:** SoftRoCE (rxe0), single-machine loopback, Linux
**Goal:** Validate three core design assumptions of SemiRDMA before committing to implementation

---

## 1. Analysis Questions

| ID | Question | Design Component |
|----|----------|-----------------|
| Q1 | Does UC Write-with-Immediate generate a receiver-side CQE on SoftRoCE? | CQE-driven ratio control (RQ4) |
| Q2 | Does the receiver buffer retain stale data when a UC write fails silently? | Ghost gradient & masked aggregation (RQ2) |
| Q3 | What is the WQE posting rate across chunk sizes, and where is the bottleneck? | Write granularity optimization (RQ1) |

---

## 2. Key Findings

### Finding 1: CQE-driven completion tracking is FEASIBLE (Test 1 — PASS)

UC Write-with-Immediate on SoftRoCE produces a receiver CQE with:
- **Opcode:** `IBV_WC_RECV_RDMA_WITH_IMM` (correct)
- **imm_data:** `0xDEADBEEF` delivered intact (correct)
- **Buffer:** changed from `0xAA` → `0x42` (zero-copy write confirmed)
- **Sender CQE:** `IBV_WC_SUCCESS` with opcode `RDMA_WRITE`

**Design implication:** The core mechanism of SemiRDMA's ratio controller — counting receiver-side CQEs to determine `received_ratio` — is validated. This is a significant advantage over UDP-based approaches (MLT, OptiReduce) that must rely on timeouts or application-layer ACKs.

### Finding 2: Ghost gradient is a DIFFERENT VARIANT than hypothesized (Test 2 — PARTIAL)

**Hypothesized behavior:** When no Receive WR is posted, the entire Write-with-Immediate (both RDMA data and completion) is silently dropped. Buffer retains old data.

**Observed behavior:** The RDMA Write portion **succeeds** (buffer changed from `0x42` → `0xFF`), but **no CQE is generated** on the receiver side.

This reveals that Write-with-Immediate on SoftRoCE UC decomposes into two independent operations:

| Operation | Requires Receive WR? | Observed result |
|-----------|----------------------|-----------------|
| RDMA Write (data transfer) | No | **Succeeds** — data written to remote buffer |
| Immediate completion (CQE) | Yes | **Fails** — no CQE generated |

**Design implication — this is actually MORE interesting for the paper:**

The ghost gradient problem is not "stale data persists" but rather **"new data arrives without the receiver knowing about it."** In a real lossy network scenario with packet drops:

1. **Packet loss → PSN mismatch:** When a packet in a multi-packet RDMA Write is lost, subsequent packets are silently discarded by the receiver QP. The buffer ends up with **partial old + partial new data** — a corrupted ghost gradient.

2. **No-RQ-WR scenario (what we tested):** Data arrives completely, but the receiver has no CQE signal. In the actual SemiRDMA design, this case won't occur because we always pre-post Receive WRs. But it proves that the CQE mechanism is the **only** reliable delivery signal — you cannot trust buffer content alone.

**Next step required:** Test ghost gradient via `tc netem` packet loss injection to observe the true partial-write / PSN-mismatch behavior.

### Finding 3: WQE rate scales inversely with chunk size; throughput plateaus at ~500 MB/s (Test 3)

| Chunk Size | Time (ms) | WQE/s | Throughput |
|------------|-----------|-------|------------|
| 4 KB | 10.5 | 95,201 | 371.9 MB/s |
| 16 KB | 31.3 | 31,945 | 499.1 MB/s |
| 64 KB | 123.8 | 8,075 | 504.7 MB/s |
| 256 KB | 503.6 | 1,986 | 496.4 MB/s |
| 1 MB | 2,314.7 | 432 | 432.0 MB/s |

**Observations:**

1. **WQE/s decreases monotonically:** 95K (4KB) → 432 (1MB). Each WQE carries proportionally more data at larger sizes, so per-WQE processing overhead dominates at small sizes.

2. **Throughput peaks at 64KB (~505 MB/s):** This is the sweet spot where per-WQE overhead is amortized but single-WQE latency hasn't grown too large.

3. **Throughput drops at 1MB (432 MB/s):** Large writes on SoftRoCE become less efficient — likely due to memory copy overhead and kernel scheduling in the software emulation path.

4. **4KB throughput is notably lower (372 MB/s):** Per-WQE overhead is ~10.5 μs per WQE. At 4KB per WQE, this overhead is significant relative to the data transfer time.

**Design implications for RQ1 (Write granularity):**

- **Chunk size floor:** 16KB appears to be the practical minimum on SoftRoCE. Below 16KB, WQE overhead erodes throughput.
- **Optimal range:** 16KB–64KB gives the best throughput while keeping per-loss impact moderate.
- **Budget calculation example:** For a ResNet-50 layer with 2MB gradients at 64KB chunks = 32 WQEs per layer. At 8,075 WQE/s, one layer takes ~4ms — feasible for iteration times of 100ms+.
- **CRITICAL CAVEAT:** These numbers are for SoftRoCE (software emulation). Real ConnectX-5 hardware will have dramatically different WQE rates (likely 10–100x higher). The relative trends (throughput vs. chunk size) may also differ. These results inform code architecture but NOT final parameter selection.

---

## 3. Strongest Supported Comparisons

| Claim | Evidence Strength | Caveat |
|-------|------------------|--------|
| UC Write-with-Immediate generates receiver CQE | **Strong** (deterministic, single test sufficient) | SoftRoCE only; needs ConnectX-5 confirmation |
| CQE is the sole reliable delivery signal | **Strong** (Test 2 proves buffer content is not trustworthy for completion) | Different from packet-loss ghost gradient |
| 16KB–64KB is the SoftRoCE throughput sweet spot | **Moderate** (single run, 1000 iters, no variance data) | SoftRoCE-specific; HW numbers will differ |
| Ghost gradient from packet loss exists | **Not yet tested** | Needs tc netem experiment |

---

## 4. Main Caveats and Blockers

### Caveats

1. **SoftRoCE ≠ hardware RDMA.** All absolute performance numbers (WQE/s, MB/s) are SoftRoCE-specific. Relative trends may or may not transfer to ConnectX-5. Reviewers will ask about this.

2. **Single-run benchmark (Test 3).** No variance estimates, no confidence intervals. The WQE rate numbers are point estimates from one run of 1000 iterations each. For a validation test this is acceptable; for paper-quality claims it is not.

3. **Ghost gradient Test 2 tested a mechanism (no-RQ-WR) that differs from the real-world scenario (packet loss + PSN mismatch).** The finding is still valuable — it proves CQE is the only reliable signal — but the headline "ghost gradient exists" needs qualification.

### Blockers for Next Phase

| Blocker | Priority | Action |
|---------|----------|--------|
| Ghost gradient via packet loss not yet demonstrated | High | Add tc netem test (0.1%–5% loss rate) |
| No variance data for WQE benchmark | Medium | Re-run with 5+ seeds on CloudLab |
| SoftRoCE UC QP behavior may differ from ConnectX-5 | Medium | Validate on CloudLab hardware ASAP |

---

## 5. What Changed in Our Understanding

**Before Phase 1:**
- We assumed Write-with-Immediate is atomic: either both data and completion succeed, or both fail.
- We expected ghost gradient = stale buffer data.
- We had no empirical WQE rate data.

**After Phase 1:**
- Write-with-Immediate data transfer and CQE generation are **independent** on UC QP. Data can arrive without a CQE. This strengthens the argument for CQE-based tracking: it is the **only** reliable completion signal.
- Ghost gradient from packet loss (partial write + PSN desync) is still the primary concern for the paper, but needs explicit testing with loss injection.
- The WQE rate data gives a first estimate for chunk size selection, with 16KB–64KB as the initial sweet spot on SoftRoCE.
- All three fundamental mechanisms work on SoftRoCE: UC QP creation, Write-with-Immediate, and CQE generation. The project can proceed to core transport implementation.
