# SemiRDMA 进度 & 下一步计划

> **最后更新：** 2026-04-23
> **当前阶段：** Phase 3 已完结；Phase 4 (lossy wire validation + paper writing) 启动
> **关键前置文档：** [phase2/phase2-final.md](phase2/phase2-final.md) + [phase3/phase3-final.md](phase3/phase3-final.md)

---

## 0. 当前 snapshot

### 0.1 已完成
- Phase 1 UC QP 语义四条硬约束（P0 实验）
- Phase 2 Core Transport C++ 四模块 + RQ1/RQ2/RQ4 SoftRoCE 验证
- Phase 3 Stage A（aliyun SoftRoCE DDP 数值正确性 A1+A2）
- Phase 3 Stage B（CloudLab 真 NIC）CX-6 Lx (c240g5) 归档 + CX-5 (amd203/amd196) post-fix 主数据
- Phase 3 B.5 RC-Baseline / RC-Lossy 12-cell 对照
- Ratio controller effective-loss bug 修复（commit `9386f2e`）
- Hybrid Ring AllReduce (UC reduce-scatter + gloo all-gather) 实现 + magnitude compensation
- H3 drift 假设否决；A2 "+0.5 gap" 真正根因定位为 CX-5 CQE bursty latency + 5ms timeout 过紧

### 0.2 已锁定的设计结论（不再改动）
- `chunk_bytes = 16384`（Phase 2 RQ1）
- `ratio = 0.95` 作为 floor，dynamic target = `max(0.95, 1 − loss − 0.005)`
- `GhostMask::apply` 默认 on
- Hybrid 保留为 conditional safeguard（lossy wire 场景）

### 0.3 已知 open 问题
- **CX-5 benign wire 不是论文主数据的合适平台**：wire 太干净，SemiRDMA 的 tail-hiding 卖点不 visible
- **timeout 参数在不同 wire 上需要重标定**：benign wire 上 500ms 合理，lossy wire 上 5ms 才有意义
- **Hybrid vs semirdma 的真实差距需要 lossy wire 验证**：当前 benign wire 上两者不可区分

---

## 1. 下一步优先级（Phase 4 Lossy Wire Validation）

按 ROI 排。每条任务标注**单人工作量估计**。

### P0 — 申请第三台 CloudLab 节点（阻塞项，~1 day wall-clock）

**目标：** 在 amd203/amd196 的同一交换机 hop 上拉起第三个 amd 节点作为 **hammer** 机器，跑背景流把 25 GbE 交换机 link 饱和到 95%+，制造 switch queue overflow → natural packet drop。

**动作：**
1. CloudLab 上申请 `amd*.utah.cloudlab.us` 第三节点（应该跟 amd203 同 rack / 同 ToR）
2. 验证三节点同一 experiment LAN（`10.10.1.x`）
3. 安装 `iperf3` + `D-ITG`（如需 burst 流量）
4. 确认交换机 PFC off、ECN off（都在 `scripts/cloudlab/link_setup.sh` 默认）

**产物：** 可执行的 `scripts/cloudlab/hammer_iperf.sh`，能把 link 拉到指定 utilization 区间（50% / 80% / 95%）

### P1 — Lossy wire 下 hybrid vs semirdma 5-cell 最小矩阵（~2 h）

**矩阵：**
- transport ∈ {semirdma, semirdma_hybrid}
- timeout_ms ∈ {5, 50, 500}
- background load ∈ {0%, 80% link util}
- seed = 42（minimum viable）
- L=0, 500 step ResNet-18 / CIFAR-10

**判定：**
- 若 `hybrid + t=5ms + 80% load` 的 final loss 明显优于 `semirdma + t=5ms + 80% load`：hybrid 在 lossy+tight-timeout 场景有价值 → **paper 核心卖点成立**
- 若两者接近：hybrid 在 lossy wire 也不必需 → paper 需要其他 motivation

### P2 — Lossy wire 下 5-transport tail-latency 对比（~6 h）

**矩阵：**
- transport ∈ {gloo, rc_baseline, rc_lossy, semirdma, semirdma_hybrid}
- background load ∈ {0%, 50%, 80%, 95%}
- timeout_ms = 5 (tight，tail-hiding 目标操作点)
- seed = {42, 123, 7}
- L=0, 500 step

**主指标：**
- iter_time p50 / p99 / p99:p50 ratio（OptiReduce 对标指标）
- Final train loss 3-seed mean
- Effective bucket drop rate（从 `n_missing` log 聚合）

**预期输出：** 一张 4×5 的 tail-latency Pareto 表，能画成 paper 的核心 figure

### P3 — 3-seed convergence 确认（**部分完成，节点释放截断**）

2026-04-23 节点释放前在 amd203/amd196 上启动了 4-cell 3-seed 矩阵，只跑完 1/4：

- ✅ `semirdma + t=500 + seed 123` → final loss **1.284**（vs RC 1.121，gap +0.16）
- ⚠️ `semirdma + t=500 + seed 7` → step 400/500 处中断（last loss 1.10）
- ❌ `hybrid + t=500 + seed 123` → 未启动
- ❌ `hybrid + t=500 + seed 7` → 未启动

**关键发现**：seed 42 的 0.015 gap 是 seed-lucky，seed 123 的真实 gap 是 0.16。phase3-final §5 已更新为 2-seed mean（semirdma+t=500 = 1.080 vs RC-Baseline = 0.991，+0.089 gap）。

**剩余工作**：新节点到手后把 hybrid+t=500 × seed 123/7 + semirdma+t=500 seed 7 补齐（~25 min）。

---

## 2. Stage 2（可选，并行）—— Hybrid C++ RC phase 2

**当前 hybrid 实现**：phase 2 走 gloo TCP all-gather。gloo 不是 RDMA，走 kernel TCP 路径，有额外 ~0.5ms 开销 per bucket。

**Stage 2 目标：** 在 C++ `UCQPEngine` 里加 **RC QP 支持**，hybrid phase 2 走真 RC RDMA（Write + completion），`torch.distributed.all_gather_into_tensor` 换成自定义 RC ring-broadcast。

**价值：**
- 消除 gloo TCP 开销，hybrid 的 iter_time overhead 从 +50% 降到 +5-10%
- 支持 world_size > 2 的 ring topology（当前 hybrid 仅 2-rank）
- 论文框架更干净：两阶段都是 RDMA，不引入 TCP 依赖

**工作量：** 估 3-5 天（读 ibv RC QP state machine + 实装 + 跨节点测试）

**何时做：** 如果 P1 显示 hybrid 在 lossy wire 有价值，Stage 2 值得做（论文图的"真 RDMA hybrid"更漂亮）；如果 P1 否决 hybrid，Stage 2 放弃。

---

## 3. Paper 写作启动清单（P1/P2 完成后）

### 3.1 叙事结构草案

1. **Motivation**: Cloud RoCE + RC tail latency problem (OptiReduce-aligned) + UC 的 silent loss problem
2. **Insight**: SGD tolerates 1–5% chunk loss; transport 可以 trade reliability for tail
3. **Design**: UC QP + chunk-level `{has_cqe, valid_len}` + CQE-driven ratio controller + ghost mask
4. **Correctness safeguard** (hybrid): UC reduce-scatter + RC all-gather eliminates rank drift under tight-timeout lossy regime
5. **Evaluation**:
   - Phase 2 RQ1/RQ2/RQ4 结论（SoftRoCE 参数空间）
   - Phase 3 Stage A DDP 集成正确性（A1 bit-for-bit + A2 monotone degradation）
   - Phase 4 Lossy wire head-to-head 5-transport（P1/P2 结果）

### 3.2 需要补的数据（paper 要求）
- ✅ UC vs RC wire tail latency probe (`rq_hybrid_tail_probe.sh`)，证 CX-5 wire 基本 benign
- ❌ Lossy wire 下 5-transport tail 对比 (P2)
- ❌ TTA (time to accuracy) 对比 — 当前只有 train loss at step 500
- ❌ 大模型验证（ResNet-50 / GPT-2）— 当前只有 ResNet-18

### 3.3 submission timeline
- INFOCOM 2027 abstract 2026-07-17 / full paper 2026-07-24
- 倒排推，Phase 4 (P0+P1+P2) 需要在 **2026-05 月底** 前完成才留出足够写作时间
- 当前 4-23，还有约 5 周完成 lossy validation + 数据回填

---

## 4. 不做的事（scope 锁定）

- **不做** 真机 > 2 节点 ring AllReduce 的端到端训练验证：留给未来工作
- **不做** 其他 workload（除 ResNet-18/50, GPT-2/BERT）
- **不做** CUDA RDMA / GPUDirect（CPU tensor staging 路径已经可行）
- **不做** 生产级错误恢复 / QP 断线重连
- **不做** 非 Mellanox NIC（Broadcom / Intel E810 等）

---

## 5. 风险 & Plan B

| 风险 | 概率 | 影响 | Plan B |
|---|:-:|:-:|---|
| CloudLab 第三节点申请不到 | 低 | 高 | 改用 MX 类节点（GPU + ConnectX）或回退 D-ITG flood 单节点模拟 |
| Lossy wire 上 hybrid 相对 semirdma **没有** 显著优势 | 中 | 中 | Paper 叙事从 "hybrid 是核心设计" 退到 "hybrid 是 conditional safeguard"；卖点回归 timeout-tuned semirdma |
| 真实 lossy wire 的 tail 并不被 RC retx 主导 | 中 | 高 | 在 paper 里诚实报告 + 引用 OptiReduce 的 wire 模型；强调 Cloud lossy RoCE 的 target environment |
| 第三节点交换机 queue 不 overflow（硬件太强） | 低 | 中 | 用 D-ITG burst / 调整 buffer limit / 引入第四节点 |
| INFOCOM ddl 撞到 paper 没收敛 | 低 | 高 | 转投 SoCC 2026 R2（ddl 2026-07-14，早 10 天但审稿窗口短） |

---

## 6. 本周行动项（2026-04-23 起）

1. **立刻**：申请 CloudLab 第三节点（amd 系列，同 rack amd203）
2. **今天 / 明天**：P3 3-seed 补齐（benign wire 上 hybrid + semirdma × seed 123, 7 × timeout=500）
3. **本周**：P0 hammer 脚本 + link saturation sanity
4. **下周**：P1 5-cell 最小矩阵 (decides hybrid 命运)
5. **两周内**：P2 完整 5-transport 矩阵 (paper 核心 figure 数据)

---

## 7. 开放的工程债

保留但不阻塞 paper 的技术债（Phase 5 submit 后再碰）：

- [ ] `src/transport/layer_analyzer.{h,cpp}` — Phase 3 Stage C 原计划的 per-layer 重要性 scoring，后来推迟。当前 chunk_bytes 固定 16KB，没按层自适应
- [ ] `semirdma_hybrid_allreduce_hook` 仅支持 `world_size=2`，扩 N-rank ring 是 Stage 2 任务
- [ ] Python hook 里 `import numpy as np` 在 _HOOK_LOCK 内部（性能 hot path），应提到 module-level
- [ ] CloudLab session 间的 dataset 重新 stage 每次都花 ~5 min，应做成 shared NFS 或 pre-cached
