/*
 * uc_qp_engine.h — RAII C++ wrapper for UC QP lifecycle
 *
 * Encapsulates the Phase 1 rdma_common.h patterns into a C++ class:
 *   - Device open, PD, CQ, buffer, MR, UC QP creation (constructor)
 *   - QP state transitions INIT → RTR → RTS (bring_up)
 *   - Offset-based RDMA Write / Write-with-Immediate (post_write)
 *   - Zero-length Recv WR posting (post_recv)
 *   - Batch CQ polling with timeout (poll_cq)
 *   - RAII cleanup in destructor
 *
 * Key change from Phase 1: post_write uses (local_offset, remote_offset, length)
 * instead of always starting from the buffer base.  This enables ChunkManager
 * to post independent WRs for each chunk within a single MR.
 */

#pragma once

#include <infiniband/verbs.h>

#include <atomic>
#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>

namespace semirdma {

// Remote QP connection parameters (exchanged via TCP bootstrap)
struct RemoteQpInfo {
    uint32_t      qpn;
    union ibv_gid gid;
};

// Remote Memory Region descriptor (exchanged via TCP bootstrap)
struct RemoteMR {
    uint64_t addr;
    uint32_t rkey;
};

// Simplified view of a CQ completion event
struct Completion {
    uint64_t           wr_id;
    enum ibv_wc_opcode opcode;
    enum ibv_wc_status status;
    uint32_t           imm_data;  // Host byte order; valid for RECV_RDMA_WITH_IMM
};

class UCQPEngine {
public:
    // Allocate all RDMA resources: device, PD, CQ, buffer, MR, UC QP.
    // buffer_bytes is rounded up to page alignment internally.
    // Throws std::runtime_error on any failure.
    UCQPEngine(const std::string& dev_name,
               size_t buffer_bytes,
               int    sq_depth,
               int    rq_depth);

    ~UCQPEngine();

    UCQPEngine(const UCQPEngine&)            = delete;
    UCQPEngine& operator=(const UCQPEngine&) = delete;

    // Transition QP: RESET → INIT → RTR → RTS.
    // Throws std::runtime_error on failure.
    void bring_up(const RemoteQpInfo& remote);

    // Post a Write or Write-with-Immediate WR.
    // local_offset / remote_offset are byte offsets within the respective MR buffers.
    // Returns wr_id.  Throws on ibv_post_send failure.
    uint64_t post_write(uint64_t        wr_id,
                        size_t          local_offset,
                        size_t          remote_offset,
                        size_t          length,
                        const RemoteMR& remote,
                        bool            with_imm,
                        uint32_t        imm_data = 0);

    // Post a zero-length Receive WR (required before each Write-with-Imm arrives).
    // Throws on ibv_post_recv failure.
    void post_recv(uint64_t wr_id);

    // Post n zero-length Receive WRs with sequential wr_ids
    // [base_wr_id, base_wr_id + n), chained via ibv_recv_wr::next so that
    // a single ibv_post_recv syscall drains them all.  No-op when n <= 0.
    // Throws on ibv_post_recv failure.
    void post_recv_batch(int n, uint64_t base_wr_id = 0);

    // Number of Receive WRs posted minus receive CQEs consumed so far
    // (IBV_WC_RECV and IBV_WC_RECV_RDMA_WITH_IMM).  Lets Python / higher
    // layers re-fill the RQ before it drains.  Approximate — non-atomic
    // read of an atomic counter; good enough for throttling decisions.
    int outstanding_recv() const { return outstanding_recv_.load(std::memory_order_relaxed); }

    // Poll CQ for up to max_n completions.
    // timeout_ms == 0: single non-blocking ibv_poll_cq call.
    // timeout_ms  > 0: loop until at least one result or timeout.
    // Returns 0..N completions.  Throws on ibv_poll_cq error (n < 0).
    std::vector<Completion> poll_cq(int max_n, int timeout_ms = 0);

    // Accessors
    uint8_t* local_buf()  const { return buf_; }
    size_t   buf_bytes()  const { return buf_size_; }
    uint32_t qpn()        const { return qp_ ? qp_->qp_num : 0; }

    RemoteMR local_mr_info() const;
    RemoteQpInfo local_qp_info() const;

private:
    ibv_context*  ctx_       = nullptr;
    ibv_pd*       pd_        = nullptr;
    ibv_cq*       cq_        = nullptr;
    ibv_qp*       qp_        = nullptr;
    ibv_mr*       mr_        = nullptr;
    uint8_t*      buf_       = nullptr;
    size_t        buf_size_  = 0;
    int           ib_port_   = 1;
    int           gid_index_ = -1;
    union ibv_gid gid_;

    // Tracks outstanding Receive WRs for post_recv_batch / outstanding_recv.
    // Incremented by post_recv / post_recv_batch, decremented in poll_cq
    // whenever a receive-side CQE (IBV_WC_RECV* family) is drained.
    std::atomic<int> outstanding_recv_{0};

    void cleanup();
};

} // namespace semirdma
