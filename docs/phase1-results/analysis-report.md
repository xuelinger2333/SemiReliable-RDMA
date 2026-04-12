# Phase 1 分析报告：SoftRoCE 上的 UC QP 验证

**日期：** 2026-04-12
**环境：** SoftRoCE (rxe0)，单机 loopback，Linux
**目标：** 在进入核心实现之前验证 SemiRDMA 的三个关键设计假设

---

## 1. 分析问题

| 编号 | 问题 | 对应设计组件 |
|------|------|-------------|
| Q1 | UC Write-with-Immediate 在 SoftRoCE 上是否生成接收端 CQE？ | CQE 驱动的比率控制 (RQ4) |
| Q2 | UC Write 静默失败时，接收端 buffer 是否保留旧数据？ | Ghost gradient 与 masked aggregation (RQ2) |
| Q3 | 不同 chunk 大小下 WQE 发射速率如何？瓶颈在哪里？ | Write 粒度优化 (RQ1) |

---

## 2. 核心发现

### 发现 1：CQE 驱动的完成追踪**可行**（Test 1 — PASS）

UC Write-with-Immediate 在 SoftRoCE 上产生的接收端 CQE 包含：
- **Opcode:** `IBV_WC_RECV_RDMA_WITH_IMM`（正确）
- **imm_data:** `0xDEADBEEF` 完整传递（正确）
- **Buffer:** 从 `0xAA` → `0x42`（zero-copy write 确认）
- **发送端 CQE:** `IBV_WC_SUCCESS`，opcode 为 `RDMA_WRITE`

**设计影响：** SemiRDMA Ratio Controller 的核心机制——通过计数接收端 CQE 来确定 `received_ratio`——得到验证。这是相比 UDP 方案（MLT、OptiReduce 依赖超时或应用层 ACK）的显著优势。

### 发现 2：Ghost gradient 是与假设**不同的变体**（Test 2 — PARTIAL）

**原始假设：** 没有 Receive WR 时，整个 Write-with-Immediate（数据和完成通知）都被静默丢弃，buffer 保留旧数据。

**实际观察：** RDMA Write 数据部分**成功写入**（buffer 从 `0x42` → `0xFF`），但**没有生成 CQE**。

这揭示了 Write-with-Immediate 在 SoftRoCE UC 上分解为两个独立操作：

| 操作 | 需要 Receive WR？ | 观察结果 |
|------|-------------------|---------|
| RDMA Write（数据传输） | 否 | **成功** — 数据写入远端 buffer |
| Immediate 完成通知（CQE） | 是 | **失败** — 没有生成 CQE |

**设计影响——这对论文实际上更有价值：**

Ghost gradient 问题不是"旧数据残留"，而是**"新数据到达但接收端不知道"**。在真实 lossy 网络场景中：

1. **丢包 → PSN 失序：** 当多包 RDMA Write 中的某个包丢失时，后续包被接收端 QP 静默丢弃。Buffer 最终包含**部分旧 + 部分新数据**——损坏的 ghost gradient。

2. **无 RQ WR 场景（本次测试）：** 数据完整到达，但接收端没有 CQE 信号。在实际 SemiRDMA 设计中，这种情况不会发生（我们总会预先 post Receive WR）。但它证明了 CQE 机制是**唯一可靠**的交付信号——不能仅靠检查 buffer 内容。

**下一步：** 需要通过 `tc netem` 丢包注入测试真正的部分写入 / PSN 失序行为。

### 发现 3：WQE 速率与 chunk 大小成反比；吞吐量在 ~500 MB/s 处饱和（Test 3）

| Chunk 大小 | 耗时 (ms) | WQE/s | 吞吐量 |
|------------|-----------|-------|--------|
| 4 KB | 10.5 | 95,201 | 371.9 MB/s |
| 16 KB | 31.3 | 31,945 | 499.1 MB/s |
| 64 KB | 123.8 | 8,075 | 504.7 MB/s |
| 256 KB | 503.6 | 1,986 | 496.4 MB/s |
| 1 MB | 2,314.7 | 432 | 432.0 MB/s |

**观察：**

1. **WQE/s 单调递减：** 95K (4KB) → 432 (1MB)。每个 WQE 在较大尺寸时承载更多数据，因此小尺寸时 per-WQE 开销占主导。

2. **吞吐量在 64KB 处达到峰值（~505 MB/s）：** 这是 per-WQE 开销被摊薄、但单 WQE 延迟尚未过大的最优点。

3. **1MB 时吞吐量下降（432 MB/s）：** 大 Write 在 SoftRoCE 上效率降低——可能由于内存拷贝开销和内核调度。

4. **4KB 吞吐量明显偏低（372 MB/s）：** per-WQE 开销约 10.5 μs，在 4KB 数据量下占比显著。

**对 RQ1（Write 粒度）的设计影响：**

- **Chunk 大小下限：** 16KB 是 SoftRoCE 上的实际最小值。低于 16KB 时 WQE 开销侵蚀吞吐量。
- **最优范围：** 16KB–64KB 提供最佳吞吐量，同时每次丢失影响可控。
- **预算计算示例：** ResNet-50 某层 2MB 梯度，64KB chunk = 32 WQE。按 8,075 WQE/s 计算，一层约 4ms——对于 100ms+ 的迭代时间是可行的。
- **重要警告：** 这些数字来自 SoftRoCE（软件模拟）。真实 ConnectX-5 硬件上 WQE 速率预计高 10–100 倍。相对趋势（吞吐量 vs. chunk 大小）也可能不同。这些结果指导代码架构，但**不能**用于最终参数选择。

---

## 3. 最强支撑的对比结论

| 结论 | 证据强度 | 注意事项 |
|------|---------|---------|
| UC Write-with-Immediate 生成接收端 CQE | **强**（确定性行为，单次测试即可） | 仅在 SoftRoCE 上验证；需 ConnectX-5 确认 |
| CQE 是唯一可靠的交付信号 | **强**（Test 2 证明 buffer 内容不可信） | 与丢包 ghost gradient 是不同机制 |
| 16KB–64KB 是 SoftRoCE 的吞吐量最优区间 | **中等**（单次运行，1000 次迭代，无方差数据） | SoftRoCE 特有；硬件数据会不同 |
| 丢包导致的 ghost gradient 存在 | **尚未测试** | 需要 tc netem 实验 |

---

## 4. 主要限制与阻塞项

### 限制

1. **SoftRoCE ≠ 硬件 RDMA。** 所有绝对性能数据（WQE/s、MB/s）都是 SoftRoCE 特有的。相对趋势是否适用于 ConnectX-5 需要验证。审稿人会追问这一点。

2. **单次运行基准测试（Test 3）。** 没有方差估计、没有置信区间。WQE 速率数据是每种 chunk 大小 1000 次迭代单次运行的点估计。作为验证测试可以接受；但不能直接用于论文。

3. **Ghost gradient Test 2 测试的机制（无 RQ WR）与真实场景（丢包 + PSN 失序）不同。** 发现仍然有价值（证明 CQE 是唯一可靠信号），但"ghost gradient 存在"的标题需要限定条件。

### 下一阶段阻塞项

| 阻塞项 | 优先级 | 行动 |
|--------|-------|------|
| 丢包导致的 ghost gradient 尚未验证 | 高 | 添加 tc netem 测试（0.1%–5% 丢包率） |
| WQE 基准测试无方差数据 | 中 | 在 CloudLab 上多次运行（5+ seeds） |
| SoftRoCE UC QP 行为可能与 ConnectX-5 不同 | 中 | 尽快在 CloudLab 硬件上验证 |

---

## 5. 认知变化

**Phase 1 之前：**
- 我们假设 Write-with-Immediate 是原子的：数据和完成通知要么同时成功，要么同时失败。
- 我们预期 ghost gradient = buffer 中的旧数据残留。
- 我们没有经验性的 WQE 速率数据。

**Phase 1 之后：**
- Write-with-Immediate 的数据传输和 CQE 生成是 UC QP 上的**独立操作**。数据可以在没有 CQE 的情况下到达。这强化了 CQE 追踪的论据：它是**唯一可靠**的完成信号。
- 丢包导致的 ghost gradient（部分写入 + PSN 失序）仍然是论文的主要关注点，但需要通过丢包注入进行显式测试。
- WQE 速率数据为 chunk 大小选择提供了初步估计，16KB–64KB 是 SoftRoCE 上的初始最优区间。
- SoftRoCE 上三个基本机制都能工作：UC QP 创建、Write-with-Immediate 和 CQE 生成。项目可以进入核心传输层实现阶段。
