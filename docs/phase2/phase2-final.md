# Phase 2 · Core Transport Layer · 最终总结

> **时间窗：** 2026-04-16 → 2026-04-19（2 周实施）
> **状态：** 已收尾。所有 C++ 模块、gtest、RQ1/RQ2/RQ4 实验完成并在 SoftRoCE 上验证。
> **历史细节：** [history/](history/) 目录保留原始设计、日志、各 RQ 独立结果文档。
> **下一阶段：** [../phase3/phase3-final.md](../phase3/phase3-final.md)

---

## 1. Phase 2 干了什么

把 Phase 1 P0 实验验证的 UC QP 语义（单 WR 内部丢包、CQE 充要条件、纯前缀截断、几何丢包模型）工程化成**四个 C++ 模块**，在 aliyun SoftRoCE 上跑通点对点 chunk 传输 + 三个 RQ 参数实验。**不做 AllReduce、不做 PyTorch 集成**（那是 Phase 3）。

## 2. 产出代码（src/ 下锁定）

| 模块 | 文件 | 职责 |
|---|---|---|
| `UCQPEngine` | `src/transport/uc_qp_engine.{h,cpp}` | RAII 封装 UC QP 生命周期；`post_write(offset, length)` 支持 MR 内分段；`poll_cq` 暴露 imm_data |
| `ChunkManager` | `src/transport/chunk_manager.{h,cpp}` | `ChunkSet` 按 chunk_bytes 切分；每 chunk 状态 `{has_cqe, valid_len}` |
| `RatioController` | `src/transport/ratio_controller.{h,cpp}` | `wait_for_ratio(cs, ratio, timeout_ms)` — 只看 CQE，绝不扫 buffer |
| `GhostMask` | `src/transport/ghost_mask.{h,cpp}` | 对无 CQE 的 chunk 区域 `memset(0)`；`apply_noop` 提供对照 |

测试：
- `tests/phase2/test_chunk_roundtrip` / `test_ratio_timeout` / `test_ghost_mask` — gtest 单元
- `tests/phase2/test_chunk_sweep` / `test_rms_error` / `test_ratio_sweep` — RQ 实验主程序

## 3. 三个 RQ 的结论（一句话版）

| RQ | 结论 | 来源 |
|---|---|---|
| **RQ1 — Write 粒度** | 4 KiB 起 throughput 饱和；**16 KiB** 是 ghost 减少 / WQE 开销 / tail latency 的综合甜点，P99−P50 gap < 0.1 ms | chunk sweep 5×4 矩阵 |
| **RQ2 — Ghost 缓解** | `GhostMask::apply` 把 per-element RMS 误差比压到 **0.707**（1% loss）/ **0.707**（5% loss），理论 `1/√2` 吻合小数点后 3 位 | rms_error point-to-point |
| **RQ4 — Ratio/Timeout** | Operating point `(ratio=0.95, timeout=20ms)` 在 loss=1% 下实现 `achieved_ratio ≥ 0.95` 且 `wait_p99 = 15.9ms`，比 `ratio=1.00 baseline=100ms` 降低 **6.3×** tail | ratio×timeout 16-cell 扫描 |

## 4. Phase 3 直接继承的默认参数

| 参数 | 默认值 | 依据 |
|---|---:|---|
| `chunk_bytes` | **16384** | RQ1 SoftRoCE 饱和平台；真机需重扫（Phase 3 Stage B） |
| `ratio` | **0.95** | RQ4 最优 operating point |
| `timeout_ms` | **20**（SoftRoCE），Phase 3 在真机需重标定 | RQ4；真机 CQE 分布不同 → 见 Phase 3 |
| `ghost_mask` | **on** (`GhostMask::apply`) | RQ2 证明数值正确 |
| `rq_depth` | `num_chunks + 64` | 预投递 + 按消费量补充，避免多轮累积 |

## 5. SoftRoCE 阶段的局限（carry-over 到 Phase 3）

- **SoftRoCE 天花板 ~72 MB/s**：真机 25–100 Gbps 后 WQE rate 上限、timeout 绝对值都会改变 → 真机需重标定
- **Per-chunk Bernoulli ≠ per-packet netem**：RQ1 用的是 client 对每个 chunk 以概率 p 跳过 post_write 的软件模型，与 Phase 1 的 netem per-packet 丢包不是同一模型。真机用什么 loss 模型由 Phase 3 的 loss injection 策略决定
- **RQ4 的 20ms timeout 不迁移**：Phase 2 说得很清楚 — 真机 CQE 到达 p99 比 SoftRoCE 快一个数量级，Phase 3 应按 `timeout = 1.5 × CQE_p99` 重新标定（这是 Phase 3 踩到的大坑之一；见 phase3-final §3）

## 6. API 增补欠账（Phase 3 补上）

RQ1 实施日志里标注的两个小接口改进：

- `UCQPEngine::post_recv_batch(n)` + `outstanding_recv()` — 避免调用方手工记 recv WR 数
- `wait_for_ratio` 语义注释 — "**to threshold**, not to drain"，需要排空的调用方应显式补 `poll_cq(max, short_timeout)`

两个都在 Phase 3 pybind11 封装阶段落实。

## 7. 历史文档映射

| Phase 2 原始文档 | 所在位置 |
|---|---|
| `design-core-transport.md`（设计锁定 + §8 结果汇总） | [history/design-core-transport.md](history/design-core-transport.md) |
| `rq1-log-implementation.md`（实施日志 + RQ1 结果） | [history/rq1-log-implementation.md](history/rq1-log-implementation.md) |
| `rq2-results-ghost-masking.md`（RQ2 完整分析） | [history/rq2-results-ghost-masking.md](history/rq2-results-ghost-masking.md) |
| `rq4-results-ratio-timeout.md`（RQ4 完整分析） | [history/rq4-results-ratio-timeout.md](history/rq4-results-ratio-timeout.md) |
