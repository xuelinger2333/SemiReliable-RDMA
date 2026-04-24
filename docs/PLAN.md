# SemiRDMA 进度 & 下一步计划

> **最后更新：** 2026-04-25
> **当前阶段：** Phase 4 — XDP 中间盒 lossy-wire 平台贯通；hybrid 已删；paper 核心对照待跑
> **关键前置文档：** [phase2/phase2-final.md](phase2/phase2-final.md) + [phase3/phase3-final.md](phase3/phase3-final.md) + [phase4/hybrid-dead-end.md](phase4/hybrid-dead-end.md)

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
- `chunk_bytes = 16384`（Phase 2 RQ1）
- `ratio = 0.95` 作为 floor，dynamic target = `max(0.95, 1 − loss − 0.005)`
- `GhostMask::apply` 默认 on
- **唯一 UC-backed hook**：`semirdma_allreduce_hook`（hybrid 删除后）
- **GID idx 3**（RoCE v2 IPv4-mapped）是 middlebox ARP-spoof 有效的前提；
  `run_p1_matrix.sh` 在 `MIDDLEBOX_HOST` 非空时自动 pin

### 0.3 已知 open 问题
- **pure semirdma 在 0–10% wire drop 下稳健 converge**（final_loss
  0.87–1.36），但 **vs. RC-baseline 的直接对比还没跑**。paper 核心卖点
  ("UC-based semi-reliable > RC 在 lossy wire 下") 需要 P2 填数据
- **timeout 参数跨 wire 的重标定**：benign wire 上 500ms，lossy wire 上
  50ms 看起来合理，但 drop=0.05/0.1 下是否有更 aggressive 的 5ms 操作点
  能让 RC 崩得更彻底 / semirdma 扛得更清晰，值得扫 1 档

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

### P2 🔜 下一步 — semirdma vs RC-baseline 在 wire drop 下的对比（paper 主数据）

**核心卖点**：UC-based semirdma 在 wire loss 下仍 converge；RC（标准
可靠 RDMA）在 wire loss 下崩溃（ib_write_bw smoke：1% drop → BW 从
9 G 掉到 0.23 G，−97%）。这才是 paper 的 main figure。

**矩阵（初版）：**
- transport ∈ {semirdma, rc_baseline, rc_lossy}
- drop_rate ∈ {0, 0.001, 0.005, 0.01, 0.05}（5 档）
- timeout_ms = 50（semirdma 用；RC 忽略此字段）
- seed ∈ {42, 1337}（先 2 seed 看方向，后续扩到 3）
- STEPS = 500
- = 3 × 5 × 2 = 30 cells ≈ 3.5 h

**主指标：**
1. final_loss 2-seed mean（convergence story）
2. mean_iter_ms + iter_ms p50 / p95（tail latency story）
3. bucket-level effective_drop_rate（从 semirdma completion log 聚合 vs. middlebox 配置值）

**判定：**
- **primary**: 存在 drop rate 区间 `[d_lo, d_hi]`，其中 rc_baseline
  final_loss 显著高于 semirdma（>0.2，~4× seed variance）→ paper 主故
  事成立
- **secondary**: rc_baseline iter_ms 随 drop 上升而爆涨（retx 效应）
  → tail story 对照 OptiReduce

**前置检查（~5 min）：** `rc_baseline` / `rc_lossy` transport 分支在
`train_cifar10.py` 和 `semirdma.baselines` 里应还在；但因今天 hammer
删除时没动 baselines，需 single-cell smoke 确认 import 不 broken。

### P3 — 更大模型 / 更长训练（写作 sprint 前）

paper reviewer 问 "ResNet-small 500 步够不够" 的概率非 0。P2 给出方
向后，选 1 个 hero cell（drop=0.01 或 0.05）重跑：
- model: ResNet-50 或 GPT-2-small
- STEPS = 3000
- 2 seeds

用于 "conclusion holds at scale" 的补充实验。**不现在做**，留到 P2
结论明确后再决定是否需要。

---

## 3. Paper 写作启动清单（P2 完成后）

### 3.1 叙事结构草案

1. **Motivation**: Cloud RoCE 下 RC 的 tail-latency / reliability-under-loss
   problem（对标 OptiReduce）+ UC 的 silent-loss problem
2. **Insight**: SGD tolerates 1–5% chunk loss; transport 可以 trade
   reliability for tail
3. **Design**: UC QP + chunk-level `{has_cqe, valid_len}` + CQE-driven
   ratio controller + ghost mask
4. **Negative result (honest reporting)**: hybrid UC + gloo-reliable-
   broadcast correctness-safeguard 在实测下 **被 pure semirdma 严格支配**，
   因为 magnitude compensation 放大方差比 drift 本身更伤 SGD。删掉。
   见 [phase4/hybrid-dead-end.md](phase4/hybrid-dead-end.md)
5. **Evaluation**:
   - Phase 2 RQ1/RQ2/RQ4 结论（SoftRoCE 参数空间）
   - Phase 3 Stage A DDP 集成正确性（A1 bit-for-bit + A2 monotone degradation）
   - Phase 4 P2 — **semirdma vs RC-baseline（± rc_lossy）** 在 wire
     drop 下的 convergence + tail 对照（待跑）
   - 可能需要补：OptiReduce / gloo-UDP baseline（后续）

### 3.2 需要补的数据（paper 要求）
- ✅ P1 hybrid 裁决（已否决并删除）
- ❌ **P2 semirdma vs RC-baseline × drop sweep**（优先级 #1）
- ❌ TTA (time to accuracy) 对比 — 当前只有 train loss at step 500
- ❌ 大模型验证（ResNet-50 / GPT-2）— 当前 ResNet-small 500 步
- ❌ **其他 baseline**：OptiReduce (gloo-UDP + Hadamard)、MLT 等，
  P2 完成后排（用户明确标记为"之后再说"）

### 3.3 submission timeline
- INFOCOM 2027 abstract 2026-07-17 / full paper 2026-07-24
- 当前 2026-04-25，剩 ~12 周
- P2 预期 ~1 天（含 rc_baseline smoke + 30-cell 矩阵 + 分析）
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
| P2 下 RC 不像预期那样崩溃（BW 不暴跌、final_loss 不明显高）| 中 | 高 | 在 paper 诚实报告；扩 drop rate 到 10-20%；或转 tail-latency-only 卖点（OptiReduce 风格）|
| rc_baseline / rc_lossy hook 被前几轮 hammer 清理误伤 | 低 | 中 | P2 前做 single-cell smoke；broken 就 git revert 局部 |
| 大模型（ResNet-50 / GPT-2）在 CPU-only amd203/amd196 上跑太慢 | 中 | 中 | 只跑 hero cell + TTA 代替 500-step loss；或推迟到 GPU 节点 |
| CloudLab amd203/amd196/amd186 被释放 | 中 | 高 | 已 push 所有脚本到 origin；节点重申请后 bootstrap_fresh_node.sh 一键恢复 + middlebox_setup.sh bootstrap 一次 |
| INFOCOM ddl 撞到 paper 没收敛 | 低 | 高 | 转投 SoCC 2026 R2（ddl 2026-07-14，早 10 天但审稿窗口短） |

---

## 6. 本周行动项（2026-04-25 起）

1. **今天收尾**：archive P1 结果 + 更新 PLAN.md + 删 hybrid 收尾验证 ✅
2. **下一步（立刻）**：P2 前置 — `rc_baseline` / `rc_lossy` transport
   的 single-cell smoke (drop=0, STEPS=100) 确认没被 hammer 清理误伤
3. **今晚 / 明天**：P2 主矩阵 —
   `DROP_RATES="0 0.001 0.005 0.01 0.05" TRANSPORTS="semirdma rc_baseline rc_lossy" TIMEOUTS_MS=50 STEPS=500`
   × 2 seeds = 30 cells ≈ 3.5 h
4. **下周**：P2 结果分析；决定是否需要更高 drop rate / 更长 STEPS
5. **两周内**：大模型 hero cell + TTA 补充（paper 补弹药）

---

## 7. 开放的工程债

保留但不阻塞 paper 的技术债（Phase 5 submit 后再碰）：

- [ ] `src/transport/layer_analyzer.{h,cpp}` — Phase 3 Stage C 原计划的 per-layer 重要性 scoring，后来推迟。当前 chunk_bytes 固定 16KB，没按层自适应
- [ ] Python hook 里 `import numpy as np` 在 _HOOK_LOCK 内部（性能 hot path），应提到 module-level
- [ ] CloudLab session 间的 dataset 重新 stage 每次都花 ~5 min，应做成 shared NFS 或 pre-cached
- [ ] `middlebox_setup.sh status` 的 `rate : ? ppm (?%)` 显示 bug — bpftool json 解析某条路径失败；stats 本身正常
- [ ] `parse_cell` 读 loss_per_step.csv 时的 `tr -d '\r'` 是 Windows-CRLF 兼容补丁；根因在 training 写 CSV 没强制 LF
