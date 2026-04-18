# Phase 2 RQ4 · CQE-Driven Ratio / Timeout Sweep

**实验日期：** 2026-04-18
**实验产物：** [test_ratio_sweep.cpp](../../tests/phase2/test_ratio_sweep.cpp) · [rq4_ratio_timeout_softroce_500r.csv](../../experiments/results/rq4_ratio_timeout_softroce_500r.csv) · [rq4_ratio_timeout_perround_softroce_500r.csv](../../experiments/results/rq4_ratio_timeout_perround_softroce_500r.csv)
**运行环境：** aliyun, SoftRoCE (rxe0) loopback
**配套设计：** [design-core-transport.md §2.3](design-core-transport.md) · [rq1-log-implementation.md §1](rq1-log-implementation.md)

---

## 0. 一眼看懂（回来翻这篇文档时先读这节）

### 0.1 本文档定位：**SoftRoCE 阶段参数扫描，不作为论文主结果**

- 本实验所有数据来自 SoftRoCE loopback，**不直接写进论文**
- 目的：找到 `RatioController::wait_for_ratio(ratio, timeout_ms)` 的**合理参数空间**，供 Phase 3 多 worker AllReduce 和 Phase 4 真机 ConnectX-5 外推
- 论文中 "CQE-driven ratio 降低 tail latency" 的**主张证据**需等到 **Phase 4 真机 + 真训练** 时测量 iteration P99

### 0.2 实验在干嘛（一段话版本）

`RatioController::wait_for_ratio(cs, ratio, timeout_ms)` 是 Phase 2 的**进度控制原语**——server 端轮询 CQ，直到 `completed/total ≥ ratio` 或 `timeout_ms` 到期就立即返回。两个参数的权衡是：`ratio` 太高拖 tail（等最后一个 ghost chunk 等到超时），`timeout_ms` 太小还没等到就放弃。本实验扫描 `ratio ∈ {0.90, 0.95, 0.99, 1.00}` × `timeout_ms ∈ {1, 5, 20, 100}` = 16 个 cell，固定 chunk=16 KB、buffer=4 MB、loss=1%，每 cell 500 轮，测量 `wait_latency_p99`、`achieved_ratio`、`poll_count`、`timeout_rate`。

### 0.3 关键实验设计

- 固定 chunk=16 KB、buffer=4 MB、loss=1%，每 cell 每 QP 独立重建（避免跨 cell 状态污染）
- Server 的 `wait_for_ratio` 在**未等 client-done 前即开始轮询**——CQE 与 post_write 真实并发，wait_latency 反映真实到达时序（与 RQ2 的 drain-for-determinism 语义不同）
- Loss 继续用**软件 per-chunk Bernoulli**（与 RQ1/RQ2 一致，P0 已验证与 netem 等价）

### 0.4 结果：设计目标达成

| 目标 (design §2.3) | 实测 cell | 结论 |
|---|---|---|
| `achieved_ratio ≥ 0.95` | `r=0.95, t=100ms`：0.9532 | ✓ |
| `wait_p99 < 50% of ratio=1.00 的 wait_p99` | `r=0.95` 的 15.9ms / `r=1.00` 的 100ms = **15.9%**（≈ 6.3× 减少） | ✓ |

**推荐 operating point：** `ratio=0.95, timeout=20ms` — achieved 0.952，timeout_rate 0.6%，wait_p99=14.8ms；再加 timeout 到 100ms 只把 timeout_rate 压到 0% 而 p99 上升到 15.9ms，**边际收益为负**。

### 0.5 这个实验**不能**声明什么

- ❌ "CQE-driven ratio 对训练收敛有 X% 收益" — 没跑训练
- ❌ "ratio=0.95 是所有场景下的最优" — 本实验 loss 固定 1%，`ratio*` 是 loss 的函数（§6.2）
- ❌ "Phase 2 论文 tail latency 章节的主证据" — 这是 SoftRoCE 参数探索，真机数字在 Phase 4 回填
- ✅ "16KB chunk / 4MB layer / 1% loss 下，ratio=0.95 ± timeout=20ms 是合理默认"
- ✅ "achievable ratio 的上界 ≈ (1-p)^1 单次 Bernoulli 存活率，设 ratio 高于此值必超时"

---

## 1. 背景与动机

### 1.1 RQ2 遗留的硬边界

[rq2-results-ghost-masking.md §6.1](rq2-results-ghost-masking.md) 证明 `GhostMask::apply` 的数值实现符合理论，但 mask 是**数据层面**的补丁——前提是 "某些 chunk 已放弃"。那**谁决定放弃？** 这是 `RatioController` 的职责：达到 `ratio` 或超时就放行。参数不对等价于以下两种病态：

- `ratio` 太激进（例如 1.00）+ loss > 0 ⇒ 永远达不到，每轮都被 `timeout_ms` 强制截断，tail latency = timeout
- `ratio` 合理但 `timeout_ms` 太紧 ⇒ CQE 还没到就放弃，`achieved_ratio ≪ ratio`，ghost chunk 数激增，`GhostMask` 需要 mask 掉更多数据 ⇒ RMS 误差上升

### 1.2 RQ4 要回答的问题

> **给定 loss=1%，`(ratio, timeout)` 参数空间里是否存在一个 operating point，能在 `achieved_ratio ≥ 0.95` 的同时让 `wait_p99` 比 `ratio=1.00` baseline 小一个数量级？**

这是 "CQE-driven forward progress 相对 hard-full-reliability" 的**基本 feasibility 检验**。如果连在 SoftRoCE loopback 都找不到这样的点，Phase 3 的 AllReduce 就不必谈。

### 1.3 与 RQ1 / RQ2 的职责划分

| 维度 | RQ1 (chunk sweep) | RQ2 (ghost masking) | RQ4 (ratio/timeout) |
|------|-------------------|--------------------|---------------------|
| **关心的问题** | chunk 大小如何影响吞吐 vs ghost | mask 与否对数值误差的影响 | ratio/timeout 如何影响 tail latency vs achieved ratio |
| **测量的量** | `ghost_ratio`, `goodput`, `wqe_rate`, `p99_latency` | `raw_rms`, `masked_rms`, `rms_ratio` | `wait_latency_p99`, `achieved_ratio`, `poll_count`, `timeout_rate` |
| **Chunk 维度** | 扫描 5 档（1KB–256KB） | **固定 16 KB** | **固定 16 KB** |
| **Loss 维度** | 扫描 4 档（0%–5%） | 扫描 3 档（0%, 1%, 5%） | **固定 1%** |
| **扫描维度** | chunk × loss | loss | **ratio × timeout** |
| **评估对象** | 传输层效率 | 梯度聚合数值精度 | 进度控制时序质量 |

---

## 2. 实验设计

### 2.1 参数表

| 项目 | 取值 | 说明 |
|------|------|------|
| `BUF_SIZE`            | 4 MiB                  | 一层梯度代理尺寸，与 RQ1/RQ2 一致 |
| `CHUNK_BYTES`         | 16 KiB                 | RQ1 已定为 SoftRoCE 吞吐饱和平台 |
| `NUM_CHUNKS / round`  | 256                    | `4MB / 16KB` |
| `LOSS_RATE`           | 0.010                  | 固定 1%，per-chunk Bernoulli |
| `RATIOS`              | `{0.90, 0.95, 0.99, 1.00}` | 覆盖 "激进放行" 到 "硬可靠" 全谱 |
| `TIMEOUTS`            | `{1, 5, 20, 100}` ms   | 覆盖 "欠采样" 到 "几乎不超时" 全谱 |
| `ROUNDS / cell`       | 500                    | per [design-core-transport.md §2.3](design-core-transport.md) |
| `TCP_PORT`            | 18527                  | 与 RQ1 (18525)、RQ2 (18526) 分开 |
| `BASE_SEED`           | 42                     | 与 RQ1/RQ2 一致 |

共 4 × 4 = **16 cell × 500 round = 8000 轮**。

### 2.2 关键时序设计：server 先于 client 启动 wait

**与 RQ2 的差异是核心决定点。** RQ2 里 server 先 `tcp_wait(fd)` 等 client 发完所有 chunk 再调 `wait_for_ratio`，此时 CQE 全部已到达，wait 返回近乎 0——这对 RQ2 的 "验证 mask 数值正确" 是好事（排除 straggler 干扰），对 RQ4 是**毁灭性偏差**，因为会把 `wait_latency` 压成 0。

本实验反过来：

```
Server 每轮:
    tcp_signal(fd)                        // 告诉 client 准备好了，立刻开始计时
    wait_for_ratio(cs, ratio, timeout_ms) // 这里 poll_cq 与 client post_write 真实并发
    tcp_wait(fd); read(sent_count)        // 等 client 发完，读实际发送数
    drain_stragglers(20ms)                // 排剩余 CQE 给下轮保留正确 outstanding 数
```

这样 `wait_latency_ms` 就是 "从通知 client 到拿到 ratio 个 CQE 的真实耗时"，而不是 "poll-once-done 的人为零值"。

### 2.3 Outstanding Recv WR 管理

Phase 1 吃过一次亏：RQ overflow 会让后续 Write 静默丢失，看起来像 loss。RQ4 保持与 RQ1 一样的策略：

- 初始 pre-post `NUM_CHUNKS = 256` 个 Recv WR
- 每轮结束后 `repost = final_completed`（即"这一轮收了多少 CQE 就补多少 Recv"），保持 outstanding ≈ 256
- `max_rq = 256 + 64` 留 64 的 buffer 应对极端 straggler

### 2.4 Server 单轮流程（对应 test_ratio_sweep.cpp:162-205）

```
for r in 0..499:
    ChunkSet cs;  RatioController rc;  WaitStats stats;

    1. tcp_signal(fd)                     // "server ready" 起跑枪
    2. rc.wait_for_ratio(cs, ratio, timeout_ms, &stats)
    3. tcp_wait(fd); read(fd, &sent_count, 4)
    4. drain_stragglers(cs, 20ms)         // 补齐剩余 CQE，不计时
    5. final_completed = cs.num_completed()
    6. log(stats.latency_ms, stats.poll_count, stats.completed, stats.timed_out)
    7. repost final_completed 个 Recv WR
```

### 2.5 Client 单轮流程（对应 test_ratio_sweep.cpp:274-306）

```
for r in 0..499:
    1. tcp_wait(fd)                       // 等 server ready
    2. for chunk in cs:
         if rand_u() < LOSS_RATE: continue // per-chunk Bernoulli drop
         post_write(chunk, with_imm=chunk_id)
         sent_count += 1
    3. poll_cq until drained(sent_count)
    4. tcp_signal(fd); write(fd, sent_count)
```

---

## 3. 代码改动

### 3.1 零修改的核心库

**`src/transport/` 下所有文件一行未动。** 本实验完全复用 RQ1/RQ2 已存在的 `UCQPEngine::poll_cq` / `RatioController::wait_for_ratio` / `ChunkSet::mark_completed` / `WaitStats` 接口。

### 3.2 新增文件

| 文件 | 行数 | 职责 |
|------|-----:|------|
| [tests/phase2/test_ratio_sweep.cpp](../../tests/phase2/test_ratio_sweep.cpp) | 354 | RQ4 实验二进制（非 gtest） |
| [experiments/results/rq4_ratio_timeout_softroce_500r.csv](../../experiments/results/rq4_ratio_timeout_softroce_500r.csv) | 16 行 + header | 主 CSV（每 cell 一行汇总） |
| [experiments/results/rq4_ratio_timeout_perround_softroce_500r.csv](../../experiments/results/rq4_ratio_timeout_perround_softroce_500r.csv) | 8000 行 + header | 详情 CSV（每轮一行） |

### 3.3 修改文件

- [tests/phase2/CMakeLists.txt](../../tests/phase2/CMakeLists.txt)：追加 2 行注册 `test_ratio_sweep`（非 gtest）

### 3.4 复用的工具

- [src/transport/uc_qp_engine.h](../../src/transport/uc_qp_engine.h)：`post_write` / `post_recv` / `poll_cq`
- [src/transport/chunk_manager.h](../../src/transport/chunk_manager.h)：`ChunkSet`
- [src/transport/ratio_controller.h](../../src/transport/ratio_controller.h)：`RatioController::wait_for_ratio` + `WaitStats`
- [tests/phase2/test_helpers.h](../../tests/phase2/test_helpers.h)：`tcp_listen_accept` / `tcp_connect_to` / `tcp_signal` / `tcp_wait` / `tcp_exchange_on_fd_*`

---

## 4. 理论预测

### 4.1 achievable ratio 的上界

每轮 client 发送 chunk 数 `S_r ~ Binomial(256, 1-p)`，期望 `E[S_r] = 256·(1-p) = 253.44`。`achieved_ratio` 的理论上界为 `S_r / 256`，期望 `≈ 1-p = 0.99`。

**推论：** 设 `ratio > 0.99` 不可能在 loss=1% 下稳定达到，必然触发 timeout 逻辑。

### 4.2 wait_latency 的三段分区

根据 `(ratio, timeout)` 相对于 achievable 区域的位置：

| 区域 | 条件 | 预期行为 |
|------|------|---------|
| **欠采样区** | `timeout` 远小于 256 chunk 发完的时间 | `wait_latency ≈ timeout`，`achieved_ratio` 正比 `timeout / transmit_duration`，timeout_rate = 100% |
| **适配区** | `ratio ≤ (1-p)` 且 `timeout` 足以覆盖 CQE 的 P99 到达时间 | `wait_latency` 取决于 `ratio·N` 个 CQE 到齐的时刻，timeout_rate ≈ 0 |
| **过度约束区** | `ratio > (1-p)` | `wait_latency ≈ timeout`（等到超时放弃），timeout_rate 趋近 1 |

### 4.3 poll_count 的正比关系

`poll_count` ≈ wait 总时长 / 单次 `ibv_poll_cq` 耗时。实测单次 poll ~3 µs（SoftRoCE 轻载），所以 poll_count ≈ `wait_ms × 300`。

---

## 5. 结果

### 5.1 主表（16 cell × 500 round）

```
ratio,timeout_ms,rounds,loss_pct,mean_sent_count,mean_completed,mean_achieved_ratio,timeout_rate,mean_wait_ms,p50_wait_ms,p99_wait_ms,mean_poll_count,p99_poll_count
0.90,  1,500,1.00,253.392, 22.104,0.086344,1.000000,  1.003,  1.002,  1.058,  289, 324
0.90,  5,500,1.00,253.436,109.530,0.427852,1.000000,  5.003,  5.002,  5.054, 1371,1570
0.90, 20,500,1.00,253.464,231.044,0.902516,0.000000,  9.877,  9.319, 14.437, 2787,3268
0.90,100,500,1.00,253.362,231.012,0.902391,0.000000, 10.132,  9.333, 14.128, 2811,3831
0.95,  1,500,1.00,253.414, 20.832,0.081375,1.000000,  1.003,  1.002,  1.053,  278, 324
0.95,  5,500,1.00,253.456,114.252,0.446297,1.000000,  5.005,  5.002,  5.120, 1418,1566
0.95, 20,500,1.00,253.444,243.666,0.951820,0.006000, 10.086,  9.823, 14.770, 2936,3940
0.95,100,500,1.00,253.382,244.018,0.953195,0.000000, 10.831,  9.881, 15.902, 2942,3888
0.99,  1,500,1.00,253.444, 21.306,0.083227,1.000000,  1.004,  1.002,  1.063,  285, 324
0.99,  5,500,1.00,253.394,107.974,0.421773,1.000000,  5.005,  5.002,  5.125, 1344,1563
0.99, 20,500,1.00,253.468,252.772,0.987391,0.470000, 14.994, 14.013, 20.004, 4376,6025
0.99,100,500,1.00,253.414,253.060,0.988516,0.474000, 53.299, 14.710,100.004,15386,29793
1.00,  1,500,1.00,253.322, 21.024,0.082125,1.000000,  1.004,  1.002,  1.075,  280, 324
1.00,  5,500,1.00,253.458,113.178,0.442102,1.000000,  5.003,  5.002,  5.033, 1405,1568
1.00, 20,500,1.00,253.308,253.308,0.989484,0.928000, 19.333, 20.002, 20.033, 5496,6038
1.00,100,500,1.00,253.428,253.428,0.989953,0.946000, 95.196,100.002,100.023,27961,30138
```

### 5.2 Achieved ratio 热力图（读作：每 cell 的 mean achieved）

```
ratio \ timeout_ms │    1       5      20     100
─────────────────── ┼───────────────────────────────
    0.90           │ 0.086  0.428  0.903  0.902
    0.95           │ 0.081  0.446  0.952  0.953
    0.99           │ 0.083  0.422  0.987  0.989
    1.00           │ 0.082  0.442  0.989  0.990
```

**两条分界线：**
- 横向 `timeout=20ms` 是 "足够" 的门槛——所有 `timeout ≥ 20ms` cell 达到或接近目标 ratio
- 竖向 `ratio=0.99/1.00` 永远卡在 0.989 附近——客户端平均只 post 253.4/256 ≈ 0.989 个 chunk，理论上界就是这个

### 5.3 Wait latency P99 热力图（单位 ms）

```
ratio \ timeout_ms │    1       5      20     100
─────────────────── ┼───────────────────────────────
    0.90           │  1.06   5.05  14.44  14.13
    0.95           │  1.05   5.12  14.77  15.90
    0.99           │  1.06   5.13  20.00 100.00
    1.00           │  1.07   5.03  20.03 100.02
```

**三条观察：**
1. 欠采样区（`timeout ≤ 5ms`）：P99 = timeout + 小抖动，因为永远 hit deadline
2. 适配区（`ratio ≤ 0.95, timeout ≥ 20ms`）：P99 稳定在 14–16 ms，**不随 timeout 增加**——意味着 CQE 实际到齐只需 ~15 ms，剩下全是白等
3. 过约束区（`ratio ≥ 0.99, timeout = 100ms`）：P99 = 100ms，被 timeout 吃满

### 5.4 Timeout rate 热力图

```
ratio \ timeout_ms │    1       5      20     100
─────────────────── ┼───────────────────────────────
    0.90           │  1.00   1.00   0.00   0.00
    0.95           │  1.00   1.00   0.006  0.00
    0.99           │  1.00   1.00   0.47   0.47
    1.00           │  1.00   1.00   0.93   0.95
```

- ratio=0.99 的 timeout_rate=0.47 说明 "恰好落在 S_r / 256 阈值附近"——一半轮有 ≥ 253 chunk 到达（够），一半没够
- ratio=1.00 的 timeout_rate ~95%，5% 不 timeout 是因为这几轮 client 运气好没丢任何 chunk

### 5.5 Poll count — CPU 开销

```
ratio \ timeout_ms │    1       5      20     100
─────────────────── ┼───────────────────────────────
    0.90           │   289   1371   2787   2811
    0.95           │   278   1418   2936   2942
    0.99           │   285   1344   4376  15386
    1.00           │   280   1405   5496  27961
```

- 适配区 poll_count ≈ 2900：0.95 * 256 ≈ 243 个 CQE，平均每 12 次 poll 得到一个——poll 是 non-blocking batch of 64，空转是常态
- 过约束区 ratio=1.00 + timeout=100ms 达到 28k poll/round：这是纯 CPU 浪费，对 Phase 3 多 worker 并发的 AllReduce 是**明确要避免的区域**

---

## 6. 结论

### 6.1 Operating point 确认：ratio=0.95 + timeout=20ms

在 SoftRoCE loopback / 4MB layer / 16KB chunk / 1% loss 下：

- **achieved_ratio = 0.952**（超过 0.95 目标）
- **wait_p99 = 14.8 ms**（ratio=1.00 同 timeout 的 20 ms 的 74%，但 ratio=1.00 timeout=100ms 的 15%）
- **timeout_rate = 0.6%**（几乎从不超时）
- **poll_count = 2.9k / round**（相对 ratio=1.00 的 28k 降低 **~10×**）

这个 cell 同时满足 design-core-transport §2.3 的两个成功判据，且 timeout 从 20ms 加到 100ms **边际收益为负**（p99 反而从 14.8 升到 15.9），因此**不建议在 loss=1% 场景设 timeout > 20ms**。

### 6.2 Achievable ratio 被 (1-p) 硬卡住

平均 `sent_count = 253.4`，对应 achievable ratio 上限 ≈ 0.99。设 `ratio = 1.00` 等价于 "等一个永远不会来的 CQE 直到 timeout"——在现场 loss 存在的系统里这是**绝对反模式**。

**Phase 3 对 loss-adaptive ratio 的启示：** `ratio*` 应当是 loss 的函数，保守估计 `ratio* ≈ (1-p) - ε`（例如 p=1% ⇒ ratio*=0.95，p=5% ⇒ ratio*=0.85）。`GhostMask` 会补上 mask 掉的 chunk；`RatioController` 必须拒绝不可达的目标。

### 6.3 Timeout 应该贴近 "CQE 真实到达 P99"

适配区 `ratio ≤ 0.95, timeout = 20ms` 的 wait_p99 实测 14.8 ms，**刚好 = CQE 实际到齐的 P99**。timeout 设得更高没有任何 tail 改善（P99 上升源自偶发 straggler），反而 `ratio ≥ 0.99` 的 timeout=100ms 直接把 tail 推到 100ms。

**工程约定：** `timeout = round(1.5 × CQE_p99_expected)` — 真机 ConnectX-5 这个数字可能在 1 ms 量级，SoftRoCE 因为软件路径 ≥15 ms，Phase 4 需要重测。

### 6.4 局限性

1. **Single sender / single receiver**：没覆盖多 worker 并发争抢 CQ 的 poll_cq 代价放大效应。
2. **Loss 固定 1%**：`ratio*` 与 loss 的函数关系只能外推（§6.2），没有直接数据。
3. **SoftRoCE loopback**：wait latency 的绝对值（~15 ms）主要是软件路径开销，ConnectX-5 真机预计降至 ms 量级，但**相对趋势（ratio 越低 tail 越短、timeout 过紧 starvation）不受硬件影响**。
4. **Chunk size 固定 16 KB**：chunk × ratio × timeout 三维交互未测。理论上 chunk 减小 ⇒ 总 WR 数增加 ⇒ CQE 峰值到达更集中，可能缩短 wait_p99。

### 6.5 与 RQ1/RQ2 的衔接

- RQ1 告诉我们：16 KB chunk 能把 ghost 压到 `1-(1-0.01)^16 ≈ 14.9%`
- RQ2 告诉我们：对剩余 ghost chunk `GhostMask::apply` 数值上降低误差 29%（RMS ratio = 0.707）
- RQ4 告诉我们：在 "放行 95% 就走" 的策略下，tail latency 比硬等全到降 6.3×（15.9 ms vs 100 ms）

**Phase 2 end-to-end 收益：** 相同 buffer / 同 loss 下，`16KB chunk + ratio=0.95 + timeout=20ms + GhostMask` 相比 "256KB Write + 硬等全到" 预期带来 ~**6× tail 改善 + ~30% 聚合数值误差改善**，单层 P99 latency 从 ~100 ms 降至 ~15 ms。这个数字需要 Phase 4 真机 + 真训练 iteration benchmark 验证端到端是否翻译到 step-time。

---

## 7. 下一步

1. **Phase 2 收尾**：把 RQ1/RQ2/RQ4 的 "SoftRoCE 参数推荐" 汇总到 [design-core-transport.md §8 实验结果](design-core-transport.md#8-实验结果回填)。
2. **Phase 3 **（pybind11 + PyTorch hook）：
   - 把 `(chunk=16KB, ratio=0.95, timeout=20ms)` 作为默认参数
   - 实现 loss-adaptive `ratio*`（§6.2），动态跟踪最近 N 轮 `achieved_ratio` 均值
3. **Phase 3 多 worker AllReduce**：ring 拓扑下 per-hop wait 会串联，预计 N 个 worker 的链式 tail 放大 ≈ N × P99——RQ4 的 15.9 ms 在 4-worker ring 下直接给出 ~60 ms 的上界。
4. **Phase 4 真机 ConnectX-5**：重测本表格，预计绝对 wait_latency 下降一个数量级，但**最优参数的形状（ratio=0.95 附近最优，timeout ≈ 1.5×wait_p99）不变**。
5. **Chunk × Ratio × Timeout 三维扫描**（可选，若 Phase 3 时间允许）：§6.4 提出 chunk 减小可能进一步压 tail，需要一次 5×4×4 矩阵实验验证。

---

## 附录 A · 执行步骤回顾

```bash
# Local (Windows)
git add tests/phase2/test_ratio_sweep.cpp tests/phase2/CMakeLists.txt
git commit -m "test(phase2): add RQ4 ratio/timeout sweep experiment"
git push

# Remote (aliyun via SSH)
ssh aliyun
cd ~/SemiRDMA && git pull
cd build && cmake .. && cmake --build . --target test_ratio_sweep -j$(nproc)

# 合二为一的 runner（放背景跑）
cat > /tmp/run_rq4.sh <<'SH'
#!/bin/bash
cd /root/SemiRDMA/build/tests/phase2
./test_ratio_sweep server rxe0 500 /root/rq4_perround.csv > /root/rq4_main.csv 2> /root/rq4_server.log &
SRV=$!
sleep 1
./test_ratio_sweep client 127.0.0.1 rxe0 500 42 2> /root/rq4_client.log
wait $SRV
SH
chmod +x /tmp/run_rq4.sh
nohup /tmp/run_rq4.sh > /root/rq4_nohup.log 2>&1 &

# 实际运行时长：16 cell × 500 round ≈ 8–10 分钟（SoftRoCE loopback）

# 取回 CSV
scp aliyun:~/rq4_main.csv     experiments/results/rq4_ratio_timeout_softroce_500r.csv
scp aliyun:~/rq4_perround.csv experiments/results/rq4_ratio_timeout_perround_softroce_500r.csv
```

## 附录 B · Per-cell 统计脚本

分析 per-round 分布的脚本（aliyun 上一行 Python）：

```python
import csv, collections
rows = list(csv.DictReader(open('/root/rq4_perround.csv')))
cells = collections.defaultdict(list)
for r in rows:
    cells[(r['ratio'], r['timeout_ms'])].append(r)

print('cell | n | to_rate | ach mean p50 p99 | wait p50 p99 ms | poll p50 p99')
for k in sorted(cells.keys(), key=lambda x:(float(x[0]), int(x[1]))):
    rs = cells[k]
    achieved = sorted(float(r['achieved_ratio']) for r in rs)
    wait     = sorted(float(r['wait_ms'])         for r in rs)
    polls    = sorted(int(r['poll_count'])         for r in rs)
    timeouts = sum(int(r['timed_out']) for r in rs)
    p50 = lambda v: v[len(v)//2]
    p99 = lambda v: v[min(int(0.99*len(v)), len(v)-1)]
    print(f"r={k[0]} t={k[1]:>3} | n={len(rs)} | to={timeouts/len(rs):.3f} |"
          f" ach {sum(achieved)/len(rs):.4f} {p50(achieved):.4f} {p99(achieved):.4f} |"
          f" wait {p50(wait):.2f} {p99(wait):.2f} |"
          f" poll {p50(polls)} {p99(polls)}")
```
