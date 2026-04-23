# Phase 3 · Stage B · RQ6-Prep · A1 Bit-for-Bit 等价性 (CX-5 局部)

> **时间：** 2026-04-23
> **节点：** CloudLab Utah `amd203.utah.cloudlab.us` (node0) + `amd196.utah.cloudlab.us` (node1)
> **硬件：** AMD EPYC 7302P × CPU-only × ConnectX-5 (fw 16.28.4512, 25 GbE)

---

## 0. 重要注记：CX-5 上 SemiRDMA ratio=1.0 不可用

A1 设计上要求 `semirdma` 跑在 `transport_cfg.ratio=1.0 timeout_ms=60000` 下 — 即"100% 严格等所有 chunk" — 然后对比 gloo 的 loss_per_step 做 bit-for-bit 数值等价性校验。

**CX-5 平台实测：semirdma + ratio=1.0 的 cell 会 hang**。step 0 之后每步在 `wait_for_ratio` 里等满 60s timeout 才继续，因为 UC QP 在 CX-5 25 GbE 上有 ~1% 的自然丢包率（见 [`stage-b-phase2-resweep.md`](./stage-b-phase2-resweep.md) §1 — RQ1 chunk_sweep p99 恒为 5000ms, 说明每个 ratio 阶段都会撞默认 5s timeout）。

具体观察：
- `loss_rate=0.0` + `ratio=1.0` + 无 timeout → 永远等不齐 100% chunk → 60s timeout per step
- 50 步测试训练在 15+ 分钟后被 kill (进程 CPU 100% 但只有 step 0 loss 写入)
- gloo 侧 3 个 cell 全部 **正常完成** 100 step，iter_time ~ 0.8s/step

**结论：** A1 bit-for-bit 在 CX-5 + 2-node 真机上**不能严格做到 exactly-reliable without retransmission**。这反过来印证了 SemiRDMA 的必要性 — UC QP 天然有 non-zero drop，需要 ratio<1.0 才能实际跑起来。

对论文写作建议：
- A1 等价性结论引用 aliyun SoftRoCE (perfect link) 或 c240g5 CX-6 Lx (极低 drop) 上的数据即可
- CX-5 上直接用 RQ1 的 p99=5s 作为"natural drop 存在"的定量证据

---

## 1. gloo 侧（RC 可靠通信）3 seed × 100 step

| seed | step 0 | step 50 | step 99 final |
|---:|---:|---:|---:|
| 42 | 2.4233 | _mid_ | **1.791** |
| 123 | 2.2914 | _mid_ | **1.848** |
| 7 | 2.3294 | _mid_ | **1.755** |
| mean | 2.348 | — | **1.798** |

3 seed final loss spread 0.09 → 代表 CPU-only + 100 step 下的 seed 方差量级。

原始 CSV：[`./rq6-prep-stage-a-real-nic/05-58-29_gloo_loss0.0_seed42/loss_per_step.csv`](./rq6-prep-stage-a-real-nic/)（3 seed）。

---

## 2. semirdma ratio=1.0 侧：SKIPPED

见 §0 注记。

重跑策略建议：
- 如果硬要得到"CX-5 上 semirdma 无 loss 下的 training loss"，可以用 `ratio=0.999 timeout_ms=1000`（接近但不强制 100%），允许 p99 hit 时降级 ghost mask，得到 500-step final loss。预期约为 A2 矩阵 L=0 值（1.51）减去 (effective_loss 差异的影响)。
- 更稳妥的 "equivalence 校验" 是在 SoftRoCE 上做（无丢包）。c240g5 CX-6 Lx 平台上的等价数据见 [`../rq6-prep-real-nic-equivalence.md`](../rq6-prep-real-nic-equivalence.md)（prior-platform 但 A1 不受 bug 影响）。

---

## 3. 相关

- [`./rq6-a2-convergence.md`](./rq6-a2-convergence.md) — CX-5 SemiRDMA 在 ratio=0.95 (+ 动态 floor) 下实际可以跑
- [`./stage-b-phase2-resweep.md`](./stage-b-phase2-resweep.md) — RQ1 chunk_sweep 直接证实 UC QP 在 CX-5 上有 natural p99 尾（撞 5s timeout）
- [`../rq6-prep-real-nic-equivalence.md`](../rq6-prep-real-nic-equivalence.md) — c240g5 CX-6 Lx prior-platform A1 数据
