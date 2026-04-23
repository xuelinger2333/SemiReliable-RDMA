# Phase 3 · Stage B · RQ6-Prep · Stage A 等价性在真 NIC 上复现

> ## 🟢 PRIOR-PLATFORM REFERENCE — data valid, platform deprecated
>
> **归档时间：** 2026-04-23
>
> A1 bit-for-bit 跑时显式设置 `transport_cfg.ratio=1.0 timeout_ms=60000`，**跳过了** ratio-controller bug 分支（[transport.py:267-271](../../python/semirdma/transport.py#L267-L271)），所以本页数据 **不受 bug 影响**。
>
> 然而采集平台 c240g5 (Wisconsin) CX-6 Lx + P100 已被 **amd203/amd196 (Utah) CX-5 + CPU-only** 替代。本页结果作为 **CX-6 Lx 25 GbE + GPU 平台参考基线** 保留；同矩阵在 CX-5 上重跑，结果 → [`results-cx5-amd203-amd196/rq6-prep-stage-a-real-nic/`](./results-cx5-amd203-amd196/rq6-prep-stage-a-real-nic/)。
>
> CSV 原件已挪到 [`results-cx6lx25g-c240g5_archive/rq6-prep-stage-a-real-nic/`](./results-cx6lx25g-c240g5_archive/rq6-prep-stage-a-real-nic/)。

---

> **时间：** 2026-04-23
> **节点：** CloudLab Wisconsin `c240g5-110231` (node0) + `c240g5-110225` (node1)
> **硬件：** 2× Intel Xeon Silver 4114, 187 GiB RAM, **Tesla P100 PCIe 12 GB**, **CX-6 Lx 25 GbE** (RoCEv2 GID 1, MTU 9000, PFC off)
> **目的：** 在跑 RQ6 五 baseline（design §2.2）之前，先把 Stage A 的 A1 bit-for-bit 等价性结论在真硬件 + GPU 上验证一遍，确保 DDP comm hook 在 CX-6 Lx + Tesla P100 上没有数值偏差。

CSV 落盘 [`docs/phase3/results-cx6lx25g-c240g5_archive/rq6-prep-stage-a-real-nic/`](./results-cx6lx25g-c240g5_archive/rq6-prep-stage-a-real-nic/)。硬件背景 + Phase 2 重跑见 [stage-b-hardware-notes.md §8](./stage-b-hardware-notes.md#8-2026-04-23--c240g5-双节点替代记录--phase-2-真机重跑) + [stage-b-phase2-resweep.md](./stage-b-phase2-resweep.md)。

---

## 0. 摘要

| seed | step 0 loss (gloo / semirdma) | max\|Δloss\| | max_rel_err | step 99 loss (gloo / semirdma) |
|---:|:---:|:---:|:---:|:---:|
| 42  | 2.4233 / **2.4233** | **0.00e+00** | **0.0000%** | 1.7906 / 1.7906 |
| 123 | 2.2914 / **2.2914** | **0.00e+00** | **0.0000%** | 1.7338 / 1.7338 |
| 7   | 2.3294 / **2.3294** | **0.00e+00** | **0.0000%** | 1.7346 / 1.7346 |

**结论：** Stage A 在 aliyun SoftRoCE 上拿到的 A1 "DDP 数学正确" 结论在 c240g5 / Tesla P100 / CX-6 Lx 25 GbE / RoCEv2 真硬件上 **逐步精确复现**，3 seed × 100 step × 2 transport = 600 数据点全部 0 偏差。step 0 的 gloo / semirdma loss 也 ε-精确等于 [rq5-results-ddp-baseline.md §0.4](./rq5-results-ddp-baseline.md) 的 SoftRoCE 数字（2.423 / 2.291 / 2.329），验证 DistributedSampler 种子重现 + GPU/CPU 数据通路一致。

---

## 1. 实验设计

### 1.1 配置

| 项 | 值 | 来源 |
|---|---|---|
| 模型 | ResNet-18（CIFAR-10 stem 改 3×3, maxpool=Identity） | Stage A 同 |
| 数据 | CIFAR-10, batch=128/worker, `DistributedSampler(seed=cfg.seed)` | Stage A 同 |
| world_size | 2 (1 process per node, 2 nodes) | 真机 |
| 设备 | 每 rank 1× Tesla P100 12 GB | 真机 |
| 通信 | rank0=node0 (10.10.1.1, dev=rocep94s0f0) ↔ rank1=node1 (10.10.1.2, dev=rocep94s0f1) | 真机 |
| 链路 | CX-6 Lx 25 GbE DAC, RoCEv2 GID 1, MTU 9000, PFC off | 见 [hardware-notes §8.1](./stage-b-hardware-notes.md#8-2026-04-23--c240g5-双节点替代记录--phase-2-真机重跑) |
| transport 对照 | gloo (Gloo TCP allreduce) vs semirdma (UC QP allreduce) | Stage A 同 |
| A1 参数 | `ratio=1.0, timeout_ms=60000, loss_rate=0.0` | Stage A `rq5-results §0.3` |
| 步数 | 100 (warmup 10) | Stage A 100 步同 |
| seeds | 42, 123, 7 | Stage A 同 |

### 1.2 启动方式

矩阵脚本 [`scripts/cloudlab/run_a1_real_nic.sh`](../../scripts/cloudlab/run_a1_real_nic.sh)，单一入口，自动跑 6 cell（3 seed × 2 transport），通过 SSH 在 node0 上一并启动两端 torchrun，每 cell 用独立 master_port + semirdma_port 避免冲突。

```bash
NODE_PEER_HOST=chen123@10.10.1.2 bash scripts/cloudlab/run_a1_real_nic.sh
```

每 cell 用：
```
torchrun --nnodes=2 --node_rank={0,1} --master_addr=10.10.1.1 --master_port=$mport \
  experiments/stage_a/train_cifar10.py --config-name stage_b_cloudlab \
  transport=$T loss_rate=0.0 seed=$S steps=100 warmup_steps=10 \
  transport_cfg.dev_name=$DEV transport_cfg.ratio=1.0 transport_cfg.timeout_ms=60000 \
  dist.semirdma_port=$sport
```

总耗时约 11 min（6 cell × ~110 sec）。

---

## 2. 结果

### 2.1 Bit-for-bit 校核（rank 0 写入 `loss_per_step.csv`）

每个 cell 100 行 (step, loss)。对照同 seed 的 gloo / semirdma 两份 CSV 的 step-by-step loss：

```
 seed |  gloo s0 |  semi s0 |     max|D| |    maxrel |  gloo s99 | semi s99
------------------------------------------------------------------------------
   42 |   2.4233 |   2.4233 |   0.00e+00 |   0.0000% |    1.7906 |   1.7906
  123 |   2.2914 |   2.2914 |   0.00e+00 |   0.0000% |    1.7338 |   1.7338
    7 |   2.3294 |   2.3294 |   0.00e+00 |   0.0000% |    1.7346 |   1.7346
```

每对 (gloo, semirdma) 100 步 loss 数值精确一致，**6 位小数级别 0 偏差**。设计 §2.4 验收阈值是 `max_rel_err < 1%`，实测 **远超**。

### 2.2 与 aliyun SoftRoCE Stage A 对照

|  | aliyun SoftRoCE (rq5-results §0.4) | c240g5 + P100 + CX-6 Lx (本节) | 是否一致 |
|---|:---:|:---:|:---:|
| seed=42 step 0 loss | 2.423 | **2.4233** | ✓ |
| seed=123 step 0 loss | 2.291 | **2.2914** | ✓ |
| seed=7 step 0 loss | 2.329 | **2.3294** | ✓ |
| max\|Δloss\| (gloo vs semi) | 0.00e+00 | **0.00e+00** | ✓ |
| max_rel_err | 0.0000% | **0.0000%** | ✓ |

**完全等价**：模型权重初始化 / DistributedSampler / 数据增强 / SGD 优化器 / DDP AllReduce 全部在 GPU + 真硬件 RoCE 通路上跟 SoftRoCE CPU 通路给出比特级一致的训练轨迹。

### 2.3 收敛曲线（不在 A1 范围内，仅供参考）

100 步内三 seed 都从 ~2.3 下降到 ~1.7，正常 CIFAR-10 ResNet-18 lr=0.1 SGD 行为：

| seed | step 0 | step 50 | step 99 |
|---:|---:|---:|---:|
| 42 | 2.4233 | 2.0193 | 1.7906 |
| 123 | 2.2914 | 1.9684 | 1.7338 |
| 7 | 2.3294 | 1.9911 | 1.7346 |

A2（loss=0.01 收敛验证）和真训练 TTA 不在本文件范围，留给 RQ6 主实验。

---

## 3. 这个验证**不**回答的问题

参考 [rq5-results §0.5](./rq5-results-ddp-baseline.md) 的"不能声明"惯例：

1. ❌ "SemiRDMA 在真硬件上比 Gloo 快" — 100 步 iter_time 数据没采集；本实验只比 loss 数值
2. ❌ "1% chunk loss 下真硬件训练能收敛" — A2 在本节没跑（loss=0 only）；留给 RQ6 主实验
3. ❌ "ResNet-50 / GPT-2 等大模型 bit-for-bit" — 本节只验 ResNet-18
4. ❌ "5 baseline 端到端比较" — RC-Baseline / RC-Lossy / UD-Naive 没接，是 RQ6 主体
5. ✅ "DDP comm hook 在 CX-6 Lx + Tesla P100 + RoCEv2 上数值正确"
6. ✅ "Phase 2 SoftRoCE→真硬件迁移，UC QP+ChunkManager+RatioController+GhostMask 训练通路无回归"

---

## 4. 工程踩坑（留给 RQ6 主实验）

| # | 问题 | 解决 |
|---|------|------|
| 1 | `ModuleNotFoundError: semirdma` 在 torchrun 里 | `pip install -e .` 一次（setup_env.sh 不做这一步） |
| 2 | `ModuleNotFoundError: semirdma._semirdma_ext` (node1) | `cp build/python/semirdma/_semirdma_ext*.so python/semirdma/`；setup_env 的 next-steps 提示已经写明，但需要 build 后手动跑一次 |
| 3 | `ConnectionRefused 10.10.1.1:port+1` SemiRDMA bootstrap 失败 | 两 rank 用同一个 `master_addr` 当 peer host 错；改 [`train_cifar10.py`](../../experiments/stage_a/train_cifar10.py) 接 `SEMIRDMA_PEER_HOST` env，rank 0 设 node1 IP / rank 1 设 node0 IP |
| 4 | reboot 后 RDMA 命名变化（mlx5_2 → rocep94s0f0） | 走 PCI-stable rocep 命名 + [`scripts/cloudlab/detect_rdma_dev.sh`](../../scripts/cloudlab/detect_rdma_dev.sh) 自动检测 |
| 5 | reboot 后 MTU=1500 + PFC=on 还原 | [`scripts/cloudlab/link_setup.sh`](../../scripts/cloudlab/link_setup.sh) 一键恢复 jumbo + PFC off |
| 6 | node0→node1 SSH key 没配，矩阵脚本 ssh peer 时挂 | `ssh-keygen -t ed25519` + 把 node0 pub 加 node1 authorized_keys + `ssh-keyscan` known_hosts |

---

## 5. 后续行动

- [x] **本节**：Stage A 等价性在 c240g5 + CX-6 Lx + P100 上 0 偏差复现 ✅
- [ ] **P1a**：[`scripts/cloudlab/netem_inject.sh`](../../scripts/cloudlab/netem_inject.sh) 注入 1%/3%/5% per-packet loss 在 25 GbE 链路（RQ6 必备）
- [ ] **P1b**：RC-Baseline / RC-Lossy DDP 集成（RC QP 在 NCCL 不可用 → 用 perftest 路径或自写 RC transport）
- [ ] **P1c**：UD-Naive DDP 集成（UD QP，无可靠性，作为 lower bound）
- [ ] **P2**：4-baseline 主实验跑批 → `rq6-results-real-nic-comparison.md`
- [~] **OptiReduce**：用户决定推迟到下一轮

---

## 6. 相关文件

- [`scripts/cloudlab/run_a1_real_nic.sh`](../../scripts/cloudlab/run_a1_real_nic.sh) — A1 矩阵 launcher
- [`scripts/cloudlab/detect_rdma_dev.sh`](../../scripts/cloudlab/detect_rdma_dev.sh) — RDMA 设备自动检测（应对 mlx5/rocep 命名差）
- [`scripts/cloudlab/link_setup.sh`](../../scripts/cloudlab/link_setup.sh) — reboot 后恢复 MTU + PFC
- [`experiments/stage_a/train_cifar10.py`](../../experiments/stage_a/train_cifar10.py) — DDP 训练入口（接 `SEMIRDMA_PEER_HOST` env）
- [`experiments/configs/stage_b_cloudlab.yaml`](../../experiments/configs/stage_b_cloudlab.yaml) — Hydra config（chunk_bytes=16384, ratio=0.95, timeout=5）
- [`docs/phase3/rq5-results-ddp-baseline.md`](./rq5-results-ddp-baseline.md) — Stage A on aliyun SoftRoCE（对照基准）
- [`docs/phase3/results-cx6lx25g-c240g5_archive/rq6-prep-stage-a-real-nic/`](./results-cx6lx25g-c240g5_archive/rq6-prep-stage-a-real-nic/) — 6 个 loss_per_step.csv 原始数据
