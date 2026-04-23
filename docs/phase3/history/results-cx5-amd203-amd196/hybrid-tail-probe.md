# Hybrid AllReduce Tail Probe · CX-5 empirical measurement

> ⚠️ **本文 §2.3 "H3 root cause = ratio cutoff timing 非对称" 的解读已被修正。**
> 后续实验（见 [`./hybrid-timeout-investigation.md`](./hybrid-timeout-investigation.md)）通过 hybrid hook 的 bit-identity assert 证明 drift 不存在；A2 矩阵 "+0.5 gap" 的主因是 **`timeout_ms=5` 过紧** 造成的 CQE 延迟 → ghost-mask 误杀，不是 drift。本文的 RC vs UC wire 实测（§1-§2.2）结论仍然成立（wire 基本 benign），但 §2.3/§2.4/§2.5 关于 H3 机制的推测应以后文为准。

> **时间：** 2026-04-23
> **节点：** amd203 (node0) + amd196 (node1)，ConnectX-5 fw 16.28.4512, 25 GbE DAC
> **脚本：** [`scripts/cloudlab/rq_hybrid_tail_probe.sh`](../../../scripts/cloudlab/rq_hybrid_tail_probe.sh)
> **CSV：** [`./hybrid-tail-probe/hybrid_tail_probe.csv`](./hybrid-tail-probe/hybrid_tail_probe.csv)

---

## 0. 触发问题

Hybrid AllReduce 设计提案：**reduce-scatter 用 UC**（tail 被 RatioController 限住） **+ all-gather 用 RC**（owner 唯一 → 两 rank 字节相同 → H3 drift = 0）。该设计的价值命题取决于一个经验前提：

> **RC all-gather 在 cloud lossy RoCE 上的 tail 是否被 retx timeout 主导？**

如果是 → hybrid 省不下 tail，跟 full-RC 差不多；
如果否 → hybrid 是清晰 win（砍一半 tail + 零 drift）。

本 probe 用 `ib_write_lat` 在 CX-5 上直接测 RC vs UC 每-message 的 p99/p999/max 分布，回答这个问题。

---

## 1. 实测数据（20000 iters × 5 sizes × 2 transports）

| size | transport | p50 (µs) | p99 (µs) | p999 (µs) | max (µs) | p99/p50 |
|------:|---|---:|---:|---:|---:|---:|
| 4 KiB | RC | 5.58 | 5.62 | 6.75 | 7.78 | **1.01** |
| 4 KiB | UC | 5.61 | 5.65 | 6.80 | 8.59 | 1.01 |
| 16 KiB | RC | 10.13 | 10.33 | 11.31 | 12.39 | **1.02** |
| 16 KiB | UC | 10.20 | 10.39 | 11.31 | 13.13 | 1.02 |
| 64 KiB | RC | 26.95 | 27.21 | 28.27 | 29.60 | **1.01** |
| 64 KiB | UC | 26.96 | 27.12 | 28.19 | 29.10 | 1.01 |
| 256 KiB | RC | 91.33 | 91.69 | 92.70 | 96.72 | **1.00** |
| 256 KiB | UC | 91.35 | 91.47 | 92.62 | 93.83 | 1.00 |
| 1 MiB | RC | 349.37 | 349.47 | 350.75 | 352.92 | **1.00** |
| 1 MiB | UC | 349.34 | 349.44 | 350.55 | 351.94 | 1.00 |

---

## 2. 结论

### 2.1 核心发现

**RC 和 UC 在 CX-5 DAC wire 上 tail 分布几乎完全相同**。100k 总 round-trips 里看不到任何 RC retx 事件；max 也只比 p50 高 2-5%。

### 2.2 反驳了什么

这些数据**反驳**了原先的预期 "RC on lossy RoCE has bad tail due to retx timeout"。至少在 amd203↔amd196 DAC 直连的 CX-5 链路上：

- **wire 基本无丢包**：20k iters × 5 sizes = 100k RC round-trips 全没触发 retx
- **RC tail = UC tail**：两者每档 p99/p50 差 <1%，完全在噪声范围
- **hybrid 提案的 RC all-gather 阶段零 tail 代价**：只要 wire 继续无损，hybrid design 相对 full-UC **没有任何 tail penalty**

### 2.3 但这也重新解读了 A2 / B.5 +0.5 gap 的来源

如果 wire 没丢包，那么 SemiRDMA post-fix 在 L=0 档下的"+0.51 vs RC-Baseline" **不是 wire loss 造成的 asymmetric ghost**，而是：

> **ratio<1.0 receiver-side 早退机制本身是非对称的**

具体机制：
- `cfg.ratio=0.95` → 动态 target = max(0.95, 0.995) = 0.995
- receiver 拿到 99.5% chunks 就 break out，剩下 0.5% chunks（~14 chunks out of 2880）被 ghost-masked 成零——**即使这些 chunks 其实还在飞，会在下一毫秒到达**。
- 两 rank 各自的"哪 0.5% 被踢"取决于**receiver 侧 poll 顺序和 timing 抖动**，两 rank 独立 → 踢掉不同的 chunks → averaged gradient 不等 → H3 drift。

所以 H3 的 root cause **不是 wire asymmetric loss**，而是 **ratio<1.0 cutoff 的 timing-dependent 非确定性**。这是 SemiRDMA 设计里的 inherent trade-off：**早退 = tail 控制 = 非确定性 ghost pattern = rank 间不对称**。

### 2.4 对 hybrid 设计的 re-framing

在 **lossless wire** 上（本 probe 验证的场景）：
- Full-RC AllReduce: tail 跟 UC 一样快，0 drift → **最佳选择**
- Hybrid UC-RS + RC-AG: 跟 full-RC 一样的 tail，0 drift → 等价
- SemiRDMA vanilla (UC-UC): 跟 full-RC 一样的 tail，+0.5 drift → **没有 upside**

**SemiRDMA 的 value proposition 在这条 wire 上不 visible**，因为前提 ("RC retx tail is bad") 不成立。

在 **lossy wire** 上（论文目标场景，需要其他硬件证明）：
- RC retx timeout 假设 10-100 ms → RC AllReduce 整体 tail 被 retx 事件主导
- UC + ratio cutoff → 跳过丢的 chunks，tail 被 cutoff 参数限住（比如 5ms）
- Hybrid → RS 阶段拿到 UC 的 tail 优势，AG 阶段付 RC retx 代价（但只 1 次）

### 2.5 Paper 叙事的 consequence

这个 probe 的数据有两个战略性意义：

1. **当前 CX-5 测试平台不足以证明 SemiRDMA 的核心卖点**。CloudLab DAC 直连太干净，看不到 RC 的 tail 劣势。论文主数据需要在**真正 lossy 的环境**（PFC-off + 竞争流量 + 多交换机 hop）上跑。
2. **H3 的 root cause 需要重新书写**——不是 "wire loss asymmetric"，而是 "ratio cutoff timing-dependent"。这个发现反而更好论证：它是 **SemiRDMA 机制本身的属性**，不依赖任何 wire assumption，处处生效。

---

## 3. 下一步验证建议

### 3.1 在 CX-5 上复现 lossy wire 行为
- netem 对 RoCE 无效（see `netem_inject.sh` header 注）
- 需要靠 **ECN/PFC pressure via congestion**：拉满 Gb 带宽 + 让交换机 hit queue limit 才会自然丢
- CloudLab amd203/amd196 之间似乎是单-switch-hop DAC，不容易 saturate
- 可能的替代：在实验上再开一个第三节点同时 hammer 交换机，制造排队丢包

### 3.2 直接验证 hybrid design in the current SemiRDMA codebase
- 在 `hooks.py` 里改成两阶段：UC QP 做 reduce-scatter，gloo `all_reduce` 做 all-gather（gloo 走 TCP，字节 reliable 且 symmetric）
- 跑 A2 的 12-cell 矩阵再一遍，看 SemiRDMA final loss 是否从 1.51 → ~1.00（RC-Baseline 水平）
- 如果成立：H3 机制 confirmed，hybrid 是 fix

### 3.3 A1 bit-for-bit 在 ratio 接近 1.0 的场景
- 当前 A1 `ratio=1.0` 在 CX-5 上 hang，因为 `wait_for_ratio` 死等 100% chunks
- 但本 probe 显示 wire 实际上无损 → 所有 chunks 本应到达 → 死等应该很快满足
- Hang 的真实原因可能是：**最后几个 chunks 的 CQE 延迟**（poll_cq 看不到已到达的 chunk）触发 timeout 而非 wire drop
- 这个 hypothesis 可以通过在 test_chunk_sweep 里加 `busy-poll with generous timeout` 来测

---

## 4. 相关文档

- [`./rq6-a2-convergence.md`](./rq6-a2-convergence.md) — A2 SemiRDMA post-fix 矩阵（显示 +0.5 final-loss gap vs RC）
- [`./rq6-b5-rc-baselines.md`](./rq6-b5-rc-baselines.md) — B.5 RC-Baseline + RC-Lossy 对比（含 H3 分析）
- [`./stage-b-phase2-resweep.md`](./stage-b-phase2-resweep.md) — RQ1 chunk_sweep 数据（p99=5000ms 之谜解开了：是 test 框架 timeout，非 wire drop）
- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) §5 — H3 原先预测（需要 update：root cause 不是 asymmetric wire drop 而是 asymmetric ratio cutoff）
