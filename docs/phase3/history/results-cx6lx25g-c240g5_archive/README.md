# Phase 3 Stage B · CX-6 Lx 25 GbE 原始数据归档（已淘汰平台）

> **归档时间：** 2026-04-23
> **原始采集平台：** CloudLab Wisconsin `c240g5-110231` (node0) + `c240g5-110225` (node1)
> **硬件：** 2× Intel Xeon Silver 4114 + Tesla P100 PCIe 12 GB + **Mellanox CX-6 Lx 25 GbE** (fw 20.38.1002, RoCEv2 GID 1)
> **替代平台：** CloudLab Utah `amd203` + `amd196` (AMD EPYC 7302P + CX-5 fw 16.28.4512, 25 GbE)，结果落在 [`../results-cx5-amd203-amd196/`](../results-cx5-amd203-amd196/)。

---

## 为什么归档

两个同时发生的触发：

1. **Ratio-controller bug 修复** (commit `9386f2e`, 2026-04-23)：`python/semirdma/transport.py:257-271` 修复后，`await_gradient` 的 receive target 从硬编码 `cfg.ratio = 0.95` 改为动态 `max(cfg.ratio, 1 − loss_rate − 0.005)`。修复前，**A2 SemiRDMA 12 cell 的 effective loss 恒为 ~5%**，与名义 `cfg.loss_rate ∈ {0, 1, 3, 5}%` 无关。详见 [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md)。

2. **硬件平台切换**：从 c240g5 (Wisconsin) CX-6 Lx 25 GbE + P100 切换到 amd203/amd196 (Utah) CX-5 25 GbE + CPU-only。打磨阶段所有数据迁移到新平台，以保持统一比较基线。

---

## 子目录归属

| 子路径 | 数据类别 | 是否受 ratio-bug 影响 | 备注 |
|---|---|---|---|
| `rq6-prep-a2-real-nic/` | A2 SemiRDMA 12 cell (500 step × 4 loss × 3 seed) | ❌ **PRE-FIX，数据无效** | 12 cell 实测 effective loss 均 ~5%，与名义 loss 不等价 |
| `rq6-b5-rc-baselines/` | B.5 RC-Baseline + RC-Lossy 4 cell（12 中的前 4） | ✅ 不受影响（C++ + gloo 路径） | 数据本身有效，但 §1.3 头对头对比中的 A2 侧失真 |
| `rq6-prep-stage-a-real-nic/` | A1 bit-for-bit 6 cell (gloo vs SemiRDMA ratio=1.0) | ✅ 不受影响（显式 `ratio=1.0`） | 参考基线，已由 CX-5 新数据替代 |
| `microbench_c240g5/` | M2/M3/M5 verbs-local 微基准 | ✅ 不受影响（不走 DDP hook） | CPU/NIC 本地开销参考值 |
| `rq1_chunk_sweep_*.csv` | Phase 2 RQ1 真机重扫（chunk size 扫描） | ✅ 不受影响（C++ `test_chunk_sweep`） | CX-6 Lx 参考基线 |
| `rq2_rms_error_*.csv` | Phase 2 RQ2 真机重扫（ghost mask RMS） | ✅ 不受影响（C++ `test_rms_error`） | CX-6 Lx 参考基线 |
| `rq4_ratio_timeout_*.csv` | Phase 2 RQ4 真机重扫（ratio × timeout sweep） | ✅ 不受影响（C++ `test_ratio_sweep`） | CX-6 Lx 参考基线 |

---

## 访问/对比规则

- 本目录所有数据**只用于历史审计与 bug 复现**，**不用于论文主结论**。
- 需要对比 "相同 workload 跨硬件平台" 时，可以引用本目录数据作为 CX-6 Lx 的参考基线（M1-M5 / RQ1/2/4 / A1 不受 bug 影响）。
- **严禁**将本目录的 A2 12-cell 数据作为 "SemiRDMA 在 1/3% 丢包下表现" 的证据 — 那些 cell 测的都是 ~5% effective loss。

---

## 相关文档

- [`../rq6-semirdma-effective-loss-analysis.md`](../rq6-semirdma-effective-loss-analysis.md) — bug 根因分析
- [`../stage-b-hardware-notes.md`](../stage-b-hardware-notes.md) §8 — c240g5 硬件细节 / §9 — amd203/amd196 硬件细节
- [`../../results-cx5-amd203-amd196/README.md`](../../results-cx5-amd203-amd196/README.md) — 新平台结果入口
