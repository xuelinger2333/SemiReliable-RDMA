/*
 * uc_qp_engine.h — RAII C++ wrapper for UC/RC QP lifecycle
 *
 * Named historically for the UC use-case, but dual-mode since 2026-04-25:
 * switching ``qp_type`` to "rc" reuses the exact same Write-with-Immediate
 * data path with the NIC's hardware-implemented RC reliability (ACK,
 * retransmit, retry-exhausted error).  This is what NCCL does internally
 * too — both paths call ``ibv_create_qp`` with IBV_QPT_{UC,RC} on the same
 * NIC — so the RC path here is HW-official, not a self-built reliability
 * layer.
 *
 * Encapsulates the Phase 1 rdma_common.h patterns into a C++ class:
 *   - Device open, PD, CQ, buffer, MR, UC/RC QP creation (constructor)
 *   - QP state transitions INIT → RTR → RTS (bring_up)
 *   - Offset-based RDMA Write / Write-with-Immediate (post_write)
 *   - Zero-length Recv WR posting (post_recv)
 *   - Batch CQ polling with timeout (poll_cq)
 *   - RAII cleanup in destructor
 *
 * RC vs UC differences (handled transparently by bring_up via qp_type):
 *   - RTR adds max_dest_rd_atomic, min_rnr_timer
 *   - RTS adds timeout, retry_cnt, rnr_retry, max_rd_atomic
 *   - Wire behavior: RC ACKs every segment, retransmits on loss; UC fires
 *     and forgets.  On lossy wire, RC send-CQE for a dropped segment is
 *     delayed by ``4.096us * 2^timeout`` per retry; if retry_cnt is
 *     exhausted, the send-CQE comes back with ``IBV_WC_RETRY_EXC_ERR``
 *     and the QP transitions to ERR state (non-recoverable).
 *
 * Key change from Phase 1: post_write uses (local_offset, remote_offset, length)
 * instead of always starting from the buffer base.  This enables ChunkManager
 * to post independent WRs for each chunk within a single MR.
 */

#pragma once

#include <infiniband/verbs.h>

#include "transport/chunk_manager.h"

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
    // Allocate all RDMA resources: device, PD, CQ, buffer, MR, UC/RC QP.
    // buffer_bytes is rounded up to page alignment internally.
    // Throws std::runtime_error on any failure.
    //
    // Params:
    //   gid_index = -1 (default) means auto-discover by trying {1, 0, 2, 3}
    //     and using the first non-zero GID.  A specific non-negative value
    //     pins to that GID index (e.g. 3 for RoCE v2 IPv4-mapped, required
    //     when routing through an XDP middlebox that relies on kernel ARP).
    //   qp_type = "uc" (default) creates an Unreliable Connected QP;
    //     qp_type = "rc" creates a Reliable Connected QP (HW-level retx).
    //     Any other value throws.
    //   rc_timeout (RC only; ignored for UC) = Mellanox ``timeout`` attr
    //     in log2(4.096 us) units.  14 ≈ 67 ms per retry; valid range 0..31.
    //   rc_retry_cnt (RC only) = number of retransmit attempts before the
    //     send-CQE returns IBV_WC_RETRY_EXC_ERR.  7 is the IB max.
    //   rc_rnr_retry (RC only) = RNR retransmit attempts.  7 is infinite.
    //   rc_min_rnr_timer (RC only) = min RNR NAK timer, log2 encoding.
    //     12 ≈ 0.64 ms (Mellanox OFED default).
    //   rc_max_rd_atomic (RC only) = max outstanding RDMA Read + atomic ops
    //     initiated by this QP.  We only issue Write-with-Imm so 0 suffices,
    //     but some HW rejects 0; 1 is the safe minimum.
    UCQPEngine(const std::string& dev_name,
               size_t buffer_bytes,
               int    sq_depth,
               int    rq_depth,
               int    gid_index        = -1,
               const std::string& qp_type = "uc",
               int    rc_timeout       = 14,
               int    rc_retry_cnt     = 7,
               int    rc_rnr_retry     = 7,
               int    rc_min_rnr_timer = 12,
               int    rc_max_rd_atomic = 1);

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

    // Fast path: post an entire bucket's worth of chunks in a single C++ call,
    // using wr.next chaining so each wave of up to (sq_depth_throttle - 1) WRs
    // goes to the NIC via ONE ibv_post_send invocation.  This eliminates
    // ~10K Python ↔ C++ boundary crossings per bucket (RDMA data path is
    // userspace MMIO; the per-WR overhead at the Python layer was a pure
    // interpreter / pybind cost, not a syscall cost).
    //
    // Generates an internal ChunkSet [base_offset .. base_offset+total_bytes)
    // with stride = chunk_bytes, returns it for downstream wait_for_ratio
    // bookkeeping.  Drains tail SEND CQEs before returning so the next
    // bucket starts with inflight=0.
    //
    // wr_id_base lets the caller allocate a unique wr_id range
    // [wr_id_base .. wr_id_base + n_chunks) per bucket so post-mortem
    // debugging can cross-reference SEND CQEs with their originating chunk.
    //
    // Throws on any ibv_post_send / ibv_poll_cq error or on tail-drain timeout.
    ChunkSet post_bucket_chunks(size_t          base_offset,
                                size_t          remote_base_offset,
                                size_t          total_bytes,
                                size_t          chunk_bytes,
                                int             sq_depth_throttle,
                                int             drain_timeout_ms,
                                const RemoteMR& remote,
                                bool            with_imm,
                                uint64_t        wr_id_base);

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
    // Captured from ibv_query_port at construction; used as path_mtu in RTR.
    // Hardcoding IBV_MTU_1024 here used to silently 4×-fragment every IB
    // packet on CX-5/6 active_mtu=4096 links, killing UC delivery.
    enum ibv_mtu  active_mtu_ = IBV_MTU_1024;

    // QP mode + RC params (captured at construction, consumed by bring_up).
    // qp_type_ is the enum form of the string arg; RC params are ignored
    // in UC mode.
    enum class QPKind { UC, RC };
    QPKind qp_type_         = QPKind::UC;
    int    rc_timeout_      = 14;
    int    rc_retry_cnt_    = 7;
    int    rc_rnr_retry_    = 7;
    int    rc_min_rnr_timer_ = 12;
    int    rc_max_rd_atomic_ = 1;

    // Tracks outstanding Receive WRs for post_recv_batch / outstanding_recv.
    // Incremented by post_recv / post_recv_batch, decremented in poll_cq
    // whenever a receive-side CQE (IBV_WC_RECV* family) is drained.
    std::atomic<int> outstanding_recv_{0};

    void cleanup();
};

} // namespace semirdma
