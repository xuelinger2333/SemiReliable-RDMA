# Phase 1 Code Walkthrough

## Architecture Overview

All three tests share the same pattern: **server/client dual-process over SoftRoCE loopback**.

```
  Process A (server)              Process B (client)
  ┌──────────────────┐            ┌──────────────────┐
  │ 1. Open rxe0     │            │ 1. Open rxe0     │
  │ 2. Create UC QP  │            │ 2. Create UC QP  │
  │ 3. QP → INIT     │            │ 3. QP → INIT     │
  │ 4. Post Recv WR  │            │                  │
  │                  │            │                  │
  │ 5. TCP listen ◄──── exchange ────► 5. TCP connect │
  │    {qpn, gid,    │   QP info  │    {qpn, gid,   │
  │     rkey, addr}  │            │     rkey, addr}  │
  │                  │            │                  │
  │ 6. QP → RTR      │            │ 6. QP → RTR     │
  │                  │            │ 7. QP → RTS      │
  │                  │            │                  │
  │ 7. Poll CQ ◄─────── RDMA Write ──── 8. Post Send │
  │    (wait CQE)    │  with Imm  │    (poll CQ)     │
  └──────────────────┘            └──────────────────┘
```

Server stays in RTR (only receives). Client goes to RTS (sends).

---

## rdma_common.h — Shared RDMA Utilities

### Key Data Structures

```c
struct qp_info {          // Exchanged via TCP between server & client
    uint32_t      qpn;    // QP number — needed for dest_qp_num in RTR
    uint32_t      rkey;   // Remote key — authorizes RDMA Write to this MR
    uint64_t      addr;   // Virtual address of registered buffer
    union ibv_gid gid;    // GID for RoCE addressing (16 bytes)
};

struct rdma_ctx {          // All RDMA resources bundled
    ibv_context/pd/cq/qp/mr  // Standard RDMA resource chain
    void *buf;                // Data buffer (page-aligned)
    struct qp_info local_info; // Our side's metadata
};
```

### Resource Initialization: `rdma_init_ctx()`

The initialization chain follows the standard RDMA pattern:

```
ibv_open_device(rxe0)
  → ibv_alloc_pd()          // Protection Domain
    → ibv_create_cq()       // Completion Queue (256 entries)
      → aligned_alloc()     // Page-aligned buffer
        → ibv_reg_mr()      // Register buffer for RDMA (LOCAL_WRITE | REMOTE_WRITE)
          → ibv_query_gid() // Find valid GID (prefer index 1 = RoCEv2)
            → ibv_create_qp(IBV_QPT_UC)  // Unreliable Connected QP
```

Critical detail: the MR access flags include `IBV_ACCESS_REMOTE_WRITE` — this allows the remote peer to RDMA Write into our buffer.

### UC QP State Machine

```
  RESET ──► INIT ──► RTR ──► RTS
              │         │       │
              │ attrs:   │ attrs:│ attrs:
              │ port     │ mtu   │ sq_psn
              │ pkey     │ dest  │
              │ access   │ qpn   │
              │          │ ah    │
              │          │ rq_psn│
```

**UC-specific notes (vs. RC):**
- RTR needs `rq_psn` (the PSN we expect to receive)
- RTR does NOT need `max_dest_rd_atomic` or `min_rnr_timer` (RC-only)
- RTS does NOT need `timeout`, `retry_cnt`, `rnr_retry`, `max_rd_atomic` (RC-only)
- `ah_attr.is_global = 1` with GRH is required for RoCE (uses GID, not LID)

### TCP Exchange Protocol

```
Server                           Client
listen(18515)
accept() ◄──────────── connect()
write(local_info) ────────────► read(remote_info)
read(remote_info) ◄──────────── write(local_info)
close()                          close()
```

Server sends first, client receives first. This ordering prevents deadlock.

### Post Operations

**`rdma_post_write_imm()`** — the core operation for SemiRDMA:
```c
wr.opcode     = IBV_WR_RDMA_WRITE_WITH_IMM;
wr.imm_data   = htonl(imm_data);   // Network byte order!
wr.wr.rdma.remote_addr = remote->addr;
wr.wr.rdma.rkey        = remote->rkey;
```

The NIC reads from our local buffer (`sge.addr`) and writes directly to the remote buffer (`remote_addr`) — zero-copy. The `imm_data` is delivered via the receiver's CQE.

**`rdma_post_recv()`** — posted with NULL scatter list:
```c
wr.sg_list = NULL;
wr.num_sge = 0;
```
For Write-with-Immediate, the Receive WR exists solely to generate a CQE. The data goes to the RDMA address, not the Receive WR's scatter list.

---

## Test 1: test_uc_write_imm.c

### Purpose
Verify that UC Write-with-Immediate generates a receiver-side CQE on SoftRoCE.

### Flow

| Step | Server | Client |
|------|--------|--------|
| 1 | Fill buffer with `0xAA` | Fill buffer with `0x42` |
| 2 | Post Receive WR (wr_id=1) | — |
| 3 | TCP exchange | TCP exchange |
| 4 | QP → RTR | QP → RTR → RTS |
| 5 | Poll CQ (10s timeout) | Post Write-with-Immediate (imm=`0xDEADBEEF`) |
| 6 | Check: CQE opcode, imm_data, buffer | Check: send CQE status |

### Key Verification Points

```c
// Server checks 4 things:
pass_status = (wc.status == IBV_WC_SUCCESS);
pass_opcode = (wc.opcode == IBV_WC_RECV_RDMA_WITH_IMM);
pass_imm    = (ntohl(wc.imm_data) == 0xDEADBEEF);
pass_buf    = (buf[0] == 0x42);
```

### Why imm_data uses htonl/ntohl

The `ibv_send_wr.imm_data` field is defined as `__be32` (big-endian). Setting it requires `htonl()` on the sender. The receiver's `ibv_wc.imm_data` is also big-endian, so `ntohl()` is needed to recover the original value.

---

## Test 2: test_ghost_gradient.c

### Purpose
Verify that when a UC Write-with-Immediate fails (no Receive WR), the buffer retains stale data.

### Key Design: Persistent TCP Connection

Unlike Test 1, this test keeps the TCP connection open for synchronization across two rounds:

```c
// Server
int tcp = tcp_listen_accept(TCP_PORT);  // Accept, keep open
// ... Round 1 ...
tcp_signal(tcp);  // Tell client: "go ahead with Round 2"
tcp_wait(tcp);    // Wait for client: "Round 2 done"
// ... Check buffer ...
close(tcp);
```

This avoids race conditions between rounds — each side waits for an explicit sync byte before proceeding.

### The Critical Moment

```c
// Server: Round 2
// *** Do NOT post Receive WR ***
// (simulating: what if the receiver doesn't expect this data?)
tcp_signal(tcp);  // Tell client to proceed
```

The server deliberately skips `rdma_post_recv()` before Round 2. This tests what happens when Write-with-Immediate has no Receive WR to consume.

### Observed Result

The RDMA data write **succeeded** (buffer → `0xFF`) but no CQE was generated. This is because on SoftRoCE, the RDMA Write portion and the completion notification are independent:

```
Write-with-Immediate = RDMA Write (always executes) + CQE (requires Receive WR)
```

### Design Implication

In the real SemiRDMA system, the ghost gradient problem comes from **packet loss** (not missing Receive WRs). When packets are lost:
1. PSN goes out of sync
2. Subsequent packets are silently discarded
3. Buffer has partial old data = true ghost gradient

The no-RQ-WR test still proves something crucial: **CQE is the only reliable delivery signal.** You cannot inspect buffer content to determine delivery.

---

## Test 3: test_wqe_rate.c

### Purpose
Measure WQE posting rate at different chunk sizes to inform the chunk size selection in RQ1.

### Benchmark Structure

Uses plain RDMA Write (no Immediate) to measure raw WQE rate without receiver-side overhead.

```c
// Batched posting with periodic signaling
for (int i = 0; i < NUM_ITERS; i++) {
    bool sig = ((i + 1) % SIG_INTERVAL == 0) || (i == NUM_ITERS - 1);
    rdma_post_write(&ctx, &remote, chunk, i, sig);
    if (sig) {
        rdma_poll_cq_spin(ctx.cq, &wc);  // Drain CQ to free SQ slots
    }
}
```

**Why signal every 64th WQE?**
- Signaling every WQE adds CQ polling overhead
- Not signaling means SQ overflows (SQ depth = 256)
- Signal every 64th: posts 64 WQEs, drains 1 CQE (which implicitly completes all 64)

**Why use `rdma_poll_cq_spin` instead of `rdma_poll_cq`?**
- `rdma_poll_cq` has a timeout check with `clock_gettime()` calls — adds measurement noise
- `rdma_poll_cq_spin` busy-waits without timing overhead — better for tight benchmark loops

### Server Role

The server is passive — it just provides a target buffer and waits for the client to finish:

```c
// Server: register 16MB buffer, go to RTR, wait for TCP signal
rdma_init_ctx(&ctx, dev_name, LARGE_BUF, 16, 16);
// ... exchange, RTR ...
// Wait for client "done" signal via TCP
```

No Receive WRs needed because plain RDMA Write doesn't consume them.
