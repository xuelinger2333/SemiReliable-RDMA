# Phase 3 · Stage B · RQ6 · Loss Injection Strategy

> **时间：** 2026-04-23
> **背景：** RQ6 ([design-ddp-integration.md §2.2](./design-ddp-integration.md#22-stage-b--cloudlab-connectx-5-真机--五路-baseline-对比week-78may-25--jun-7)) 要在 1%/3%/5% per-packet 丢包条件下对比 5 条 baseline 的 Time-to-Accuracy。本文记录在 c240g5 + CX-6 Lx 真硬件上选择 loss-injection 机制的工程过程，以及最终采用的方案。

---

## 0. 一眼看懂

| 方案 | 结论 |
|------|------|
| **tc netem on netdev**（design 原始计划） | ❌ **完全无效**：CX-6 Lx RoCE 数据平面绕过 kernel qdisc。实测 ib_write_bw 在 `tc qdisc add ... netem loss 1%` 下仍跑 24.39 Gbps（baseline 等价） |
| 交换机 drop policy | ❌ 不适用：c240g5 是 DAC 直连点对点，没有可编程交换机 |
| Mellanox `mlxconfig` fault inject | ⚠️ 复杂、固件依赖、对单一 NIC 行为，不便控制 |
| **App-level Bernoulli drop in DDP comm hook** | ✅ **采用方案** |

**采用方案：** 每个 baseline 在 DDP comm hook 内对 bucket bytes 做 seeded Bernoulli per-chunk drop，drop rate ∈ {0.0, 0.01, 0.03, 0.05}。所有 4 个 baseline 共用同一 chunk 划分 + 同一 RNG seed pool，确保 apples-to-apples。这跟 SemiRDMA 现有 `cfg.loss_rate` 的 [Bernoulli 实现](../../python/semirdma/transport.py)同一逻辑层面。

---

## 1. 经验：tc netem 对 RoCE 无效

### 1.1 复现实验

c240g5 + CX-6 Lx + 25 GbE（[hardware-notes §8.1](./stage-b-hardware-notes.md#8-2026-04-23--c240g5-双节点替代记录--phase-2-真机重跑)），1% loss tc qdisc：

```bash
$ sudo tc qdisc add dev enp94s0f0np0 root netem loss 1%
$ sudo tc qdisc show dev enp94s0f0np0
qdisc netem 8001: root refcnt 585 limit 1000 loss 1%

# kernel TCP/UDP 路径：tc 生效
$ ping -c 100 -i 0.01 -q 10.10.1.2
100 packets transmitted, 99 received, 1% packet loss

# RoCE 路径：tc 不生效
$ ib_write_bw -d rocep94s0f0 -x 1 -p 18519 -s 65536 -F -D 5 --report_gbits
 65536      139548      24.39 Gb/s avg     # 与 baseline 完全相同
```

### 1.2 原因

ConnectX-5/6 系列 mlx5 ASIC 的 RDMA 数据通路：
1. 应用 `ibv_post_send(qp, wr)` 把 WR 写入 SQ ring
2. 触发 doorbell（写一次 mmap'd register）
3. NIC 硬件直接从主存 DMA 读取 payload，封装 RoCEv2 → 发到 wire

整个路径**完全不经过 Linux 网络栈** sk_buff / qdisc / netfilter。tc qdisc 装在 netdev 的 `q_disc->enqueue` 钩子上，而 RoCE 包从来没碰到过那个钩子。这就是 RDMA "kernel bypass" 的核心机制（与 DPDK / XDP 类似但更激进）。

参考：Mellanox 工程师在多个论坛/issue 里确认这一行为，例如 mlx5_core 源码 `drivers/net/ethernet/mellanox/mlx5/core/en_rep.c` 的 sk_buff 路径只用于非 RoCE 流量（management、TCP/UDP）。

### 1.3 对 RQ6 的影响

design §2.2 说 "Stage B 必须用真 netem 或真网络丢包"。从今天的实测看，**真 netem 在硬件 RoCE NIC 上不工作**。剩下的真网络丢包路径：
- 编程式交换机：c240g5 没有
- DAC 直连物理层：理论可拔掉/插回引入 link flapping，但不可控不可重复
- NIC 固件 fault injection (mlxconfig): 复杂，文档少，容易把 NIC 弄到不可恢复状态
- 升级到带 P4 / SmartSwitch 的 testbed：超出本 phase 范围

**结论：** RQ6 的 lossy baselines 改用应用层 Bernoulli drop，并在 paper 中明确说明这是 *模拟* 的丢包（不是真 wire-level loss），讨论这一选择对结论的影响范围。

---

## 2. App-level Bernoulli drop 方案

### 2.1 已有：SemiRDMA hook 的 loss_rate

[`python/semirdma/transport.py`](../../python/semirdma/transport.py) 的 `SemiRDMATransport` 在 `post_gradient` 内对每个 chunk 独立做 `random_uniform() < loss_rate` 决定是否 skip `post_write`。loss_seed 通过 cfg 传入，每个 (rank, seed) 组合生成确定性 drop 序列。

Phase 2 RQ4 的 RatioController + GhostMask 设计就是为了让 SemiRDMA 在这种丢包下仍能 forward progress（ratio=0.95 容忍 5% 丢失）。

### 2.2 新增：RC-Baseline / RC-Lossy / UD-Naive 也走 Bernoulli

为对照公平，每个 baseline 在 DDP comm hook 里以同一逻辑做 chunk-level drop：

| Baseline | Chunking | Loss-handling | Bernoulli drop |
|----------|----------|---------------|---------------|
| **RC-Baseline** | 单 bucket → 一次 RC `post_write` | RC HW retx 保证全到 | drop_rate=0（设计上不丢） |
| **RC-Lossy** | 同 RC-Baseline | RC HW retx 仍保证全到，但 hook 强制 mask 部分 chunks 为 0 | 1%/3%/5% mask（数学上等价于真丢包+0填充） |
| **UD-Naive** | 切 chunk + UD `post_send`（无 retx） | 不等任何 chunk，全发完即返回 | 1%/3%/5% mask（在发送端预先 drop） |
| **SemiRDMA** | 切 chunk + UC `post_write_with_imm` | RatioController 等 ratio=0.95 + GhostMask 修正 stale | 1%/3%/5% mask（已实现） |

**注意 RC-Lossy 的语义**：RC QP 的硬件 retx 保证不丢，所以"真丢包模型 + RC retx"在 wire 层等价于 "无丢包 + 软件 mask"。我们用后者实现。论文里诚实说明：RC-Lossy 数据不是 RC 在丢包链路下的实测，而是 RC 在 0% 丢包链路上跑 + 软件层每次 mask 一定比例 chunk 给 ghost 行为，等价于"如果 wire 真在丢包但 RC 决定不补偿"。这跟 OptiReduce 论文的某些 baselines 设计原理一致。

### 2.3 实现位置

新增模块 `python/semirdma/baselines/`（待创建）：
- `rc_hook.py` — `rc_allreduce_hook` 用 `dist.all_reduce(group=rc_group)` + 可选 mask
- `ud_hook.py` — `ud_naive_allreduce_hook` 自写 UD QP send/recv + drop
- `__init__.py` — 注册四个 hook

[`experiments/configs/stage_b_cloudlab.yaml`](../../experiments/configs/stage_b_cloudlab.yaml) 加 `transport ∈ {gloo, rc_baseline, rc_lossy, ud_naive, semirdma}` 选择。

[`experiments/stage_a/train_cifar10.py`](../../experiments/stage_a/train_cifar10.py) 的 `_install_hook` 扩展支持 4 个新 transport（已支持 gloo / semirdma）。

---

## 3. 实现优先级

| 优先级 | 项 | 工作量 |
|--------|----|----|
| P1 | `rc_baseline` hook（直接复用 `dist.all_reduce(backend=gloo)` + 不 mask）→ 跟 transport=gloo 数学等价，但显式标注是 "假定真硬件 RC retx 完美工作" 的 reference | <1 day（基本只是 alias） |
| P1 | `rc_lossy` hook（`dist.all_reduce` + 软件 mask 模拟丢包）| 1 day |
| P1 | `ud_naive` hook（UD QP raw send，无 retx）— 需要新 C++ 路径或大幅简化的 UD 版 transport | 2-3 days |
| P2 | RQ6 主实验跑批 + draft `rq6-results-real-nic-comparison.md` | 3-5 days |

UD-Naive 是最重的，因为现有 transport 是 UC QP only。可考虑：
- (a) 写完整 UD QP transport（多花 2 天）
- (b) 简化为 "UC QP + ratio=0 + timeout=1 ms"（"无可靠性"的功能等价，省掉 UD QP 实现），代价是不能声明 "在 UD QP 上"
- (c) 跳过 UD-Naive，4 baseline 改 3（gloo/RC/RC-Lossy/SemiRDMA）

建议先 (b) 跑通 RQ6 主实验拿数据，paper 里说明 UD-Naive 用 functional-equivalent UC config，时间允许再补真 UD QP。

---

## 4. 不能声明什么

1. ❌ "SemiRDMA 在真 wire-level 丢包下表现优于 RC" — 我们的丢包是软件模拟，不是 wire 层
2. ❌ "RC-Lossy 数据反映了 RC 在丢包链路下的真实 retx 开销" — RC retx 路径在我们的实验中**没被触发**（wire 0% loss）；RC-Lossy 的语义是"假装 wire 丢了 X%，RC 假装不补偿"
3. ✅ "在 chunk-level Bernoulli loss 模型下，4 baseline 的训练收敛 / TTA / p99 step time 对比"
4. ✅ "loss model 与 OptiReduce paper §5 的丢包注入方式语义一致" — 同 community standard

---

## 5. 后续动作

- [x] 文档化 tc netem 在 RoCE 下无效的 finding
- [x] 写 [`scripts/cloudlab/netem_inject.sh`](../../scripts/cloudlab/netem_inject.sh) 带显式警告（仍可用于 control-plane TCP 测试）
- [ ] **P1b**：实现 `python/semirdma/baselines/rc_hook.py`（rc_baseline + rc_lossy）
- [ ] **P1c**：实现 `python/semirdma/baselines/ud_hook.py`（UD-Naive 简化版优先）
- [ ] **P2**：4-baseline 矩阵跑 ResNet-18 / CIFAR-10 × loss ∈ {0, 0.01, 0.03, 0.05} × 3 seed
- [ ] **P2-doc**：起草 `docs/phase3/rq6-results-real-nic-comparison.md`

---

## 6. 相关文件

- [`scripts/cloudlab/netem_inject.sh`](../../scripts/cloudlab/netem_inject.sh) — tc helper（带 RoCE 无效警告）
- [`docs/phase3/design-ddp-integration.md`](./design-ddp-integration.md#22-stage-b--cloudlab-connectx-5-真机--五路-baseline-对比week-78may-25--jun-7) §2.2 — RQ6 设计
- [`python/semirdma/transport.py`](../../python/semirdma/transport.py) — SemiRDMA 现有 Bernoulli drop（已实现的参考）
- [`docs/phase3/rq6-prep-real-nic-equivalence.md`](./rq6-prep-real-nic-equivalence.md) — Stage A 在真 NIC 上的等价性验证（前置工作）
