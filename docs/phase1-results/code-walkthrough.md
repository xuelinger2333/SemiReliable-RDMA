# Phase 1 代码详解

## 架构概览

三个测试共享相同的模式：**SoftRoCE loopback 上的 server/client 双进程**。

```
  进程 A (server)                 进程 B (client)
  ┌──────────────────┐            ┌──────────────────┐
  │ 1. 打开 rxe0     │            │ 1. 打开 rxe0     │
  │ 2. 创建 UC QP    │            │ 2. 创建 UC QP    │
  │ 3. QP → INIT     │            │ 3. QP → INIT     │
  │ 4. Post Recv WR  │            │                  │
  │                  │            │                  │
  │ 5. TCP listen ◄──── 交换 QP ────► 5. TCP connect │
  │    {qpn, gid,    │    元数据   │    {qpn, gid,   │
  │     rkey, addr}  │            │     rkey, addr}  │
  │                  │            │                  │
  │ 6. QP → RTR      │            │ 6. QP → RTR     │
  │                  │            │ 7. QP → RTS      │
  │                  │            │                  │
  │ 7. 轮询 CQ ◄────── RDMA Write ──── 8. Post Send │
  │    (等待 CQE)    │  with Imm  │    (轮询 CQ)     │
  └──────────────────┘            └──────────────────┘
```

Server 停留在 RTR 状态（只接收）。Client 进入 RTS 状态（发送）。

---

## rdma_common.h — 共享 RDMA 工具库

### 核心数据结构

```c
struct qp_info {          // 通过 TCP 在 server 与 client 之间交换
    uint32_t      qpn;    // QP 编号 — RTR 中的 dest_qp_num 需要
    uint32_t      rkey;   // Remote key — 授权 RDMA Write 写入此 MR
    uint64_t      addr;   // 注册 buffer 的虚拟地址
    union ibv_gid gid;    // RoCE 寻址用的 GID（16 字节）
};

struct rdma_ctx {          // 所有 RDMA 资源捆绑在一起
    ibv_context/pd/cq/qp/mr  // 标准 RDMA 资源链
    void *buf;                // 数据 buffer（页对齐）
    struct qp_info local_info; // 本端元数据
};
```

### 资源初始化：`rdma_init_ctx()`

初始化链遵循标准 RDMA 模式：

```
ibv_open_device(rxe0)
  → ibv_alloc_pd()          // Protection Domain
    → ibv_create_cq()       // Completion Queue（256 条目）
      → aligned_alloc()     // 页对齐 buffer
        → ibv_reg_mr()      // 注册 buffer 用于 RDMA（LOCAL_WRITE | REMOTE_WRITE）
          → ibv_query_gid() // 查找有效 GID（优先 index 1 = RoCEv2）
            → ibv_create_qp(IBV_QPT_UC)  // Unreliable Connected QP
```

关键细节：MR access flags 包含 `IBV_ACCESS_REMOTE_WRITE`——允许远端对等方 RDMA Write 写入我们的 buffer。

### UC QP 状态机

```
  RESET ──► INIT ──► RTR ──► RTS
              │         │       │
              │ 属性:    │ 属性: │ 属性:
              │ port     │ mtu   │ sq_psn
              │ pkey     │ dest  │
              │ access   │ qpn   │
              │          │ ah    │
              │          │ rq_psn│
```

**UC 特有注意事项（vs. RC）：**
- RTR 需要 `rq_psn`（期望接收的 PSN）—— 这是我们最初遗漏导致 `Invalid argument` 的参数
- RTR **不需要** `max_dest_rd_atomic` 或 `min_rnr_timer`（RC 专用）
- RTS **不需要** `timeout`、`retry_cnt`、`rnr_retry`、`max_rd_atomic`（RC 专用）
- RoCE 环境下 `ah_attr.is_global = 1` 且使用 GRH 是必须的（用 GID 寻址，不用 LID）

### TCP 交换协议

```
Server                           Client
listen(18515)
accept() ◄──────────── connect()
write(local_info) ────────────► read(remote_info)
read(remote_info) ◄──────────── write(local_info)
close()                          close()
```

Server 先发送，Client 先接收。此顺序避免死锁。

### Post 操作

**`rdma_post_write_imm()`** — SemiRDMA 的核心操作：
```c
wr.opcode     = IBV_WR_RDMA_WRITE_WITH_IMM;
wr.imm_data   = htonl(imm_data);   // 网络字节序！
wr.wr.rdma.remote_addr = remote->addr;
wr.wr.rdma.rkey        = remote->rkey;
```

NIC 从我们的本地 buffer（`sge.addr`）读取数据，直接写入远端 buffer（`remote_addr`）——零拷贝。`imm_data` 通过接收端的 CQE 传递。

**`rdma_post_recv()`** — 使用空散列表 post：
```c
wr.sg_list = NULL;
wr.num_sge = 0;
```
对于 Write-with-Immediate，Receive WR 存在的唯一目的是生成 CQE。数据去往 RDMA 地址，不是 Receive WR 的散列表。

---

## Test 1：test_uc_write_imm.c

### 目的
验证 UC Write-with-Immediate 在 SoftRoCE 上是否生成接收端 CQE。

### 流程

| 步骤 | Server | Client |
|------|--------|--------|
| 1 | 用 `0xAA` 填充 buffer | 用 `0x42` 填充 buffer |
| 2 | Post Receive WR (wr_id=1) | — |
| 3 | TCP 交换 | TCP 交换 |
| 4 | QP → RTR | QP → RTR → RTS |
| 5 | 轮询 CQ（10s 超时） | Post Write-with-Immediate (imm=`0xDEADBEEF`) |
| 6 | 检查：CQE opcode, imm_data, buffer | 检查：发送 CQE 状态 |

### 关键验证点

```c
// Server 检查 4 项：
pass_status = (wc.status == IBV_WC_SUCCESS);
pass_opcode = (wc.opcode == IBV_WC_RECV_RDMA_WITH_IMM);
pass_imm    = (ntohl(wc.imm_data) == 0xDEADBEEF);
pass_buf    = (buf[0] == 0x42);
```

### 为什么 imm_data 需要 htonl/ntohl

`ibv_send_wr.imm_data` 字段定义为 `__be32`（大端序）。发送时需要 `htonl()` 转换。接收端的 `ibv_wc.imm_data` 也是大端序，因此需要 `ntohl()` 恢复原始值。

---

## Test 2：test_ghost_gradient.c

### 目的
验证 UC Write-with-Immediate 失败（无 Receive WR）时，buffer 是否保留旧数据。

### 关键设计：持久 TCP 连接

与 Test 1 不同，本测试保持 TCP 连接以实现两轮之间的同步：

```c
// Server
int tcp = tcp_listen_accept(TCP_PORT);  // 接受连接，保持打开
// ... Round 1 ...
tcp_signal(tcp);  // 告知 client："开始 Round 2"
tcp_wait(tcp);    // 等待 client："Round 2 完成"
// ... 检查 buffer ...
close(tcp);
```

这避免了轮次间的竞争条件——每一端在继续之前都等待显式的同步字节。

### 关键时刻

```c
// Server：Round 2
// *** 故意不 post Receive WR ***
// （模拟：如果接收端不期待这些数据会怎样？）
tcp_signal(tcp);  // 告知 client 继续
```

Server 在 Round 2 之前故意跳过 `rdma_post_recv()`。这测试当 Write-with-Immediate 没有 Receive WR 可消费时会发生什么。

### 观察结果

RDMA 数据写入**成功**（buffer → `0xFF`），但没有生成 CQE。这是因为在 SoftRoCE 上，RDMA Write 部分和完成通知是独立的：

```
Write-with-Immediate = RDMA Write（始终执行） + CQE（需要 Receive WR）
```

### 设计影响

在真实的 SemiRDMA 系统中，ghost gradient 问题来自**丢包**（不是缺少 Receive WR）。当数据包丢失时：
1. PSN 失序
2. 后续数据包被静默丢弃
3. Buffer 包含部分旧数据 = 真正的 ghost gradient

无 RQ WR 测试仍然证明了一个关键事实：**CQE 是唯一可靠的交付信号。** 你不能通过检查 buffer 内容来判断数据是否交付。

---

## Test 3：test_wqe_rate.c

### 目的
测量不同 chunk 大小下的 WQE 发射速率，为 RQ1 的 chunk 大小选择提供数据。

### 基准测试结构

使用纯 RDMA Write（无 Immediate）测量原始 WQE 速率，避免接收端开销。

```c
// 批量 post + 周期性信号
for (int i = 0; i < NUM_ITERS; i++) {
    bool sig = ((i + 1) % SIG_INTERVAL == 0) || (i == NUM_ITERS - 1);
    rdma_post_write(&ctx, &remote, chunk, i, sig);
    if (sig) {
        rdma_poll_cq_spin(ctx.cq, &wc);  // 排空 CQ 以释放 SQ 槽位
    }
}
```

**为什么每 64 个 WQE 信号一次？**
- 每个 WQE 都信号：CQ 轮询开销太大
- 完全不信号：SQ 溢出（SQ 深度 = 256）
- 每 64 个信号一次：post 64 个 WQE，排空 1 个 CQE（隐式完成全部 64 个）

**为什么用 `rdma_poll_cq_spin` 而不是 `rdma_poll_cq`？**
- `rdma_poll_cq` 有超时检查和 `clock_gettime()` 调用——增加测量噪声
- `rdma_poll_cq_spin` 纯忙等待无计时开销——更适合紧凑的基准测试循环

### Server 角色

Server 是被动的——只提供目标 buffer 并等待 client 完成：

```c
// Server：注册 16MB buffer，进入 RTR，等待 TCP 信号
rdma_init_ctx(&ctx, dev_name, LARGE_BUF, 16, 16);
// ... 交换，RTR ...
// 等待 client 的 "done" TCP 信号
```

不需要 Receive WR，因为纯 RDMA Write 不消费它们。
