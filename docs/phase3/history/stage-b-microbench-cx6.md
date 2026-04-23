# Phase 3 · Stage B · CloudLab CX-6 单节点 micro-benchmark 归档

> ## 🟢 PRIOR-PLATFORM REFERENCE
>
> **归档时间：** 2026-04-23
>
> M1-M5 微基准测的是 HCA-local verbs 软件栈常数（`poll_cq / post_recv_batch / ChunkSet::construct / reg_mr / apply_ghost_mask`），**不经过 DDP hook**，因此 **不受 ratio-controller bug 影响，数据本身有效**。
>
> 但采集硬件 d7525 CX-6 + Xeon 已换成 **amd203/amd196 CX-5 + EPYC**。**M3/M5 的 CPU/cache-bound 部分会变**（EPYC 7302P vs Xeon Silver 4114），需要在 CX-5 上重跑一遍（见 [`results-cx5-amd203-amd196/stage-b-microbench/`](./results-cx5-amd203-amd196/stage-b-microbench/)，由 C.1 矩阵填充）；**M1 (poll_cq) / M2 (post_recv) / M4 (reg_mr) 的 NIC-local 开销**理论上在 CX-5 vs CX-6 类似（都是 doorbell + PCIe roundtrip），可作为跨平台回归参考。
>
> CSV 原件在 [`results-cx6lx25g-c240g5_archive/microbench_c240g5/`](./results-cx6lx25g-c240g5_archive/microbench_c240g5/)。

---

> **时间：** 2026-04-21
> **节点：** CloudLab Wisconsin `node0.chen123-301515.rdma-nic-perf-pg0.wisc.cloudlab.us`（d7525）
> **目的：** 在第二个节点申请到之前，把 CX-6 HCA-local 软件栈的 5 类 micro 常数钉死，作为 Stage B 开工前的真机基线。
> **脚本：** [experiments/stage_b/microbench_cx6_local.py](../../experiments/stage_b/microbench_cx6_local.py)
> **原始数据：** `experiments/results/stage_b/microbench/microbench_2026-04-21_00-53-35/`（本地保留，gitignored；summary 嵌入本文档 §5）

---

## 0.  为什么跑这个 bench（背景）

单节点 CX-6 上**真机 loopback 走不通**——详见 [stage-b-hardware-notes.md §1](./stage-b-hardware-notes.md)：物理环线没有、NIC `loopback` flag 是 `[fixed]` off、单口无 internal switch。任何需要 packet 流动的测试（RQ1 chunk sweep、RQ2 loss-recovery、RQ4 ratio-wait）都被硬件堵死。

但 Phase 2 / Stage A 的延迟模型有一半是**软件栈常数**（verbs syscall、doorbell、pybind 跨语言调用、memset-style 补齐）。这些在 aliyun SoftRoCE 上被 NIC 的 CPU-path 延迟盖住，现在终于能独立测出来。**CX-6 的 NIC 延迟比 SoftRoCE 低 1-2 个数量级，所以这些"软件常数"在 Stage B 的端到端训练里会占明显比例**——是论文 §实现开销分析的输入数据。

---

## 1.  环境

```
hostname     : node0.chen123-301515.rdma-nic-perf-pg0.wisc.cloudlab.us
uname        : Linux 5.15.0-168-generic  x86_64
distro       : Ubuntu 22.04
cpu          : AMD EPYC 7302 16-Core (64 logical cores, NUMA 2× socket)
memory       : 128 GiB
NIC          : Mellanox ConnectX-6 MT28908 (vendor_part_id=4123)
fw_ver       : 20.38.1002
mlx5_core    : 1597440 bytes (in kernel)
mlx5_ib      : 393216 bytes
port state   : DOWN / DISABLED (no peer; experiment LAN dangling)
link_layer   : Ethernet (RoCEv2, GID idx 1)
python       : 3.10.12
torch        : 2.11.0+cpu
```

NIC port 是 DOWN 的，这不阻止任何 HCA-local 或 RESET/INIT 状态下的 verbs 调用。

---

## 2.  方法

**所有测试都不需要 carrier、不需要对端。** 五类 bench：

| ID | 指标 | 所需 QP 状态 | 说明 |
|----|------|--------------|------|
| M1 | `ibv_poll_cq` 空轮询延迟 | INIT | 扫 `max_n ∈ {1, 4, 16, 64}`，200k iter/cell |
| M2 | `ibv_post_recv` 批量吞吐 | INIT（verbs 规范允许） | 扫 `batch_n ∈ {1, 10, 100, 1000}`，100 trials/cell |
| M3 | `UCQPEngine(...)` 构造全流程 | —（每次全新 engine） | 扫 `buffer_bytes ∈ {1,4,16,64,256} MiB`，10 trials/cell；用线性回归分离 `reg_mr` 与固定开销 |
| M4 | Python→pybind→C++ trampoline | INIT（调 `outstanding_recv()`） | 1M iter，极简 int getter |
| M5 | `apply_ghost_mask` CPU 吞吐 | — (pure-CPU) | 扫 `buf ∈ {1,16,256} MiB × loss ∈ {0,1,10}%`，20 trials/cell，chunk_bytes=16384 |

**时间测量：** `time.perf_counter_ns`；每项都有 1000 次 warm-up 预热（M1/M4）。
**数据保存：** 每项写 CSV（raw per-sample），加一份 summary.json 汇总。
**可复现：** 脚本接受 `--dev / --out / --iters-* / --trials-*` 参数；timestamped 子目录；同一节点多次跑会堆叠不覆盖。

---

## 3.  结果

### 3.1  M1 · `poll_cq` 空轮询延迟

| `max_n` | median (ns) | p99 (ns) | mean (ns) | min (ns) |
|---------|-------------|----------|-----------|----------|
| 1  | **440** | 551 | 441.8 | 410 |
| 4  | **441** | 481 | 443.7 | 420 |
| 16 | **441** | 491 | 445.0 | 420 |
| 64 | **541** | 601 | 546.4 | 511 |

**观察：** `max_n ∈ {1, 4, 16}` 几乎同值（~441 ns），`max_n=64` 跳到 541 ns。拐点在 `max_n ≈ 16` 之后。这说明：

- **空 CQ poll 的成本主要来自 per-call（syscall / doorbell 检查）开销**，不是 per-WC 扫描
- `max_n` 从 1 到 16，libibverbs 内部扫描开销接近 0（因为 CQ 是空的，立即返回）
- `max_n=64` 多 100 ns，可能是 WC array 的栈分配成本

**Stage B implication：** [RatioController](../../src/transport/ratio_controller.cpp) 的 poll 循环用 `max_n=16` 最划算——再大只会多栈分配不会捡到更多 WC。一个 core 每秒能做 **~2.27 M polls**（1/441 ns），目前 Phase 2 代码在 aliyun SoftRoCE 上是 ~1.6 M polls/sec，所以真机 CX-6 上 ratio controller 的 CPU 预算**更宽松**，可以考虑提高 polling 频率来压 tail latency。

### 3.2  M2 · `post_recv_batch` 批量吞吐

| `batch_n` | median per-WR (ns) | p99 per-WR (ns) | 总时间 / batch (ns) | trials |
|-----------|--------------------|-----------------|---------------------|--------|
| 1    | **401**   | 30,166 | 401     | 100 |
| 10   | **46.1**  |   737 | 461     | 100 |
| 100  | **10.5**  |    53 | 1,050   | 100 |
| 1000 | **9.4**   |    10.6 | 9,400 |   5 |

> `batch_n=1000` 只 5 个 trial 是因为 RQ（rq_depth=16384）用完了——没对端无法 drain，所以总 post 数有上限；5 trials 已足以看稳态。

**观察：**
- **单个 `post_recv` 约 401 ns**；10 个 batch 降到 46 ns/WR（**8.7× amortization**）；100 个 batch 到 10.5 ns/WR（**38× amortization**）
- `batch_n=100` → `batch_n=1000` 几乎没变化（10.5 → 9.4 ns/WR），说明 **batch ≥ 100 已饱和 doorbell-share 收益**
- 意味着 **doorbell 是 per-batch 成本，WR list 构造 + memcpy 是 per-WR 成本**，后者远小于前者

**Stage B implication：** 
1. Stage A 的 `post_recv_batch(320)` 是合理设置；进一步放到 1000 以上没收益。
2. 如果将来 Stage B2 上 GPT-2（bucket 可达 500 MiB → ~32k chunks），一次性 `post_recv_batch(8192)` 也只是 80 µs 成本，**不是 hot path**。
3. 单个 `post_recv(1)` 的 400 ns p99 可达 30 µs（见 p99 栏），**避免零散逐 chunk post**。

### 3.3  M3 · `UCQPEngine` 构造成本

| `buffer_bytes` | median (ms) | p99 (ms) | n |
|----------------|-------------|----------|---|
| 1 MiB   | 4.50  | 20.85 | 10 |
| 4 MiB   | 4.81  | 24.77 | 10 |
| 16 MiB  | 12.60 | 12.76 | 10 |
| 64 MiB  | 36.85 | 37.46 | 10 |
| 256 MiB | 133.20 | 135.92 | 10 |

**线性回归（4 MiB → 256 MiB，因为 1 MiB 可能有 outlier）：**

```
t(ms) = 2.77 + 0.510 × buf_MiB
        ↑             ↑
        固定开销      per-MiB MR 注册
```

拆解：
- **固定开销 ≈ 2.77 ms** = `ibv_open_device` + `alloc_pd` + `create_cq` + `create_qp` + `modify_qp(INIT)`
- **MR 注册吞吐 ≈ 1.96 GiB/s** = `(1 / 0.510 ms/MiB) × 1024 MiB/GiB`（~ memory bandwidth / page-table build 开销）

**256 MiB MR 要 133 ms，其中 130 ms 是页固定 + 页表建立**。

**Stage B / RQ3 implication：**
- 启动一次 DDP training：`SemiRDMATransport` 的 engine 构造 + 256 MiB MR 注册 **~133 ms**，对 500-step 训练是 <1% 的 warm-up。
- **RQ3 跨层自适应 chunk sizing 如果涉及 MR 重注册**：每次 reshape 256 MiB buffer 付 130 ms；一个训练 step 在 Stage A 是 15 s（SoftRoCE），Stage B 真机应该 < 100 ms → **每 step 重注册会吃掉 100%+ step 时间**，不可行。RQ3 必须用**预分配多 buffer + switch**，或者限制 reshape 到 O(每 100 step) 频率。
- 如果 Stage B 真训练 step 降到 20-50 ms（ResNet-18 小 batch 可能），重注册频率上限更苛——这是 RQ3 设计的硬约束。

### 3.4  M4 · pybind11 trampoline 开销

`engine.outstanding_recv()`（一个裸 int getter）× 1,000,000 次：

| 指标 | 值 (ns) |
|------|---------|
| median | **230** |
| p99 | 281 |
| mean | 228.6 |
| min | 210 |

**Stage B implication：** Python → pybind → C++ 空调用约 **230 ns**。
- Stage A 的 allreduce hook 每 step 调 `post_gradient` + `await_completion` 约 10 次（每 bucket 一次 post、一次 wait），共 **~2.3 µs** pybind overhead / step。
- ResNet-18 一个真 step 在 Stage B（估计）2-20 ms → pybind 占 **0.01% – 0.1%**，**完全不是 hot path**，不需要 batch API。
- 但 `local_buf_view()` 每次 Python 读写都跨 pybind 是**另一回事**——Stage A 已经用 `np.frombuffer` 只创建一次 view 之后长期持有，符合预期最优路径。

### 3.5  M5 · `apply_ghost_mask` CPU 吞吐

chunk_bytes = 16384 (16 KiB)。表里给出两列："时间" 是 median 执行时间，"zero 带宽" 是**只把 zero-fill 的 chunks 计入**的真实 memset 吞吐（与 loss_rate 正相关）。

| buf | loss | median time | zero bytes | zero-fill 带宽 | 备注 |
|------|------|-------------|-----------|----------------|------|
| 1 MiB   |  0% | 450.5 ns | 0 B     | N/A     | 纯 chunk loop 开销 (~7 ns / chunk × 64 chunks) |
| 1 MiB   |  1% | 486 ns   | ~16 KiB | 31 GiB/s | 掩盖 loop 开销，不稳定 |
| 1 MiB   | 10% | 2.05 µs  | ~100 KiB | 47 GiB/s | 小 buffer 跨 cache line |
| 16 MiB  |  0% | 1.04 µs  | 0 B     | N/A     | 1024 个 chunk 的纯 loop 约 1 ns/chunk |
| 16 MiB  |  1% | 6.00 µs  | ~160 KiB | 25 GiB/s | |
| 16 MiB  | 10% | 48.54 µs | ~1.6 MiB | 32 GiB/s | |
| 256 MiB |  0% | 10.58 µs | 0 B     | N/A     | 16384 chunks 的 pure branch loop |
| 256 MiB |  1% | 214.9 µs | ~2.56 MiB | 11.6 GiB/s | |
| 256 MiB | 10% | 2.11 ms  | ~25.6 MiB | **12.1 GiB/s** | 稳态 memset 带宽 |

**观察：**
1. loss=0% 的"带宽"数字（summary.json 里给出的 `throughput_gibps`）实际上是**纯 chunk-loop 代价**（每 chunk 一次 `has_cqe` 分支判断，没有 memset）。应该看 loss > 0% 的数。
2. **稳态 memset 带宽约 12 GiB/s**（256 MiB @ 10% loss 的真实带宽），这是 EPYC 7302 单核 memset 速度，合理。
3. 小 buffer（1 / 16 MiB）带宽虚高——因为被 L2/L3 cache 包住；真实训练 bucket 在 >100 MiB 量级，应以 256 MiB 行为为准。

**Stage B / RQ2 implication：**
- ResNet-18 bucket ~47 MiB @ 10% loss → `apply_ghost_mask` 成本 **~400 µs/step**（插值），相对 step 时间（Stage A SoftRoCE 15 s, Stage B 真机预估 20 ms）占 **~2%**——不是 bottleneck。
- GPT-2 级别 bucket ~500 MiB @ 10% loss → **~4 ms/step**，Stage B 真机 step 如果 50 ms 就是 **8%**——开始显著。如果 RQ2 需要优化，**多线程 ghost_mask** 是首选（当前是单核 memset）。
- Phase 2 RQ2 报告的 "RMS error 降 29%" 是正确性指标，不被 CPU 吞吐影响。Stage B 可以复用 Phase 2 的 ghost_mask 实现，**不需要动代码**。

---

## 4.  关键常数汇总（Stage B 论文 §实现开销 会用）

| 常数 | 值 | 来源 | 用途 |
|------|-----|------|------|
| CX-6 `poll_cq` 空轮询延迟 | **441 ns** (max_n ≤ 16) | M1 | RatioController tight-loop 的 per-poll 成本 |
| CX-6 `poll_cq` `max_n=64` | 541 ns | M1 | batch poll 上限选择 |
| CX-6 `post_recv` 单次延迟 | **401 ns** | M2 | 零散 post 下限 |
| CX-6 `post_recv_batch` 平摊 | **10.5 ns/WR** @ batch=100 | M2 | 大 bucket 的 warm-up 成本 |
| CX-6 `reg_mr` 吞吐 | **1.96 GiB/s** | M3 (linreg) | RQ3 adaptive reshape 上限 |
| CX-6 QP/CQ/PD 固定构造开销 | **2.77 ms** | M3 (linreg) | engine startup 预算 |
| Python → pybind11 → C++ 延迟 | **230 ns** | M4 | 每次 hook 调用的硬底 |
| d7525 `ghost_mask` 单核吞吐 | **~12 GiB/s** (256 MiB, 10% loss) | M5 | RQ2 在大 bucket 上的成本预估 |

---

## 5.  附录 · summary.json 完整内容（归档用）

```json
[
  { "bench": "poll_cq_empty", "cell": "max_n=1",  "n_samples": 200000, "median_ns": 440.0, "p99_ns": 551,   "mean_ns": 441.85, "min_ns": 410 },
  { "bench": "poll_cq_empty", "cell": "max_n=4",  "n_samples": 200000, "median_ns": 441.0, "p99_ns": 481,   "mean_ns": 443.75, "min_ns": 420 },
  { "bench": "poll_cq_empty", "cell": "max_n=16", "n_samples": 200000, "median_ns": 441.0, "p99_ns": 491,   "mean_ns": 444.96, "min_ns": 420 },
  { "bench": "poll_cq_empty", "cell": "max_n=64", "n_samples": 200000, "median_ns": 541.0, "p99_ns": 601,   "mean_ns": 546.35, "min_ns": 511 },
  { "bench": "post_recv_batch", "cell": "batch_n=1",    "n_samples": 100, "median_ns": 401.0,  "p99_ns": 30166, "mean_ns": 711.3,  "min_ns": 381.0 },
  { "bench": "post_recv_batch", "cell": "batch_n=10",   "n_samples": 100, "median_ns": 46.1,   "p99_ns": 737.4, "mean_ns": 58.97,  "min_ns": 45.0 },
  { "bench": "post_recv_batch", "cell": "batch_n=100",  "n_samples": 100, "median_ns": 10.47,  "p99_ns": 53.1,  "mean_ns": 14.68,  "min_ns": 10.11 },
  { "bench": "post_recv_batch", "cell": "batch_n=1000", "n_samples":   5, "median_ns": 9.387,  "p99_ns": 10.59, "mean_ns": 9.60,   "min_ns": 9.307 },
  { "bench": "pybind_trampoline", "cell": "outstanding_recv", "n_samples": 1000000, "median_ns": 230.0, "p99_ns": 281, "mean_ns": 228.64, "min_ns": 210 },
  { "bench": "construct", "cell": "buf_mib=1",   "n_samples": 10, "median_ns":   4498957,  "p99_ns":  20845654, "mean_ns":   6125128,  "min_ns":   4439967 },
  { "bench": "construct", "cell": "buf_mib=4",   "n_samples": 10, "median_ns":   4807856,  "p99_ns":  24774371, "mean_ns":   6817298,  "min_ns":   4771969 },
  { "bench": "construct", "cell": "buf_mib=16",  "n_samples": 10, "median_ns":  12601924,  "p99_ns":  12758868, "mean_ns":  12610990,  "min_ns":  12452934 },
  { "bench": "construct", "cell": "buf_mib=64",  "n_samples": 10, "median_ns":  36847448,  "p99_ns":  37464350, "mean_ns":  36990825,  "min_ns":  36636608 },
  { "bench": "construct", "cell": "buf_mib=256", "n_samples": 10, "median_ns": 133197025,  "p99_ns": 135915012, "mean_ns": 133454900,  "min_ns": 132702707 },
  { "bench": "ghost_mask", "cell": "buf_mib=1 loss_pct=0",    "n_samples": 20, "median_ns":    450.5, "p99_ns":    4778, "mean_ns":   672.25, "min_ns":   421.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=1 loss_pct=1",    "n_samples": 20, "median_ns":    486.0, "p99_ns":    1322, "mean_ns":   633.70, "min_ns":   421.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=1 loss_pct=10",   "n_samples": 20, "median_ns":   2049.0, "p99_ns":    2955, "mean_ns":  2027.75, "min_ns":   962.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=16 loss_pct=0",   "n_samples": 20, "median_ns":   1042.0, "p99_ns":    3206, "mean_ns":  1146.70, "min_ns":  1012.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=16 loss_pct=1",   "n_samples": 20, "median_ns":   5996.0, "p99_ns":    9969, "mean_ns":  6260.15, "min_ns":  3126.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=16 loss_pct=10",  "n_samples": 20, "median_ns":  48536.0, "p99_ns":   57578, "mean_ns": 49053.00, "min_ns": 43592.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=256 loss_pct=0",  "n_samples": 20, "median_ns":  10584.5, "p99_ns":   15108, "mean_ns": 10947.50, "min_ns": 10519.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=256 loss_pct=1",  "n_samples": 20, "median_ns": 214912.5, "p99_ns":  236814, "mean_ns": 210739.20,"min_ns":170209.0 },
  { "bench": "ghost_mask", "cell": "buf_mib=256 loss_pct=10", "n_samples": 20, "median_ns":2113283.5, "p99_ns": 2233368, "mean_ns":2107409.75,"min_ns":2003117.0 }
]
```

---

## 6.  不能声明什么（scope caveats）

遵循 [rq2-results-ghost-masking.md §5.4](../phase2/rq2-results-ghost-masking.md) / [stage-b-hardware-notes.md §6](./stage-b-hardware-notes.md#6-不能声明什么-scope-caveats) 的惯例：

1. **不是端到端性能数据。** 全部测的是**本地 HCA / 驱动 / CPU 常数**，一次 wire-level packet 都没发过。不能用这些数据预测 Stage B 的 training throughput。
2. **不代表 CX-5。** d7525 上是 CX-6 MT28908 / fw 20.38.1002。CX-5（原计划 d7615）的 WQE rate / PCIe gen / doorbell 实现有差异。
3. **不代表生产环境。** `port_state=DOWN` 下的 `poll_cq` 和 `post_recv` 走的是 kernel fast-path 不是硬件 packet engine；如果端口 UP 且有实际 completion 来了，CQE 处理成本会**大于** 441 ns（要读 WC 字段、更新生产者索引等）。当前数字是**下限**，不是典型值。
4. **`apply_ghost_mask` 带宽是单核。** 多线程版本需要另测；Stage B 若决定并行化，M5 的数字作为"单核基线"做加速比。
5. **`reg_mr` 的线性回归只在 4-256 MiB 成立。** 1 MiB 数据（4.50 ms 对比 4 MiB 的 4.81 ms）几乎无变化，可能是 mlx5 驱动有小 MR 的 path 优化；< 4 MiB 的行为不清楚。

---

## 7.  相关文件索引

- [experiments/stage_b/microbench_cx6_local.py](../../experiments/stage_b/microbench_cx6_local.py) — 脚本（可复现，未来 CX-6 节点重跑直接用）
- [docs/phase3/stage-b-hardware-notes.md](./stage-b-hardware-notes.md) — d7525 硬件盘点 + 单节点验证
- [docs/phase3/rq5-results-ddp-baseline.md](./rq5-results-ddp-baseline.md) — Stage A (aliyun SoftRoCE) DDP 数值等价结果
- [docs/phase3/design-ddp-integration.md §2.2](./design-ddp-integration.md) — Stage B 总体设计（本文档数据用来 refine 其中的实现开销估算）
- [CLAUDE.md "Known Risks"](../../CLAUDE.md#known-risks) — 本次数据直接针对 "CX-5/6 WQE rate" 风险行
