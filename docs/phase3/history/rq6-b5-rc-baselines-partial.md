# Phase 3 · Stage B · B.5 · RC-Baseline + RC-Lossy 部分数据 + Resume Handoff

> ## 🟡 PARTIALLY SUPERSEDED — RC data valid, A2 cross-reference invalid
>
> **归档时间：** 2026-04-23
>
> 本文档记录的 4/12 B.5 cell（RC-Baseline 3 seed + RC-Lossy 1% seed=42）**数据本身仍然有效** — RC 路径走的是 `dist.all_reduce` + 软件 mask，**不经过** 有 ratio bug 的 SemiRDMA transport。
>
> 然而 [§1.2 头对头对比](#12-rc-lossy-1-loss-seed-42-vs-rc-baseline-seed-42) 中 "RC-Lossy 1% vs A2 SemiRDMA 1%" 的比较 **失真**：A2 那侧在 pre-fix 下 effective loss ≈ 5%，不是名义 1%。详见 [rq6-semirdma-effective-loss-analysis.md](./rq6-semirdma-effective-loss-analysis.md)。
>
> 采集平台 c240g5 (Wisconsin) CX-6 Lx + P100 已被 **amd203/amd196 (Utah) CX-5 + CPU-only** 替代。完整 12-cell B.5 正在 CX-5 上重跑，结果 → [`results-cx5-amd203-amd196/rq6-b5-rc-baselines/`](./results-cx5-amd203-amd196/rq6-b5-rc-baselines/)。
>
> CSV 原件已挪到 [`results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/`](./results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/)。本文档的 resume 指令（§2）已过时 — 新矩阵直接用 [`run_b5_real_nic.sh`](../../scripts/cloudlab/run_b5_real_nic.sh) 在 CX-5 上重跑。

---

> **时间：** 2026-04-23 03:00 (cluster time)
> **节点：** CloudLab Wisconsin `c240g5-110231` (node0) + `c240g5-110225` (node1)
> **状态（归档快照）：** 跑了 4 / 12 cell 后集群时间窗口结束，主动 kill 矩阵 + 保存数据。剩 8 cell **不会** 在 c240g5 上续跑（硬件已退役）；CX-5 上从 0 开始跑完整 12 cell。

---

## 0. 已落盘的 4 cell (1/3 of B.5 matrix)

| cell | transport | loss | seed | rows | 状态 |
|---|---|---|---|---|---|
| 0 | rc_baseline | 0.0 | 42 | 501 | ✅ 完整 |
| 1 | rc_baseline | 0.0 | 123 | 501 | ✅ 完整 |
| 2 | rc_baseline | 0.0 | 7 | 501 | ✅ 完整 |
| 3 | rc_lossy | 0.01 | 42 | 501 | ✅ 完整 |

CSV 落盘：[`docs/phase3/results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/`](./results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/)。每 cell 含 `loss_per_step.csv` / `iter_time.csv` / `grad_norm.csv`。

## 0.bis 待跑的 8 cell

| cell | transport | loss | seed |
|---|---|---|---|
| 4 | rc_lossy | 0.01 | 123 |
| 5 | rc_lossy | 0.01 | 7 |
| 6 | rc_lossy | 0.03 | 42 |
| 7 | rc_lossy | 0.03 | 123 |
| 8 | rc_lossy | 0.03 | 7 |
| 9 | rc_lossy | 0.05 | 42 |
| 10 | rc_lossy | 0.05 | 123 |
| 11 | rc_lossy | 0.05 | 7 |

## 1. 早期分析（4-cell 数据可见的 claim）

### 1.1 RC-Baseline 3 seed 收敛

| seed | step 0 | step 100 | step 250 | step 499 |
|---:|---:|---:|---:|---:|
| 42 | 2.4233 | 1.7364 | 1.3654 | 1.0095 |
| 123 | 2.2914 | 1.7821 | 1.4218 | 1.0648 |
| 7 | 2.3294 | 1.8202 | 1.4671 | 1.1083 |

3 seed 均稳定收敛到 ~1.0 final loss（500 step ResNet-18 / CIFAR-10 / lr=0.1）。

### 1.2 RC-Lossy 1% loss seed 42 vs RC-Baseline seed 42

| step | RC-Baseline | RC-Lossy 1% | Δ |
|---:|---:|---:|---:|
| 0 | 2.4233 | 2.4233 | 0.0000 |
| 100 | 1.7364 | 1.7551 | +0.019 |
| 250 | 1.3654 | 1.3958 | +0.030 |
| 499 | 1.0095 | 1.0408 | +0.031 |

1% chunk loss 让 final loss 微升 +0.031（**+3.1% relative**），趋势平滑无发散，符合 SGD 容忍预期。

### 1.3 头对头：RC-Lossy 1% vs **A2 SemiRDMA 1%**（同 seed=42）

| step | RC-Baseline (ref) | RC-Lossy 1% | **SemiRDMA 1% (A2)** |
|---:|---:|---:|---:|
| 0 | 2.4233 | 2.4233 | 2.4233 |
| 100 | 1.7364 | 1.7551 | 1.9262 |
| 250 | 1.3654 | 1.3958 | 1.7273 |
| 499 | 1.0095 | 1.0408 | 1.3932 |

⚠️ **关键观察**：SemiRDMA 1% 的 final loss (1.39) 显著**高于** RC-Lossy 1% (1.04)，差距 +0.35（+34% relative）。

这跟之前在 B.5 cell 0 早期数据观察到的"SemiRDMA 在真硬件上即使 loss=0 也比 RC-Baseline 收敛慢"的现象一致。可能原因（待 RQ4 分析进一步验证）：

1. **Implicit ghost rate**：(0.95, 5ms) operating point 在真硬件大 bucket（3000 chunks）下，5ms timeout 可能让自然抖动到达晚的 chunk 被 zero-mask，造成超出 cfg.loss_rate 的 effective loss
2. **Buffer slot 划分代价**：SemiRDMA 用 256 MiB MR 切 2 slot，buffer copy/MR-register 不直接影响 loss，但可能影响 gradient buffer 的字节级精度
3. **Chunk-level vs 全 bucket 的丢包模型差异**：RC-Lossy 在 reduced gradient 上做 chunk mask；SemiRDMA 在 sender side per-chunk drop（不同 rank drop 不同 chunk → AllReduce 后等价 loss rate × 2）

⚠️ 这个对比不能立刻成"RC-Lossy 比 SemiRDMA 好"的结论 — 需要：
- B.5 完整 12 cell（特别是 3% / 5% loss）跑完做完整对比
- 调研 SemiRDMA implicit ghost rate（看 completion ratio 数据）
- 调研 RC-Lossy 双 rank drop pattern 是否真的是独立的 → AllReduce 后等价 ~2× loss rate

但**这是一个值得 paper §discussion 段诚实讨论的现象**，反过来证明 SemiRDMA 设计参数选择对收敛影响显著，给 RQ3 (LayerAnalyzer 自适应 chunk) 立动机。

### 1.4 iter_time 对比

| cell | p50 (ms) | p99 (ms) | p99/p50 |
|---|---:|---:|---:|
| RC-Baseline L0 S42 | 待跑分析脚本 | | |
| RC-Lossy 1% S42 | 待 | | |
| A2 SemiRDMA 1% S42 (从 A2 doc) | 798 | 898 | 1.12× |

需要本地跑 Python 才能计算（目前本机没有 uv-managed Python 环境）。在续跑后 + uv 装好 Python 后一次性算。

---

## 2. Resume Handoff（下次 session）

### 2.1 续跑 8 个剩余 cell（最简）

```bash
ssh chen123@c240g5-110231.wisc.cloudlab.us
cd ~/SemiRDMA
nohup bash -c 'NODE_PEER_HOST=chen123@10.10.1.2 bash scripts/cloudlab/run_b5_real_nic.sh' \
    > /tmp/b5_matrix.log 2>&1 &
echo $! > /tmp/b5_matrix.pid
```

run_b5_real_nic.sh 的 cell-skip helper（[scripts/cloudlab/_matrix_lib.sh](../../scripts/cloudlab/_matrix_lib.sh)）会自动检测已完成的 4 cell + 跳过，从 cell 4 (rc_lossy L0.01 S123) 续跑。

预计耗时：8 cell × ~7.5 min = **~60 min**。

### 2.2 续跑前 sanity 检查

```bash
ssh chen123@c240g5-110231.wisc.cloudlab.us
# link / GPU 状态（reboot 可能 reset）
bash ~/SemiRDMA/scripts/cloudlab/link_setup.sh           # MTU 9000 + PFC off
nvidia-smi --query-gpu=name,memory.used --format=csv,noheader
bash ~/SemiRDMA/scripts/cloudlab/day0_check.sh
```

### 2.3 续跑后 archive 命令

```bash
# 跑完后 (T+60 min) 把 8 个新 cell 拉回本地
for d in $(ssh chen123@c240g5-110231.wisc.cloudlab.us 'find ~/SemiRDMA/experiments/results/stage_b/2026-04-23 -mindepth 1 -maxdepth 1 -type d -name "*rc_lossy*" -newer ~/SemiRDMA/experiments/results/stage_b/2026-04-23/02-46-12_rc_lossy_loss0.01_seed42'); do
    ts_name=$(basename $d)
    name=$(echo $ts_name | sed -E 's/^[0-9-]+_(.*)_loss([0-9.]+)_seed([0-9]+)/\1_L\2_S\3/')
    mkdir -p docs/phase3/results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/$name
    scp -q chen123@c240g5-110231.wisc.cloudlab.us:$d/{loss_per_step,iter_time,grad_norm}.csv docs/phase3/results-cx6lx25g-c240g5_archive/rq6-b5-rc-baselines/$name/
done
```

### 2.4 全数据回到本地后做的事

1. uv 装好本地 Python（用户已要求）
2. 跑分析脚本算 4-cell + 12-cell 对照表（loss curves / iter_time p50/p99 / completion ratio）
3. 完成 [`docs/phase3/rq6-b5-rc-baselines.md`](./rq6-b5-rc-baselines.md)（替代本 partial 文档）
4. 调研 §1.3 的 SemiRDMA implicit ghost 现象
5. commit + push

---

## 3. 当前 commits 状态

| commit | 内容 |
|---|---|
| 082f982 | feat(phase3): B.5 baselines + dispatcher + launcher |
| 1ad3291 | chore(cloudlab): cell-level skip support |
| (本 commit) | docs(phase3): B.5 partial data (4/12) + resume handoff |

剩余 8 cell 续跑 + 完整 doc → 下次 session 一次性 commit。
