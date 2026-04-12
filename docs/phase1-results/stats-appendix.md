# Phase 1 统计附录

## Test 1：UC Write-with-Immediate CQE 验证

### 测试性质
确定性正确性测试（非统计性测试）。单次成功运行即可，原因：
- CQE 生成是确定性的协议行为，不是随机结果。
- 测试验证 4 个二元条件（CQE 是否收到、opcode 是否正确、imm_data 是否正确、buffer 是否写入）。
- 所有 4 个条件均通过。

### 原始观察

| 指标 | 期望值 | 观察值 | 状态 |
|------|--------|--------|------|
| CQE 收到 | 是 | 是 | PASS |
| CQE opcode | `RECV_RDMA_WITH_IMM` | `RECV_RDMA_WITH_IMM` | PASS |
| CQE imm_data | `0xDEADBEEF` | `0xDEADBEEF` | PASS |
| Buffer[0] | `0x42` | `0x42` | PASS |
| Buffer 内容 (64B) | 全部 `0x42` | 全部 `0x42` | PASS |
| 发送端 CQE 状态 | `SUCCESS` | `SUCCESS` | PASS |
| 发送端 CQE opcode | `RDMA_WRITE` | `RDMA_WRITE` | PASS |

### 推断性声明
不适用——这是正确性断言，不是测量。

---

## Test 2：Ghost Gradient 验证

### 测试性质
确定性行为测试，结果与预期不同。

### 原始观察

**Round 1（正常 Write-with-Immediate，已 post Receive WR）：**

| 指标 | 期望值 | 观察值 | 状态 |
|------|--------|--------|------|
| CQE 收到 | 是 | 是 | OK |
| CQE opcode | `RECV_RDMA_WITH_IMM` | `RECV_RDMA_WITH_IMM` | OK |
| imm_data | `0x11111111` | `0x11111111` | OK |
| Buffer[0] | `0x42` | `0x42` | OK |

**Round 2（Write-with-Immediate，未 post Receive WR）：**

| 指标 | 假设 A（完全丢弃） | 假设 B（仅数据到达） | 观察值 |
|------|-------------------|---------------------|--------|
| CQE 收到 | 否 | 否 | **否** |
| Buffer[0] | `0x42`（旧值） | `0xFF`（新值） | **`0xFF`（新值）** |
| Buffer 内容 (32B) | 全部 `0x42` | 全部 `0xFF` | **全部 `0xFF`** |

**结论：** 假设 B 成立。在 SoftRoCE UC QP 上，没有 Receive WR 的 Write-with-Immediate：
- RDMA Write 数据传输：**成功**
- CQE 生成：**不发生**

### 限制
本测试触发的失败模式与真实丢包场景不同：
- 已测试：无 Receive WR → 无 CQE，但数据到达
- 真实场景：丢包 → PSN 失序 → 数据**不**到达（部分或全部）
- 丢包场景需要 tc netem 测试（尚未完成）

---

## Test 3：WQE 速率微基准测试

### 实验参数

| 参数 | 值 |
|------|-----|
| 传输方式 | UC QP，RDMA Write（无 Immediate） |
| 设备 | SoftRoCE (rxe0) |
| 拓扑 | 单机 loopback |
| Buffer 大小 | 16 MB（发送端和接收端） |
| 每种 chunk 大小迭代次数 | 1000 |
| 预热 | 10 次迭代 |
| 信号间隔 | 每 64 个 WQE |
| 运行次数 | 1（无重复测量） |

### 原始结果

| Chunk 大小 | 耗时 (ms) | WQE/s | 吞吐量 (MB/s) |
|------------|-----------|-------|---------------|
| 4 KB | 10.5 | 95,201 | 371.9 |
| 16 KB | 31.3 | 31,945 | 499.1 |
| 64 KB | 123.8 | 8,075 | 504.7 |
| 256 KB | 503.6 | 1,986 | 496.4 |
| 1 MB | 2,314.7 | 432 | 432.0 |

### 衍生指标

| Chunk 大小 | 单 WQE 延迟 (μs) | 吞吐效率 (% 峰值) | 单次丢失影响 (25M 参数梯度) |
|------------|------------------|-------------------|---------------------------|
| 4 KB | 10.5 | 73.7% | 0.004% |
| 16 KB | 31.3 | 98.9% | 0.016% |
| 64 KB | 123.8 | 100.0%（峰值） | 0.064% |
| 256 KB | 503.6 | 98.4% | 0.256% |
| 1 MB | 2,314.7 | 85.6% | 1.0% |

*单次丢失影响 = chunk_size / (25M params × 4 bytes/param) = chunk_size / 100MB*

### 缩放分析

WQE/s 与 chunk 大小近似满足反比关系：

```
WQE/s ≈ K / chunk_size_KB
```

拟合：K ≈ 95,201 × 4 = 380,804。验证：
- 16 KB：预测 380,804/16 = 23,800 → 观察 31,945（偏高）
- 64 KB：预测 380,804/64 = 5,950 → 观察 8,075（偏高）
- 256 KB：预测 380,804/256 = 1,488 → 观察 1,986（偏高）
- 1 MB：预测 380,804/1024 = 372 → 观察 432（偏高）

模型在较大尺寸时低估，说明 SoftRoCE 有一个固定的 per-WQE 开销（~10μs）加上一个亚线性的 per-byte 开销。

### 统计限制

- **无方差估计。** 每种 chunk 大小仅单次运行，无法计算置信区间。
- **无 seed 变化。** 确定性基准测试，但系统负载可能引入方差。
- **SoftRoCE 特有。** 软件模拟路径（内核模块 + 内存拷贝）与硬件 DMA 本质不同。这些数据指导代码结构，不用于论文声明。
- **未测试 Write-with-Immediate 变体。** 仅测试了纯 RDMA Write。Write-with-Immediate 在接收端有额外的 Receive WR post 开销。

### 论文质量数据的阻塞项

1. 运行 5+ 次以获取 `mean ± std`
2. 添加 Write-with-Immediate 变体进行直接对比
3. 在 ConnectX-5 硬件上重复测试（CloudLab）
4. 在并发流量下测试以模拟云环境竞争
