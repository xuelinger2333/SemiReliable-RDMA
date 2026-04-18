# Phase 2 设计文档 · Core Transport Layer

**时间窗：** 2026-04-27 → 2026-05-10（Week 3–4）
**前置依赖：** Phase 1 P0 实验已完成（见 [p0-walkthrough.md](../phase1/p0-walkthrough.md)）
**产出物：** `src/transport/` 下四个 C++ 模块 + 对应 gtest + root `CMakeLists.txt`
**本文档的地位：** 开工前的设计锁定，写代码之前所有接口、职责、验证计划都在这里定死。本文档**不含实现代码**，只有接口草案、架构决策、实验方法。

---

## 1. 背景与动机

### 1.1 Phase 1 给 Phase 2 的四条硬约束

Phase 1 P0 实验（500 轮 × 6 档丢包率扫描）把四条核心事实钉死，每一条都直接约束 Phase 2 的设计空间：

| # | 结论 | 证据章节 | Phase 2 含义 |
|---|------|---------|-------------|
| ① | 单个 Write WR 内部丢一包 → 整个 WR 作废，概率 `1-(1-p)^c`（c = WR 的 MTU 包数） | [walkthrough §7.3](../phase1/p0-walkthrough.md#73-验证-1full-严格符合-1-p256) | `ChunkManager` 必须把大 buffer 切成**多个独立 WR**，每个 WR 的 `c` 足够小使得 `(1-p)^c` 接近 1 |
| ② | 接收端 CQE（`IBV_WC_RECV_RDMA_WITH_IMM`）是全送达的**充要条件** | [walkthrough §7.4](../phase1/p0-walkthrough.md#74-验证-2cqe--full-永远相等) | `RatioController` 只数 CQE，**绝不扫 buffer**——扫 buffer 毫无意义（无法区分"新数据恰好等于 OLD_PATTERN"和"旧数据"） |
| ③ | 污染模式是**纯前缀截断**（3000 轮 0 CORRUPT） | [walkthrough §7.5](../phase1/p0-walkthrough.md#75-验证-3-0-corrupt--纯前缀截断) | `GhostMask` 每个 chunk 只需 `{bool has_cqe; size_t valid_len;}`，**不需要 bitmap**（比 per-word bitmap 省 1000×） |
| ④ | 几何丢包模型成立（`avg_first_old_off` 匹配截断几何分布 ±3%） | [walkthrough §7.6](../phase1/p0-walkthrough.md#76-验证-4avg_first_old_off-符合截断几何分布) | Phase 2 的仿真实验可以**继续沿用软件丢包注入**作为真机 `netem` 的替代，审稿可辩护 |

另外 [walkthrough §7.9](../phase1/p0-walkthrough.md#79-为什么这条陡峭的-1-p256-曲线并不判-uc-死刑) 澄清的 "UC 两层语义"（WR 内部硬语义 vs. WR 之间互不干扰）是整个 Phase 2 架构的**前提假设**——没有这条假设，切 chunk 就没有意义。

### 1.2 目标产出物清单

Phase 2 结束时（2026-05-10）必须存在：

- `src/transport/uc_qp_engine.h` + `.cpp`
- `src/transport/chunk_manager.h` + `.cpp`
- `src/transport/ratio_controller.h` + `.cpp`
- `src/transport/ghost_mask.h` + `.cpp`
- `src/utils/logging.h`、`src/utils/timing.h`
- `CMakeLists.txt`（root）
- `tests/phase2/` 下三个 gtest 二进制：`test_chunk_roundtrip`、`test_ratio_timeout`、`test_ghost_mask`
- `tests/phase2/test_chunk_sweep`：RQ1 的 chunk 大小扫描实验（C++ binary + 脚本）
- 本文档的"实验结果"子节被后续填充（Phase 2 结束时回填）

### 1.3 非目标

Phase 2 **不**做的事情（避免 scope creep）：

- PyTorch DDP 集成（Phase 3）
- Layer Analyzer / 跨层自适应（Phase 3）
- CloudLab ConnectX-5 部署（Phase 2 末尾如果时间允许可以开始，但不是必须）
- 任何形式的 pybind11 绑定（Phase 3 上半）
- 真正的 AllReduce 算法（Phase 2 只做**点对点**的 chunk 传输验证）

---

## 2. 研究问题与实验方法

Phase 2 要回答四个 Research Question 中的 **RQ1、RQ2、RQ4**。RQ3（跨层自适应）只预留接口，留给 Phase 3。

### 2.1 RQ1：Write 粒度优化

**问题：** 给定丢包率 `p` 和 NIC 的 WQE rate 上限 `W_max`，什么样的 chunk 大小 `C*` 使得"时间 × ghost 量"的联合代价最小？

**度量指标：**

- `wqe_throughput` — 每秒发出的 WR 数（用 `std::chrono` 打点）
- `observed_ghost_ratio` — 整层梯度里"**没收到 CQE 的 chunk 字节数** / 总字节数"
- `effective_goodput` — 整层梯度的"**成功送达字节数** / 耗时"（GB/s）
- `tail_latency_p99` — 一层梯度从 `post_send` 第一个 chunk 到 `wait_for_ratio` 返回的 P99 时间

**实验设计：**

- **固定参数：** 一层梯度 = 4 MB，每档跑 500 轮，随机种子固定为 42
- **扫描维度 1（chunk 大小）：** `{1 KB, 4 KB, 16 KB, 64 KB, 256 KB}`
- **扫描维度 2（丢包率）：** `{0%, 0.1%, 1%, 5%}`（4 档，对应 P0 结果的子集）
- **结果矩阵：** 5 × 4 = 20 个 cell，每 cell 记录四个度量

**预期结果（先验）：**

- `wqe_throughput` 会随 chunk 变小而**接近 SoftRoCE 上限**（SoftRoCE 的 WQE 开销主要在软件路径上，预计 1 KB chunk 会把 CPU 打满）
- `observed_ghost_ratio` 会严格 ≈ `1 - (1-p)^c`（P0 结论 ① 的直接推广）
- `effective_goodput` 有一个 sweet spot：太大被 ghost 吃掉，太小被 WQE 吞吐吃掉

**成功判定：** 能画出一张 `chunk_size × ghost_ratio × goodput` 的三维权衡图，并指出每个 `p` 下的最优 chunk 大小。误差范围在 P0 理论预测的 5% 以内算通过。

### 2.2 RQ2：Ghost Gradient 缓解

**问题：** 用 `GhostMask.valid_len` 做 masked aggregation 相比"什么都不做直接 sum" 能减少多少 gradient RMS 误差？

**度量指标：**

- `rms_error_raw` — 不做 masking，直接对收到的 buffer sum，和 "全送达的理论 sum" 之间的 RMS
- `rms_error_masked` — 用 `valid_len` 把失败区域置零后 sum，和 "全送达的理论 sum" 之间的 RMS
- `rms_error_ratio` — `rms_error_masked / rms_error_raw`

**实验设计：**

- 一层梯度填入 `N(0, 1)` 随机浮点数（固定 seed）
- 两个 worker 之间做 point-to-point gradient 传输，触发软件丢包注入
- 分别跑 `raw`（无 masking）和 `masked`（应用 GhostMask）两种后处理
- chunk 大小固定为 RQ1 结果的最优值（或先定为 16 KB）
- 丢包率扫 `{1%, 5%}`，每档 200 轮

**预期结果：**

- `rms_error_ratio` 应当显著 < 1（`masked` 比 `raw` 小一个数量级）
- 因为 ghost 区域是"上一轮残留"，它的数值分布和"本轮真实梯度"通常相关性很低 → masking 相当于把噪声置零

**成功判定：** 在 `p=5%` 下 `rms_error_ratio ≤ 0.2`。

### 2.3 RQ4：CQE 驱动的 Ratio 控制

**问题：** `RatioController.wait_for_ratio(ratio, timeout_ms)` 的 `(ratio, timeout)` 参数如何影响端到端延迟和收敛质量？

**度量指标：**

- `wait_latency_p50/p99` — `wait_for_ratio` 函数本身的耗时分布
- `achieved_ratio` — 实际返回时已经成功的 chunk 比例（可能因为 timeout 提前返回）
- `cqe_poll_count` — 一次等待里调用了多少次 `ibv_poll_cq`（衡量 CPU 开销）

**实验设计：**

- 一层梯度 4 MB，chunk 固定 16 KB = 256 chunk
- 扫描 `ratio ∈ {0.90, 0.95, 0.99, 1.00}`
- 扫描 `timeout_ms ∈ {1, 5, 20, 100}`
- 丢包率固定为 `p=1%`
- 每个组合 500 轮

**预期结果：**

- `ratio=1.00` 的 tail 会被"最后一个 chunk 特别慢"拖长，`ratio=0.95` 能显著缩短 P99
- `timeout_ms` 太小会导致 `achieved_ratio < ratio`；太大会白白等 ghost chunk

**成功判定：** 找到一个 `(ratio, timeout)` 组合使得 `achieved_ratio ≥ 0.95` 且 `wait_latency_p99` 小于 `ratio=1.00` 的 50%。

### 2.4 RQ3：跨层自适应（仅预留接口）

Phase 2 只做接口占位（`LayerImportance` struct），不做任何实验。留给 Phase 3 的 Layer Analyzer。

---

## 3. 代码结构

### 3.1 模块关系图

```
  Application (test binaries, 后续是 PyTorch hook)
          │
          ▼
  ┌──────────────────┐
  │  ChunkManager    │  切分 / 重装 / 每 chunk 状态
  └────┬─────────────┘
       │ uses
       ▼
  ┌──────────────────┐    ┌──────────────────┐
  │ RatioController  │───▶│    GhostMask     │
  │  (CQE counting)  │    │  (valid_len 应用) │
  └────┬─────────────┘    └──────────────────┘
       │ uses
       ▼
  ┌──────────────────┐
  │   UCQPEngine     │  QP 生命周期 / post_send / poll_cq
  └────┬─────────────┘
       │ wraps
       ▼
     libibverbs
```

**依赖方向：** 单向自上而下。`GhostMask` 和 `RatioController` **不互相依赖**——`RatioController` 决定"谁成功了"，然后把结果交给 `ChunkManager`，`ChunkManager` 再把状态传给 `GhostMask` 做 masking。

**为什么不让 RatioController 直接 call GhostMask？** 因为 RQ2 需要能跑"不做 masking"的对照组，GhostMask 必须是**可选步骤**。解耦它们让上层测试代码能自由选择是否应用 masking。

### 3.2 目录布局

```
SemiRDMA/
├── CMakeLists.txt                  ← 新建，root 级别
├── src/
│   ├── transport/
│   │   ├── uc_qp_engine.h
│   │   ├── uc_qp_engine.cpp
│   │   ├── chunk_manager.h
│   │   ├── chunk_manager.cpp
│   │   ├── ratio_controller.h
│   │   ├── ratio_controller.cpp
│   │   ├── ghost_mask.h
│   │   └── ghost_mask.cpp
│   └── utils/
│       ├── logging.h               ← 简单的 stderr 包装器（不引入 spdlog 依赖）
│       └── timing.h                ← std::chrono 打点工具
└── tests/
    └── phase2/
        ├── CMakeLists.txt
        ├── test_chunk_roundtrip.cpp    ← gtest: 切分-发送-重装的单元测试
        ├── test_ratio_timeout.cpp       ← gtest: RQ4 参数扫描
        ├── test_ghost_mask.cpp          ← gtest: RQ2 对照实验
        └── test_chunk_sweep.cpp         ← RQ1 的 5×4 扫描主程序（非 gtest）
```

### 3.3 `UCQPEngine` 接口草案

**职责：** 封装 `tests/phase1/rdma_common.h` 里已经验证的 UC QP 生命周期管理，**不引入任何新的 RDMA verbs 调用**。目的是让 Phase 2 的所有模块共享一个 C++ RAII 式的 QP 对象，而不是继续用 C 风格的 `rdma_ctx` struct。

**从哪里 refactor：** [tests/phase1/rdma_common.h](../../tests/phase1/rdma_common.h) 第 91–246 行（`rdma_open_device`、`rdma_find_gid`、`rdma_init_ctx`、`rdma_modify_qp_to_init/rtr/rts`）和 314–399 行（`rdma_post_write`、`rdma_post_write_imm`、`rdma_poll_cq`）。

```cpp
// src/transport/uc_qp_engine.h
#pragma once
#include <infiniband/verbs.h>
#include <cstdint>
#include <string>
#include <vector>

namespace semirdma {

// 对端 QP 的连接参数（通过 TCP 交换）
struct RemoteQpInfo {
    uint32_t      qpn;
    union ibv_gid gid;
};

// 本地/远端 MR 的描述符
struct RemoteMR {
    uint64_t addr;
    uint32_t rkey;
};

// 一个 CQE 事件的精简视图
struct Completion {
    uint64_t            wr_id;
    enum ibv_wc_opcode  opcode;
    enum ibv_wc_status  status;
    uint32_t            imm_data;   // 仅在 RECV_RDMA_WITH_IMM 时有效
};

class UCQPEngine {
public:
    UCQPEngine(const std::string& dev_name,
               size_t buffer_bytes,
               int    sq_depth,
               int    rq_depth);
    ~UCQPEngine();

    UCQPEngine(const UCQPEngine&)            = delete;
    UCQPEngine& operator=(const UCQPEngine&) = delete;

    // 状态迁移：RESET → INIT → RTR → RTS
    void bring_up(const RemoteQpInfo& remote);

    // Post 一个 Write WR（可选 IMM）。返回传入的 wr_id（方便链式调用）
    // local_offset:  本地 buffer 的起始偏移（不是指针，而是 registered MR 内的偏移）
    // length:        本次 Write 的字节数
    // remote_offset: 远端 buffer 的起始偏移
    uint64_t post_write(uint64_t wr_id,
                        size_t   local_offset,
                        size_t   remote_offset,
                        size_t   length,
                        const RemoteMR& remote,
                        bool     with_imm,
                        uint32_t imm_data = 0);

    // 对应接收端：post 一个 zero-length Recv WR（UC Write-with-Imm 需要）
    void post_recv(uint64_t wr_id);

    // 轮询 CQ，返回 0..n 个完成事件。timeout_ms=0 表示非阻塞单次调用
    std::vector<Completion> poll_cq(int max_n, int timeout_ms);

    // 访问器
    ibv_pd*  pd()         const { return pd_; }
    uint8_t* local_buf()  const { return buf_; }
    size_t   buf_bytes()  const { return buf_size_; }
    RemoteMR local_mr()   const;                 // 给 TCP 交换用
    uint32_t qpn()        const { return qp_->qp_num; }
    const union ibv_gid& gid() const { return gid_; }

private:
    ibv_context* ctx_  = nullptr;
    ibv_pd*      pd_   = nullptr;
    ibv_cq*      cq_   = nullptr;
    ibv_qp*      qp_   = nullptr;
    ibv_mr*      mr_   = nullptr;
    uint8_t*     buf_  = nullptr;
    size_t       buf_size_ = 0;
    int          ib_port_  = 1;
    int          gid_index_= -1;
    union ibv_gid gid_;
};

} // namespace semirdma
```

**关键设计决策：**

1. **单一 buffer + 按 offset 寻址**。Phase 1 的 `rdma_post_write` 每次都用 `rctx->buf` 做起始地址，不支持在同一 MR 内分段 Write。Phase 2 改成 `post_write(local_offset, remote_offset, length)`，这样 `ChunkManager` 可以在**同一 MR** 内部按 chunk 切分。不做多 MR 是因为 `ibv_reg_mr` 的开销很高。
2. **RAII**。析构函数按 `rdma_cleanup` 的顺序释放所有资源。拷贝禁用。
3. **不包含 TCP 交换**。`RemoteQpInfo` 的获取留给调用者（测试代码里继续用 Phase 1 的 `tcp_server_exchange`/`tcp_client_exchange`），`UCQPEngine` 只负责 QP 本身。这让单元测试可以 mock QP info。
4. **Completion 结构暴露 imm_data**。RQ4 实验需要用 IMM 携带 `chunk_id`，`RatioController` 通过 `imm_data` 知道"哪个 chunk 成功了"。

### 3.4 `ChunkManager` 接口草案

**职责：** 给定一个连续的 buffer 区域和一个 chunk 大小，生成 N 个独立的 `ChunkDescriptor`，每个对应一个未来的 `post_write` 调用。跟踪每个 chunk 的完成状态。

```cpp
// src/transport/chunk_manager.h
#pragma once
#include <cstdint>
#include <cstddef>
#include <vector>

namespace semirdma {

struct ChunkDescriptor {
    uint32_t chunk_id;      // 用作 wr_id 和 imm_data 的低 32 位
    size_t   local_offset;  // 在本地 buffer 内的偏移（字节）
    size_t   remote_offset; // 在远端 buffer 内的偏移（字节）
    size_t   length;        // 本 chunk 的字节数（最后一个 chunk 可能短于 chunk_bytes）
};

struct ChunkState {
    bool   has_cqe   = false;  // 从 RatioController 更新
    size_t valid_len = 0;      // 给 GhostMask 用；收到 CQE 后 = length
};

class ChunkSet {
public:
    // 把 [base_offset, base_offset+total_bytes) 按 chunk_bytes 切分
    ChunkSet(size_t base_offset, size_t total_bytes, size_t chunk_bytes);

    size_t size() const { return chunks_.size(); }
    const ChunkDescriptor& chunk(size_t i) const { return chunks_[i]; }
    ChunkState&            state(size_t i)       { return states_[i]; }
    const ChunkState&      state(size_t i) const { return states_[i]; }

    // 根据 CQE 的 imm_data 反查 chunk index（O(1) 因为 chunk_id == index）
    void mark_completed(uint32_t chunk_id);

    // 便于测试/日志
    size_t num_completed() const;
    double completion_ratio() const;

private:
    std::vector<ChunkDescriptor> chunks_;
    std::vector<ChunkState>      states_;
};

} // namespace semirdma
```

**关键设计决策：**

1. **`chunk_id == 数组下标`**。这样 `mark_completed(imm_data)` 就是 O(1) 数组访问。简单且 Phase 2 够用（未来如果要做多层并发传输再引入 `unordered_map`）。
2. **不持有 buffer**。`ChunkSet` 只存 offset 和 length，不存指针——因为 buffer 是 `UCQPEngine` 管理的。这样同一个 `UCQPEngine` 的 buffer 可以被切成不同的 `ChunkSet`（例如不同层梯度）。
3. **`ChunkState.valid_len`**。根据 P0 结论 ③（纯前缀截断），这是 `GhostMask` 唯一需要的信息。收到 CQE 就是 `length`，没收到就是 `0`（Phase 2 不做"部分恢复"，Phase 3 可以引入中间值）。

### 3.5 `RatioController` 接口草案

**职责：** 给定一个 `ChunkSet`，循环 `poll_cq`，每收到一个完成事件就 `mark_completed`，直到达到 `ratio` 或 `timeout_ms`。

```cpp
// src/transport/ratio_controller.h
#pragma once
#include "uc_qp_engine.h"
#include "chunk_manager.h"

namespace semirdma {

struct WaitStats {
    double   latency_ms;       // 从调用到返回的耗时
    uint32_t poll_count;       // 调用了多少次 ibv_poll_cq
    uint32_t completed;        // 返回时成功的 chunk 数
    bool     timed_out;
};

class RatioController {
public:
    RatioController(UCQPEngine& engine) : engine_(engine) {}

    // 阻塞等待，直到 cs.completion_ratio() >= ratio 或超时
    // 返回 true 表示达到了 ratio，false 表示超时
    bool wait_for_ratio(ChunkSet& cs,
                        double    ratio,
                        int       timeout_ms,
                        WaitStats* stats = nullptr);

private:
    UCQPEngine& engine_;
};

} // namespace semirdma
```

**关键设计决策：**

1. **只看 CQE，绝不扫 buffer**。这是 P0 结论 ② 的直接翻译。实现里 `poll_cq` 返回的每个 `Completion` 的 `imm_data` 就是 `chunk_id`，直接 `cs.mark_completed(imm_data)`。
2. **`WaitStats` 作为可选输出**。生产路径不需要，但 RQ4 实验必须拿到这些度量。
3. **单线程轮询**。Phase 2 不引入 event channel / 阻塞等待，因为 P0 已经确认 `ibv_poll_cq` 的延迟极低（微秒级），轮询模型够用。Phase 3 如果延迟成瓶颈再考虑 event-driven。

### 3.6 `GhostMask` 接口草案

**职责：** 对没收到 CQE 的 chunk，把对应 buffer 区域置零；对收到 CQE 的 chunk，按 `valid_len` 保留。

```cpp
// src/transport/ghost_mask.h
#pragma once
#include "chunk_manager.h"

namespace semirdma {

class GhostMask {
public:
    // 对 buf[base_offset, base_offset+total_bytes) 的范围应用 mask
    // cs 里未收到 CQE 的 chunk，对应字节被置零
    static void apply(uint8_t* buf, const ChunkSet& cs);

    // RQ2 实验用：不做 masking（no-op），用于对照组
    static void apply_noop(uint8_t* buf, const ChunkSet& cs) { (void)buf; (void)cs; }
};

} // namespace semirdma
```

**关键设计决策：**

1. **纯静态函数**。`GhostMask` 没有状态，不需要类实例。写成 `class` 只是为了命名空间对齐。
2. **就地修改 buffer**。不拷贝，直接 `memset` 掉失败区域。调用者负责保证 `buf` 指向正确的本地 buffer。
3. **明确提供 `apply_noop`**。RQ2 对照组不需要"自己写一个 if"，直接换函数指针。

### 3.7 Utils

`src/utils/logging.h`：

```cpp
// 保持 Phase 1 的 LOG_INFO/LOG_ERR 风格，不引入 spdlog 依赖
#define SEMIRDMA_LOG_INFO(fmt, ...) fprintf(stderr, "[INFO]  " fmt "\n", ##__VA_ARGS__)
#define SEMIRDMA_LOG_ERR(fmt, ...)  fprintf(stderr, "[ERROR] %s:%d: " fmt "\n", \
                                             __FILE__, __LINE__, ##__VA_ARGS__)
```

`src/utils/timing.h`：

```cpp
namespace semirdma {
class Stopwatch {
public:
    Stopwatch();
    void   reset();
    double elapsed_ms() const;
    double elapsed_us() const;
private:
    std::chrono::steady_clock::time_point start_;
};
} // namespace semirdma
```

---

## 4. 构建与依赖

### 4.1 Root `CMakeLists.txt`（规划）

```cmake
cmake_minimum_required(VERSION 3.16)
project(SemiRDMA CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# 依赖
find_library(IBVERBS_LIB ibverbs REQUIRED)
find_package(GTest QUIET)   # 没装也能编主库，只有 tests 会 skip

# 核心库
add_library(semirdma_transport
    src/transport/uc_qp_engine.cpp
    src/transport/chunk_manager.cpp
    src/transport/ratio_controller.cpp
    src/transport/ghost_mask.cpp
)
target_include_directories(semirdma_transport PUBLIC src/)
target_link_libraries(semirdma_transport PUBLIC ${IBVERBS_LIB})

# 单元测试
if (GTest_FOUND)
    enable_testing()
    add_subdirectory(tests/phase2)
endif()
```

### 4.2 构建顺序

1. `uc_qp_engine` — 无内部依赖，只依赖 libibverbs
2. `chunk_manager` — 无依赖（纯数据结构）
3. `ratio_controller` — 依赖 `uc_qp_engine` + `chunk_manager`
4. `ghost_mask` — 依赖 `chunk_manager`

（3）和（4）可以并行开发。

### 4.3 测试环境

- **SoftRoCE rxe0** on cs528，沿用 Phase 1 的设置
- **丢包注入** 继续用软件模型（`test_chunk_sweep` 里用 RNG 决定哪些 chunk 不 post）
- **真机验证** 留到 Phase 2 末尾（如果 CloudLab ConnectX-5 节点可用）

---

## 5. 验证计划

### 5.1 单元测试（gtest）

**`test_chunk_roundtrip.cpp`：**
- 构造 4 MB buffer + 16 KB chunk_size → 期望 256 个 chunk
- Server/client 起两个 `UCQPEngine`，client post 全部 chunk，server `poll_cq` 收全
- 断言：`ChunkSet.num_completed() == 256`，buffer 内容逐字节等于 client 填充的 pattern

**`test_ratio_timeout.cpp`：**
- 故意只 post 前 240 个 chunk（跳过最后 16 个模拟丢失）
- `wait_for_ratio(0.90, 100)` 应当返回 true，`wait_for_ratio(1.00, 100)` 应当返回 false 且 timed_out=true
- 断言 `WaitStats.completed >= 240`

**`test_ghost_mask.cpp`：**
- 同上 post 240/256，应用 `GhostMask::apply`
- 断言前 240 个 chunk 区域 = client pattern，后 16 个 chunk 区域 = 全零

### 5.2 集成实验（RQ1/RQ2/RQ4）

三个 RQ 对应的实验主程序都放在 `tests/phase2/test_chunk_sweep.cpp`（或拆成三个文件），输出 CSV 到 `experiments/results/phase2-*/` 下，沿用 Phase 1 的 `summary.csv` 格式。

**端到端健全性对比**（Phase 1 vs Phase 2）：

- Phase 1 `test_netem_loss` 在 `p=1%, 256 KB Write` 下的 ghost ratio ≈ `1 - (1-0.01)^256 ≈ 92.4%`
- Phase 2 `test_chunk_sweep` 在 `p=1%, 16 KB chunk` 下的 ghost ratio 应当 ≈ `1 - (1-0.01)^16 ≈ 14.9%`
- **Phase 2 的 chunk 化带来 ~6× 的 ghost 减少** —— 这是本次工作的核心 selling point

### 5.3 回归检查

- Phase 2 的任何改动不能让 Phase 1 的 `tests/phase1/test_netem_loss` 编译失败（两者独立目录，但 root CMakeLists 要同时覆盖）
- `make -C tests/phase1` 和 `cmake --build build` 两条构建路径都要保持能跑

---

## 6. 开放问题

Phase 2 开工前无法确定、需要等第一批实验数据或讨论的事项：

1. **Chunk 大小的最优下界在 SoftRoCE 上测不准** — SoftRoCE 的 WQE rate 主要受软件路径 CPU 限制，和 ConnectX-5 的真机曲线差别大。Phase 2 可能要在 SoftRoCE 上先定一个"合理范围"（例如 4–64 KB），真机最优值等 Phase 3 CloudLab 再定。
2. **`valid_len` 的原子性** — 单生产者（RatioController）单消费者（主线程调用 GhostMask）的模型下不需要锁，但如果未来要把 `wait_for_ratio` 放到独立线程，就要加 `std::atomic<size_t>`。Phase 2 先假设单线程。
3. **CQE 的 wr_id 和 imm_data 的对应** — UC Write-with-Imm 接收端 CQE 里 `wr_id` 是**本地 Recv WR 的 id**，不是 client 的 Send WR id。所以 `ChunkSet.mark_completed` 只能用 `imm_data`（我们自己塞进去的 `chunk_id`），不能用 `wr_id`。这个**关键细节**必须在 `UCQPEngine::poll_cq` 的实现里显式处理。
4. **GhostMask 的置零 vs 保留前缀** — P0 确认了"前缀是对的、后缀是 ghost"，所以理论上可以只对**没收到 CQE** 的 chunk 做 memset。但如果一个 chunk **部分送达**（在 P0 的模型里等价于"这个 chunk 没 post IMM"），那实际上有部分字节是对的。Phase 2 先简化为"chunk 级别全有/全无"，部分恢复留给 Phase 3。
5. **TCP 交换代码放哪** — Phase 1 的 `tcp_server_exchange/client_exchange` 还留在 `rdma_common.h`。Phase 2 是把它也搬进 `src/transport/`，还是留在测试代码里？倾向于**留在测试代码**，因为 TCP 交换是 bootstrap，不属于 transport 层。

---

## 7. 时间线与里程碑

| 日期 | 里程碑 |
|------|--------|
| Apr 27 (Mon) | 本文档 review 完成 + 开工 |
| Apr 30 (Thu) | `UCQPEngine` + `ChunkManager` 完成，`test_chunk_roundtrip` 绿色 |
| May 3 (Sun) | `RatioController` + `GhostMask` 完成，所有单元测试绿色 |
| May 6 (Wed) | `test_chunk_sweep` RQ1 数据出炉 |
| May 8 (Fri) | RQ2 + RQ4 数据出炉 |
| May 10 (Sun) | Phase 2 总结文档（回填到本文件 §8）|

## 8. 实验结果（Phase 2 结束时回填）

*待 Phase 2 实验结束填入。预计内容：RQ1 的三维权衡图、RQ2 的 masking 对比表、RQ4 的参数扫描热力图、和 Phase 1 基线的对比表。*

---

## 附录 A：与 CLAUDE.md 的对齐检查

本文档对应 [CLAUDE.md](../../CLAUDE.md) 里的：

- **架构章节**（第 37–78 行）列出的四个模块 `UCQPEngine` / `ChunkManager` / `RatioController` / `GhostMask` — 全部 §3 里有接口草案 ✓
- **时间线**（第 181–189 行）Week 3–4 = 2026-04-27 → 2026-05-10 — §1.2 目标产物对齐 ✓
- **RQ 章节**（第 105–120 行）RQ1/RQ2/RQ4 在本文档 §2 里逐一展开 ✓；RQ3 显式推迟到 Phase 3 ✓
- **编码规范**（第 213–226 行）C++17 / 200–400 行每文件 / RAII / `snake_case` / `#pragma once` — §3 所有接口都遵循 ✓
