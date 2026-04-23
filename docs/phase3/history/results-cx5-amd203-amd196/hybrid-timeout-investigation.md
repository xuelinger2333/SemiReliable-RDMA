# Hybrid AllReduce · Timeout 诊断与 H3 重解读

> **时间：** 2026-04-23
> **节点：** amd203 (node0) + amd196 (node1)，ConnectX-5 fw 16.28.4512, 25 GbE DAC
> **上下文：** A2 矩阵显示 SemiRDMA `L=0` vs RC-Baseline `L=0` 存在 **+0.51 的 final-loss gap**（mean 1.514 vs 1.005）。最初假设是 H3 rank-asymmetric ghost drift。本文记录通过 hybrid 架构 + 位等价 assert + timeout 敏感性实验得出的**修正结论**。

---

## 0. TL;DR

**原来的假设错了。** A2 矩阵的 +0.51 gap 主要来源是：

> **`timeout_ms=5`（5 毫秒）在 CX-5 UC Write-with-Imm CQE 生成上太激进**，把那些"5ms 没到但 50–500ms 能到"的合法 chunks 错误地 ghost-mask 成零。

**不是 H3 drift。** Hybrid 架构 + bit-identity assert 证明两 rank 最终 averaged gradient 在每步都字节相同 —— drift 不存在。修 timeout 后，**原 `semirdma` hook 就已经收敛到 RC-Baseline 水平**，hybrid 在 CX-5 benign wire 上看不到显著增益。

**Hybrid 的真实价值**：在真 lossy wire + tight-timeout 场景（论文目标环境），asymmetric ghost mask 会大量发生 → drift 会可见 → hybrid 的 phase 2 reliable all-gather 变成 correctness safeguard。需要第三台机器制造有损环境重测验证。

---

## 1. 诊断实验链

### 1.1 E1 — Bit-identity assert（50 step, timeout=5ms）

在 `semirdma_hybrid_allreduce_hook` 每次返回前加 rank-broadcast 比较：

```python
if os.environ.get("SEMIRDMA_HYBRID_ASSERT") == "1":
    reference = flat.clone()
    dist.broadcast(reference, src=0)
    assert torch.equal(reference, flat)  # raise on any mismatch
```

**结果：** 50 bucket × 2 rank = 100 次 assert，**全部通过**。
→ **hybrid 架构的 phase 2 gloo all-gather 确实保证两 rank 位等价 → 不存在 drift**。

### 1.2 E1 副产物 — n_missing 分布（timeout=5ms）

同样日志里记录每步每 rank 的 ghost-masked chunk 数量：

| rank | 健康 buckets (n_missing≈5/1450) | 灾难性 buckets (n_missing≥100/1450) |
|:-:|:-:|:-:|
| rank 1 接收（方向 0→1） | 48/50 | 1/50 |
| rank 0 接收（方向 1→0） | 37/50 | **7/50**（135, 197, 631, 869, 1325, 1340, 1360） |

rank 0 接收方向 **14% buckets 丢失 10–94% 的 chunks**。natural wire drop 是 ~0.4%（健康 baseline n_missing=5）。灾难性 buckets 来源必须是 **CQE 生成的 burst latency**，不是实际丢包。

### 1.3 E1-v2 — timeout=500ms 重跑

仅改 `transport_cfg.timeout_ms=500`，其他不变：

| rank | n_missing=5-6 | n_missing≥50 |
|:-:|:-:|:-:|
| rank 1 | 49/50 | 1/50 (n=53) |
| rank 0 | 49/50 | 1/50 (n=821) |

→ **几乎彻底消除 bursty ghost masking**。iter_time 只增加 1.5s / 50 step（3%）因为 timeout 只在真正晚到时才延长等待。

### 1.4 E2 — Hybrid + timeout=500 convergence（seed 42, 500 step）

```
transport=semirdma_hybrid, loss_rate=0.0, timeout_ms=500, seed=42, 500 step
→ final loss = 0.731
```

**对比 RC-Baseline seed 42 = 0.860** → 在单 seed 噪声内已经达到或优于 RC-Baseline。

### 1.5 E3 — 原 semirdma + timeout=500（ablation）

同样参数但换回原 `semirdma` hook：

```
transport=semirdma, loss_rate=0.0, timeout_ms=500, seed=42, 500 step
→ final loss = 0.875
```

**对比 RC-Baseline seed 42 = 0.860** → 在单 seed 噪声内达到 RC-Baseline。

---

## 2. 汇总：所有 seed 42 数据点

| config | seed 42 final loss | vs RC-Baseline (0.860) | iter_ms ~ |
|---|---:|---:|---:|
| RC-Baseline | 0.860 | — | 750 |
| **hybrid + timeout=500** | **0.731** | **−0.129** | 1100 |
| **semirdma + timeout=500** | **0.875** | **+0.015** | 1100 |
| hybrid + timeout=5 (post-mag-comp) | 1.259 | +0.399 | 720 |
| hybrid + timeout=5 (pre-mag-comp) | 1.237 | +0.377 | 720 |
| semirdma + timeout=5 | 1.300 | +0.440 | 680 |

**关键读数：**

- timeout=5 → timeout=500：**+0.4 gap 消失**（无论 hybrid 或 semirdma）
- 同 timeout=500 下：**semirdma 和 hybrid 差 0.14**，在 RC-Baseline 3-seed spread (0.26) 噪声内

→ **A2 +0.5 gap 的 ~99% 来自 timeout 问题，~1% 才可能归因于 H3**。

---

## 3. 为什么 5ms timeout 在 CX-5 上太激进

### 3.1 已知事实

- CX-5 fw 16.28.4512 + mlx5 driver + UC Write-with-Imm
- RQ1 chunk_sweep 实测：**每轮 p99 = 5000ms**（= 默认 5s 测试框架 timeout），即每轮都至少有一个 chunk 拖到 5s 级别
- `ib_write_lat` (RC vs UC) probe 显示 wire 基本无损，p99 latency per-chunk < 350µs
- 但 CQE 生成 vs wire delivery 不是同一个 bottleneck：mlx5 driver 在某些条件下会 batch/defer CQE

### 3.2 推测机制

CX-5 UC Write-with-Imm 的接收端 CQE 生成存在 **bursty 高延迟现象**：大多数 chunk 在几十微秒内 CQE 可见，但偶发 burst（~每 7 buckets 一次 on rank 0 rx direction）会让 ~1000 个 chunk 的 CQE 同时 stall 到几百毫秒。原因可能是：

- Driver-level completion coalescing / interrupt moderation
- mlx5 CQE ring full → flush stall
- EPYC NUMA / PCIe lane 上的 DMA 回写延迟
- 某些 ACK clocking 上的 head-of-line

本文未做微架构级定位（不在 paper 关键路径上）。对 paper 的 operational takeaway 是：**选 timeout 必须基于实测 CQE 延迟分布，5ms 在 CX-5 上过紧**。

### 3.3 这不是 "wire loss"

与常规理解的 "packet drop" 不同：chunks 其实**到了**（RC probe 显示 wire lossless），只是 receiver 的 verbs API 层没在 5ms 内把 CQE 暴露给 python 用户代码。`await_gradient` 因 timeout 过早 break out，`apply_ghost_mask` 把这些 chunks 的 buffer 区域清零。从 SGD 视角看等同于丢包，但从 wire 视角看不是。

---

## 4. Hybrid 的 value proposition 重写

### 4.1 原错误叙事

> "Hybrid fixes H3 drift, which is the root cause of A2 +0.5 gap."

✗ 经 E1 assert + E3 ablation 否定。

### 4.2 修正后叙事

> Hybrid 的价值是 **conditional**：只在 asymmetric ghost mask 大量发生的 regime（真 lossy wire + tight-timeout tail-hiding）才 visible。
>
> - **Benign wire + generous timeout**（如本 CX-5 CloudLab 当前状态）：natural drop ~0.4%/step，H3 drift 累积 500 step 也不足以偏离 RC。此时 hybrid ≈ semirdma ≈ RC-Baseline，hybrid 多付一次 gloo all-gather 但没 visible benefit。
> - **Lossy wire + tight timeout**（论文目标场景，未在本平台实测）：大量 asymmetric ghost → hybrid 的 phase 2 reliable all-gather 保证 rank 间位等价 → **drift-free correctness**；同时 phase 1 UC 仍然拿到 tail-low benefit。

### 4.3 保留理由

- 代码已经写好测好（~150 行）
- 在 lossy wire 场景是 correctness safeguard，不保留未来补回去 ugly
- 天然 generalize 到 N>2 ring（当前 2-rank 实现是 Stage 1 de-risk）

---

## 5. 下一步：第三台机器有损环境验证

hybrid 的真实价值在当前 CX-5 DAC 直连上不 visible，因为这条 wire 本身 benign。需要**引入真 loss** 才能观测 hybrid vs semirdma 的差异。

### 5.1 不可用的 loss 注入方法

- ❌ **tc netem**：mlx5 RoCE 绕过 kernel netdev，netem qdisc 不生效（已在 `scripts/cloudlab/netem_inject.sh` header 确认）
- ❌ **sender-side Bernoulli drop (cfg.loss_rate)**：这是 software loss，作为对称 ghost-mask 分析可用（见 B.5 RC-Lossy），但不能证明 hybrid 对 wire asymmetric drop 的修正价值
- ❌ **PFC off + 低流量**：没有拥塞就不会丢

### 5.2 建议方法：第三台 hammer 节点 + 交换机排队丢包

- **设备**：申请第三台 CloudLab 节点（amd* 系列，同交换机 hop）作为背景流发生器
- **流量**：用 `iperf3 -u -b 20G -P 8` 或 `D-ITG` 把 25 GbE 交换机 link 跑到 95%+ utilization
- **现象**：amd203 ↔ amd196 的 SemiRDMA 训练流量跟背景流竞争 → switch queue 溢出 → 自然丢包
- **metric**：
  - p99/p50 iter-time ratio（OptiReduce 使用的 tail 指标）
  - chunks CQE 分布（bimodal：大部分 fast + 长尾真实 drop）
  - final loss 对比 hybrid vs semirdma

### 5.3 实验矩阵设计（预估）

| 变量 | 值 |
|---|---|
| background load | 0%（clean）、50%、80%、95% link util |
| transport | semirdma、semirdma_hybrid、rc_baseline（gloo TCP） |
| timeout_ms | 5（tight tail-hiding）、50、500（generous） |
| loss_rate (cfg) | 0（纯 wire loss） |
| seed | 42、123、7 |

2-3 days 工作量，可以跟 Stage 2 hybrid C++ RC QP 实装并行。

### 5.4 预期结果

- **semirdma @ tight timeout + lossy wire**：final loss 高（drift 累积）
- **hybrid @ tight timeout + lossy wire**：final loss 接近 rc_baseline（drift-free），p99 iter_time 低于 rc_baseline（UC 避免 retx）
- **Pareto**：hybrid 拿到 (low tail, zero drift) 双赢，semirdma 或 rc_baseline 各丢一项

---

## 6. 代码与 paper 变更记录

### 6.1 代码

- [`python/semirdma/hooks.py`](../../../python/semirdma/hooks.py) — 保留 `semirdma_hybrid_allreduce_hook`（含 magnitude compensation），诊断 assert/log 已清理
- [`scripts/cloudlab/run_a2_real_nic.sh`](../../../scripts/cloudlab/run_a2_real_nic.sh) — `TRANSPORT` env var 支持 `semirdma_hybrid` 切换
- **没改的**：`src/transport/*` C++（UC QP engine 不变）、`python/semirdma/transport.py`、原 `semirdma_allreduce_hook`（A2 老数据对照用）

### 6.2 Paper 叙事修正

需要更新：

- [`rq6-a2-convergence.md`](./rq6-a2-convergence.md) §2.1 — "A2 +0.5 gap 来自 H3 drift" 要改成 "来自 timeout 过紧 + 小部分 drift"
- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) §5 (H3 预测) — 预测的 residual drift 量级实际上远小于 timeout 效应
- [`./hybrid-tail-probe.md`](./hybrid-tail-probe.md) §2.3 — 重解读基础上引入 "timeout > CQE burst" 机制

### 6.3 保留作为未来工作

- **Stage 2：** hybrid C++ RC QP 实装，替换 phase 2 gloo TCP，扩展到 N>2 ring
- **第三节点 lossy wire 平台：** 本文 §5 描述的 validation 实验

---

## 7. 相关文档

- [`./rq6-a2-convergence.md`](./rq6-a2-convergence.md) — A2 原始 12-cell 矩阵（pre-timeout-fix）
- [`./rq6-b5-rc-baselines.md`](./rq6-b5-rc-baselines.md) — RC-Baseline + RC-Lossy 12-cell 对照
- [`./hybrid-tail-probe.md`](./hybrid-tail-probe.md) — CX-5 wire RC vs UC tail 实测（验证 wire 本身 benign）
- [`./stage-b-phase2-resweep.md`](./stage-b-phase2-resweep.md) §1 — RQ1 chunk_sweep p99=5000ms 的先兆（已暗示 CX-5 CQE 长尾存在，当时未深究）
- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) — ratio controller bug fix（独立成立，不受本文修正影响）
