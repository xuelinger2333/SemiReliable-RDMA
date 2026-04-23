# Phase 3 Stage B · CX-5 (amd203/amd196) 结果树

> **启用时间：** 2026-04-23
> **平台：** CloudLab Utah `amd203` (node0) + `amd196` (node1)，实验名 `chen123-302000.rdma-nic-perf-pg0`
> **硬件：** AMD EPYC 7302P (16C/32T) × 125 GiB RAM × **CPU-only** (no GPU) × **Mellanox ConnectX-5** (fw 16.28.4512, 25 GbE, RoCEv2 GID 1, 实测 `ib_write_bw` = 24.39 Gbps = 97.6% 线速)
> **软件：** Ubuntu 22.04.2 LTS / Linux 5.15.0-168 / torch 2.11.0+cpu / uc_qp pybind11 (commit `9386f2e` + detect_rdma_dev + day0_check fixes)

---

## 为什么开新平台目录

- **Bug 修复后的 post-fix 数据必须与 pre-fix CX-6 数据物理分开**。pre-fix CX-6 数据归档到 [`../results-cx6lx25g-c240g5_archive/`](../results-cx6lx25g-c240g5_archive/)（见其 README）。
- **CPU-only + CX-5 + EPYC** 是新硬件 profile。之前 c240g5 是 Xeon Silver + P100 GPU + CX-6 Lx，性能模型不直接迁移。
- 本目录所有结果都是 **打磨阶段数据**，用于验证修复后方法的正确性和定性趋势。**论文最终数据**将在一个固定的长驻节点上重跑。

---

## 子目录规划（待 C.1–C.5 落盘）

| 子路径 | 矩阵内容 | 驱动脚本 | 预期 wall-clock |
|---|---|---|---|
| `rq6-prep-a2-real-nic/` | A2 SemiRDMA **post-fix** 12 cell: loss ∈ {0, 1, 3, 5}% × 3 seed × 500 step | [`run_a2_real_nic.sh`](../../../scripts/cloudlab/run_a2_real_nic.sh) | ~100 min |
| `rq6-b5-rc-baselines/` | B.5 RC-Baseline + RC-Lossy 12 cell (相同矩阵) | [`run_b5_real_nic.sh`](../../../scripts/cloudlab/run_b5_real_nic.sh) | ~60 min |
| `rq6-prep-stage-a-real-nic/` | A1 bit-for-bit 6 cell (gloo vs semirdma ratio=1.0，3 seed × 2 transport) | [`run_a1_real_nic.sh`](../../../scripts/cloudlab/run_a1_real_nic.sh) | ~45 min |
| `stage-b-phase2-resweep/` | Phase 2 C++ RQ1/RQ2/RQ4 在 CX-5 上的 chunk/ratio/timeout 扫描 | `build/tests/test_*` | ~30 min |
| `stage-b-microbench/` | M1-M5 verbs-local 微基准（poll_cq / post_recv / reg_mr / ghost_mask） | `build/benchmarks/*` | ~15 min |

---

## 已知约束 / CX-5 与 CX-6 的差异

| 维度 | c240g5 CX-6 Lx (归档) | amd203/amd196 CX-5 (当前) | 影响 |
|---|---|---|---|
| 固件 | 20.38.1002 | **16.28.4512** | CX-5 较老但 UC QP 支持完整 |
| 链路 | 25 GbE | 25 GbE | 相同带宽等级 |
| `ib_write_bw` baseline | 24.39 Gbps | **24.39 Gbps** | 一致，硬件不是瓶颈 |
| Path MTU | 4096 (设置 9000，协商下降) | 4096 (同) | 16 KiB chunk 仍需拆分为 ~4 MTU |
| 设备命名 | 单 ACTIVE mlx5_2 | **双 ACTIVE**（mlx5_0 管理 LAN + mlx5_2 实验 LAN） | `detect_rdma_dev.sh` 已修复为偏好 `enp*s*f*np*` |
| GPU | P100 12 GB | **无 GPU (CPU-only torch)** | iter_time 升高：P100 ~800 ms → CPU ~1-2 s 估计 |
| CPU | Xeon Silver 4114 (2S × 10C) | **EPYC 7302P (1S × 16C)** | 单 socket 但核更多，memcpy/ghost_mask 可能更快 |

---

## 相关代码修复（本 session 期间）

- `scripts/cloudlab/detect_rdma_dev.sh` — 多 ACTIVE 节点偏好实验 LAN (`enp<bus>s<slot>f<func>np<port>`)
- `scripts/cloudlab/day0_check.sh` — 同上，先正则匹配实验 LAN 再 fall back

两个 fix 都是防御性：旧单 ACTIVE 节点（c240g5/d7525-wisc）行为不变；新多 ACTIVE 节点 (amd203/amd196 Utah d6515 class) 得到正确结果。

---

## 相关文档

- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) — transport.py bug 根因
- [`../stage-b-hardware-notes.md`](../stage-b-hardware-notes.md) §9 — CX-5 平台详细硬件信息
- [`../results-cx6lx25g-c240g5_archive/README.md`](../results-cx6lx25g-c240g5_archive/README.md) — CX-6 Lx 归档（历史参考）
