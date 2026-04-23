# Phase 3 设计文档 · DDP Integration + Real-NIC + Layer Analyzer

**时间窗：** 2026-05-11 → 2026-06-21（Week 5–10，共 6 周）
**前置依赖：** Phase 2 Core Transport 已完成（见 [design-core-transport.md §8](../phase2/design-core-transport.md#8-实验结果phase-2-收尾汇总)）
**产出物：** `src/bindings/` + `python/semirdma/` + `src/transport/layer_analyzer.{h,cpp}` + 三阶段实验数据
**本文档的地位：** Phase 3 开工前的设计锁定。与 Phase 2 设计文档一样，**不含实现代码**，只有接口草案、阶段划分、实验方法。三个 Stage（A/B/C）各自对应独立的实验结果文档（Stage A 结束时写 `rq5-results-*.md`，以此类推），本文档只做总设计。

---

## 1. 背景与动机

### 1.1 Phase 2 给 Phase 3 的四条硬约束

Phase 2 在 SoftRoCE 上完成了 RQ1/RQ2/RQ4 三个实验，把下列事实钉死：

| # | 结论 | 证据 | Phase 3 含义 |
|---|------|------|-------------|
| ① | `chunk=16KB` 是 SoftRoCE throughput 饱和平台，更小 chunk 有 20%+ WQE 开销 | [rq1-log §4.3](../phase2/rq1-log-implementation.md#43-分析goodput-随-chunk-size-的变化) | Stage A 默认 `chunk_bytes=16384`，Stage C 才引入 per-layer 变化 |
| ② | `GhostMask::apply` 在 stale ⊥ truth 假设下把 RMS 误差降 29%，实现数值正确 | [rq2-results §5.3](../phase2/rq2-results-ghost-masking.md#53-与理论对比) | Stage A 的 DDP hook 默认 `mask=on`；Stage B 真训练里验证"训练层收益" |
| ③ | `(ratio=0.95, timeout=20ms)` 是 SoftRoCE 下 6.3× 降 tail 的 operating point | [rq4-results §6.1](../phase2/rq4-results-ratio-timeout.md#61-operating-point-确认ratio095--timeout20ms) | Stage A 直接使用；Stage B 在 ConnectX-5 重标定（真机 CQE 更快） |
| ④ | Per-chunk Bernoulli 软件丢包 ≠ per-packet netem 丢包 | [design-core-transport §8.3](../phase2/design-core-transport.md#83-phase-1--phase-2-的理论-ghost-降幅) | Stage B 必须用**真 netem** 或**真网络丢包**，不再用软件 Bernoulli |

### 1.2 目标产出物清单

Phase 3 结束时（2026-06-21）必须存在：

**代码（`src/` 新增）：**
- `src/bindings/py_semirdma.cpp`（pybind11 绑定）
- `src/transport/layer_analyzer.h` + `.cpp`（Stage C 引入）
- `src/transport/ratio_controller.h` 注释更新（carry-over from Phase 2）
- `src/transport/uc_qp_engine.h` 增补 `post_recv_batch(n)` / `outstanding_recv()`（carry-over from Phase 2）

**代码（`python/` 新建目录）：**
- `python/semirdma/__init__.py`
- `python/semirdma/transport.py`（Python 侧 transport 封装）
- `python/semirdma/hooks.py`（PyTorch DDP `allreduce_hook` 实现）
- `python/semirdma/config.py`（dataclass 配置）
- `python/semirdma/layer_analyzer.py`（Stage C 引入）

**构建：**
- `setup.py` + `pyproject.toml`（pybind11 编译 + 可 `pip install -e .`）

**实验 / 脚本：**
- `scripts/cloudlab/`（Stage B 的 ConnectX-5 部署脚本）
- `experiments/configs/`（Hydra 配置，Phase 3 引入）
- 三份 results 文档：`rq5-results-ddp-baseline.md`（Stage A 收尾）、`rq6-results-real-nic-comparison.md`（Stage B 收尾）、`rq3-results-layer-analyzer.md`（Stage C 收尾）

**测试：**
- `tests/phase3/test_binding_sanity.py`（pybind11 单元）
- `tests/phase3/test_ddp_hook.py`（2-worker DDP 端到端）
- `tests/phase3/test_layer_analyzer.py`（Stage C）

### 1.3 非目标

Phase 3 **不**做的事情：

- 新的 UC QP 语义扩展（保持 Phase 2 的 transport 层不动，除了 §1.2 列的两个 API 增补）
- 除 ResNet-50 / GPT-2 / BERT 之外的 workload
- 多机（> 2 节点）训练 — Stage B 只跑 2 节点 point-to-point；多机 ring-AllReduce 留给 Phase 4（若时间允许）
- 生产级错误恢复 / QP 断线重连（研究原型 scope）
- CUDA RDMA / GPUDirect（PyTorch CPU tensor → host memory → RDMA，Stage A 确认可行即止）

---

## 2. 三阶段划分与研究问题

Phase 3 内部分为三个线性依赖的 Stage，每个 Stage 各有一个主 RQ 和一份独立的结果文档。不要同时开跑两个 Stage——Stage 间的接口契约在前一个 Stage 收尾时锁定。

### 2.1 Stage A · DDP 集成正确性（Week 5–6，May 11 → May 24）

**目标：** 把 Phase 2 的 C++ transport 通过 pybind11 暴露给 PyTorch，实现 DDP `allreduce_hook`，在 SoftRoCE 上跑通 2-worker 训练并验证**数值正确性**。

#### RQ5：Uniform-chunk SemiRDMA DDP 在 loss=0% 下是否与 Gloo 产生相同训练轨迹？

**度量指标：**

- `loss_curve_diff` — SemiRDMA 与 Gloo 的 per-step loss 绝对值差，累积到 N 步
- `final_accuracy_gap` — 训练结束 validation accuracy 差（SoftRoCE 上 N=2 worker，训练短，不看最终 accuracy，只看前 500 步曲线相对差）
- `grad_norm_per_step` — 每步聚合后梯度 L2 范数（两路径对比）
- `iter_time` — 单步 forward + backward + comm + optim 时间

**实验设计：**

- 2-worker SoftRoCE loopback（与 Phase 2 RQ1 同机器）
- Workload：**ResNet-18 / CIFAR-10**（轻量级，SoftRoCE 吞吐撑得住；ResNet-50 留给 Stage B）
- 扫描条件：
  - **A1：** SemiRDMA + loss=0% vs Gloo + loss=0%（**必须数值近似相等**）
  - **A2：** SemiRDMA + loss=1%(per-chunk) vs Gloo + loss=0%（验证 1% 丢梯度下 loss 曲线保持单调下降）
- 每条 500 step，batch=128，lr=0.1，seed=42

**成功判定：**

- A1：`loss_curve_diff / loss_value < 1%` across 500 steps（数值等价的操作定义）
- A2：SemiRDMA 的 loss 在 500 步内下降 ≥ Gloo 的 90%（1% 梯度丢失不应让收敛大幅退化；这是对 "semi-reliable 假设" 的第一次端到端检验）

**不做的事：**

- 不跑大模型（ResNet-50/GPT-2 留给 Stage B，有真带宽才有意义）
- 不比 OptiReduce / UD-Naive（等 Stage B 有完整 5-baseline 框架再比）
- 不做 Layer Analyzer 预研（Stage C 专门做）

### 2.2 Stage B · CloudLab ConnectX-5 真机 + 五路 Baseline 对比（Week 7–8，May 25 → Jun 7）

**目标：** 把 Stage A 的 DDP pipeline 搬到 CloudLab ConnectX-5，先重标定 Phase 2 参数，再跑 **CLAUDE.md 对标的 5 条 baseline**，产出论文主实验表。

#### RQ6：ConnectX-5 真机 + per-packet 丢包下，SemiRDMA 的 Time-to-Accuracy 是否优于 RC-Lossy 和 OptiReduce？

**度量指标（完整版，用于论文）：**

- **Time-to-Accuracy (TTA)** — 达到目标 validation accuracy 的 wall-clock 时间（ResNet-50 → 75% top-1 / BERT → 0.5 F1 / GPT-2 → 语言模型 perplexity 阈值）
- **Iteration time** — mean / P50 / P99 per-step 耗时
- **Final accuracy** — 固定 step 数后的 test accuracy
- **Gradient loss rate** — 训练期间 `measured ghost_ratio` 的分布
- **Communication fraction** — `comm_time / iter_time`
- **Tail latency P99** — 单次 AllReduce 的 P99 耗时
- **WQE throughput / CQE polling overhead** — transport 层计数器

**实验设计：**

| 条件 | Transport | Reliability | 丢包注入 |
|------|-----------|-------------|----------|
| **RC-Baseline** | RC QP | 硬件重传 | 无 netem |
| **RC-Lossy** | RC QP + netem 1% / 3% / 5% | 硬件重传（但 tail 爆） | netem per-packet |
| **OptiReduce** | Gloo TCP + UBT + Hadamard | 软件弹性 | netem per-packet |
| **UD-Naive** | UD QP | 无重传 | netem per-packet |
| **SemiRDMA** | **UC QP + Phase 2 stack** | **软件半可靠** | netem per-packet |

**先决任务（Stage B 开工第 1 周）：**

1. **Phase 2 参数真机重标定** — 在 ConnectX-5 上重跑 RQ1/RQ4 的参数扫描（每个 cell 减到 100 轮，够画图），产出`rq6-a-results-real-nic-recalibration.md`
2. **确认 per-packet 丢包下 ghost 模型** — [design-core-transport §8.3](../phase2/design-core-transport.md#83-phase-1--phase-2-的理论-ghost-降幅) 的 6× 理论降幅必须在真机 netem 下**实测**

**主实验：**

- Workload：**ResNet-50 / ImageNet 子集（2 节点）**、**GPT-2 small / OpenWebText 子集（2 节点）**、**BERT-base / WikiText（2 节点）**
- 每 workload × 5 baseline × 3 loss rate（0% / 1% / 5%）= 45 cell
- 每 cell 训练到目标 accuracy 或 max_steps（取先到），至少重复 3 seed

**成功判定：**

- **必须成立**：SemiRDMA 的 `P99 iteration time < RC-Lossy × 50%`（否则主 selling point 不成立）
- **强目标**：SemiRDMA 的 `TTA < OptiReduce × 1.0`（真机零拷贝 RDMA vs TCP 应该有明显优势）
- **可选**：SemiRDMA 的 `final_accuracy ≥ Gloo-Baseline × 99%`（证明 1-5% loss 不吃掉 accuracy）

**失败分析预案：**

- 若 SemiRDMA 在 BERT/GPT 类敏感模型上 final_accuracy 掉很多 → 回到 Stage C 看 Layer Analyzer 能否补救
- 若 RC-Lossy 的 P99 没爆（ConnectX-5 硬件快到无所谓）→ loss rate 加码到 10% 再试

### 2.3 Stage C · Layer Analyzer + RQ3（Week 9–10，Jun 8 → Jun 21）

**目标：** 实现 RQ3 承诺的跨层自适应 chunk，在 Stage B 产出的 baseline 之上作为 **ablation** 进入论文。

#### RQ3：每层根据梯度敏感度 + 实时 loss 反馈动态调整 chunk size，是否相对 uniform-16KB 带来可测量的 TTA 或 P99 改善？

**度量指标：**

- `TTA_improvement` — Uniform-chunk baseline 对比 Adaptive-chunk 的 TTA 差（相对值 %）
- `per_layer_chunk_dist` — 训练结束时各层最终 chunk size 分布
- `layer_importance_correlation` — LayerAnalyzer 打分与 final gradient magnitude 的相关系数
- `chunk_adaptation_overhead` — Analyzer 本身的 CPU 开销占总 iter 时间的百分比

**实验设计：**

- 基础设施：完全继承 Stage B 的 CloudLab ConnectX-5 + 5-baseline 对比框架
- 只在 SemiRDMA 这一条路径上打开 / 关闭 Layer Analyzer，其他 4 条 baseline 不变
- 扫描条件：
  - **C1：** SemiRDMA + uniform 16KB（= Stage B 的 SemiRDMA 结果，零工作量，直接复用）
  - **C2：** SemiRDMA + adaptive chunk（LayerAnalyzer 基于梯度 L2 范数打分，chunk size ∈ {4KB, 16KB, 64KB}）
  - **C3：** SemiRDMA + adaptive chunk + loss-adaptive ratio（LayerAnalyzer 额外驱动每层 ratio 在 `[1-p-ε, 1-p+ε]` 区间）

**成功判定：**

- **必要**：Adaptive 的 TTA 不劣于 uniform 超过 5%（保守地"至少不伤害"）
- **充分**：Adaptive 在至少一个 workload 上给出 ≥ 10% 的 TTA 改善或 ≥ 20% 的 P99 改善
- 否则 RQ3 结论写作 "**提出但经验上非决定性**"，论文作为次要贡献（不拉高整体 selling point）

---

## 3. 代码结构

### 3.1 模块关系图

```
  PyTorch DDP model
        │
        ▼
  python/semirdma/hooks.py   (register_allreduce_hook)
        │
        ▼
  python/semirdma/transport.py  (Python 封装，调 pybind11)
        │
        ▼
  src/bindings/py_semirdma.cpp   ←→   src/transport/ (Phase 2 模块，未改)
                                      ├─ uc_qp_engine
                                      ├─ chunk_manager
                                      ├─ ratio_controller
                                      ├─ ghost_mask
                                      └─ layer_analyzer   (Stage C 新增)
                                             │
                                             └─ python/semirdma/layer_analyzer.py
```

**依赖方向：**
- Python 调 pybind11 → pybind11 调 C++ transport。严格单向，Python 侧不重新实现任何 transport 逻辑
- `layer_analyzer` 的 **打分策略** 放 Python（好改、好做 ablation），**执行接口** 保留 C++ 侧（可以从 hook 里 O(1) 拿到）

### 3.2 目录布局

```
SemiRDMA/
├── src/
│   ├── transport/
│   │   ├── uc_qp_engine.{h,cpp}    ← Phase 2 (增补 post_recv_batch / outstanding_recv)
│   │   ├── chunk_manager.{h,cpp}   ← Phase 2 (未改)
│   │   ├── ratio_controller.{h,cpp}← Phase 2 (header 注释补 "to-threshold" 语义)
│   │   ├── ghost_mask.{h,cpp}      ← Phase 2 (未改)
│   │   └── layer_analyzer.{h,cpp}  ← Stage C 新增
│   └── bindings/
│       └── py_semirdma.cpp         ← Stage A 新增
├── python/
│   └── semirdma/
│       ├── __init__.py
│       ├── transport.py            ← Stage A
│       ├── hooks.py                ← Stage A
│       ├── config.py               ← Stage A
│       └── layer_analyzer.py       ← Stage C
├── tests/
│   └── phase3/
│       ├── test_binding_sanity.py  ← Stage A (pytest)
│       ├── test_ddp_hook.py        ← Stage A
│       └── test_layer_analyzer.py  ← Stage C
├── experiments/
│   └── configs/                    ← Hydra configs (Phase 3 引入)
│       ├── stage_a_baseline.yaml
│       ├── stage_b_comparison.yaml
│       └── stage_c_adaptive.yaml
└── scripts/
    └── cloudlab/                   ← Stage B 部署脚本
```

### 3.3 `py_semirdma.cpp` 接口草案（Stage A）

pybind11 模块只暴露**最小**集合——能让 Python 构造 transport、post Write、等 ratio、apply mask 即可。所有复杂逻辑留在 C++ 侧。

```cpp
// src/bindings/py_semirdma.cpp
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "transport/ghost_mask.h"

namespace py = pybind11;
using namespace semirdma;

PYBIND11_MODULE(_semirdma_ext, m) {
    py::class_<UCQPEngine>(m, "UCQPEngine")
        .def(py::init<const std::string&, size_t, int, int>(),
             py::arg("dev_name"), py::arg("buffer_bytes"),
             py::arg("sq_depth"), py::arg("rq_depth"))
        .def("bring_up", &UCQPEngine::bring_up)
        .def("post_write", &UCQPEngine::post_write)
        .def("post_recv", &UCQPEngine::post_recv)
        .def("poll_cq", &UCQPEngine::poll_cq, py::arg("max_n"), py::arg("timeout_ms"))
        // 把 local buffer 暴露成 numpy view（零拷贝）
        .def("local_buf_view",
             [](UCQPEngine& e) {
                 return py::memoryview::from_memory(e.local_buf(), e.buf_bytes());
             });

    py::class_<ChunkSet>(m, "ChunkSet")
        .def(py::init<size_t, size_t, size_t>(),
             py::arg("base_offset"), py::arg("total_bytes"), py::arg("chunk_bytes"))
        .def("size", &ChunkSet::size)
        .def("num_completed", &ChunkSet::num_completed)
        .def("completion_ratio", &ChunkSet::completion_ratio)
        .def("mark_completed", &ChunkSet::mark_completed);

    py::class_<RatioController>(m, "RatioController")
        .def(py::init<UCQPEngine&>())
        .def("wait_for_ratio", &RatioController::wait_for_ratio,
             py::arg("cs"), py::arg("ratio"), py::arg("timeout_ms"),
             py::arg("stats") = nullptr);

    m.def("apply_ghost_mask", [](py::array_t<uint8_t> buf, const ChunkSet& cs) {
        GhostMask::apply(buf.mutable_data(), cs);
    });
}
```

**关键决策：**

1. **零拷贝 buffer view**。`local_buf_view` 用 `py::memoryview`，Python 侧可以 `np.frombuffer(view, dtype=np.float32)` 直接读写，无拷贝开销
2. **TCP bootstrap 仍留在测试代码侧**。pybind11 不暴露 `tcp_exchange_*`——Python 侧自己写 `socket` 做 QP info 交换（更符合 Python 的生态）
3. **不暴露 `UCQPEngine::poll_cq`** 的 `Completion` 结构体。包成 dict 返回：`[{wr_id, opcode_name, status_name, imm_data}, ...]`

### 3.4 `hooks.py` 接口草案（Stage A）

DDP 的 `register_comm_hook(process_group, hook)` 期望 hook 签名 `(state, bucket) -> Future[torch.Tensor]`。bucket 是一个 flattened gradient tensor，包含**多层** gradient。

```python
# python/semirdma/hooks.py
import torch
import torch.distributed as dist
from torch.distributed import GradBucket
from torch.futures import Future
from .transport import SemiRDMATransport
from .config import TransportConfig

def semirdma_allreduce_hook(
    state: "SemiRDMAHookState",
    bucket: GradBucket,
) -> Future[torch.Tensor]:
    """
    Replace DDP's default AllReduce with SemiRDMA.

    Phase 3 scope: 2-worker point-to-point, not ring-AllReduce.
    Future (Phase 4): ring-AllReduce.
    """
    tensor = bucket.buffer()             # flat 1-D float32 tensor
    transport: SemiRDMATransport = state.transport

    # Serialize the gradient tensor into transport's registered MR
    transport.post_gradient(tensor)

    # Block until ratio reached (or timeout)
    fut = transport.await_completion()   # returns Future[torch.Tensor]
    return fut


class SemiRDMAHookState:
    def __init__(self, cfg: TransportConfig, rank: int, world_size: int):
        assert world_size == 2, "Phase 3 scope: 2-worker only"
        self.transport = SemiRDMATransport(cfg, rank=rank)
        self.cfg = cfg
```

**关键决策：**

1. **GradBucket 粒度**：一个 bucket 就是一次 `post_gradient` 调用；bucket 内部多层共享一次 SemiRDMA 传输。这是 Stage A 的简化版，Stage C 的 LayerAnalyzer 才会拆到 per-layer
2. **仅 2-worker**：Phase 3 显式拒绝 ring-AllReduce（单次 all-reduce 就是 A↔B 的 bidirectional exchange）。多 worker 留给 Phase 4
3. **不做 bucket rebuild**：保留 DDP 的默认 bucket 分配策略，不侵入 DDP 内部

### 3.5 `layer_analyzer.{h,cpp}` 接口草案（Stage C）

```cpp
// src/transport/layer_analyzer.h
#pragma once
#include <cstdint>
#include <vector>
#include <unordered_map>

namespace semirdma {

struct LayerImportance {
    uint32_t layer_id;
    float    grad_norm;      // L2 norm of most recent gradient
    float    loss_rate;      // observed ghost_ratio for this layer (EWMA)
    float    score;          // combined importance: grad_norm / (1 - loss_rate)
};

class LayerAnalyzer {
public:
    LayerAnalyzer(size_t num_layers, float ewma_alpha = 0.2f);

    // Called after each step: update per-layer scores
    void update(uint32_t layer_id, float grad_norm, float observed_ghost_ratio);

    // Called before each step: pick chunk size for this layer
    size_t chunk_size_for(uint32_t layer_id) const;

    // Called before each step: pick ratio threshold
    double ratio_for(uint32_t layer_id) const;

    // Introspection
    const std::vector<LayerImportance>& importance_table() const { return table_; }

private:
    float  ewma_alpha_;
    std::vector<LayerImportance> table_;
    // chunk_size lookup: 低重要度 → 大 chunk (节省 WQE); 高重要度 → 小 chunk (细粒度)
    static constexpr size_t SIZE_BUCKETS[3] = { 4096, 16384, 65536 };
};

} // namespace semirdma
```

**关键决策：**

1. **只三档 chunk size**：{4KB, 16KB, 64KB}。扫更多档没有信息增益，徒增 ablation 维度
2. **EWMA 平滑 loss_rate**：单步观测的 ghost_ratio 噪声大（每层 chunk 少时方差高），α=0.2 给 ~5 步记忆
3. **决策逻辑 Python 侧可替换**：C++ 侧保留默认策略，但 `hooks.py` 可以传一个 Python callback 覆盖打分函数（做 Fisher / Hutchinson / 最后一层更重要等 ablation）

---

## 4. 构建与依赖

### 4.1 Python 包构建

```python
# pyproject.toml
[build-system]
requires = ["setuptools>=61", "pybind11>=2.11", "cmake>=3.16"]
build-backend = "setuptools.build_meta"

[project]
name = "semirdma"
version = "0.3.0"
requires-python = ">=3.9"
dependencies = [
    "torch>=2.0",
    "numpy>=1.24",
    "hydra-core>=1.3",
    "omegaconf>=2.3",
]

[project.optional-dependencies]
test = ["pytest>=7", "pytest-asyncio"]
experiments = ["datasets", "transformers", "matplotlib"]
```

`setup.py` 负责把 `src/bindings/py_semirdma.cpp` 用 pybind11 编译成 `_semirdma_ext.so`，链接 `libsemirdma_transport.a`（Phase 2 构建的静态库）+ `libibverbs`。

### 4.2 新依赖（超越 Phase 2）

| 依赖 | 用途 | 可选性 |
|------|------|--------|
| **pybind11** | C++ ↔ Python 桥梁 | 必需 |
| **PyTorch ≥ 2.0** | DDP hook | 必需 |
| **Hydra** + **OmegaConf** | 实验配置 | 必需（符合 coding-style rule） |
| **NumPy** | tensor ↔ MR buffer 视图 | 必需 |
| **datasets** + **transformers** | BERT / GPT-2 workload | Stage B 才需要 |
| **matplotlib / seaborn** | 绘图 | results 文档阶段才需要 |

### 4.3 CloudLab 环境（Stage B 新增）

参考 [CLAUDE.md 的 Risk 表](../../CLAUDE.md#known-risks)：ConnectX-5 节点需提前预约，SoftRoCE 作为 fallback。

- `scripts/cloudlab/provision.sh` — 新 image 初始化（安装 `ibverbs` / PyTorch / semirdma）
- `scripts/cloudlab/netem_inject.sh` — 双向 netem `loss 1%/3%/5%` 参数化脚本
- `scripts/cloudlab/run_baseline.sh` — 跑指定 baseline 的统一入口

### 4.4 CI / 本地验证

- Stage A：`pip install -e .` 必须在 aliyun SoftRoCE 上成功；`pytest tests/phase3/` 必须全绿
- Stage B：CloudLab 节点上能跑 `python -m semirdma.cli train --config=stage_b_comparison.yaml`
- Stage C：保持 Stage A 的测试不退化；新测试 `test_layer_analyzer.py` 独立绿

---

## 5. 验证计划

### 5.1 Unit / 集成测试

**Stage A：**
- `test_binding_sanity.py`：构造 `UCQPEngine(rxe0, 4MB, 16, 320)`，`post_write` + `poll_cq`，验证返回结构字段完整
- `test_ddp_hook.py`：起 2 个子进程做 2-worker DDP，hook 换成 `semirdma_allreduce_hook`，跑 10 step ResNet-18，断言 loss 单调下降

**Stage B：**
- 无新单测（复用 Stage A 的 + Phase 2 的）；主要是端到端训练跑通即算过关
- 每个 baseline 必须 log 出 wall-clock TTA / P99 iteration / final accuracy 三件套，缺一个就视为失败

**Stage C：**
- `test_layer_analyzer.py`：人工构造 5 层梯度（norm 分别是 0.1 / 1.0 / 0.1 / 0.5 / 2.0），断言 `chunk_size_for` 对第 5 层返回最小 chunk（4KB），对第 1 层返回最大（64KB）

### 5.2 回归检查

- Phase 2 的三个 gtest 和三个非 gtest 二进制全部保持能跑（root CMakeLists 统一覆盖）
- Phase 1 `test_netem_loss.c` 不因 Phase 3 改动报错（C/C++ 混编无干扰）

### 5.3 数据真实性审稿预演

每个 Stage 收尾的 results 文档必须包含 rq2-results-ghost-masking §0.5 风格的"**不能声明什么**" 章节，明确划分：
- "SoftRoCE 工程验证" 类结论（Stage A）
- "真机参数标定" 类结论（Stage B 第一周）
- "论文主结果"（Stage B 主实验 + Stage C ablation）

---

## 6. 开放问题

Phase 3 开工前无法确定、需要跑出第一批数据后再迭代的事项：

1. **DDP bucket 粒度与 SemiRDMA chunk 粒度的映射**。PyTorch DDP 默认 bucket ~25 MB；16KB chunk 意味着一个 bucket 约 1600 chunks，`NUM_CHUNKS` 超过 1024 时 `ChunkSet` 和 `RatioController` 的数组/poll 循环效率需要重评估。可能需要把 bucket size 调到 4 MB（与 Phase 2 一层梯度代理一致）。
2. **CPU tensor → RDMA MR 的零拷贝是否稳定？** PyTorch tensor 的底层 storage 不保证地址稳定（可能被 caching allocator 迁移），DDP hook 拿到的 `bucket.buffer()` 生命周期需要核实，必要时要 pin memory 或做一次 memcpy 到注册 MR。
3. **CUDA tensor 的路径要不要做？** 若 workload 运行在 GPU，需要先 `tensor.cpu()` 再 Write，这会引入 PCIe 拷贝开销（GPUDirect RDMA 目前**显式非目标**，§1.3）。Phase 3 可以只在 CPU 训练上跑通，benchmark 结论是"if gradient already on host"；Phase 4 再讨论 GDR。
4. **OptiReduce 的复现工作量**。如果 OptiReduce 的开源实现不能直接在 CloudLab ConnectX-5 节点跑，我们只能引用论文数字作为对比，这会削弱 Stage B 的说服力。Stage B 开工前第一件事是**试跑 OptiReduce**。
5. **Layer Analyzer 的 per-layer ghost_ratio 信号怎么得到**。RatioController 目前只返回 bucket 级别的 completion，per-layer 需要把每层梯度单独过一次 transport——或者引入 "sub-bucket" 概念。这个决定留到 Stage C 开工前再定。
6. **Stage C 的 adaptive ratio 与 Phase 2 RQ4 的硬等式的冲突**。RQ4 推荐 `ratio ≤ (1-p)`，但 LayerAnalyzer 可能对重要层设 `ratio = 0.99` 来换更高精度。这意味着重要层会频繁超时，是 Stage C 必须正面处理的 tradeoff。

---

## 7. 时间线与里程碑

| 日期 | Stage | 里程碑 |
|------|-------|--------|
| May 11 (Mon) | A | 本文档 review 完成 + 开工；`setup.py` + pybind11 骨架可编译 |
| May 14 (Thu) | A | `py_semirdma.cpp` 绑定完成，`test_binding_sanity.py` 绿 |
| May 17 (Sun) | A | `hooks.py` + 2-worker DDP 跑通 ResNet-18 / CIFAR-10 / loss=0% |
| May 22 (Fri) | A | A1 + A2 数据出炉 |
| May 24 (Sun) | A | `rq5-results-ddp-baseline.md` 完成，Stage A 收尾 |
| May 25 (Mon) | B | CloudLab 节点预约 + `provision.sh` 到位 |
| May 28 (Thu) | B | 真机 RQ1/RQ4 重标定完成（100 轮简版） |
| Jun 3 (Wed) | B | 5 baseline 在 ResNet-50 上首轮数据出炉 |
| Jun 6 (Sat) | B | BERT + GPT-2 主实验完整跑完 |
| Jun 7 (Sun) | B | `rq6-results-real-nic-comparison.md` 完成，Stage B 收尾 |
| Jun 8 (Mon) | C | `layer_analyzer.{h,cpp}` + `layer_analyzer.py` 落地 |
| Jun 14 (Sun) | C | C1 / C2 / C3 三组 ablation 数据完整 |
| Jun 21 (Sun) | C | `rq3-results-layer-analyzer.md` 完成，Phase 3 全部收尾 |

**缓冲日**：每个 Stage 末尾预留 1 天给 "实验重跑 / bug 修复"；若 Stage A 超期（最常见的风险源：pybind11 CMake 集成），从 Stage B 的非关键路径（如 GPT-2 workload）里借时间。

---

## 8. 实验结果（Phase 3 结束时回填）

*待三个 Stage 分别结束后回填指针，本节内容保持与 Phase 2 §8 同构：*
- *推荐默认参数（若 Stage B 真机推翻了 Phase 2 的 `(0.95, 20ms)`）*
- *Stage A 正确性验证的一句话结论 + 指向 rq5 doc*
- *Stage B 五路 baseline 的 TTA / P99 主表 + 指向 rq6 doc*
- *Stage C 的 Adaptive vs Uniform ablation 表 + 指向 rq3 doc*
- *Phase 4（论文写作）的 carry-over 问题*

---

## 附录 A · 与 CLAUDE.md 的对齐检查

本文档对应 [CLAUDE.md](../../CLAUDE.md) 里的：

- **架构章节**（第 37–78 行）列出的 `Layer Analyzer / Chunk Manager / Ratio Controller / UC QP Engine` — Phase 3 只扩展 Layer Analyzer，不动其他三个 ✓
- **时间线**（第 181–189 行）Week 5–10 = 2026-05-11 → 2026-06-21 — §7 里程碑逐周对齐 ✓
- **RQ 章节**（第 105–120 行）RQ3 + 新增 RQ5 (Stage A) + RQ6 (Stage B) 在本文档 §2 里逐一展开 ✓
- **五条 baseline** — §2.2 的对比表格直接引自 CLAUDE.md "Five Comparison Baselines" 表 ✓
- **编码规范**（第 213–226 行）—
  - C++：C++17 / 200–400 行每文件 / RAII / `snake_case` / `#pragma once` — §3 所有接口都遵循 ✓
  - Python：type hints / Hydra + OmegaConf / factory 模式 — §3.4/§3.5 接口遵循 global rules/coding-style.md ✓

## 附录 B · Stage 间的硬依赖图

```
Stage A (pybind11 + DDP hook, SoftRoCE)
   │  输出：能跑的 Python API + Stage A 结果文档
   │  契约：`SemiRDMATransport.post_gradient(tensor) → Future[tensor]` 接口稳定
   ▼
Stage B (ConnectX-5 真机 + 5 baseline 对比)
   │  输入：Stage A 的 Python API（不改）
   │  输出：论文主表 + 真机参数重标定
   │  契约：Stage B 主实验的 SemiRDMA cell（uniform 16KB）= Stage C 的 C1 基线
   ▼
Stage C (Layer Analyzer + RQ3 ablation)
   │  输入：Stage B 的 C1 数据 + Stage A 的 hooks.py（扩展 per-layer）
   │  输出：RQ3 ablation 表
```

跨 Stage 的**唯一可破坏契约**是 Stage B 真机参数重标定后推翻 Stage A 的默认值——届时 Stage A 的 `rq5-results` 文档需要加 "SoftRoCE vs 真机" 的差异章节，Stage B / C 直接用新值。
