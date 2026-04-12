# Phase 1 Statistics Appendix

## Test 1: UC Write-with-Immediate CQE Verification

### Nature of Test
Deterministic correctness test (not statistical). A single successful run is sufficient because:
- CQE generation is a deterministic protocol behavior, not a stochastic outcome.
- The test verifies 4 binary conditions (CQE received, opcode correct, imm_data correct, buffer written).
- All 4 conditions passed.

### Raw Observations

| Metric | Expected | Observed | Status |
|--------|----------|----------|--------|
| CQE received | Yes | Yes | PASS |
| CQE opcode | `RECV_RDMA_WITH_IMM` | `RECV_RDMA_WITH_IMM` | PASS |
| CQE imm_data | `0xDEADBEEF` | `0xDEADBEEF` | PASS |
| Buffer[0] | `0x42` | `0x42` | PASS |
| Buffer content (64B) | All `0x42` | All `0x42` | PASS |
| Sender CQE status | `SUCCESS` | `SUCCESS` | PASS |
| Sender CQE opcode | `RDMA_WRITE` | `RDMA_WRITE` | PASS |

### Inferential Claims
Not applicable — this is a correctness assertion, not a measurement.

---

## Test 2: Ghost Gradient Verification

### Nature of Test
Deterministic behavior test with unexpected outcome.

### Raw Observations

**Round 1 (normal Write-with-Immediate, Receive WR posted):**

| Metric | Expected | Observed | Status |
|--------|----------|----------|--------|
| CQE received | Yes | Yes | OK |
| CQE opcode | `RECV_RDMA_WITH_IMM` | `RECV_RDMA_WITH_IMM` | OK |
| imm_data | `0x11111111` | `0x11111111` | OK |
| Buffer[0] | `0x42` | `0x42` | OK |

**Round 2 (Write-with-Immediate, NO Receive WR posted):**

| Metric | Hypothesis A (full drop) | Hypothesis B (data only) | Observed |
|--------|--------------------------|--------------------------|----------|
| CQE received | No | No | **No** |
| Buffer[0] | `0x42` (old) | `0xFF` (new) | **`0xFF` (new)** |
| Buffer content (32B) | All `0x42` | All `0xFF` | **All `0xFF`** |

**Conclusion:** Hypothesis B confirmed. On SoftRoCE UC QP, Write-with-Immediate without a posted Receive WR:
- RDMA Write data transfer: **succeeds**
- CQE generation: **does not occur**

### Limitation
This test exercises a different failure mode than real-world packet loss:
- Tested: no Receive WR → no CQE, but data arrives
- Real world: packet loss → PSN mismatch → data does NOT arrive (partially or fully)
- The packet-loss scenario requires tc netem testing (not yet done)

---

## Test 3: WQE Rate Micro-benchmark

### Experiment Parameters

| Parameter | Value |
|-----------|-------|
| Transport | UC QP, RDMA Write (no Immediate) |
| Device | SoftRoCE (rxe0) |
| Topology | Single-machine loopback |
| Buffer size | 16 MB (source and target) |
| Iterations | 1000 per chunk size |
| Warmup | 10 iterations |
| Signal interval | Every 64th WQE |
| Runs | 1 (no repeated measurements) |

### Raw Results

| Chunk Size | Time (ms) | WQE/s | Throughput (MB/s) |
|------------|-----------|-------|-------------------|
| 4 KB | 10.5 | 95,201 | 371.9 |
| 16 KB | 31.3 | 31,945 | 499.1 |
| 64 KB | 123.8 | 8,075 | 504.7 |
| 256 KB | 503.6 | 1,986 | 496.4 |
| 1 MB | 2,314.7 | 432 | 432.0 |

### Derived Metrics

| Chunk Size | Per-WQE Latency (μs) | Throughput Efficiency (% of peak) | Loss Impact (% of 25M-param gradient) |
|------------|----------------------|-----------------------------------|---------------------------------------|
| 4 KB | 10.5 | 73.7% | 0.004% |
| 16 KB | 31.3 | 98.9% | 0.016% |
| 64 KB | 123.8 | 100.0% (peak) | 0.064% |
| 256 KB | 503.6 | 98.4% | 0.256% |
| 1 MB | 2,314.7 | 85.6% | 1.0% |

*Loss Impact = chunk_size / (25M params × 4 bytes/param) = chunk_size / 100MB*

### Scaling Analysis

WQE/s vs. chunk size follows an approximate inverse relationship:

```
WQE/s ≈ K / chunk_size_KB
```

Fitting: K ≈ 95,201 × 4 = 380,804. Checking:
- 16 KB: predicted 380,804/16 = 23,800 → observed 31,945 (higher)
- 64 KB: predicted 380,804/64 = 5,950 → observed 8,075 (higher)
- 256 KB: predicted 380,804/256 = 1,488 → observed 1,986 (higher)
- 1 MB: predicted 380,804/1024 = 372 → observed 432 (higher)

The model under-predicts at larger sizes, suggesting SoftRoCE has a per-WQE fixed cost (~10μs) plus a per-byte cost that is sub-linear.

### Statistical Limitations

- **No variance estimates.** Single run per chunk size. Cannot compute confidence intervals.
- **No seed variation.** Deterministic benchmark, but system load could cause variance.
- **SoftRoCE-specific.** Software emulation path (kernel module + memory copy) is fundamentally different from hardware DMA. These numbers inform code structure, not paper claims.
- **No Write-with-Immediate variant.** Benchmarked plain RDMA Write only. Write-with-Immediate adds Receive WR posting overhead on the receiver side.

### Blockers for Paper-Quality Data

1. Run 5+ repetitions to get `mean ± std`
2. Add Write-with-Immediate variant for direct comparison
3. Replicate on ConnectX-5 hardware (CloudLab)
4. Test with concurrent traffic to simulate cloud contention
