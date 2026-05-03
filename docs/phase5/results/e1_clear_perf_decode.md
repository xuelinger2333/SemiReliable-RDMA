# E1 — clear_perf.csv decode (control_plane_overhead + regression attribution)

Generated: 2026-05-03 from 9 clear_t1 cells (latest 200-step run per (drop, seed)).


## Track 1: control_plane_overhead

Defined as `(clear_t1.iter_ms_med − phase4.iter_ms_med) / phase4.iter_ms_med`.
Per E1 plan the pass criterion is overhead ≤ 1%.

| drop | phase4 iter_ms | clear_t1 iter_ms | overhead | ≤1%? |
|---|---|---|---|---|
| 0.00 | 6512.9 | 6623.4 | +1.70% | FAIL |
| 0.01 | 6561.9 | 11739.8 | +78.91% | FAIL |
| 0.05 | 6479.8 | 11613.5 | +79.23% | FAIL |

## Per-bucket median ms breakdown (steady-state, last 150 steps)

Each cell row aggregates 150 buckets; outer column = mean across n=3 seeds per drop.

| col / drop |   0.00   |   0.01   |   0.05   |  Δ(0.05−0) |
|---|---|---|---|---|
| to_bytes_ms    |   13.24 |    9.81 |    9.61 | -3.63 |
| stage_ms       |    2.80 |    3.15 |    3.08 | +0.27 |
| threads_ms     |  114.63 | 5181.25 | 5116.43 | +5001.80 |
| send_ms        |   95.34 | 5160.09 | 5095.36 | +5000.02 |
| recv_ms        |  107.14 | 5172.99 | 5108.66 | +5001.51 |
| finalize_ms    |    0.05 |    1.30 |    1.54 | +1.49 |
| average_ms     |   68.96 |   68.82 |   68.46 | -0.49 |
| from_numpy_ms  |    6.97 |    7.27 |    7.51 | +0.54 |
| hook_total_ms  |  225.11 | 5278.82 | 5210.65 | +4985.54 |

**Reference: phase4 iter_ms median:**
- drop=0.0: 6512.92 ms/step
- drop=0.01: 6561.87 ms/step
- drop=0.05: 6479.77 ms/step

## Track 3: repair attribution

**recv_count / n_chunks** = fraction of chunks delivered (UC drop is sender-side; this measures effective UC delivery rate). The complementary set is what CLEAR repair must recover.

**send_ms + recv_ms per dropped chunk** estimates the per-chunk repair cost. (send_ms + recv_ms is the data plane + repair plane wall time.)

| drop | n_chunks | recv_count (median) | delivery rate | send_ms (med) | recv_ms (med) | total_ms (med) |
|---|---|---|---|---|---|---|
| 0.00 | 2729 | 2729 | 100.0% | 95.3 | 107.1 | 185.0 |
| 0.01 | 2729 | 2702 | 99.0% | 5160.1 | 5173.0 | 5252.1 |
| 0.05 | 2729 | 2594 | 95.1% | 5095.4 | 5108.7 | 5187.4 |

Interpretation:
- delivery rate < 100% under loss>0 → drops are observed at receiver, repair is invoked
- Compare send_ms / recv_ms inflation drop=0 → drop=0.05; the column with the largest absolute jump pinpoints the repair-traffic bottleneck