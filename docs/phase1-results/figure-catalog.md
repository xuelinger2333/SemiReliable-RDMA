# Phase 1 Figure Catalog

## Figure 1: WQE Rate vs. Chunk Size (Dual-Axis)

- **Filename:** `figures/figure-01-wqe-rate-vs-chunk.txt` (ASCII; no plotting library available)
- **Purpose:** Show the tradeoff between WQE posting rate and throughput across chunk sizes
- **Data source:** Test 3 output (1000 iters per chunk, SoftRoCE UC QP)

### ASCII Visualization

```
WQE/s (log scale)                          Throughput (MB/s)
100,000 |  *                                    |         600
        |                                       |
 10,000 |      *                                |         500  + --- + --- +
        |              *                        |              |           |
  1,000 |                       *               |         400  *
        |                              *        |
    100 |                                       |         300
        +---+-------+-------+---------+-----   +
           4KB    16KB    64KB     256KB   1MB
        
        * = WQE/s (left axis)     + = Throughput (right axis)
```

### Caption Requirements
- State device (SoftRoCE rxe0), transport (UC QP RDMA Write), iterations (1000)
- Label both y-axes clearly
- Note that WQE/s uses log scale
- Include caveat: "SoftRoCE software emulation; hardware RDMA rates expected 10–100x higher"

### Key Observation
Throughput peaks at 16KB–64KB (~500 MB/s) then declines. WQE/s decreases monotonically. The crossover suggests **16KB–64KB** is the optimal chunk size range on SoftRoCE where per-WQE overhead is amortized without excessive per-chunk latency.

### Interpretation Checklist
1. **Why this figure?** To identify the chunk size sweet spot that balances WQE overhead against loss granularity.
2. **What to notice?** Throughput plateau at 16–256KB; sharp WQE/s drop at larger sizes; 4KB under-performs.
3. **What it changes:** Sets the initial chunk size range for the Chunk Manager implementation (Week 3–4). Below 16KB is likely inefficient even on hardware.

---

## Figure 2: Write-with-Immediate Behavior Matrix

- **Filename:** (table, not plot)
- **Purpose:** Summarize the observed UC Write-with-Immediate behavior under different conditions

### Data

| Condition | RDMA Data Written? | Receiver CQE? | Buffer State | Implication |
|-----------|--------------------|---------------|--------------|-------------|
| Normal (RQ WR posted) | Yes | Yes (`RECV_RDMA_WITH_IMM`) | New data | Happy path |
| No RQ WR posted | **Yes** | **No** | New data (undetected) | CQE is sole signal |
| Packet loss (not yet tested) | Partial/No | No | Stale/partial data | True ghost gradient |

### Key Observation
The Write-with-Immediate operation is **decomposable**: data transfer and CQE generation are independent. This has a direct design consequence — the Ratio Controller MUST rely on CQE count, never on buffer content inspection.

### Interpretation Checklist
1. **Why?** To clarify what "ghost gradient" means in the SemiRDMA context.
2. **What to notice?** Row 2 — data arrives but receiver doesn't know. This is worse than "no data" because the receiver can't distinguish new data from old.
3. **What it changes:** Confirms masked aggregation must be CQE-bitmap-driven. Buffer content is not a reliable completion signal.

---

## Figure 3: Per-Loss Impact vs. Chunk Size

- **Filename:** `figures/figure-03-loss-impact.txt` (ASCII)
- **Purpose:** Connect chunk size to gradient loss impact for a typical model

### ASCII Visualization

```
Loss impact per dropped chunk (% of gradient)

  1.0% |                                          *  (1 MB)
       |
       |
  0.25%|                            *  (256 KB)
       |
  0.06%|              *  (64 KB)
  0.02%|      *  (16 KB)
  0.00%|  *  (4 KB)
       +--+------+------+-----------+-----------+---
         4KB   16KB   64KB       256KB         1MB

Reference: 25M-parameter model (ResNet-50), 100 MB total gradient
```

### Caption Requirements
- State the model used for reference (ResNet-50, 25M params, float32)
- Note this is per-chunk impact; actual gradient loss depends on loss rate × chunk count

### Key Observation
At 64KB chunks, a single lost chunk affects only 0.064% of the gradient — well within the 1–5% tolerance established by MLT/OptiReduce. Even at 1MB, a single loss is only 1%. This validates fine-grained chunking as a viable strategy.

### Interpretation Checklist
1. **Why?** To quantify the RQ1 tradeoff between chunk size and per-loss damage.
2. **What to notice?** Impact scales linearly with chunk size. At the optimal throughput range (16–64KB), per-loss impact is negligible.
3. **What it changes:** Combined with Figure 1, this narrows the design space: 16–64KB chunks give near-peak throughput with minimal per-loss impact.
