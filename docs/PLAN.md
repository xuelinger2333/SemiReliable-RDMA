# SemiRDMA 进度 & 下一步计划

> **最后更新：** 2026-04-29
> **当前阶段：** **Phase 5 (planning) — CLEAR pivot**。基于 [deep-research-report.md](../deep-research-report.md) 的判断，论文 headline 从「layer-aware loss tolerance」转向 **CLEAR — Completion-Labeled Erasure Attribution for RoCE UC**（见 [phase5/PHASE5_PLAN.md](phase5/PHASE5_PLAN.md)）。Phase 4 PR-A/B/C 工作降级为 scaffolding / ablation。
> **关键前置文档：** [phase5/PHASE5_PLAN.md](phase5/PHASE5_PLAN.md) + [phase5/clear-design.md](phase5/clear-design.md) + [phase5/code-reorg.md](phase5/code-reorg.md) + [phase5/experiments.md](phase5/experiments.md) + [phase4/hybrid-dead-end.md](phase4/hybrid-dead-end.md) + [phase4/prb-results.md](phase4/prb-results.md)
>
> **Phase 5 决策点未确认（user sign-off pending）：** 见 [phase5/PHASE5_PLAN.md §9](phase5/PHASE5_PLAN.md)。下方 Phase 4 章节保留为完成状态参考。

---

## 0. 当前 snapshot

### 0.1 已完成
- Phase 1 UC QP 语义四条硬约束（P0 实验）
- Phase 2 Core Transport C++ 四模块 + RQ1/RQ2/RQ4 SoftRoCE 验证
- Phase 3 Stage A（aliyun SoftRoCE DDP 数值正确性 A1+A2）
- Phase 3 Stage B（CloudLab 真 NIC）CX-6 Lx (c240g5) 归档 + CX-5 (amd203/amd196) post-fix 主数据
- Phase 3 B.5 RC-Baseline / RC-Lossy 12-cell 对照
- Ratio controller effective-loss bug 修复（commit `9386f2e`）
- **Phase 4 XDP 中间盒平台（amd186）**：ARP-spoof "bump in the wire" + Bernoulli drop on UDP:4791，端到端校准 drop_pct 准确到 ±0.1%，见
  [scripts/cloudlab/middlebox_setup.sh](../scripts/cloudlab/middlebox_setup.sh)
  + [xdp_dropbox/xdp_dropbox.bpf.c](../scripts/cloudlab/xdp_dropbox/xdp_dropbox.bpf.c)
- **Phase 4 P1 lossy-wire 矩阵（20 cells, 2–3 seeds）**：semirdma vs
  semirdma_hybrid × drop ∈ {0, 0.01, 0.05, 0.1} × timeout=50ms × STEPS=500
- **Hybrid 删除（2026-04-25）**：hybrid 在所有 drop 档位严格劣于 pure
  semirdma（final loss 高 +0.03～+0.64，iter_ms 高 +11%）。详见
  [phase4/hybrid-dead-end.md](phase4/hybrid-dead-end.md)。代码移除：commit `7257186`

### 0.2 已锁定的设计结论（不再改动）
- `chunk_bytes = 4096`（CX-5 path_mtu, Phase 3 Stage B）
- `ratio = 0.95` 作为 floor，flat-mode dynamic target = `max(0.95, 1 − loss − 0.005)`
- `GhostMask::apply` 默认 on
- **两条 UC-backed hook**：
  - `semirdma_allreduce_hook`（flat-ratio mode，paper 主对照）
  - `layer_aware_dispatcher_hook`（per-layer p_L mode，PR-A 落地）
- **GID idx 3**（RoCE v2 IPv4-mapped）是 middlebox ARP-spoof 有效的前提；
  `run_p1_matrix.sh` 在 `MIDDLEBOX_HOST` 非空时自动 pin
- **PR-A 设计三件套**（commit `33e7c57`）：
  - `LossToleranceRegistry`（per-module p_L + instance-level default_p）
  - `WireCalibrator`（continuous EMA on ε / σ_jitter / B from training traffic，no probe burst）
  - `layer_aware_dispatcher_hook`（per-bucket safety check `p < eps_global + margin`）

### 0.3 已知 open 问题
- **PR-B 数据已得**：[phase4/prb-results.md](phase4/prb-results.md)
  - drop=0：layer_aware vs flat: Δfinal_loss = +0.005 (NS), iter_ms = -11%
  - drop=0.01：Δ = +0.10 (~1.5σ), iter_ms = -20%
  - drop=0.05：Δ = +0.13 (~1.5σ), iter_ms = -20%
  - 故事：layer_aware 在 uniform p=0.10 下用 final_loss 换 iter_ms。
- **PR-C 阻塞 paper per-layer 卖点**：default `bucket_cap_mb=512` 让 ResNet-18
  全部参数挤进一个 bucket，per-bucket routing 退化为 per-step 单一决策。
  必须先实现 imm_data bucket_id encoding 才能让 BN/conv/fc 真正走不同路径。
- **NIC tail rare crash**：PR-B v3 18 cell 中 1 cell 在 SEED=123 drop=0.05
  layer_aware 下出现 39% bucket-1 delivery + RC fallback timeout。3 次 isolated
  rerun 全部成功；归类为 transient，根因未刨清，可能跟 matrix-sequence 状态
  / dispatcher race 有关。PR-C 后需要重新评估。
- **timeout 参数跨 wire 的重标定**：layer-aware 用 calibrator 推导 T_max
  替代了固定值；flat semirdma 仍然依赖 `cfg.timeout_ms = 200`。Stage B paper
  补一个 `T_max(L)` ablation。

---

## 1. 下一步优先级（Phase 4 完成 + Paper 主数据）

### P0 ✅ 完成 — XDP middlebox 平台（2026-04-24）

amd186 上 XDP eBPF 单口 "bump in the wire" 转发 + ARP/IPv6 邻居欺骗。
端到端实测 drop_pct 准确到目标 ±0.1%（13.4 M RoCE packets，drop=0.01
设定下实测 0.9966%）。见
[scripts/cloudlab/middlebox_setup.sh](../scripts/cloudlab/middlebox_setup.sh)。

### P1 ✅ 完成 — hybrid vs semirdma 裁决（2026-04-24/25）

20 cells 实验（3 seeds × drop ∈ {0, 0.01} + 2 seeds × drop ∈ {0.05, 0.1}）
给出明确答案：hybrid 在所有测过的操作点下都严格劣于 pure semirdma，
且 iter_ms 贵 +11%。**hybrid 已删除**。详见
[phase4/hybrid-dead-end.md](phase4/hybrid-dead-end.md)。

Raw data: [phase4/raw_data/aggregate_final.csv](phase4/raw_data/aggregate_final.csv)

### P2 ✅ 完成 — semirdma vs RC 三方对照（2026-04-25）

3 seed × 3 transport (`rc_rdma`, `rc_lossy`, `semirdma`) × 3 drop
(0, 0.01, 0.05) × STEPS=500 × t=200ms = 27 cells，3.4 h。
归档：[phase4/raw_data/p0_3seed_ref_20260425_110928/](phase4/raw_data/p0_3seed_ref_20260425_110928/)

主结论：
- drop=0：rc_rdma == rc_lossy（math-equivalent in last-50 mean） ≈ semirdma；
  3-seed std 0.04，原"0.057 advantage" 是 seed 噪声
- drop=0.01 / 0.05：**rc_rdma 全部 IBV_WC_RETRY_EXC_ERR 崩溃**（每 seed 都崩），
  semirdma 全程 converge → paper 主故事 "RC 崩 vs SemiRDMA 存活" 成立
- iter_ms：rc_rdma > semirdma > rc_lossy（layer-aware 后续显示 iter_ms 优势）

侧产品发现：phantom ghost residual 跟 final_loss Pearson r=0.951
（[scripts/analysis/ghost_vs_loss.py](../scripts/analysis/ghost_vs_loss.py)），
NIC tail variance 在同 seed 下能让 total_missed 摆 100×，
是 PR-A layer-aware mode 引入的核心动机。

### PR-A ✅ 完成 — Layer-aware mode 落地（2026-04-26 commit `33e7c57`）

opt-in transport mode `cfg.layer_aware=True`：
- per-layer p_L registration（`LossToleranceRegistry`）
- continuous wire calibration via training traffic（`WireCalibrator`，no probe burst）
- per-bucket dispatcher：`p < ε_global + margin` → RC，否则 SemiRDMA UC，
  `ratio = 1 − p_bucket`，`T_max(L) = T_min + K·σ_jitter`

31/31 unit tests pass，包括 1 个 RDMA-gated E2E loopback。
两次 follow-up 修复：
- commit `967703e` (Hypothesis M)：cross-rank `eps_ema` all_reduce
- commit `9e18230` (Hypothesis N)：calibrator 用 post-drain `n_completed`

### PR-B ✅ 完成 — Real-NIC layer-aware vs flat 对照（2026-04-26）

3 seed × 2 transport × 3 drop = 18 cells，~2.1 h wall。
归档：[phase4/raw_data/p0_prb_v3_20260426_100315/](phase4/raw_data/p0_prb_v3_20260426_100315/)

| drop | flat semirdma | layer_aware | Δ final_loss | Δ iter_ms |
|---:|---:|---:|---:|---:|
| 0 | 1.0558±0.027  854 ms | 1.0611±0.060  758 ms | +0.005 (NS) | **−11%** |
| 0.01 | 1.0533±0.066  963 ms | 1.1562±0.027  772 ms | +0.103 (~1.5σ) | **−20%** |
| 0.05 | 1.0062±0.046  944 ms | 1.1634±0.072  774 ms | +0.157 (~1.5σ) | **−20%** |

故事：layer_aware 用 +0.10 final_loss 换 -20% iter_ms（per-bucket counter
threshold 比 flat 99.5% 早出）。但**这是 uniform p=0.10 下的故事**，
不是 paper 想要的 per-layer heterogeneous benefit。要那个，必须 PR-C。

详见 [phase4/prb-results.md](phase4/prb-results.md)。

### PR-C 🔜 下一步 — Per-bucket routing via imm_data bucket_id encoding

**为什么必须**：default `bucket_cap_mb=512` 让 ResNet-18 整模型挤进 1 bucket，
per-bucket routing 退化为 per-step 单一决策。要想 demo "BN→RC, conv→SemiRDMA, fc→borderline"，
必须 `bucket_cap_mb=1` 的 ~50 个 bucket，但当前 imm_data 编码是 chunk_id
within bucket（0..N-1）—— 跨 rank 的 concurrent buckets 会 alias chunk_id，
receiver 把 bucket K+1 的 CQE 标到 bucket K 的 cs 上，bucket K+1 全部 ghost-mask 归零。
（这点已在 `experiments/stage_a/train_cifar10.py:194-204` 注释里记录）

**协议改动**：
```
imm_data = (bucket_id_mod256 << 24) | (chunk_id & 0xffffff)
             8 bits                       24 bits (16M chunks)
```

ResNet-18 at bucket_cap_mb=1 ≈ 50 buckets/step，bucket_id 每 5 步循环一次，
8 bits 充足。

**需要改的文件**：
- `src/transport/ratio_controller.{h,cpp}`：`wait_for_ratio` 多一个
  `expected_bucket_id` 参数，foreign-bucket CQE 走 pending queue
- `src/transport/chunk_set.{h,cpp}`：API 不变（chunk_id 仍是 24 bit local）
- `src/bindings/py_semirdma.cpp`：更新绑定
- `python/semirdma/transport.py`：`post_gradient(bucket_id=...)`,
  `await_gradient(bucket_id=...)`，前置 drain pending queue
- `python/semirdma/hooks.py` / `layer_aware/dispatcher.py`：传 bucket_id
- `experiments/stage_a/train_cifar10.py`：`bucket_cap_mb` 改为 YAML knob
- 单元测试 + E2E 测试更新

**估算**：~1 工作日（C++ + binding 3-4 h，Python 1-2 h，测试 2-3 h，validation 1 h）

**判定**：
- pass criterion 1：`bucket_cap_mb=1` + uniform p=0.05 训练完成无 collision
- pass criterion 2：heterogeneous registry（BN p=0, conv p=0.05, fc p=0.01）
  下 dispatcher DIAG 显示 mixed RC + SemiRDMA per-step
- pass criterion 3：`p0_prb_v3` 的 18 cells 在 `bucket_cap_mb=512` 下行为不变
  （回归测试）

### PR-D 候选 — Heterogeneous p_L sweep + 5+ seed extension

PR-C 落地后才能跑：
- 3+ seed × {flat semirdma, semirdma_layer_aware (uniform p=0.10),
  semirdma_layer_aware (heterogeneous BN=0/conv=0.05/fc=0.01)} × 3 drop
- ≥ 5 seed extension 把 std 压到 paper-grade

时间：~3-4 h on 类似 amd203 的节点。

### P3 — 更大模型 / 更长训练（写作 sprint 前）

paper reviewer 问 "ResNet-small 500 步够不够" 的概率非 0。PR-D 给出方
向后，选 1 个 hero cell（drop=0.01 或 0.05）重跑：
- model: ResNet-50 或 GPT-2-small
- STEPS = 3000
- 2 seeds

用于 "conclusion holds at scale" 的补充实验。**不现在做**，留到 PR-D
结论明确后再决定是否需要。

---

## 3. Paper 写作启动清单（PR-C/PR-D 完成后）

### 3.1 叙事结构草案

1. **Motivation**: Cloud RoCE 下 RC 的 tail-latency / reliability-under-loss
   problem（对标 OptiReduce）+ UC 的 silent-loss problem
2. **Insight**: SGD tolerates 1–5% chunk loss; transport 可以 trade
   reliability for tail；**且**不同层的容忍度不同，应该让 application 显式声明
3. **Design**:
   - UC QP + chunk-level `{has_cqe, valid_len}` + CQE-driven ratio controller + ghost mask（flat-ratio 形态，PR-A 之前的版本）
   - **Layer-aware extension**：per-layer p_L registry + per-bucket dispatcher
     between RC (tight budget) and SemiRDMA UC (lossy budget) + continuous
     wire calibration (ε / σ_jitter / B from training traffic, no probe burst)
   - T_max(L) 从 wire physics 推导，仅作异常兜底
4. **Negative result A (honest reporting)**: hybrid UC + gloo-reliable-
   broadcast correctness-safeguard 在实测下 **被 pure semirdma 严格支配**，
   因为 magnitude compensation 放大方差比 drift 本身更伤 SGD。删掉。
   见 [phase4/hybrid-dead-end.md](phase4/hybrid-dead-end.md)
5. **Negative result B (honest reporting)**: L.2 quiescent drain
   wedges after bucket 7 on CX-5。L-only bounded loop 落地，phantom
   ghost ~0.03% 残留作为 known caveat。见 [DEBUG_LOG.md](../DEBUG_LOG.md) Hypothesis L.2。
6. **Evaluation**:
   - Phase 2 RQ1/RQ2/RQ4 结论（SoftRoCE 参数空间）
   - Phase 3 Stage A DDP 集成正确性（A1 bit-for-bit + A2 monotone degradation）
   - Phase 4 P2 — **semirdma vs rc_rdma + rc_lossy** ✅ 27 cells, 3 seed
     ([phase4/raw_data/p0_3seed_ref_*/](phase4/raw_data/)). 主结论：
     drop>0 时 rc_rdma 全 IBV_WC_RETRY_EXC_ERR 崩溃；semirdma converge
   - Phase 4 PR-B — **flat semirdma vs layer_aware (uniform p=0.10)**
     ✅ 18 cells, 3 seed ([phase4/prb-results.md](phase4/prb-results.md))
   - Phase 4 PR-C — **layer_aware heterogeneous p_L (BN=0/conv=0.05/fc=0.01)**
     ❌ 待跑（依赖 imm_data bucket_id encoding）
   - 可能需要补：OptiReduce / gloo-UDP baseline（后续）

### 3.2 需要补的数据（paper 要求）
- ✅ P1 hybrid 裁决（已否决并删除）
- ✅ P2 semirdma vs RC × 3 drop × 3 seed（27 cells, 已归档）
- ✅ PR-B layer_aware vs flat × 3 drop × 3 seed（18 cells, 已归档）
- ❌ **PR-C heterogeneous-p_L sweep**（优先级 #1，~1 工作日实现 + ~2 h compute）
- ❌ TTA (time to accuracy) 对比 — 当前只有 train loss at step 500
- ❌ 大模型验证（ResNet-50 / GPT-2）— 当前 ResNet-18 500 步
- ❌ **其他 baseline**：OptiReduce (gloo-UDP + Hadamard)、MLT 等，
  PR-C 完成后排（用户明确标记为"之后再说"）

### 3.3 submission timeline
- INFOCOM 2027 abstract 2026-07-17 / full paper 2026-07-24
- 当前 2026-04-27，剩 ~12 周
- PR-C 预期 ~1 工作日实现 + 节点重申请 + ~3-4 h compute
- 之后留 10+ 周给大模型补数据 + paper 写作

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
| ✅ P2 下 RC 不像预期那样崩溃 | 已落地 | — | RC 在 drop=0.01/0.05 全 IBV_WC_RETRY_EXC_ERR；故事成立 |
| ✅ rc_baseline / rc_lossy hook 被 hammer 误伤 | 已检查 | — | 没有 |
| **PR-C 实现复杂度被低估** | 中 | 中 | C++ 改动若 >2 day，回退 cross-rank-barrier 方案（iter_ms 退化但不阻塞 paper） |
| **PR-C 之后 NIC tail crash 仍然 ~5%** | 中 | 中 | rerun policy + crashed cell 标记，paper 里写明 "with rerun-on-crash" methodology caveat |
| 大模型（ResNet-50 / GPT-2）在 CPU-only 节点上跑太慢 | 中 | 中 | 只跑 hero cell + TTA 代替 500-step loss；或推迟到 GPU 节点 |
| ✅ CloudLab amd203/amd196/amd186 被释放（2026-04-27 已发生）| 已发生 | 中 | 所有数据已 archive 到 [phase4/raw_data/](phase4/raw_data/)；脚本已 push origin；节点重申请后 `bootstrap_fresh_node.sh` 一键恢复 + `middlebox_setup.sh bootstrap` 一次 |
| INFOCOM ddl 撞到 paper 没收敛 | 低 | 高 | 转投 SoCC 2026 R2（ddl 2026-07-14，早 10 天但审稿窗口短） |

---

## 6. 接下来的工作（2026-04-27 起，节点已释放）

1. **本地工作（无需节点）**：
   - **PR-C 实现** — bucket_id-in-imm 协议改动（C++ + Python + tests）。
     Local-only：unit tests 在 Linux dev 环境跑，loopback E2E 跑通就行。
     ~1 工作日。
   - 写 paper outline（基于 PR-B 已有结论 + PR-C 计划），并行做。
2. **节点重申请后**：
   - PR-C E2E 真机回归（重跑 PR-B v3 矩阵，验证 18 cells 结果跟之前一致）
   - PR-C heterogeneous registry 验证（mixed-route DIAG + 收敛）
   - PR-D：5+ seed 扩展 + heterogeneous-p_L sweep
3. **两周内**：大模型 hero cell + TTA 补充（paper 补弹药）。
4. **paper writing sprint**（5-6 周后启动）：基于 PR-B + PR-C 数据.

---

## 7. 开放的工程债

保留但不阻塞 paper 的技术债（Phase 5 submit 后再碰）：

- [ ] `src/transport/layer_analyzer.{h,cpp}` — Phase 3 Stage C 原计划的 per-layer 重要性 scoring，后来推迟。PR-A 用静态 registry 替代 dynamic scoring，本目录文件可清理
- [ ] Python hook 里 `import numpy as np` 在 _HOOK_LOCK 内部（性能 hot path），应提到 module-level
- [ ] CloudLab session 间的 dataset 重新 stage 每次都花 ~5 min，应做成 shared NFS 或 pre-cached
- [ ] `middlebox_setup.sh status` 的 `rate : ? ppm (?%)` 显示 bug — bpftool json 解析某条路径失败；stats 本身正常
- [ ] `parse_cell` 读 loss_per_step.csv 时的 `tr -d '\r'` 是 Windows-CRLF 兼容补丁；根因在 training 写 CSV 没强制 LF
- [ ] runner 的 final_loss 抽样仍然是 raw step-499，paper 用法应改为 last-50 mean。已在 [scripts/analysis/matrix_aggregate.py](../scripts/analysis/matrix_aggregate.py) 实现，应该 inline 到 runner 的 `parse_cell` 函数
- [ ] PR-C 之后：把 `bucket_cap_mb=512` 的硬编码移到 YAML knob，并文档说明 trade-off
- [ ] WireCalibrator 的 `t_max_min_ms = 5` 在 small-bucket 模式下可能过大（每个 small bucket 物理 T_min 是亚毫秒）。PR-C 落地后回头降到 1ms 看 NIC tail variance 影响
