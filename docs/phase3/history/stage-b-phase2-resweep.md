# Phase 3 · Stage B · Phase 2 真机重跑（CX-6 Lx 25 GbE）

> ## 🟢 PRIOR-PLATFORM REFERENCE — C++ path, data valid
>
> **归档时间：** 2026-04-23
>
> Phase 2 的 RQ1 (`test_chunk_sweep`) / RQ2 (`test_rms_error`) / RQ4 (`test_ratio_sweep`) 跑在 C++ 独立测试里，**不走 Python transport.py 的 DDP hook**，因此 **不受 ratio-controller bug 影响**。RQ4 自己动态算 `target = sent_count / num_chunks`（见 [rq6-semirdma-effective-loss-analysis.md §2.4](./rq6-semirdma-effective-loss-analysis.md#24-为什么-phase-2-rq4-没暴露这个)）。
>
> 但采集硬件 c240g5 CX-6 Lx 25 GbE 已换成 **amd203/amd196 CX-5 25 GbE**。本页结果作为 CX-6 Lx 平台参考；同矩阵在 CX-5 上重跑，结果 → [`results-cx5-amd203-amd196/stage-b-phase2-resweep/`](./results-cx5-amd203-amd196/stage-b-phase2-resweep/)（由 C.2 矩阵填充）。

---

> **时间：** 2026-04-23
> **机器：** CloudLab Wisconsin `c240g5-110231` (node0) + `c240g5-110225` (node1)
> **硬件：** Mellanox ConnectX-6 Lx 25 GbE × 2，DAC 直连，RoCEv2 GID idx 1，MTU 9000，PFC 关
> **目的：** 把 aliyun SoftRoCE 上跑的 Phase 2 三组实验（RQ1 chunk_sweep / RQ2 ghost-mask RMS / RQ4 ratio-timeout）在真硬件上重跑一遍，验证机制等价性 + 给 Stage B 拿到新硬件的参数校准点。

CSV 结果落盘 [`experiments/results/cx6lx25g_c240g5/`](../../experiments/results/cx6lx25g_c240g5/)。硬件背景与 perftest baseline 见 [stage-b-hardware-notes.md §8](./stage-b-hardware-notes.md#8-2026-04-23--c240g5-双节点替代记录--phase-2-真机重跑)。

---

## 0. 摘要

| 实验 | aliyun SoftRoCE 旧结论 | c240g5 CX-6 Lx 25 GbE 真机结论 | 是否需要参数变更 |
|------|----------------------|--------------------------------|------|
| RQ1 chunk_sweep | 16 KiB 是 WQE/s 拐点；72.6 MB/s SoftRoCE 上限 | **16 KiB 仍是拐点**（~2.55 M WQE/s）；wire 吞吐 24.4 Gbps（perftest 测） | **chunk_bytes=16384 沿用** |
| RQ2 rms_error | 1% & 5% 丢包 ratio = 0.707（理论 1/√2） | 1% **0.7065**, 5% **0.7069** — 等价 | **GhostMask 配置不变** |
| RQ4 ratio_timeout | (0.95, 20 ms) sweet spot, wait_p99 = 14.8 ms | **(0.95, 5 ms)** sweet spot, wait_p99 = **1.46 ms** | **timeout_ms 从 20 → 5** |

---

## 1. 拓扑与命令

每组实验跑两遍：(a) 单机 loopback 在 node0（apples-to-apples 对照 aliyun），(b) 2 节点真线（node0 server, node1 client）。dev 名称：node0 = `mlx5_2`，node1 = `mlx5_1`（`rdma link show` 检测 ACTIVE 那条）。

```bash
# Loopback（两端都在 node0 上）
SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0 \
    ./test_chunk_sweep server mlx5_2 500 > rq1_chunk_sweep_cx6lx25g_loopback_500r.csv &
SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0 \
    ./test_chunk_sweep client 127.0.0.1 mlx5_2 500 42

# 2 节点真线
node0$ SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0 \
    ./test_chunk_sweep server mlx5_2 250 > rq1_chunk_sweep_cx6lx25g_2node_250r.csv
node1$ SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0 \
    ./test_chunk_sweep client 10.10.1.1 mlx5_1 250 42
```

RQ2 / RQ4 同模式，端口分别 18526 / 18527。

---

## 2. RQ1 — chunk_sweep（chunk size × loss rate）

### 2.1 测试代码 patch（关键）

`test_chunk_sweep.cpp` 原代码在每 round 末加 `usleep(5000) + poll_cq(64, 50)` 排水，是 SoftRoCE 时代为捕获散落 chunk 设的；在真硬件上这段固定 55 ms 把吞吐封顶在 ~72 MB/s（与 SoftRoCE 上限完全巧合一致，导致一开始误以为"真机和 SoftRoCE 一样慢"）。

补丁加 env 覆盖，默认行为保持兼容：
```cpp
inline int drain_ms()    { const char* e = std::getenv("SEMIRDMA_DRAIN_MS");  return e ? atoi(e) : 50;   }
inline int drain_settle_us() { const char* e = std::getenv("SEMIRDMA_SETTLE_US"); return e ? atoi(e) : 5000; }
```

CX-6 Lx 真机重跑时设 `SEMIRDMA_DRAIN_MS=0 SEMIRDMA_SETTLE_US=0`。

### 2.2 单机 loopback 结果（500 rounds × 5 chunk × 4 loss）

| chunk | loss% | ghost_ratio | goodput(MB/s) | WQE/s | p50(ms) | p99(ms) |
|------:|------:|------------:|--------------:|------:|--------:|--------:|
| 1 KB | 0.0 | 0.000 | 1018 | 1.04 M | 3.93 | 3.96 |
| 1 KB | 5.0 | 0.0501 | 1016 | 1.04 M | 3.74 | 3.77 |
| 4 KB | 0.0 | 0.000 | 8347 | 2.14 M | 0.48 | 0.50 |
| 4 KB | 5.0 | 0.0501 | 8236 | 2.11 M | 0.46 | 0.48 |
| **16 KB** | **0.0** | **0.000** | **39928** | **2.55 M** ← 峰 | 0.10 | 0.12 |
| **16 KB** | **5.0** | **0.0502** | **39232** | **2.51 M** | 0.10 | 0.10 |
| 64 KB | 0.0 | 0.000 | 145502 | 2.33 M | 0.027 | 0.038 |
| 64 KB | 5.0 | 0.0479 | 142570 | 2.28 M | 0.027 | 0.031 |
| 256 KB | 0.0 | 0.000 | 346995 | 1.39 M | 0.010 | 0.018 |
| 256 KB | 5.0 | 0.046 | 374548 | 1.50 M | 0.010 | 0.014 |

完整 20 行 CSV：[`rq1_chunk_sweep_cx6lx25g_loopback_500r.csv`](../../experiments/results/cx6lx25g_c240g5/rq1_chunk_sweep_cx6lx25g_loopback_500r.csv)

### 2.3 2 节点真线结果（250 rounds，确认数据形状一致）

[`rq1_chunk_sweep_cx6lx25g_2node_250r.csv`](../../experiments/results/cx6lx25g_c240g5/rq1_chunk_sweep_cx6lx25g_2node_250r.csv)

WQE/s 仍在 **16 KiB 处达峰 ~2.55 M/s**，与 loopback 同形状。

### 2.4 怎么解读这些数

⚠️ **chunk_sweep 测的不是端到端线速**。它的 round 时序：
1. client `post_write` 全部 chunk + 排干 sender CQE（这是"放到 NIC"，不是"到达对端"）
2. client `tcp_signal(server)` 表示发完
3. server 启动计时 `Stopwatch round_sw`
4. server 调用 `wait_for_ratio` 轮询 receive CQE
5. 退出，记 `round_ms = round_sw.elapsed_ms()`

step 4 的 wait 主要是 **server 侧轮询的 CPU 开销**，因为多数 chunk 在 step 3 之前已经到达 NIC 缓冲。所以 "effective_goodput" 在大 chunk 上飙到 384 GB/s ≫ 25 GbE 线速 = **server 侧 polling 完成的"已到达 chunk"通量**，不是 wire 通量。

但是！这正是 SemiRDMA 关心的指标：**RatioController 在固定 buffer 下能多快确认已收到 X% chunk**。WQE/s 在 16 KiB 达峰说明：
- 太小 chunk（1/4 KB）→ chunk 数太多，wait_for_ratio 内部 ChunkSet bookkeeping CPU-bound
- 太大 chunk（64/256 KB）→ chunk 数少 (16-64)，每次 poll 拿回的 chunk 总和小，CQE 处理摊不开

**结论：CPU-side ChunkSet/CQE 路径在 16 KiB chunk 下处理 WQE 最高效**。这跟链路带宽无关，与 NIC HCA 也无关，**所以 Phase 2 SoftRoCE 拐点能直接迁移到真硬件**。`chunk_bytes=16384` 沿用。

要测真 wire 带宽用 `ib_write_bw`（24.39 Gbps，见 [hardware-notes §8.2](./stage-b-hardware-notes.md#82-双节点-perftest-baseline)）。

---

## 3. RQ2 — ghost-mask RMS error

### 3.1 单机 loopback (200 rounds × 3 loss)

```
loss%  mean_ghost_ratio  mean_raw_rms   mean_masked_rms  rms_ratio
0.00   0.000             0.000e+00      0.000e+00        0.000
1.00   0.0104            1.343e-01      9.490e-02        0.7066
5.00   0.0506            3.147e-01      2.226e-01        0.7073
```

[`rq2_rms_error_cx6lx25g_loopback_200r.csv`](../../experiments/results/cx6lx25g_c240g5/rq2_rms_error_cx6lx25g_loopback_200r.csv)

### 3.2 2 节点真线 (100 rounds × 3 loss)

```
loss%  mean_ghost_ratio  mean_raw_rms   mean_masked_rms  rms_ratio
0.00   0.000             0.000e+00      0.000e+00        0.000
1.00   0.0100            1.307e-01      9.231e-02        0.7065
5.00   0.0498            3.127e-01      2.211e-01        0.7069
```

[`rq2_rms_error_cx6lx25g_2node_100r.csv`](../../experiments/results/cx6lx25g_c240g5/rq2_rms_error_cx6lx25g_2node_100r.csv)

### 3.3 结论

| 拓扑 | 1% loss ratio | 5% loss ratio |
|------|--------------:|--------------:|
| aliyun SoftRoCE | 0.707 | 0.707 |
| c240g5 loopback | **0.7066** | **0.7073** |
| c240g5 2-node real wire | **0.7065** | **0.7069** |
| 理论 1/√2 | **0.7071** | **0.7071** |

**完美等价性**：GhostMask 把 raw RMS 降到 1/√2 这件事是**纯数学**结论（zero-fill 等价于 unbiased estimator 下的 variance 减半），与硬件路径无关。本表证明 RQ2 在真机上不出意外，论文里 §RQ2 sub-section 可以直接引用 SoftRoCE + 真机两份实证。

---

## 4. RQ4 — ratio/timeout sweep

### 4.1 单机 loopback (500 rounds × 4 ratio × 4 timeout，固定 1% 丢包，16 KiB chunk)

关键单元节选：

| ratio | timeout(ms) | achieved_ratio | timeout_rate | mean_wait(ms) | p99_wait(ms) |
|------:|------------:|---------------:|-------------:|--------------:|-------------:|
| 0.90 | 1 | 0.902 | 0.000 | 0.615 | 0.631 |
| 0.95 | 1 | 0.953 | 0.002 | 0.665 | 0.687 |
| 0.95 | 5 | 0.953 | 0.000 | 0.669 | 0.681 |
| 0.99 | 5 | 0.988 | 0.492 | 2.802 | 5.002 |
| 1.00 | 5 | 0.990 | 0.906 | 4.595 | 5.002 |

完整 16 行：[`rq4_ratio_timeout_cx6lx25g_loopback_500r.csv`](../../experiments/results/cx6lx25g_c240g5/rq4_ratio_timeout_cx6lx25g_loopback_500r.csv)

### 4.2 2 节点真线 (250 rounds × 4×4)

| ratio | timeout(ms) | achieved_ratio | timeout_rate | mean_wait(ms) | p99_wait(ms) |
|------:|------------:|---------------:|-------------:|--------------:|-------------:|
| 0.90 | 1 | 0.647 | **1.000** | 1.001 | 1.002 |
| 0.90 | 5 | 0.902 | 0.000 | 1.376 | 1.379 |
| 0.95 | 1 | 0.647 | **1.000** | 1.001 | 1.002 |
| **0.95** | **5** | **0.953** | **0.000** | **1.453** | **1.458** ← sweet spot |
| 0.95 | 20 | 0.953 | 0.000 | 1.451 | 1.455 |
| 0.99 | 5 | 0.988 | 0.444 | 3.060 | 5.002 |
| 1.00 | 5 | 0.990 | 0.940 | 4.792 | 5.002 |

完整 16 行：[`rq4_ratio_timeout_cx6lx25g_2node_250r.csv`](../../experiments/results/cx6lx25g_c240g5/rq4_ratio_timeout_cx6lx25g_2node_250r.csv)

### 4.3 关键观察

1. **timeout=1ms 在真线下全部 100% 超时**（loopback 下 1ms 够）。原因：实验 LAN ping RTT 0.12 ms，但 UC RDMA Write + CQE 端到端的 chunk 路径要再过几次 PCIe + ChunkSet bookkeeping，单 chunk 的 wait 大约 1.4 ms，1ms timeout 必超。
2. **新真机 sweet spot：(0.95, 5 ms)** — wait_p99 = **1.46 ms**，timeout_rate 0%，achieved_ratio 0.953。
3. 跟 aliyun (0.95, 20 ms) wait_p99 = 14.8 ms 比，**真机延迟降到 1/10**，timeout 阈值可以收紧 4×（20→5）。
4. ratio=1.00 在真硬件上仍然不可靠（任何 chunk 真丢就必超时），与 SoftRoCE 结论一致。

### 4.4 配置建议

更新 `stage_b_cloudlab.yaml`：
- `wait_ratio: 0.95` 不变
- `wait_timeout_ms: 5`（原 20 → 5，激进 4×）

---

## 5. 不能声明什么

1. **不能从 chunk_sweep "goodput" 数字声明 wire 带宽** — 见 §2.4 解读。wire 带宽走 `ib_write_bw` 测，结果是 24.39 Gbps（97.6% 线速）。
2. **不能从 250-round 2-node 数据声明 5-σ 显著性** — 250 rounds 主要是趋势确认；正式 paper 需要至少 1k-10k rounds × 多 seed。
3. **不能跨硬件比较绝对 wait_ms** — 1.46 ms 是 c240g5 Xeon Silver 4114 + CX-6 Lx 的特定数；换 EPYC + CX-6 100GbE 会再降。
4. **不能声明 RQ1 SoftRoCE 16 KiB 在所有硬件上都是拐点** — 本节只证明在 c240g5 + CX-6 Lx 上一致；推广需要更多 NIC/CPU 组合。

---

## 6. 后续行动

1. **Stage B 配置**：[`experiments/configs/stage_b_cloudlab.yaml`](../../experiments/configs/stage_b_cloudlab.yaml) 把 dev_name 注释更新（mlx5_2/_1 不对称），timeout_ms 改 5。
2. **`test_chunk_sweep.cpp` patch 提交**：env-var override 是真机标定必需。
3. **RQ5/RQ6 端到端训练**：本轮没装 GPU/CUDA/PyTorch GPU build，留到下一轮。届时 Stage A bit-for-bit 复现 + RQ6 五 baseline。

---

## 7. 相关文件

- [`tests/phase2/test_chunk_sweep.cpp`](../../tests/phase2/test_chunk_sweep.cpp) — RQ1 二进制 + drain env override
- [`tests/phase2/test_rms_error.cpp`](../../tests/phase2/test_rms_error.cpp) — RQ2 二进制
- [`tests/phase2/test_ratio_sweep.cpp`](../../tests/phase2/test_ratio_sweep.cpp) — RQ4 二进制
- [`tests/phase2/test_chunk_roundtrip.cpp`](../../tests/phase2/test_chunk_roundtrip.cpp), [`test_ratio_timeout.cpp`](../../tests/phase2/test_ratio_timeout.cpp), [`test_ghost_mask.cpp`](../../tests/phase2/test_ghost_mask.cpp) — gtest，加了 `SEMIRDMA_DEV` env override
- [`docs/phase3/stage-b-hardware-notes.md §8`](./stage-b-hardware-notes.md#8-2026-04-23--c240g5-双节点替代记录--phase-2-真机重跑) — 节点 + perftest + microbench M1-M5
- [`docs/phase2/design-core-transport.md`](../phase2/design-core-transport.md) — RQ 矩阵原始定义
- [`experiments/results/cx6lx25g_c240g5/`](../../experiments/results/cx6lx25g_c240g5/) — 全部 CSV + microbench JSON
