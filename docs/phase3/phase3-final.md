# Phase 3 · DDP Integration + Real-NIC Validation · 最终总结

> **时间窗：** 2026-04-19 → 2026-04-23（4 天实施 + 多轮 debug）
> **状态：** Stage A / B / B.5 / Hybrid 全部实施完成，CX-5 benign wire 上 convergence 验证通过。**Lossy wire 环境的验证还没做**（需要第三台机器）—— 见 [../PLAN.md](../PLAN.md)。
> **历史细节：** [history/](history/) 保留原始 Stage A aliyun 数据、pre-fix CX-6 数据、各阶段独立结果文档、bug 分析原文。
> **前置阶段：** [../phase2/phase2-final.md](../phase2/phase2-final.md)

---

## 1. Phase 3 干了什么

把 Phase 2 的 C++ transport 通过 pybind11 暴露给 PyTorch，实现了**两个 DDP comm hook**（原版 `semirdma` + 修复 H3 的 `semirdma_hybrid`），建起**五种 transport 的 head-to-head 对比框架**（gloo / rc_baseline / rc_lossy / semirdma / semirdma_hybrid），并在 **aliyun SoftRoCE + CloudLab CX-6 Lx + CloudLab CX-5** 三个平台上跑通 ResNet-18/CIFAR-10 的 2-rank DDP 训练。

过程中发现并修复了一个 **ratio controller 致命 bug**（pre-fix 下所有 loss_rate 的 effective loss 都是 5%），并诊断出 A2 矩阵 "+0.5 final-loss gap" 的真正根因（**是 timeout 太紧，不是 H3 drift**）。

## 2. 产出代码

### 2.1 Python 侧（`python/semirdma/`）

| 文件 | 职责 |
|---|---|
| `transport.py` | C++ engine 的 Python 封装；`post_gradient` / `await_gradient` 数据路径；包含 ratio controller 动态 target 的 bug fix |
| `hooks.py` | 两个 DDP comm hook：`semirdma_allreduce_hook`（full-bucket 对称 UC 交换）+ `semirdma_hybrid_allreduce_hook`（UC reduce-scatter + gloo all-gather） |
| `config.py` | `TransportConfig` dataclass |
| `_bootstrap.py` | TCP QP info 交换 |
| `baselines/rc_hook.py` + `rc_lossy_hook.py` | 对照组：gloo AllReduce（RC-Baseline） + post-reduce 软件 mask（RC-Lossy） |

### 2.2 C++ 绑定（`src/bindings/py_semirdma.cpp`）

把 Phase 2 的四个 C++ 类 (`UCQPEngine` / `ChunkSet` / `RatioController` / `GhostMask`) 以及 `post_recv_batch` / `outstanding_recv` 的 API 增补全部暴露给 Python。

### 2.3 训练驱动（`experiments/stage_a/train_cifar10.py`）

Hydra config 驱动，`cfg.transport` 切换 5 种 hook；输出 per-step loss / iter_time / grad_norm / completion stats 的 CSV。

### 2.4 CloudLab 脚本（`scripts/cloudlab/`）

`run_a1_real_nic.sh`（A1 bit-for-bit）、`run_a2_real_nic.sh`（A2 12-cell + TRANSPORT env 支持 hybrid 切换）、`run_b5_real_nic.sh`（B.5 RC baselines）、`rq_hybrid_tail_probe.sh`（`ib_write_lat` RC vs UC probe）、`detect_rdma_dev.sh` + `day0_check.sh`（多 ACTIVE NIC 节点支持）。

---

## 3. 最重要的一件事：A2 "+0.5 gap" 的真正根因

### 3.1 最初的误判

CX-5 上 SemiRDMA L=0 × 3 seed 的 final loss mean = **1.514**，RC-Baseline = **1.005**，gap = +0.51。原假设是 **H3 rank-asymmetric ghost drift**：两 rank 各自 ghost-mask 不同 chunks → 梯度不对称 → 模型参数 drift。

### 3.2 诊断链（否决 H3）

1. **Hybrid 架构假设能修 H3**：phase 2 gloo all-gather 保证两 rank 最终 averaged tensor 字节相同
2. **Bit-identity assert**：在 hybrid hook 里加 `dist.broadcast` + `torch.equal` 验证。50 step × 2 rank × L=0 × timeout=5ms → **0 次 mismatch** → **两 rank 字面字节相同，drift 不存在**
3. **N_missing 诊断日志**：发现 rank 0 接收方向 **14% 的 bucket 丢失 135–1360 chunks**（baseline 5）。这是 CQE bursty latency，不是 wire drop
4. **timeout 5ms → 500ms**：灾难性 bucket 几乎消失（49/50 buckets 回到 baseline）
5. **timeout=500 下重测**：`semirdma` 直接达到 RC-Baseline 水平（seed 42: 0.875 vs RC 0.860）

### 3.3 结论

A2 "+0.5 gap" 的 **~99% 来自 `timeout_ms=5` 过紧**，不是 H3 drift。CX-5 UC Write-with-Imm 在 mlx5 fw 16 上有 **bursty CQE 生成延迟**（~每 7 个 bucket 出现一次 stall > 5ms），5ms timeout 把大量合法 chunk 当作丢包清零。

### 3.4 Hybrid 的真实价值（重写后）

Hybrid 的 value proposition 是 **conditional**：

- **Benign wire + generous timeout**（当前 CX-5 CloudLab 实测）：natural drop ~0.4%/step，H3 drift 累积 500 step 也不足以偏离 RC。Hybrid ≈ semirdma ≈ RC-Baseline。**Hybrid 多付一次 gloo all-gather 但没 visible benefit**
- **Lossy wire + tight timeout**（论文目标部署场景，未验证）：大量 asymmetric ghost 发生 → hybrid 的 phase 2 reliable all-gather 成为 correctness safeguard；同时 phase 1 UC 仍拿到 tail-hiding benefit

决定**保留 hybrid 实现**，因为代码已经写好测好（~150 行），未来补 lossy wire 验证时不用重写。

---

## 4. 另一个重要修复：ratio controller bug

### 4.1 症状

Pre-fix A2 矩阵（CX-6 Lx c240g5 平台）4 档 `cfg.loss_rate ∈ {0, 0.01, 0.03, 0.05}` 的 final loss 都在 1.47–1.52 之间，**没有任何 loss_rate 单调 trend** —— 像在跑同一个实验。

### 4.2 根因

[`python/semirdma/transport.py`](../../python/semirdma/transport.py) 里 `await_gradient` 默认 `r = cfg.ratio = 0.95` 硬编码。含义：**receiver 只等 95% chunks 到齐就 break**，然后 ghost-mask 剩 5%。

Effective loss = `max(cfg.loss_rate, 1 − ratio) = max(cfg.loss_rate, 0.05)` → 当 `cfg.loss_rate < 5%` 时，实际测的都是 **5% effective loss**。名义 label 对 receiver 没意义。

### 4.3 修复（commit `9386f2e`）

```python
# Pre-fix:
r = self._cfg.ratio if ratio is None else ratio

# Post-fix:
if ratio is None:
    dyn_target = 1.0 - self._cfg.loss_rate - 0.005   # 0.5% jitter slack
    r = max(self._cfg.ratio, dyn_target)
else:
    r = ratio
```

`cfg.ratio = 0.95` 从"固定 cutoff"变成"最低安全 floor"；动态 target = `1 − loss − slack` 作为主要目标。

### 4.4 Fix 不影响的路径

- Phase 2 C++ 测试（RQ1/RQ2/RQ4）：走 C++ path，不受影响
- A1 bit-for-bit：显式传 `ratio=1.0`，走 `else` 分支
- RC-Baseline / RC-Lossy hook：不用 SemiRDMA transport

---

## 5. CX-5 Benign Wire 上的最终数据（seed 42, 500 step, L=0）

| Transport | timeout | seed 42 final loss | 说明 |
|---|---:|---:|---|
| rc_baseline (gloo TCP) | — | 0.860 | Gold reference |
| **semirdma + timeout=500** | 500 ms | **0.875** | +0.015，在 3-seed noise 内 |
| **hybrid + timeout=500** | 500 ms | **0.731** | 甚至略好（seed 噪声） |
| semirdma + timeout=5 | 5 ms | 1.300 | timeout 过紧 → ghost masking 误杀 |
| hybrid + timeout=5 | 5 ms | 1.237 | 同上；hybrid 微弱 mitigation |

**读数：** 在 CX-5 benign wire 上，修对 timeout 后，semirdma 和 RC-Baseline **不可区分**（seed 42 差 1.7%）。Hybrid 在当前 wire 不带来显著增益。

### 5.1 iter_ms 代价

timeout=5 → 720ms/step；timeout=500 → 1100ms/step（+50%）。hybrid 比 semirdma 再多一次 gloo all-gather，iter_ms 差异 < 5%。

**对 benign wire**：这个 1100ms 基本是"wait for late legitimate CQE"的代价，没 tail 可 hide；**对 lossy wire**：真 drop 的 chunk 永远不到，timeout 5ms 的 ghost-mask 就是 tail-hiding benefit 本身。

---

## 6. 实验平台全景

| 平台 | 硬件 | 状态 | 主要数据 |
|---|---|---|---|
| aliyun SoftRoCE | loopback rxe0 | ✅ Stage A 完成 | A1 bit-for-bit / A2 早期 (effective-loss bug 影响但趋势有效) |
| CloudLab c240g5 CX-6 Lx | Xeon Silver 4114 + P100 + CX-6 Lx 25 GbE | 🗄️ 归档 | Pre-fix A2 12-cell / B.5 partial (4/12) —— pre-fix bug 影响 |
| CloudLab amd203/amd196 CX-5 | EPYC 7302P + CPU-only + CX-5 25 GbE | ✅ Post-fix 当前平台 | A2 + B.5 12-cell post-fix / M1-M5 microbench / RQ1 chunk_sweep / hybrid-tail-probe / hybrid-timeout 诊断 |

## 7. 本阶段已验证 / 未验证

### 7.1 已验证

- Phase 2 transport 通过 pybind11 成功接入 PyTorch DDP，5 种 transport 可切换
- Ratio controller bug 修复后，SemiRDMA 各档 loss_rate 产生名义对应的 effective loss
- Hybrid 架构两 rank 位等价（bit-identity assert 100% 通过）
- **timeout=500ms 下** CX-5 benign wire 上 SemiRDMA ≈ Hybrid ≈ RC-Baseline 收敛（seed 42）

### 7.2 未验证（下一阶段工作，见 [PLAN.md](../PLAN.md)）

- **Lossy wire 下 hybrid vs semirdma 的真正差距**：需要第三台机器制造交换机拥塞丢包
- **Tight timeout + lossy wire 下 SemiRDMA 的 tail-hiding benefit**：需要 iperf 背景流 + tail latency 实测
- **3-seed 重复性**：目前只验证了 seed 42 单点；需要 seed 123 / 7 × (hybrid+t500, semirdma+t500) 确认不是 seed-lucky
- **N > 2 ring AllReduce**：Hybrid 当前仅支持 world_size=2
- **大模型 + GPU**：全部数据都是 CPU-only ResNet-18 / CIFAR-10；ResNet-50 / GPT-2 不可外推

---

## 8. 历史文档映射

所有原始设计 / 日志 / 各阶段独立分析已移到 [history/](history/)，访问请看：

| 原始文档 | history 位置 | 为什么存档 |
|---|---|---|
| `design-ddp-integration.md` | history/design-ddp-integration.md | Phase 3 整体设计锁定 |
| `rq5-results-ddp-baseline.md` | history/rq5-results-ddp-baseline.md | Stage A aliyun SoftRoCE A1/A2（effective-loss bug 前，仍有效趋势） |
| `rq6-semirdma-effective-loss-analysis.md` | history/rq6-semirdma-effective-loss-analysis.md | Ratio controller bug 根因分析原文（本文 §4 是精简版） |
| `rq6-b5-rc-baselines-partial.md` | history/rq6-b5-rc-baselines-partial.md | CX-6 Lx 4/12 partial handoff |
| `rq6-prep-a2-real-nic-convergence.md` | history/rq6-prep-a2-real-nic-convergence.md | CX-6 Lx pre-fix A2（归档） |
| `rq6-prep-real-nic-equivalence.md` | history/rq6-prep-real-nic-equivalence.md | CX-6 Lx A1 bit-for-bit |
| `rq6-loss-injection-strategy.md` | history/rq6-loss-injection-strategy.md | netem 对 mlx5 RoCE 无效的说明 |
| `stage-b-hardware-notes.md` | history/stage-b-hardware-notes.md | 历次平台的硬件盘点（d7525 / c240g5 / amd203-amd196） |
| `stage-b-microbench-cx6.md` | history/stage-b-microbench-cx6.md | d7525 CX-6 M1-M5 微基准 |
| `stage-b-phase2-resweep.md` | history/stage-b-phase2-resweep.md | c240g5 Phase 2 C++ 重扫 |
| `results-cx5-amd203-amd196/**` | history/results-cx5-amd203-amd196/** | CX-5 当前平台 7 份结果文档（包含 hybrid-timeout-investigation 原文） |
| `results-cx6lx25g-c240g5_archive/**` | history/results-cx6lx25g-c240g5_archive/** | CX-6 Lx 归档树（pre-fix 原始数据） |
