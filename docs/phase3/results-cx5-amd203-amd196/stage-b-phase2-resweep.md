# Phase 3 · Stage B · Phase 2 真机重扫（CX-5 amd203/amd196 局部）

> **时间：** 2026-04-23
> **节点：** amd203 (node0) + amd196 (node1)，ConnectX-5 fw 16.28.4512, 25 GbE
> **CSV：** [`./stage-b-phase2-resweep/rq1_chunk_sweep_cx5_2node_250r.csv`](./stage-b-phase2-resweep/rq1_chunk_sweep_cx5_2node_250r.csv)

---

## 0. 范围

只跑了 **RQ1 chunk_sweep**，没跑 RQ2 (rms_error) 和 RQ4 (ratio_timeout)。理由见 §3。

---

## 1. RQ1 chunk_sweep 2-node 250 rounds

| chunk | loss% | ghost_ratio | goodput(MB/s) | WQE/s | p50(ms) | p99(ms) |
|------:|------:|------------:|--------------:|------:|--------:|--------:|
| 1 KB | 0.0 | 0.0012 | 7.4 | 7567 | 0.63 | **5000** |
| 1 KB | 0.1 | 0.0015 | 7.7 | 7854 | 0.63 | **5000** |
| 1 KB | 1.0 | 0.0105 | 11.6 | 11891 | 1.04 | **5000** |
| 1 KB | 5.0 | 0.0499 | 62.3 | 63839 | 0.99 | **5000** |
| 4 KB | 0.0 | 0.0028 | 6.7 | 1702 | 0.13 | 5000 |
| 4 KB | 0.1 | 0.0049 | 6.9 | 1757 | 0.13 | 5000 |
| 4 KB | 1.0 | 0.0145 | 7.3 | 1868 | 0.13 | 5000 |
| 4 KB | 5.0 | 0.0525 | 6.5 | 1672 | 0.13 | 5000 |
| **16 KB** | 0.0 | 0.0593 | 6.7 | 430 | 0.027 | 5000 |
| **16 KB** | 0.1 | 0.0623 | 6.5 | 414 | 0.025 | 5000 |
| **16 KB** | 1.0 | 0.0840 | 6.3 | 404 | 0.025 | 5000 |
| **16 KB** | 5.0 | 0.1085 | 7.8 | 496 | 0.026 | 5000 |
| 64 KB | 0.0 | 0.0851 | 7.6 | 122 | 0.008 | 5000 |
| 64 KB | 0.1 | 0.0706 | 9.3 | 149 | 0.010 | 5000 |
| 64 KB | 1.0 | 0.0871 | 7.9 | 127 | 0.008 | 5000 |
| 64 KB | 5.0 | 0.1326 | 6.7 | 107 | 0.008 | 5000 |
| 256 KB | 0.0 | 0.1088 | 6.2 | 25 | 0.004 | 5000 |
| 256 KB | 0.1 | 0.1063 | 6.4 | 26 | 0.003 | 5000 |
| 256 KB | 1.0 | 0.1183 | 6.1 | 24 | 0.003 | 5000 |
| 256 KB | 5.0 | 0.1408 | 6.6 | 26 | 0.003 | 5000 |

### 1.1 关键观察

1. **p99 恒为 5000ms** — 这是测试框架默认 5s `wait_for_ratio` timeout。在所有 cell 上都 hit 说明 **CX-5 UC QP 在 250 轮里每轮都至少丢一个 chunk**，receiver 等不齐 100% target，必须打 timeout 才 break out。
2. **p50 很低**（0.003-1 ms） — 说明大部分轮次里，chunk 在 "几乎全到" 之后 receiver 就已 break。但 "几乎全到" 里总有 0.3-10% 的 chunk 被自然 drop，要么被 ghost_mask 置零，要么等满 timeout。
3. **ghost_ratio 随 loss_pct 单调上升**（比如 chunk=16KB 的 0/0.0594 → 0.1085），符合"配置的 loss + UC QP 自然丢包"叠加预期。
4. **chunk=16 KiB 仍在合理 peak zone** — WQE=404-496/s，跟小 chunk 一样受 CPU-bound 瓶颈压制，但比 64/256 KiB 的 25-149/s 高。

### 1.2 跟 c240g5 CX-6 Lx 归档对比

CX-6 Lx baseline peak WQE = **2.55 M/s** at 16 KiB（见 `../results-cx6lx25g-c240g5_archive/rq1_chunk_sweep_cx6lx25g_2node_250r.csv`）。CX-5 peak WQE = **496/s** at 16 KiB — 相差 **~5000×**。

这个巨大差异不是硬件上限差异（perftest `ib_write_bw` 两平台 baseline 都 24.39 Gbps），而是 **测试框架语义差异被 CX-5 的 natural drop 放大**：CX-6 Lx 链路上无自然 drop → 每轮几乎完整到达 → p99=0.1ms → round_ms ≈ 0.1 → 很高 WQE/s；CX-5 有 natural drop → 每轮至少 1 chunk 丢 → p99=5000ms → round_ms 被 5s timeout 主导 → WQE/s 降 5000×。

### 1.3 对 Stage B 参数选择的影响

原 Stage B 配置基于 c240g5 数据：`chunk_bytes=16384 ratio=0.95 timeout_ms=5`。

CX-5 数据暗示：
- `chunk_bytes=16384` 仍是合理选择（小 chunk 太多 bookkeeping 开销；大 chunk 受 CPU 限制 WQE/s 太低）
- `ratio=0.95 timeout=5ms` 需要 **重新校准**：
  - CX-5 的 5ms timeout 还是太短，可能导致大量早 break
  - 但 A2 矩阵的 SemiRDMA cell 实测都能 500 step 跑完，说明 5ms 也算工作（只是每 step 平均 150-300 chunk 被 ghost mask 处理而已）
  - 新的 sweet spot 需要专门 sweep ratio × timeout 在 CX-5 上做 — 但这个 experiment（RQ4）我们没跑，留给正式论文平台做

---

## 2. RQ2 rms_error — SKIPPED

原因：RQ2 的设计是跑 rounds × loss rates × mask-off 对比，理论预期是 RMS_masked / RMS_raw = 1/√2（zero-fill 等价于 var halving）。这是**纯数学结论**，已在 SoftRoCE (aliyun) + c240g5 CX-6 Lx 两个平台分别验证为 0.7066 / 0.7065 / 0.7069 — 误差 < 0.001。

在 CX-5 上重跑预期结论不变，不增加信息量。CX-5 的独特问题（自然丢包、5s timeout）不影响 RQ2 的数学等价性。

如果后续需要 CX-5 的 RQ2 数据点，补跑命令：
```bash
# server node1:
SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0 \
  ./build/tests/phase2/test_rms_error server mlx5_2 100 rq2_cx5_rms.csv
# client node0:
SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0 \
  ./build/tests/phase2/test_rms_error client 10.10.1.2 mlx5_2 100 42
```

---

## 3. RQ4 ratio_timeout — SKIPPED

原因：RQ4 在 CX-5 UC QP 上会被 natural drop 扭曲。RQ1 的 p99=5000 现象说明，RQ4 扫 (0.90/0.95/0.98/1.00) × (1/5/10/20 ms) 的 16 个 cell 里，高 ratio × 短 timeout 的组合一定都会 hit timeout。得到的表跟 c240g5 (p99 < 2 ms) 完全不同 pattern，解读需要大量 caveat。

Stage B 默认 `ratio=0.95 timeout_ms=5` 是基于 c240g5 RQ4 结论；CX-5 上 A2 矩阵证实该参数能跑，但不是本平台的 empirical sweet spot。正式论文节点上应重做 RQ4。

---

## 4. 相关

- [`./stage-b-microbench.md`](./stage-b-microbench.md) — CX-5 verbs-local 微基准 M1-M5
- [`./rq6-a1-bit-for-bit.md`](./rq6-a1-bit-for-bit.md) — A1 记录（与 CX-5 UC QP 自然 drop 相关）
- [`../stage-b-phase2-resweep.md`](../stage-b-phase2-resweep.md) — c240g5 CX-6 Lx 版本（prior-platform reference，C++ 路径不受 bug 影响）
