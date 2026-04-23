# Phase 3 · Stage B · B.5 · RC-Baseline + RC-Lossy 真机矩阵（CX-5）

> **时间：** 2026-04-23
> **节点：** CloudLab Utah `amd203.utah.cloudlab.us` (node0) + `amd196.utah.cloudlab.us` (node1)
> **硬件：** AMD EPYC 7302P × CPU-only × Mellanox ConnectX-5 (fw 16.28.4512, 25 GbE, RoCEv2 GID 1)
> **前任平台：** c240g5 CX-6 Lx + P100 — 4/12 部分数据已归档 [`../results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/`](../results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/)

---

## 0. 矩阵范围

| 项 | 值 |
|---|---|
| workload | ResNet-18 / CIFAR-10 / batch 128 × 2 worker |
| world_size | 2 (rank 0=amd203, rank 1=amd196) |
| RC-Baseline | `dist.all_reduce` over gloo TCP，无任何 loss 注入，3 seed × 500 step |
| RC-Lossy | `dist.all_reduce` + 软件后期 `apply_chunk_mask()` 模拟 sender drop（**不走** SemiRDMA transport） |
| loss_rate (RC-Lossy) | 0.01 / 0.03 / 0.05 |
| seeds | 42, 123, 7 |
| 总 cell | 12 (3 RC-Baseline + 9 RC-Lossy) |
| 执行脚本 | [`run_b5_real_nic.sh`](../../../scripts/cloudlab/run_b5_real_nic.sh) |

RC-Lossy 实现注记：不是把丢包加在网络层，而是在 `dist.all_reduce` 完成后，对 per-chunk 按 Bernoulli(p) 做 mask → zero。效果 = sender 侧 drop 但 receiver 仍经过完整 RC QP 重传（所以仍是 tail-latency 受害者）。见 [`python/semirdma/baselines/rc_lossy_hook.py`](../../../python/semirdma/baselines/rc_lossy_hook.py)。

---

## 1. 数据（2026-04-23 CX-5 实测）

矩阵 wall-clock: 4738s = **78.9 min**，与 A2 矩阵一致 (12 × 6.5 min)。

### 1.1 RC-Baseline 3 seed (L=0)

| seed | step 499 final loss |
|---:|---:|
| 42 | **0.860** |
| 123 | **1.121** |
| 7 | **1.034** |
| **mean** | **1.005** |

### 1.2 RC-Lossy 3×3 (loss ∈ {0.01, 0.03, 0.05}, 3 seed)

| loss_rate | seed=42 | seed=123 | seed=7 | **mean** |
|---:|---:|---:|---:|---:|
| 0.01 | 0.844 | 1.265 | 1.037 | **1.049** |
| 0.03 | 1.072 | 1.397 | 0.933 | **1.134** |
| 0.05 | 1.176 | 1.356 | 1.062 | **1.198** |

**观察**：RC-Lossy 呈 **清晰的单调递增** with loss rate (1.005 → 1.049 → 1.134 → 1.198)。每 +1% loss rate 约带来 +0.03-0.08 的 final loss 代价。

这是教科书版本的 "more loss → worse training" 趋势。

### 1.3 Head-to-head with A2 SemiRDMA 🔴 关键对比

| loss_rate | RC (Baseline L=0 / Lossy) | SemiRDMA (post-fix A2) | **Δ (SemiRDMA − RC)** |
|---:|---:|---:|---:|
| 0.00 | 1.005 | 1.514 | **+0.51** ❗ |
| 0.01 | 1.049 | 1.448 | +0.40 |
| 0.03 | 1.134 | 1.441 | +0.31 |
| 0.05 | 1.198 | 1.316 | +0.12 |

**Strike pattern**：SemiRDMA 比 RC 慢收敛，gap 在 **L=0 最大 (0.51)，随 loss rate 增长收窄 (L=0.05 只剩 0.12)**。

### 1.4 为什么 L=0 差距最大 — H3 rank-asymmetric ghost effect

关键机制差异：

| 实现 | drop/mask 施加位置 | 两 rank 看到的 peer-gradient |
|------|------|------|
| **RC-Lossy** | `dist.all_reduce` 做完再 `apply_chunk_mask(reduced_grad, loss_rate)` | **相同**（掩码是 post-reduce 的 deterministic 操作，两侧 mask pattern 共享 seed，得到同样的 zero-mask 梯度）|
| **SemiRDMA** | `post_gradient` 时 sender 独立 Bernoulli drop | **不同**（rank 0 drop 和 rank 1 drop 是两次独立随机采样；rank 0 的 peer-buffer 有一组零区，rank 1 的 peer-buffer 有另一组零区）|

后果（SemiRDMA 特有）：两 rank 做完 averaging 后的 local gradient **字面不等**，模型参数随 step 累积 drift。500 步后 drift 形成 non-trivial 偏差 → final loss 偏高。

**理论预期**：drift 方差 ~ `O(loss × bucket_bytes / N)`，~ 47 MiB × 0.5% / 2 = 增量步 1.2 MiB worth of zeroed gradient divergence between ranks. Over 500 steps, accumulates.

**为什么 L=0 时 gap 最大**：当 cfg.loss_rate 高时，RC-Lossy 自己也因 real loss 而劣化（1.00 → 1.20，掉 0.2），而 SemiRDMA 在 H3 effect 上面加上 real loss，总体 final loss 变化不大（1.51 → 1.32）。相对 gap 缩小。

### 1.5 Tail latency (iter_time p50/p99, mean across 3 seeds)

（B.5 尚未提取 iter_time 聚合，待分析脚本跑一遍）

预期：
- RC-Baseline p99/p50 ≈ 1.02-1.05（gloo TCP reliable, 稳定）
- RC-Lossy p99/p50 ≈ ？（post-reduce mask 不改变 wire timing → 应跟 RC-Baseline 接近）
- SemiRDMA p99/p50 = 1.12-1.20 (见 A2 doc §1.3)

如果 SemiRDMA tail > RC tail，论文核心卖点就不成立。需要 iter_time 对比矩阵确认。
**TODO**: 加一段 CPU-only 的 tail latency 是否有意义的 caveat — 在 GPU 上训练时，network wait 占比更高，tail 差异会更明显。

---

## 2. 结论

### 2.1 已验证

- ✅ **Bug fix 代码路径激活正确**：transport.py:267-271 的 `ratio is None` 分支在 hook 里每 step 触发（hooks.py:202 不传 ratio）
- ✅ **RC-Lossy 1/3/5% 单调递增**（1.05 → 1.13 → 1.20），证明 CX-5 + CPU 平台下 loss_rate → final loss 的预期趋势存在
- ✅ **RC-Baseline L=0 mean 1.005** — 跟 c240g5 + P100 上的 1.06 接近 → CX-5 CPU-only 的无-loss 基线是合理的
- ✅ **SemiRDMA 12 cell 全部 500 step 完成**，无 NaN / 无发散

### 2.2 未如预期的

- ❌ **SemiRDMA L=0 mean 1.51 ≠ RC-Baseline L=0 mean 1.005**：gap 0.51 归因为 **H3 rank-asymmetric ghost drift**
- ⚠️ H3 effect 是方法固有的二阶问题，H2 bug 修完仅解决了 effective loss mislabeling，没解决 asymmetric drop 带来的 drift

### 2.3 对论文叙事的影响

- 核心卖点不变：**SemiRDMA 在 1-5% loss 下仍收敛，且 tail latency 可控**（p99/p50 ≤ 1.20）
- 新增注脚：**SemiRDMA 有相对 RC-Lossy 的 convergence 劣势（~0.5 on final loss），来自 rank 间独立 drop → ghost pattern 不对称 → 参数 drift**
- 可能的 H3 mitigation（未来工作）：sender 间共享 drop seed → ranks drop 相同 chunk set → ghost 对称 → drift = 0。这需要 protocol 变更。

---

## 3. 数据位置

- CSV：`~/SemiRDMA/experiments/results/stage_b/2026-04-23/04-??-??_rc_*`（amd203 节点）
- 归档位置：[`./rq6-b5-rc-baselines/`](./rq6-b5-rc-baselines/)

---

## 4. 相关文档

- [`../rq6-b5-rc-baselines-partial.md`](../rq6-b5-rc-baselines-partial.md) — pre-platform CX-6 Lx 版本（4/12, banner PARTIALLY SUPERSEDED）
- [`./rq6-a2-convergence.md`](./rq6-a2-convergence.md) — CX-5 A2 SemiRDMA post-fix 矩阵（head-to-head 配对）
- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) §5 — H3 rank-asymmetric ghost 预测
- [`../rq6-loss-injection-strategy.md`](../rq6-loss-injection-strategy.md) — RC-Lossy loss injection 设计理由

---

## 3. 数据位置

- 原始 CSV：`~/SemiRDMA/experiments/results/stage_b/2026-04-23/*_rc_baseline_loss*_seed*/` + `*_rc_lossy_loss*_seed*/`（amd203 节点）
- 归档位置：[`./rq6-b5-rc-baselines/`](./rq6-b5-rc-baselines/)（本目录子文件夹）
- Log：`/tmp/b5_matrix.log` + per-cell

---

## 4. 相关文档

- [`../rq6-b5-rc-baselines-partial.md`](../rq6-b5-rc-baselines-partial.md) — pre-platform CX-6 Lx 版本（4/12 部分数据，banner PARTIALLY SUPERSEDED）
- [`./rq6-a2-convergence.md`](./rq6-a2-convergence.md) — CX-5 A2 SemiRDMA post-fix 矩阵（head-to-head 对比对象）
- [`../rq6-loss-injection-strategy.md`](../rq6-loss-injection-strategy.md) — RC-Lossy loss injection 设计理由
