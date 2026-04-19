/*
 * uc_qp_engine.cpp — UCQPEngine implementation
 *
 * Direct translation of Phase 1 rdma_common.h (C) into C++ RAII,
 * with the critical addition of offset-based post_write.
 *
 * Reference line numbers refer to tests/phase1/rdma_common.h.
 */

#include "transport/uc_qp_engine.h"
#include "utils/logging.h"
#include "utils/timing.h"

#include <arpa/inet.h>   // htonl, ntohl

#include <algorithm>
#include <cstdlib>       // aligned_alloc, free
#include <cstring>       // memset, memcmp
#include <stdexcept>
#include <string>

namespace semirdma {

// ---------------------------------------------------------------------------
// Helper: round up to page boundary (aligned_alloc requirement)
// ---------------------------------------------------------------------------
static size_t round_up_page(size_t bytes)
{
    constexpr size_t PAGE = 4096;
    return (bytes + PAGE - 1) & ~(PAGE - 1);
}

// ---------------------------------------------------------------------------
// Constructor  (ref: rdma_init_ctx, rdma_open_device, rdma_find_gid)
// ---------------------------------------------------------------------------
UCQPEngine::UCQPEngine(const std::string& dev_name,
                       size_t buffer_bytes,
                       int    sq_depth,
                       int    rq_depth)
{
    std::memset(&gid_, 0, sizeof(gid_));

    // --- Open device (ref: lines 91-109) ---
    int num_devices = 0;
    struct ibv_device** dev_list = ibv_get_device_list(&num_devices);
    if (!dev_list || num_devices == 0) {
        throw std::runtime_error("No RDMA devices found. Is SoftRoCE configured?");
    }

    for (int i = 0; i < num_devices; i++) {
        if (dev_name == ibv_get_device_name(dev_list[i])) {
            ctx_ = ibv_open_device(dev_list[i]);
            break;
        }
    }
    ibv_free_device_list(dev_list);
    if (!ctx_) {
        throw std::runtime_error("Device '" + dev_name +
                                 "' not found. Run: rdma link show");
    }
    SEMIRDMA_LOG_INFO("Opened device: %s", dev_name.c_str());

    // --- Protection Domain (ref: line 149) ---
    pd_ = ibv_alloc_pd(ctx_);
    if (!pd_) { cleanup(); throw std::runtime_error("ibv_alloc_pd failed"); }

    // --- Completion Queue (ref: line 152) ---
    int cq_size = std::max(sq_depth + rq_depth, 256);
    cq_ = ibv_create_cq(ctx_, cq_size, nullptr, nullptr, 0);
    if (!cq_) { cleanup(); throw std::runtime_error("ibv_create_cq failed"); }

    // --- Buffer allocation (ref: lines 156-158) ---
    buf_size_ = round_up_page(buffer_bytes);
    buf_ = static_cast<uint8_t*>(std::aligned_alloc(4096, buf_size_));
    if (!buf_) { cleanup(); throw std::runtime_error("aligned_alloc failed"); }
    std::memset(buf_, 0, buf_size_);

    // --- Memory Region (ref: lines 160-161) ---
    int mr_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
    mr_ = ibv_reg_mr(pd_, buf_, buf_size_, mr_flags);
    if (!mr_) { cleanup(); throw std::runtime_error("ibv_reg_mr failed"); }

    // --- GID discovery (ref: lines 113-133) ---
    {
        struct ibv_port_attr pa;
        if (ibv_query_port(ctx_, ib_port_, &pa) != 0) {
            cleanup();
            throw std::runtime_error("ibv_query_port failed");
        }
        int try_idx[] = {1, 0, 2, 3};
        union ibv_gid zero_gid;
        std::memset(&zero_gid, 0, sizeof(zero_gid));

        bool found = false;
        for (int idx : try_idx) {
            if (idx >= static_cast<int>(pa.gid_tbl_len)) continue;
            union ibv_gid g;
            if (ibv_query_gid(ctx_, ib_port_, idx, &g) != 0) continue;
            if (std::memcmp(&g, &zero_gid, sizeof(g)) == 0) continue;
            gid_       = g;
            gid_index_ = idx;
            found      = true;
            SEMIRDMA_LOG_INFO("Using GID index %d", idx);
            break;
        }
        if (!found) {
            cleanup();
            throw std::runtime_error("No valid GID found on port " +
                                     std::to_string(ib_port_));
        }
    }

    // --- UC QP creation (ref: lines 167-179) ---
    {
        struct ibv_qp_init_attr qp_attr;
        std::memset(&qp_attr, 0, sizeof(qp_attr));
        qp_attr.send_cq             = cq_;
        qp_attr.recv_cq             = cq_;
        qp_attr.cap.max_send_wr     = sq_depth;
        qp_attr.cap.max_recv_wr     = rq_depth;
        qp_attr.cap.max_send_sge    = 1;
        qp_attr.cap.max_recv_sge    = 1;
        qp_attr.qp_type             = IBV_QPT_UC;

        qp_ = ibv_create_qp(pd_, &qp_attr);
        if (!qp_) { cleanup(); throw std::runtime_error("ibv_create_qp (UC) failed"); }
        SEMIRDMA_LOG_INFO("Created UC QP, qpn=%u", qp_->qp_num);
    }
}

// ---------------------------------------------------------------------------
// Destructor  (ref: rdma_cleanup, lines 457-466)
// ---------------------------------------------------------------------------
UCQPEngine::~UCQPEngine()
{
    cleanup();
}

void UCQPEngine::cleanup()
{
    if (qp_)  { ibv_destroy_qp(qp_);    qp_  = nullptr; }
    if (mr_)  { ibv_dereg_mr(mr_);       mr_  = nullptr; }
    if (cq_)  { ibv_destroy_cq(cq_);     cq_  = nullptr; }
    if (pd_)  { ibv_dealloc_pd(pd_);     pd_  = nullptr; }
    if (ctx_) { ibv_close_device(ctx_);   ctx_ = nullptr; }
    if (buf_) { std::free(buf_);          buf_ = nullptr; }
}

// ---------------------------------------------------------------------------
// bring_up  (ref: rdma_modify_qp_to_init/rtr/rts, lines 194-246)
// ---------------------------------------------------------------------------
void UCQPEngine::bring_up(const RemoteQpInfo& remote)
{
    // --- INIT (ref: lines 195-208) ---
    {
        struct ibv_qp_attr attr;
        std::memset(&attr, 0, sizeof(attr));
        attr.qp_state        = IBV_QPS_INIT;
        attr.pkey_index      = 0;
        attr.port_num        = ib_port_;
        attr.qp_access_flags = IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_LOCAL_WRITE;

        int flags = IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS;
        int ret = ibv_modify_qp(qp_, &attr, flags);
        if (ret) {
            throw std::runtime_error(std::string("QP->INIT failed: ") +
                                     strerror(errno));
        }
        SEMIRDMA_LOG_INFO("QP -> INIT");
    }

    // --- RTR (ref: lines 211-232) ---
    {
        struct ibv_qp_attr attr;
        std::memset(&attr, 0, sizeof(attr));
        attr.qp_state                = IBV_QPS_RTR;
        attr.path_mtu                = IBV_MTU_1024;
        attr.dest_qp_num             = remote.qpn;
        attr.ah_attr.is_global       = 1;
        attr.ah_attr.grh.dgid        = remote.gid;
        attr.ah_attr.grh.sgid_index  = gid_index_;
        attr.ah_attr.grh.hop_limit   = 1;
        attr.ah_attr.sl              = 0;
        attr.ah_attr.src_path_bits   = 0;
        attr.ah_attr.port_num        = ib_port_;
        attr.rq_psn                  = 0;

        int flags = IBV_QP_STATE | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN
                  | IBV_QP_AV | IBV_QP_RQ_PSN;
        int ret = ibv_modify_qp(qp_, &attr, flags);
        if (ret) {
            throw std::runtime_error(std::string("QP->RTR failed: ") +
                                     strerror(errno));
        }
        SEMIRDMA_LOG_INFO("QP -> RTR (dest_qpn=%u)", remote.qpn);
    }

    // --- RTS (ref: lines 235-246) ---
    {
        struct ibv_qp_attr attr;
        std::memset(&attr, 0, sizeof(attr));
        attr.qp_state = IBV_QPS_RTS;
        attr.sq_psn   = 0;

        int flags = IBV_QP_STATE | IBV_QP_SQ_PSN;
        int ret = ibv_modify_qp(qp_, &attr, flags);
        if (ret) {
            throw std::runtime_error(std::string("QP->RTS failed: ") +
                                     strerror(errno));
        }
        SEMIRDMA_LOG_INFO("QP -> RTS (sq_psn=0)");
    }
}

// ---------------------------------------------------------------------------
// post_write  (ref: rdma_post_write_imm / rdma_post_write, lines 329-376)
// ---------------------------------------------------------------------------
uint64_t UCQPEngine::post_write(uint64_t        wr_id,
                                size_t          local_offset,
                                size_t          remote_offset,
                                size_t          length,
                                const RemoteMR& remote,
                                bool            with_imm,
                                uint32_t        imm_data)
{
    struct ibv_sge sge;
    std::memset(&sge, 0, sizeof(sge));
    sge.addr   = reinterpret_cast<uint64_t>(buf_ + local_offset);
    sge.length = static_cast<uint32_t>(length);
    sge.lkey   = mr_->lkey;

    struct ibv_send_wr wr, *bad_wr = nullptr;
    std::memset(&wr, 0, sizeof(wr));
    wr.wr_id      = wr_id;
    wr.sg_list    = &sge;
    wr.num_sge    = 1;
    wr.opcode     = with_imm ? IBV_WR_RDMA_WRITE_WITH_IMM : IBV_WR_RDMA_WRITE;
    wr.send_flags = IBV_SEND_SIGNALED;

    if (with_imm) {
        wr.imm_data = htonl(imm_data);   // network byte order
    }

    wr.wr.rdma.remote_addr = remote.addr + remote_offset;
    wr.wr.rdma.rkey        = remote.rkey;

    int ret = ibv_post_send(qp_, &wr, &bad_wr);
    if (ret) {
        throw std::runtime_error(std::string("ibv_post_send failed: ") +
                                 strerror(ret));
    }
    return wr_id;
}

// ---------------------------------------------------------------------------
// post_recv  (ref: rdma_post_recv, lines 315-326)
// ---------------------------------------------------------------------------
void UCQPEngine::post_recv(uint64_t wr_id)
{
    struct ibv_recv_wr wr, *bad_wr = nullptr;
    std::memset(&wr, 0, sizeof(wr));
    wr.wr_id   = wr_id;
    wr.sg_list = nullptr;
    wr.num_sge = 0;

    int ret = ibv_post_recv(qp_, &wr, &bad_wr);
    if (ret) {
        throw std::runtime_error(std::string("ibv_post_recv failed: ") +
                                 strerror(ret));
    }
    outstanding_recv_.fetch_add(1, std::memory_order_relaxed);
}

// ---------------------------------------------------------------------------
// post_recv_batch — chain n zero-length Recv WRs into a single ibv_post_recv
// ---------------------------------------------------------------------------
void UCQPEngine::post_recv_batch(int n, uint64_t base_wr_id)
{
    if (n <= 0) return;

    std::vector<struct ibv_recv_wr> wrs(static_cast<size_t>(n));
    std::memset(wrs.data(), 0, wrs.size() * sizeof(struct ibv_recv_wr));

    for (int i = 0; i < n; i++) {
        wrs[i].wr_id   = base_wr_id + static_cast<uint64_t>(i);
        wrs[i].sg_list = nullptr;
        wrs[i].num_sge = 0;
        wrs[i].next    = (i + 1 < n) ? &wrs[i + 1] : nullptr;
    }

    struct ibv_recv_wr* bad_wr = nullptr;
    int ret = ibv_post_recv(qp_, &wrs[0], &bad_wr);
    if (ret) {
        // bad_wr points to the first WR that was not posted.  Partial posts
        // on ibv_post_recv are allowed by the verbs API; count only the WRs
        // that landed before the failure.
        int posted = 0;
        for (int i = 0; i < n; i++) {
            if (&wrs[i] == bad_wr) break;
            posted++;
        }
        if (posted > 0) {
            outstanding_recv_.fetch_add(posted, std::memory_order_relaxed);
        }
        throw std::runtime_error(std::string("ibv_post_recv (batch) failed: ") +
                                 strerror(ret));
    }
    outstanding_recv_.fetch_add(n, std::memory_order_relaxed);
}

// ---------------------------------------------------------------------------
// poll_cq  (ref: rdma_poll_cq, lines 384-399)
// ---------------------------------------------------------------------------
std::vector<Completion> UCQPEngine::poll_cq(int max_n, int timeout_ms)
{
    std::vector<struct ibv_wc> wcs(max_n);
    std::vector<Completion> results;

    Stopwatch sw;

    do {
        int n = ibv_poll_cq(cq_, max_n, wcs.data());
        if (n < 0) {
            throw std::runtime_error("ibv_poll_cq returned error");
        }
        int recv_cqe = 0;
        for (int i = 0; i < n; i++) {
            Completion c;
            c.wr_id    = wcs[i].wr_id;
            c.opcode   = wcs[i].opcode;
            c.status   = wcs[i].status;
            c.imm_data = (wcs[i].opcode == IBV_WC_RECV_RDMA_WITH_IMM)
                             ? ntohl(wcs[i].imm_data)
                             : 0;
            results.push_back(c);
            if (wcs[i].opcode == IBV_WC_RECV_RDMA_WITH_IMM ||
                wcs[i].opcode == IBV_WC_RECV) {
                recv_cqe++;
            }
        }
        if (recv_cqe > 0) {
            outstanding_recv_.fetch_sub(recv_cqe, std::memory_order_relaxed);
        }

        // Non-blocking mode (timeout_ms == 0): single poll, return immediately
        if (timeout_ms == 0) break;

        // If we got results, return them
        if (!results.empty()) break;

    } while (sw.elapsed_ms() < static_cast<double>(timeout_ms));

    return results;
}

// ---------------------------------------------------------------------------
// Accessors
// ---------------------------------------------------------------------------
RemoteMR UCQPEngine::local_mr_info() const
{
    return RemoteMR{reinterpret_cast<uint64_t>(buf_), mr_->rkey};
}

RemoteQpInfo UCQPEngine::local_qp_info() const
{
    RemoteQpInfo info;
    info.qpn = qp_->qp_num;
    info.gid = gid_;
    return info;
}

} // namespace semirdma
