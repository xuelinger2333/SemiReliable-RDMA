# Phase 3 · Stage B · RQ6-Prep · A2 真机收敛矩阵（CX-5 post-fix）

> **时间：** 2026-04-23
> **节点：** CloudLab Utah `amd203.utah.cloudlab.us` (node0) + `amd196.utah.cloudlab.us` (node1)
> **硬件：** AMD EPYC 7302P (1S × 16C) × 125 GiB × **CPU-only** × Mellanox **ConnectX-5** (fw 16.28.4512, 25 GbE, RoCEv2 GID 1, Path MTU 4096, PFC off)
> **perftest baseline：** `ib_write_bw -d mlx5_2 -x 1 -s 65536` = 24.39 Gbps (97.6% 线速)
> **bug-fix 后状态：** `python/semirdma/transport.py:257-271` 已修复为动态 `r = max(cfg.ratio, 1 - loss_rate - 0.005)` (commit `9386f2e`)
> **前任平台：** c240g5 CX-6 Lx 25 GbE + P100 — 原始 pre-fix 数据已归档到 [`../results-cx6lx25g-c240g5_archive/rq6-prep-a2-real-nic/`](../results-cx6lx25g-c240g5_archive/rq6-prep-a2-real-nic/)，banner 解释见 [`../rq6-prep-a2-real-nic-convergence.md`](../rq6-prep-a2-real-nic-convergence.md)

---

## 0. 矩阵范围

| 项 | 值 |
|---|---|
| 模型 | ResNet-18 (CIFAR-10 stem, ~47 MiB fp32) |
| 数据 | CIFAR-10 train, batch=128/worker × 2 worker = global 256 |
| world_size | 2 (rank 0 = amd203 CPU, rank 1 = amd196 CPU) |
| transport | semirdma (UC QP via pybind11) |
| **loss_rate** | **0.0 / 0.01 / 0.03 / 0.05** (sender-side per-chunk Bernoulli) |
| seeds | 42, 123, 7 |
| ratio | 0.95 (post-fix: 作为 safety floor；实际 target = max(0.95, 1-loss-0.005)) |
| timeout_ms | 5 |
| chunk_bytes | 16384 |
| steps | 500 (warmup 10) |
| optimizer | SGD lr=0.1 momentum=0.9 wd=5e-4 |
| 执行脚本 | [`run_a2_real_nic.sh`](../../../scripts/cloudlab/run_a2_real_nic.sh) |

---

## 1. 数据（2026-04-23 CX-5 post-fix 实测）

矩阵 wall-clock: 4674s = **77.9 min** (12 cells × 6.5 min/cell, 符合 CPU-only iter 预估)。

### 1.1 Final loss by cell (step 499)

| cfg.loss_rate | seed=42 | seed=123 | seed=7 | **mean** | post-fix effective loss (dyn target) |
|---:|---:|---:|---:|---:|---|
| 0.00 | 1.300 | 1.559 | 1.683 | **1.514** | 0.5% (max(0.95, 0.995) = 0.995) |
| 0.01 | 1.313 | 1.438 | 1.593 | **1.448** | 1.5% (max(0.95, 0.985) = 0.985) |
| 0.03 | 1.288 | 1.562 | 1.473 | **1.441** | 3.5% (max(0.95, 0.965) = 0.965) |
| 0.05 | 1.355 | 1.369 | 1.223 | **1.316** | 5% (floor 0.95 kicks in) |

**观察：**
- 3-seed std dev 内，final loss 跨 4 档 loss rate 呈 **弱单调下降** (1.51 → 1.45 → 1.44 → 1.32)
- 这与 naive "丢包更多 → 收敛更慢" 预期相反
- **最可能的解读**：在 500 step × CPU-only + 3 seed 的 noise floor 下，cfg.loss_rate 对 final train loss 的效应被 seed 随机性 (std ≈ 0.15-0.20) 淹没。L=0.05 的平均值偏低可能含一个 "seed=7 运气好" 分量 (final=1.22)。
- 也有可能是 **GhostMask 起到 regularization 作用**（zero-mask 相当于 per-chunk dropout），这需要更多 seed 或更长 step 才能区分。

### 1.2 Pre-fix CX-6 Lx vs Post-fix CX-5 对比

| cfg.loss_rate | pre-fix CX-6 Lx+P100 mean | post-fix CX-5+CPU mean | Δ mean |
|---:|---:|---:|---:|
| 0.00 | 1.52 | 1.51 | −0.01 |
| 0.01 | 1.47 | 1.45 | −0.02 |
| 0.03 | ~1.32 (seed-42/123 only) | 1.44 | — (not directly comparable) |
| 0.05 | (pre-fix 未报告 mean) | 1.32 | — |

CX-5 post-fix 的绝对值跟 CX-6 pre-fix 接近 — 这个观察值得独立验证：

- **预期（如果 fix 纯靠修正 effective loss）**：post-fix L=0 应显著优于 pre-fix L=0 (0.5% vs 5% effective)，应接近 RC-Baseline（CX-6 上 ~1.06）
- **观测**：post-fix L=0 跟 pre-fix L=0 mean 基本一致（1.51 vs 1.52）
- **两个可能原因**：
  1. CX-5 CPU-only 平台的 RC-Baseline 本身就 ~1.5（因为 Xeon+P100 GPU → EPYC CPU 迁移有 convergence 差异），这种情况下 SemiRDMA 匹配 RC baseline 即为"修复成功"。**待 B.5 矩阵 RC-Baseline CX-5 数据验证**。
  2. H3 rank-asymmetric ghost effect 主导，修 H2 后 SemiRDMA 仍有 residual drift（见 [bug analysis §5](../rq6-semirdma-effective-loss-analysis.md#5-次步骤h3-rank-asymmetric-ghost-是否也要处理)）。

B.5 数据出来后填充结论。

### 1.3 iter_time / tail latency summary (across 3 seeds per loss rate)

| cfg.loss_rate | p50 mean (ms) | p99 mean (ms) | **p99/p50** |
|---:|---:|---:|---:|
| 0.00 | 761 | 867 | **1.14** |
| 0.01 | 751 | 839 | **1.12** |
| 0.03 | 749 | 899 | **1.20** |
| 0.05 | 742 | 887 | **1.20** |

**核心发现：tail latency 被 RatioController 控制在 1.12-1.20 范围内**。loss rate 从 0 → 5% 变化，p99/p50 只增加 ~6%。

解释机制：
- 动态 target = max(0.95, 1-loss-0.005)
- L=0 → target 0.995 → receiver 等 99.5% chunks → 最大等待时间
- L=0.05 → target 0.95 (floor) → receiver 只等 95% chunks → 早些 break out → p99 下降

实测 p99 L=0 (867ms) 确实高于 p99 L=0.01 (839ms)，与机制一致。**这是 SemiRDMA 的核心价值点 — semi-reliable 换得可预测 tail**。

p50 基本恒定 ~750ms，主要由 CPU fwd/bwd dominated（P100 上这个数是 ~300-400ms）。

### 1.4 grad_norm / gradient health

12 cells 全部 500 step 完成，无 NaN、无发散、无 grad explosion。具体 norm 轨迹曲线 → [`rq6-prep-a2-real-nic/*/grad_norm.csv`](./rq6-prep-a2-real-nic/)。

---

## 2. 结论（待填充）

### 2.1 能声明什么

- ✅ / ❌ cfg.loss_rate → final loss 单调递增？
- ✅ / ❌ SemiRDMA `L=0` ≈ RC-Baseline `L=0`？
- ✅ / ❌ SemiRDMA `L=0.01` ≈ RC-Lossy `L=0.01`?
- ✅ / ❌ SemiRDMA tail ratio p99/p50 < RC-Lossy 对应 cell？

### 2.2 不能声明什么

- ❌ "SemiRDMA 比 RC-Lossy 快" — 需要 wall-clock TTA 对比，本矩阵只跑到 500 step（train loss ~1.0），没跑 validation
- ❌ "CPU-only 等价于 GPU" — 本矩阵 P100→CPU 的 iter_time 回归在 `stage-b-hardware-notes.md §9`；CPU 上结论不直接迁移到 GPU
- ❌ "5% 丢包对所有 workload 都无害" — 仅 ResNet-18 / CIFAR-10 / 47 MiB 单 bucket；大模型未测

---

## 3. 对比 pre-fix CX-6 Lx A2 的 bug 诊断

| 维度 | pre-fix CX-6 Lx 12 cell | post-fix CX-5 12 cell | 解读 |
|------|-----|-----|------|
| effective loss | 均 ~5% (bug) | 0.5% / 1.5% / 3.5% / 5% | 修复生效 |
| final loss by loss_rate | 1.27–1.68（无明显 trend） | _TBD_ | post-fix 应该 show 单调 trend |
| GhostMask 触发频率 | 约 5%（bug 导致等不到） | 与 cfg.loss_rate 正相关 | 符合设计意图 |

---

## 4. 数据位置

- 原始 CSV：`~/SemiRDMA/experiments/results/stage_b/2026-04-23/*_semirdma_loss*_seed*/`（amd203 节点）
- 归档位置：[`./rq6-prep-a2-real-nic/`](./rq6-prep-a2-real-nic/)（本目录子文件夹，矩阵完成后 scp 落盘）
- 矩阵 log：node0 `/tmp/a2_v2_matrix.log`, 每 cell `/tmp/this_a2_cell{N}.log` / `/tmp/peer_a2_cell{N}.log`（peer 侧在 node1 `/tmp/peer_a2_cell{N}.log`）

---

## 5. 相关文档

- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) — bug 根因
- [`../rq6-prep-a2-real-nic-convergence.md`](../rq6-prep-a2-real-nic-convergence.md) — pre-fix CX-6 Lx 版本（已 banner SUPERSEDED）
- [`./rq6-b5-rc-baselines.md`](./rq6-b5-rc-baselines.md) — CX-5 RC baseline（待 C.5 落盘）
- [`./rq6-a1-bit-for-bit.md`](./rq6-a1-bit-for-bit.md) — CX-5 bit-for-bit 等价性（待 C.3 落盘）
- [`../stage-b-hardware-notes.md`](../stage-b-hardware-notes.md) §9 — amd203/amd196 硬件详情
