# Phase 2 实施日志 · Core Transport Layer

**时间窗：** 2026-04-16 至今（仍在进行中）
**配套设计文档：** [core-transport.md](core-transport.md)
**本文档地位：** 记录 Phase 2 实施过程中**做了什么、碰到什么、怎么修的、得出什么阶段性结论**。和 `core-transport.md §8 实验结果` 是两类文档 —— 那一节等实验跑完才填；本文档是实施侧的工程日志。

---

## 1. 已完成的操作

### 1.1 核心库初版落地（commit `b2137a0`）

一次性提交了设计文档锁定的全部四个 transport 模块 + 配套 utils + gtest 脚手架，共 **1824 行**：

| 模块 | 文件 | 行数 | 职责 |
|------|------|------|------|
| `UCQPEngine` | `src/transport/uc_qp_engine.{h,cpp}` | 111 + 326 | RAII 封装 UC QP 生命周期、offset 寻址的 `post_write` / `post_recv` / `poll_cq` |
| `ChunkManager` | `src/transport/chunk_manager.{h,cpp}` | 65 + 75 | `ChunkSet` 切分 + 每 chunk 状态（`has_cqe`、`valid_len`）|
| `RatioController` | `src/transport/ratio_controller.{h,cpp}` | 43 + 57 | CQE 轮询循环，`wait_for_ratio(cs, ratio, timeout_ms)` |
| `GhostMask` | `src/transport/ghost_mask.{h,cpp}` | 34 + 20 | 对没收到 CQE 的 chunk `memset(0)`；`apply_noop` 提供对照组 |
| Utils | `src/utils/{logging,timing}.h` | — | stderr 宏 + `Stopwatch` |
| 测试脚手架 | `tests/phase2/test_helpers.h` | 253 | TCP 交换、持久同步、fork 式 server/client harness |
| Roundtrip 测试 | `tests/phase2/test_chunk_roundtrip.cpp` | 122 | 4 MB / 16 KB chunk / 0% 丢包，验证 256 chunk 全送达 |
| Ratio 测试 | `tests/phase2/test_ratio_timeout.cpp` | 171 | 只发 240/256，验证 `wait_for_ratio(0.90)` 成功、`(1.00)` 超时 |
| GhostMask 测试 | `tests/phase2/test_ghost_mask.cpp` | 224 | 240/256 + old-pattern pre-fill，验证 ghost 区域被置零 + `apply_noop` 对照 |
| Sweep 实验 | `tests/phase2/test_chunk_sweep.cpp` | 308 | RQ1 的 5×4 矩阵扫描（chunk size × loss rate） |
| 构建 | `tests/phase2/CMakeLists.txt` + root `CMakeLists.txt`（Phase 1 已存在） | — | GTest 可选、`find_library(IBVERBS_LIB)` |

设计决策全部沿用 `core-transport.md §3`，没有偏离。

### 1.2 后续五个 fix 提交

| Commit | 主题 | 文件 |
|--------|------|------|
| `3be9953` | Ratio 测试断言语义错误 | `test_ratio_timeout.cpp` |
| `5487cea` | GhostMask 测试漏 CQE | `test_ghost_mask.cpp` |
| `bbe933a` | TCP 双端口 race | `test_chunk_sweep.cpp` |
| `5632553` | 多 cell reconnect race | `test_helpers.h` |
| `6fe1a92` | Sweep 多轮 RQ 溢出 | `test_chunk_sweep.cpp` |

五个 fix 全部在**测试代码**里，核心库 `src/transport/` 一行未改。这说明设计文档里的接口锁定是对的，但 test harness 低估了工程复杂度。

---

## 2. 碰到的问题与修复

### 2.1 `wait_for_ratio` 阈值语义的两种误用

**症状：** `test_ratio_timeout` 断言 `stats.completed == CHUNKS_TO_SEND (240)`，实际 `completed` 可能只有 231。

**根因：** `RatioController::wait_for_ratio` 的循环条件是 `cs.completion_ratio() < ratio`。一旦达到目标比例就立即 return，**不会**把剩余已到达的 CQE 排空。240/256 = 93.75%，目标 0.90 在 230.4 个 chunk 时就满足了。

**修复路径：**

- `3be9953` 修正 `test_ratio_timeout`：断言改为 `completed ≥ NUM_CHUNKS * 0.90 = 230`，匹配实际阈值语义。
- `5487cea` 修正 `test_ghost_mask`：直接把目标 ratio 设成**精确的** `CHUNKS_TO_SEND / NUM_CHUNKS = 0.9375`，这样 `wait_for_ratio` 会把全部 240 个 CQE 排空后才返回 —— 避免后续的逐字节 buffer 验证读到"还没 memset 成新值"的区域。

**写进 design doc 的教训：** `wait_for_ratio` 语义是 "**to threshold, not to drain**"。如果调用方需要"排空"语义，应该在 return 后再调一次 `poll_cq(max, short_timeout)`，或者把 ratio 设成精确值。这个约定应当体现在 `RatioController` 的文档注释里（待后续 PR 补）。

### 2.2 SYNC_PORT race（bootstrap 时序错位）

**症状：** `test_chunk_sweep` 客户端连接 `SYNC_PORT` 偶发 `ECONNREFUSED`。

**根因：** 原设计用**两个**端口：`TCP_PORT=18525` 做 QP info 一次性交换、`SYNC_PORT=18526` 做每轮同步。客户端的流程是"先完成 TCP_PORT 交换 → 立刻 connect(SYNC_PORT)"；但服务端顺序是"accept(TCP_PORT) → exchange → **再** listen(SYNC_PORT)"。客户端先到，遇到没 listen 的端口。

**修复（`bbe933a`）：** 参照 Phase 1 `test_netem_loss.c` 的做法 —— 只开**一条**持久 TCP，先用它交换 QP info，再直接复用同一个 fd 做每轮 `signal/wait/sent_count` 的应用层同步。端口从两个收敛到一个，race 消失。

**教训：** bootstrap 协议和 sync 协议用一条 TCP 不仅简单，还避开了两端 listen/connect 的时序耦合。将来 Phase 3 也沿用这个 pattern。

### 2.3 Sweep 跨 cell 的 TCP reconnect race

**症状：** `test_chunk_sweep` 走完一个 cell（如 chunk=1KB, loss=0%）进入下一个 cell 时，客户端偶发 `connect: Connection refused`。

**根因：** 每个 cell 用一个独立 `UCQPEngine`（构造/析构包括 PD/CQ/QP/MR 的完整生命周期）。服务端析构完上一个 cell → 重新 `tcp_listen_accept` 之间有几十 ms 间隙，客户端如果更快就会撞上关闭的端口。

**修复（`5632553`）：** `tcp_connect_to` 加指数退避重试：最多 20 次，间隔 `50ms × attempt`。单次最坏等待约 10 秒，实测 2–3 次内就能连上。

**另一条路径（否决）：** 把 TCP 端口也跨 cell 复用。否决原因：`UCQPEngine` 的构造/析构本身是测试目标之一（验证多次 bring-up 不泄露资源），TCP 跟着重连更能反映真实 bootstrap 路径。

### 2.4 多轮 RQ 溢出（`ibv_post_recv ENOMEM`）

**症状：** `test_chunk_sweep` 在 `loss=5%` cell 的第 20 轮左右开始报 `ENOMEM`。

**根因：** 原实现每轮开始都 `post_recv(num_chunks)`，但每轮**消费**的 Recv WR 数量 = `num_chunks × (1 - loss_rate)`。`loss=5%` 下每轮留下 ~5% 未消费的 Recv WR。RQ 深度设的是 `num_chunks + 64`，约 20 轮后累积的 outstanding Recv WR 超过 RQ 上限 → `ibv_post_recv` 返回 `ENOMEM`。

**修复（`6fe1a92`）：** 改成**预投递一次 + 按消费量补充**：

```cpp
// cell 开始时预投递一轮
for (int i = 0; i < num_chunks; i++) engine.post_recv(i);

for (int r = 0; r < rounds; r++) {
    // ... 发送 + 等待 ...
    size_t completed = cs.num_completed();
    // 只补充本轮实际消费掉的数量
    for (size_t i = 0; i < completed; i++) engine.post_recv(0);
}
```

这样 outstanding Recv WR 恒等于 `num_chunks`，不会累积。

**更深层的坑（未修，已记在开放问题）：**

- 当前"消费数 = 完成数"的等价关系依赖 UC QP 的一个假设：**丢失的 Write 不会消耗 Recv WR**。Phase 1 实验佐证这一点（丢包时 RQ 没被 drain），但严格证明需要在 ConnectX-5 真机再验一次。SoftRoCE 可能有不同的行为。
- `UCQPEngine` 层面应当提供 `post_recv_batch(n)` 和 `outstanding_recv()` 查询，避免调用方手工记账。这个 API 升级留到 `test_chunk_sweep` 跑出第一批数据之后再做。

---

## 3. 阶段性结论（截至 2026-04-17）

### 3.1 设计层面

1. **接口锁定是对的。** 五个 fix 全落在 test harness，核心库没改过。`core-transport.md §3` 的 `ChunkSet / RatioController / GhostMask` 接口直接复制到代码里就能用。
2. **P0 的四条核心结论被工程化兑现了：**
   - 结论 ①（`(1-p)^c` 曲线）→ `ChunkSet` 生成 N 个独立 `ChunkDescriptor`，chunk 间互不拖累 ✓
   - 结论 ②（CQE 是充要条件）→ `RatioController` 只轮询 CQE，绝不扫 buffer ✓
   - 结论 ③（纯前缀截断）→ `ChunkState` 只需 `{has_cqe, valid_len}`，`GhostMask` 实现只有 `memset` ✓
   - 结论 ④（几何模型成立）→ `test_chunk_sweep` 继续用软件丢包注入（`rand_r < loss_rate` 就 skip 掉 `post_write`）✓

### 3.2 工程层面

1. **Ratio 语义必须文档化。** `wait_for_ratio` 是 "**to threshold**" 不是 "**to drain**"，两处测试踩坑证明这个语义容易被误解。
2. **Bootstrap + Sync 用一条持久 TCP** 是唯一稳定的做法，不要拆端口。
3. **RQ 记账必须和实际消费匹配。** SoftRoCE 下"丢失的 Write 不消耗 Recv WR"成立，依此做"预投递 + 按消费量补充"；但此假设需要在 ConnectX-5 上再验一次。

### 3.3 完成进度

| 项目 | 状态 | 完成日期 |
|------|------|----------|
| `test_chunk_roundtrip` 真机跑绿 | **已完成** ✅ — aliyun SoftRoCE (rxe0) | Apr 17 |
| `test_ratio_timeout` 真机跑绿 | **已完成** ✅ — 同上 | Apr 17 |
| `test_ghost_mask` 真机跑绿 | **已完成** ✅ — 同上 | Apr 17 |
| `test_chunk_sweep` 跑出第一批 CSV | **已完成** ✅ — 20 cell × 500 轮，零崩溃 | Apr 17 |
| RQ2 实验（masked vs raw RMS error） | 需要在 sweep 数据之上加一组 point-to-point | 预计 May 3 |
| RQ4 实验（`(ratio, timeout)` 扫描） | 需要补一个独立 binary 或扩展 sweep | 预计 May 6 |
| `core-transport.md §8` 结果回填 | 依赖以上 | 预计 May 10 |

### 3.4 需要在下一次 iteration 决定的事

1. **是否把 RQ 记账封进 `UCQPEngine`？** 目前是测试代码手工 `post_recv(0)`，不够 RAII。
2. **`wait_for_ratio` 要不要加第三种语义 `wait_and_drain`？** 或者让调用方明确 `poll_cq(..., short_timeout)` 补排？倾向后者，让策略留在调用方。
3. **软件丢包注入模型是否要和 Phase 1 对齐到**相同的 RNG**？** 现在两边都是 `rand_r`，但 seed 基础不同；如果要做 Phase1-vs-Phase2 端到端比较图，需要统一 seed 规则。

---

## 4. RQ1 Chunk Sweep 实验结果（2026-04-17）

**环境：** aliyun 服务器，SoftRoCE loopback（rxe0），MTU 1024
**参数：** 4 MB buffer，seed=42，500 rounds/cell
**数据文件：** `experiments/results/rq1_chunk_sweep_softroce_500r.csv`

### 4.1 原始数据

| chunk | loss% | ghost_ratio | goodput (MB/s) | WQE/s | P50 (ms) | P99 (ms) |
|------:|------:|------------:|---------------:|------:|---------:|---------:|
| 1 KB  | 0.0 | 0.000000 | 59.58 | 61007 | 67.10 | 69.46 |
| 1 KB  | 0.1 | 0.001016 | 59.42 | 60845 | 67.13 | 69.85 |
| 1 KB  | 1.0 | 0.010111 | 58.87 | 60286 | 67.16 | 69.80 |
| 1 KB  | 5.0 | 0.050096 | 56.55 | 57906 | 67.01 | 70.66 |
| 4 KB  | 0.0 | 0.000000 | 71.45 | 18292 | 55.96 | 56.50 |
| 4 KB  | 0.1 | 0.001010 | 71.40 | 18278 | 55.96 | 56.33 |
| 4 KB  | 1.0 | 0.010104 | 70.68 | 18094 | 55.97 | 56.53 |
| 4 KB  | 5.0 | 0.050145 | 67.83 | 17366 | 55.96 | 56.57 |
| 16 KB | 0.0 | 0.000000 | 72.50 | 4640  | 55.17 | 55.26 |
| 16 KB | 0.1 | 0.001109 | 72.41 | 4634  | 55.17 | 55.39 |
| 16 KB | 1.0 | 0.010188 | 71.73 | 4591  | 55.18 | 55.47 |
| 16 KB | 5.0 | 0.050188 | 68.83 | 4405  | 55.18 | 55.70 |
| 64 KB | 0.0 | 0.000000 | 72.59 | 1162  | 55.10 | 55.17 |
| 64 KB | 0.1 | 0.000875 | 72.53 | 1160  | 55.10 | 55.20 |
| 64 KB | 1.0 | 0.009500 | 71.90 | 1150  | 55.10 | 55.29 |
| 64 KB | 5.0 | 0.047938 | 69.09 | 1106  | 55.10 | 55.54 |
| 256 KB| 0.0 | 0.000000 | 72.59 | 290   | 55.10 | 55.29 |
| 256 KB| 0.1 | 0.001000 | 72.51 | 290   | 55.08 | 55.19 |
| 256 KB| 1.0 | 0.009000 | 71.95 | 288   | 55.08 | 55.33 |
| 256 KB| 5.0 | 0.046000 | 69.26 | 277   | 55.08 | 55.11 |

### 4.2 分析：Ghost Ratio 与理论预测

**Phase 2 实验用的是 per-chunk 软件丢包注入**：客户端对每个 chunk 以概率 `p` 独立决定是否跳过 `post_write`，与 chunk 大小无关。因此理论预测是 **`ghost_ratio ≈ p`**，而非 Phase 1 的 per-packet 几何模型 `1-(1-p)^c`。

| loss% | 理论 ghost_ratio | 观测范围（5 种 chunk size） | 最大偏差 |
|------:|-----------------:|----------------------------:|---------:|
| 0.0 | 0.0000 | 0.0000 — 0.0000 | 0.0000 |
| 0.1 | 0.0010 | 0.0009 — 0.0011 | 0.0001 |
| 1.0 | 0.0100 | 0.0090 — 0.0102 | 0.0010 |
| 5.0 | 0.0500 | 0.0460 — 0.0501 | 0.0040 |

**结论：** 观测 ghost_ratio 与 per-chunk 理论预测 `p` 高度吻合，最大绝对偏差 0.004（来自 256KB / 5% cell，因为每轮仅 16 chunks，500 轮共 8000 个样本，离散采样噪声较大）。

> **注意**：per-chunk 软件注入和 per-packet 网络丢包是两个不同的模型。真实网络中 5% 的 packet loss 对大 chunk 的 ghost_ratio 远大于 5%：例如 64KB chunk 在 MTU=1024 下包含 64 个 packet，`1-(1-0.05)^64 = 96.3%` 的 chunk 会出现 ghost。Phase 2 实验故意分离了"chunk 大小对传输效率的影响"和"packet loss 对 chunk 存活率的放大效应"，后者已在 Phase 1 P0 的 `(1-p)^c` 曲线中验证。

### 4.3 分析：Goodput 随 Chunk Size 的变化

在 0% loss 下的 effective goodput：

```
1 KB  →  59.58 MB/s   (WQE rate: 61007/s)
4 KB  →  71.45 MB/s   (WQE rate: 18292/s)   +20% vs 1KB
16 KB →  72.50 MB/s   (WQE rate:  4640/s)   +1.5% vs 4KB
64 KB →  72.59 MB/s   (WQE rate:  1162/s)   ≈饱和
256KB →  72.59 MB/s   (WQE rate:   290/s)   ≈饱和
```

**观察：**

1. **1KB → 4KB 存在显著的 WQE 开销台阶（+20%）**：1KB chunk 需要 4096 WR/round，每个 WR 经过 `ibv_post_send` 系统调用 + SoftRoCE 软件路径的开销不可忽略。4KB 把 WR 数降到 1/4，goodput 提升 20%。
2. **4KB 之后边际收益快速递减**：16KB 比 4KB 只多 1.5%，64KB 之后完全饱和在 ~72.6 MB/s。瓶颈已经从 WQE 开销转移到 SoftRoCE loopback 自身的吞吐上限。
3. **SoftRoCE 的天花板约 72.6 MB/s**（loopback，4MB buffer，单 QP）。这个数字在 ConnectX-5 真机上会有数量级的差异（25–100 Gbps line rate）。

**对 RQ1 的初步回答：** 在 SoftRoCE 上，chunk size ≥ 4KB 即可逼近 throughput 饱和。更小的 chunk（1KB）付出 ~20% 的 WQE 开销代价，但换来更细粒度的丢包容忍（每丢一个 chunk 只浪费 1KB 而非 64KB）。最优 chunk size 的选择是 **WQE overhead vs. loss granularity** 的 tradeoff，具体最优点取决于 NIC 的 WQE rate 上限（SoftRoCE 很低，ConnectX-5 很高）。

### 4.4 分析：尾部延迟

| chunk | P50 (ms) | P99 (ms) | P99-P50 gap |
|------:|---------:|---------:|------------:|
| 1 KB  | 67.10 | 69.46 | **2.36 ms** |
| 4 KB  | 55.96 | 56.50 | **0.54 ms** |
| 16 KB | 55.17 | 55.26 | **0.09 ms** |
| 64 KB | 55.10 | 55.17 | **0.07 ms** |
| 256KB | 55.10 | 55.29 | **0.19 ms** |

（以上为 0% loss 数据）

1. **1KB chunk 的 P99-P50 gap (2.36ms) 显著大于其他 chunk size**：4096 个 WR 的轮询循环本身引入了可变延迟，个别轮次可能因 CQ 突发拥塞或调度延迟导致等待时间变长。
2. **4KB+ 的 P99-P50 gap 在 0.5ms 以内**：更少的 WR 意味着 poll 循环收敛更快，尾部更稳定。
3. **loss > 0 时尾部延迟略微增加**（如 1KB/5%: P99=70.66，gap=3.66ms），因为 `wait_for_ratio` 在等待目标 ratio 时需要更多 poll 轮次，且部分轮次刚好在阈值边缘反复试探。

### 4.5 小结

| 维度 | 结论 |
|------|------|
| Ghost ratio 准确性 | per-chunk 软件注入下 `ghost_ratio ≈ p`，偏差 < 0.4%；模型正确 |
| Goodput 最优 chunk size | SoftRoCE 上 ≥ 4KB 即饱和（~72 MB/s）；1KB 有 20% WQE 开销 |
| WQE rate 上限 | SoftRoCE 下单 QP 约 61K WR/s（1KB）；ConnectX-5 待测 |
| 尾部延迟 | 1KB chunk 有 2–4ms P99-P50 gap；4KB+ 在 0.5ms 以内 |
| 传输库稳定性 | 20 cell × 500 rounds = 10000 轮次，零崩溃、零错误 CQE |

---

## 5. 下一步操作清单

按优先级排：

1. ~~在 Linux remote 跑通三个 gtest 二进制~~ — **已完成** (Apr 17)
2. ~~跑 `test_chunk_sweep` 完整 20 cell × 500 轮~~ — **已完成** (Apr 17)
3. ~~分析 sweep 结果~~ — **已完成**，见上文 §4
4. **在 ConnectX-5 真机重跑 sweep** — SoftRoCE 的 ~72 MB/s 天花板和 ~61K WQE/s 都是软件模拟极限，真机数据才能回答 "chunk size floor" 问题。需要 CloudLab 或 cs528 环境。
5. ~~写 RQ2 的 point-to-point binary~~ — **已完成** (Apr 17)，见 §6。
6. **补 per-packet loss 对照实验** — 当前 sweep 是 per-chunk 注入，需要一组 netem loss 实验（或等价的 per-packet 软件注入）来验证 `1-(1-p)^c` 模型在 Phase 2 框架下是否仍然成立。
7. **回填 `core-transport.md §8`** 并在 `docs/README.md` 里把 Phase 2 状态从"进行中"改成"已完成"。

---

## 6. RQ2 实验完成（2026-04-17）

详细结果、原理分析与讨论见独立文档 [rq2-ghost-masking-results.md](rq2-ghost-masking-results.md)。

**一句话结论：** `GhostMask::apply` 相比"原样聚合上一轮残留 buffer"稳定降低 29% 的梯度 RMS 误差（实测 ratio 0.7066 / 0.7073，与理论 `1/√2 ≈ 0.7071` 误差 < 0.1%）。

**工程侧：** `src/transport/` 一行未动；新增 [test_rms_error.cpp](../../tests/phase2/test_rms_error.cpp) (344 行) + CMakeLists 2 行；test harness 一次 run 通过（aliyun SoftRoCE loopback，~4 分钟）。
