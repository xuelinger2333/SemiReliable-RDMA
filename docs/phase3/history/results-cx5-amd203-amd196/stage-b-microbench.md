# Phase 3 · Stage B · CX-5 单节点 microbench (M1-M5)

> **时间：** 2026-04-23 07:21-07:22 (CX-5 amd203)
> **脚本：** [`experiments/stage_b/microbench_cx6_local.py`](../../../experiments/stage_b/microbench_cx6_local.py) (名字延用 cx6，代码平台无关)
> **原始 CSV / JSON：** [`./stage-b-microbench/microbench_2026-04-23_07-21-56/`](./stage-b-microbench/microbench_2026-04-23_07-21-56/)

---

## 0. M1-M5 recap (median of n samples)

| bench | cell | CX-5 amd203 median | CX-6 d7525 参考 |
|---|---|---:|---:|
| **M1 poll_cq (empty)** | max_n=1 | **431 ns** | 441 ns |
| | max_n=4 | 421 ns | — |
| | max_n=16 | 440 ns | — |
| | max_n=64 | 531 ns | — |
| **M2 post_recv_batch** | n=1 | 451 ns | — |
| | n=10 | 51 ns/WR | — |
| | n=100 | **13.5 ns/WR** (amortized) | 10.5 ns/WR |
| | n=1000 | 12.5 ns/WR | — |
| **M3 construct** | buf=1 MiB | 3.54 ms | — |
| | buf=4 MiB | 4.00 ms | — |
| | buf=16 MiB | 12.63 ms | — |
| | buf=64 MiB | 40.19 ms | — |
| | buf=256 MiB | 145.81 ms | — |
| **M4 pybind trampoline** | outstanding_recv | **231 ns** | — |
| **M5 ghost_mask** (1 MiB, 0% loss) | | 441 ns | — |
| (1 MiB, 1% loss) | | 481 ns | — |
| (16 MiB, 1% loss) | | 7.73 µs | — |
| (16 MiB, 10% loss) | | 67 µs | — |
| (256 MiB, 1% loss) | | 314 µs | — |
| (256 MiB, 10% loss) | | 3.09 ms | — |

### 0.1 关键观察

1. **M1 poll_cq_empty = 431 ns** — 几乎精确等于 CX-6 d7525 的 441 ns。这是 verbs doorbell + PCIe roundtrip 的 hardware-local 开销，不依赖 NIC 代数（对下至 CX-4，上至 CX-7 都在 400-500ns 区间）。
2. **M2 post_recv_batch amortized 13.5 ns/WR @ n=100** — 跟 d7525 的 10.5 ns/WR 近似（CX-5 略慢可能是 fw 16 vs fw 20 差异）。
3. **M3 construct 线性缩放** — `register_mr` 主要是 page-table setup，scale with buf_size/page_size。
   - 4 → 16 MiB：3.2× 缩放 → MR register cost ≈ 540 µs/MiB on EPYC
   - 64 → 256 MiB：3.6× 缩放 → 同斜率确认纯 MR register 主导
4. **M4 trampoline 231 ns** — pybind11 → C++ 跨语言调用的 fixed cost。跟 c240g5 / d7525 同量级。
5. **M5 ghost_mask** 随 loss_pct 单调缩放 → 这是 cache-bound memset 路径，CPU 主导。在 EPYC 7302P (16C, 256 MB L3) 上 256 MiB/3.09 ms = 81 GiB/s，接近 L3 带宽上限。

### 0.2 跟 c240g5 (Xeon Silver 4114) 对比

d7525 Xeon 的 ghost_mask 数字（见归档 `microbench_c240g5`）给的是 2.1 GiB/s / 3.5 GiB/s 量级。**CX-5 + EPYC 7302P 的 ghost_mask 比 CX-6 + Xeon Silver 4114 快 ~30×**（81 GiB/s vs 2-3 GiB/s）— 这是 EPYC 更好的 L3 + DDR4 带宽在同尺寸测试下的表现。

对论文影响：
- **ghost_mask cost 不再是 Stage B 瓶颈**：256 MiB bucket × 5% loss = 3 ms CPU 代价，远小于一步 SGD 的 ~800 ms fwd/bwd 开销
- 在更小的 bucket (47 MiB 单 bucket ResNet-18) 下，ghost_mask cost < 1 ms
- 这个观察 reinforces 论文 §实现开销小节的论点

---

## 1. 硬件确认

从 `environment.json`:

- CPU: AMD EPYC 7302P 16-Core Processor
- RDMA dev: `mlx5_2` (CX-5, fw 16.28.4512)
- Python: 3.10.12
- torch: 2.11.0+cpu

---

## 2. 相关

- [`./stage-b-phase2-resweep.md`](./stage-b-phase2-resweep.md) — 跨节点 RQ1 数据
- [`../stage-b-microbench-cx6.md`](../stage-b-microbench-cx6.md) — d7525 CX-6 prior-platform 版本（banner PRIOR-PLATFORM REFERENCE）
- [`../stage-b-hardware-notes.md`](../stage-b-hardware-notes.md) §9 — amd203/amd196 硬件信息
