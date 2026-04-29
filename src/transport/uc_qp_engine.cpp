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
#include <chrono>        // steady_clock for per-WR pacing
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
                       int    rq_depth,
                       int    gid_index_pref,
                       const std::string& qp_type,
                       int    rc_timeout,
                       int    rc_retry_cnt,
                       int    rc_rnr_retry,
                       int    rc_min_rnr_timer,
                       int    rc_max_rd_atomic)
{
    std::memset(&gid_, 0, sizeof(gid_));

    // Parse & validate qp_type.  We intentionally use a tiny string map so
    // Python callers don't need to import a C++ enum binding.
    if (qp_type == "uc") {
        qp_type_ = QPKind::UC;
    } else if (qp_type == "rc") {
        qp_type_ = QPKind::RC;
    } else {
        throw std::runtime_error("qp_type must be 'uc' or 'rc', got '" +
                                 qp_type + "'");
    }

    // Bounds-check RC attrs — ibv_modify_qp will fail cryptically on
    // out-of-range values, so catch them here where we have the name.
    if (qp_type_ == QPKind::RC) {
        if (rc_timeout < 0 || rc_timeout > 31) {
            throw std::runtime_error("rc_timeout must be in [0, 31]");
        }
        if (rc_retry_cnt < 0 || rc_retry_cnt > 7) {
            throw std::runtime_error("rc_retry_cnt must be in [0, 7]");
        }
        if (rc_rnr_retry < 0 || rc_rnr_retry > 7) {
            throw std::runtime_error("rc_rnr_retry must be in [0, 7]");
        }
        if (rc_min_rnr_timer < 0 || rc_min_rnr_timer > 31) {
            throw std::runtime_error("rc_min_rnr_timer must be in [0, 31]");
        }
        if (rc_max_rd_atomic < 0 || rc_max_rd_atomic > 16) {
            throw std::runtime_error("rc_max_rd_atomic must be in [0, 16]");
        }
    }
    rc_timeout_       = rc_timeout;
    rc_retry_cnt_     = rc_retry_cnt;
    rc_rnr_retry_     = rc_rnr_retry;
    rc_min_rnr_timer_ = rc_min_rnr_timer;
    rc_max_rd_atomic_ = rc_max_rd_atomic;

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

    // --- GID selection (ref: lines 113-133) ---
    // When gid_index_pref >= 0, pin to that specific index; error out if it's
    // zero / invalid.  When < 0, fall back to the old auto-discovery order
    // {1, 0, 2, 3}.  Pinning is necessary e.g. when routing through an XDP
    // middlebox on a shared switch: idx 1 (IPv6 link-local) has its dst MAC
    // derived by mlx5 HW from the GID via EUI-64 reverse (bypassing kernel
    // ARP), while idx 3 (RoCE v2 IPv4-mapped ::ffff:10.10.1.x) DOES consult
    // kernel ARP and honors the middlebox's ARP spoof.
    {
        struct ibv_port_attr pa;
        if (ibv_query_port(ctx_, ib_port_, &pa) != 0) {
            cleanup();
            throw std::runtime_error("ibv_query_port failed");
        }
        active_mtu_ = pa.active_mtu;
        SEMIRDMA_LOG_INFO("Port active_mtu=%d (IBV_MTU_%d enum)",
                          active_mtu_,
                          active_mtu_ == IBV_MTU_256  ? 256  :
                          active_mtu_ == IBV_MTU_512  ? 512  :
                          active_mtu_ == IBV_MTU_1024 ? 1024 :
                          active_mtu_ == IBV_MTU_2048 ? 2048 :
                          active_mtu_ == IBV_MTU_4096 ? 4096 : -1);
        union ibv_gid zero_gid;
        std::memset(&zero_gid, 0, sizeof(zero_gid));

        if (gid_index_pref >= 0) {
            // Explicit pin — fail hard if unusable so config mistakes surface
            // immediately instead of silently auto-selecting the wrong GID.
            if (gid_index_pref >= static_cast<int>(pa.gid_tbl_len)) {
                cleanup();
                throw std::runtime_error("gid_index " + std::to_string(gid_index_pref) +
                                         " exceeds gid_tbl_len=" +
                                         std::to_string(pa.gid_tbl_len));
            }
            union ibv_gid g;
            if (ibv_query_gid(ctx_, ib_port_, gid_index_pref, &g) != 0) {
                cleanup();
                throw std::runtime_error("ibv_query_gid failed for pinned idx " +
                                         std::to_string(gid_index_pref));
            }
            if (std::memcmp(&g, &zero_gid, sizeof(g)) == 0) {
                cleanup();
                throw std::runtime_error("GID at pinned idx " +
                                         std::to_string(gid_index_pref) +
                                         " is zero — requested GID type not configured");
            }
            gid_       = g;
            gid_index_ = gid_index_pref;
            SEMIRDMA_LOG_INFO("Using GID index %d (pinned via config)", gid_index_);
        } else {
            int try_idx[] = {1, 0, 2, 3};
            bool found = false;
            for (int idx : try_idx) {
                if (idx >= static_cast<int>(pa.gid_tbl_len)) continue;
                union ibv_gid g;
                if (ibv_query_gid(ctx_, ib_port_, idx, &g) != 0) continue;
                if (std::memcmp(&g, &zero_gid, sizeof(g)) == 0) continue;
                gid_       = g;
                gid_index_ = idx;
                found      = true;
                SEMIRDMA_LOG_INFO("Using GID index %d (auto-discovered)", idx);
                break;
            }
            if (!found) {
                cleanup();
                throw std::runtime_error("No valid GID found on port " +
                                         std::to_string(ib_port_));
            }
        }
    }

    // --- QP creation (ref: lines 167-179) ---
    {
        struct ibv_qp_init_attr qp_attr;
        std::memset(&qp_attr, 0, sizeof(qp_attr));
        qp_attr.send_cq             = cq_;
        qp_attr.recv_cq             = cq_;
        qp_attr.cap.max_send_wr     = sq_depth;
        qp_attr.cap.max_recv_wr     = rq_depth;
        qp_attr.cap.max_send_sge    = 1;
        qp_attr.cap.max_recv_sge    = 1;
        qp_attr.qp_type             = (qp_type_ == QPKind::RC)
                                         ? IBV_QPT_RC : IBV_QPT_UC;

        qp_ = ibv_create_qp(pd_, &qp_attr);
        if (!qp_) {
            cleanup();
            throw std::runtime_error(
                std::string("ibv_create_qp (") +
                (qp_type_ == QPKind::RC ? "RC" : "UC") + ") failed");
        }
        SEMIRDMA_LOG_INFO("Created %s QP, qpn=%u",
                          qp_type_ == QPKind::RC ? "RC" : "UC", qp_->qp_num);
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
    // RC adds max_dest_rd_atomic + min_rnr_timer on top of the UC attrs.
    // Per IB spec §11.2.1: these are REQUIRED for RC, silently ignored for UC.
    {
        struct ibv_qp_attr attr;
        std::memset(&attr, 0, sizeof(attr));
        attr.qp_state                = IBV_QPS_RTR;
        // Use queried active_mtu, NOT hardcoded 1024.  On CX-5/6 with
        // active_mtu=4096, hardcoding 1024 fragments every IB write into
        // 4× more packets, and any single packet loss in UC kills the
        // whole multi-packet message via PSN gap — manifesting as
        // catastrophic delivery loss even on a benign wire.
        attr.path_mtu                = active_mtu_;
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
        if (qp_type_ == QPKind::RC) {
            attr.max_dest_rd_atomic = static_cast<uint8_t>(rc_max_rd_atomic_);
            attr.min_rnr_timer      = static_cast<uint8_t>(rc_min_rnr_timer_);
            flags |= IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER;
        }
        int ret = ibv_modify_qp(qp_, &attr, flags);
        if (ret) {
            throw std::runtime_error(std::string("QP->RTR failed: ") +
                                     strerror(errno));
        }
        SEMIRDMA_LOG_INFO("QP -> RTR (dest_qpn=%u)", remote.qpn);
    }

    // --- RTS (ref: lines 235-246) ---
    // RC adds timeout, retry_cnt, rnr_retry, max_rd_atomic; UC uses only sq_psn.
    {
        struct ibv_qp_attr attr;
        std::memset(&attr, 0, sizeof(attr));
        attr.qp_state = IBV_QPS_RTS;
        attr.sq_psn   = 0;

        int flags = IBV_QP_STATE | IBV_QP_SQ_PSN;
        if (qp_type_ == QPKind::RC) {
            attr.timeout       = static_cast<uint8_t>(rc_timeout_);
            attr.retry_cnt     = static_cast<uint8_t>(rc_retry_cnt_);
            attr.rnr_retry     = static_cast<uint8_t>(rc_rnr_retry_);
            attr.max_rd_atomic = static_cast<uint8_t>(rc_max_rd_atomic_);
            flags |= IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT
                   | IBV_QP_RNR_RETRY | IBV_QP_MAX_QP_RD_ATOMIC;
            SEMIRDMA_LOG_INFO(
                "RC params: timeout=%d (~%.1f ms) retry_cnt=%d rnr_retry=%d "
                "min_rnr_timer=%d max_rd_atomic=%d",
                rc_timeout_,
                0.004096 * (1LL << rc_timeout_),
                rc_retry_cnt_, rc_rnr_retry_,
                rc_min_rnr_timer_, rc_max_rd_atomic_);
        }
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
// post_send — IBV_WR_SEND for the CLEAR control plane.  Plain SEND is
// receiver-side delivered into a posted Recv WR (via post_recv_buffer).
// ---------------------------------------------------------------------------
uint64_t UCQPEngine::post_send(uint64_t wr_id, size_t local_offset, size_t length)
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
    wr.opcode     = IBV_WR_SEND;
    wr.send_flags = IBV_SEND_SIGNALED;

    int ret = ibv_post_send(qp_, &wr, &bad_wr);
    if (ret) {
        throw std::runtime_error(std::string("ibv_post_send (SEND) failed: ") +
                                 strerror(ret));
    }
    return wr_id;
}

// ---------------------------------------------------------------------------
// post_recv_buffer — recv WR pointing at a real buffer slice.  Required for
// IBV_WR_SEND delivery (zero-length post_recv suffices for Write-with-Imm
// because only the imm_data needs a CQE slot).
// ---------------------------------------------------------------------------
void UCQPEngine::post_recv_buffer(uint64_t wr_id, size_t local_offset,
                                  size_t length)
{
    struct ibv_sge sge;
    std::memset(&sge, 0, sizeof(sge));
    sge.addr   = reinterpret_cast<uint64_t>(buf_ + local_offset);
    sge.length = static_cast<uint32_t>(length);
    sge.lkey   = mr_->lkey;

    struct ibv_recv_wr wr, *bad_wr = nullptr;
    std::memset(&wr, 0, sizeof(wr));
    wr.wr_id   = wr_id;
    wr.sg_list = &sge;
    wr.num_sge = 1;

    int ret = ibv_post_recv(qp_, &wr, &bad_wr);
    if (ret) {
        throw std::runtime_error(std::string("ibv_post_recv (buffer) failed: ") +
                                 strerror(ret));
    }
    outstanding_recv_.fetch_add(1, std::memory_order_relaxed);
}

// ---------------------------------------------------------------------------
// post_bucket_chunks — fast path
//
// Eliminates per-chunk Python ↔ C++ boundary cost by doing the entire
// chunk-emit + wave-throttle + tail-drain loop in C++.  Each wave of up
// to ``sq_depth_throttle - 1`` WRs is chained via ``wr.next`` and posted
// with a single ``ibv_post_send`` call so the libmlx5 fast path can
// burst them into the NIC doorbell without per-WR overhead.
//
// Returns a populated ChunkSet for the caller's wait_for_ratio bookkeeping.
// ---------------------------------------------------------------------------
ChunkSet UCQPEngine::post_bucket_chunks(size_t          base_offset,
                                       size_t          remote_base_offset,
                                       size_t          total_bytes,
                                       size_t          chunk_bytes,
                                       int             sq_depth_throttle,
                                       int             drain_timeout_ms,
                                       int             per_wr_pace_us,
                                       const RemoteMR& remote,
                                       bool            with_imm,
                                       uint64_t        wr_id_base)
{
    ChunkSet cs(base_offset, total_bytes, chunk_bytes);
    const size_t n_chunks = cs.size();
    if (n_chunks == 0) return cs;

    const int capacity = std::max(1, sq_depth_throttle - 1);
    int       inflight = 0;

    // Per-WR explicit pacing.
    //
    // Empirical observation (2026-04-25 on amd203/amd196 CX-5 25 GbE):
    // a tight C++ ibv_post_send loop (per_wr_pace_us=0) drops ~30% of
    // chunks at the SemiRDMA-stack level on this hardware, while the
    // pre-fix Python prototype with its ~5 µs interpreter + pybind
    // overhead per WR achieves ~99.5% delivery.  Adding explicit
    // 5-10 µs busy-wait pacing here recovers delivery to ~99% but
    // is *slower* end-to-end than the Python loop, so the Python loop
    // is currently the production path (see python/semirdma/transport.py
    // post_gradient comment + DEBUG_LOG.md hypotheses F–K).
    //
    // The mechanism is NOT identified.  `ib_write_bw -c UC -q 1 -s 4096`
    // submits at ~1.33 µs/WR on the same NIC with 0 loss, so this is
    // NOT a CX-5 hardware cliff — the bug is somewhere in our software
    // stack (most likely receiver SRQ refill rate; see DEBUG_LOG.md
    // for the open hypotheses).  This pacing parameter exists as a
    // workaround until the root cause is identified.  Set to 0 on
    // SoftRoCE.
    const auto pace_duration =
        std::chrono::microseconds(std::max(0, per_wr_pace_us));

    struct ibv_sge     sge;
    struct ibv_send_wr wr;
    struct ibv_send_wr* bad_wr = nullptr;
    std::vector<struct ibv_wc> wcs(static_cast<size_t>(capacity));

    auto last_post = std::chrono::steady_clock::now();

    for (size_t i = 0; i < n_chunks; i++) {
        // Wave throttle: hold inflight at most ``capacity``.
        while (inflight >= capacity) {
            int n = ibv_poll_cq(cq_, capacity, wcs.data());
            if (n < 0) {
                throw std::runtime_error(
                    "post_bucket_chunks: ibv_poll_cq returned error");
            }
            if (n > 0) {
                inflight = std::max(0, inflight - n);
            }
        }

        // Per-WR pacing — busy-wait the remainder of pace_duration since
        // the previous post, so the NIC TX path doesn't see a back-to-back
        // burst from libmlx5.
        if (per_wr_pace_us > 0) {
            while (std::chrono::steady_clock::now() - last_post < pace_duration) {
                // spin
            }
        }

        const ChunkDescriptor& cd = cs.chunk(i);

        std::memset(&sge, 0, sizeof(sge));
        sge.addr   = reinterpret_cast<uint64_t>(buf_ + cd.local_offset);
        sge.length = static_cast<uint32_t>(cd.length);
        sge.lkey   = mr_->lkey;

        std::memset(&wr, 0, sizeof(wr));
        wr.wr_id      = wr_id_base + i;
        wr.sg_list    = &sge;
        wr.num_sge    = 1;
        wr.opcode     = with_imm ? IBV_WR_RDMA_WRITE_WITH_IMM
                                 : IBV_WR_RDMA_WRITE;
        wr.send_flags = IBV_SEND_SIGNALED;
        if (with_imm) {
            wr.imm_data = htonl(cd.chunk_id);
        }
        wr.wr.rdma.remote_addr =
            remote.addr + remote_base_offset +
            (cd.local_offset - base_offset);
        wr.wr.rdma.rkey = remote.rkey;
        wr.next         = nullptr;

        int ret = ibv_post_send(qp_, &wr, &bad_wr);
        if (ret) {
            throw std::runtime_error(
                std::string("post_bucket_chunks: ibv_post_send failed: ") +
                strerror(ret));
        }
        inflight++;
        last_post = std::chrono::steady_clock::now();
    }

    // Tail drain: wait for remaining SEND CQEs so the next bucket starts
    // with inflight=0.  Bound by drain_timeout_ms in case a previous WR
    // had a fatal status and prevents the queue from progressing.
    Stopwatch sw;
    while (inflight > 0) {
        if (sw.elapsed_ms() >= static_cast<double>(drain_timeout_ms)) {
            SEMIRDMA_LOG_WARN(
                "post_bucket_chunks: tail drain timeout (inflight=%d, "
                "n_chunks=%zu)", inflight, n_chunks);
            break;
        }
        int n = ibv_poll_cq(cq_, capacity, wcs.data());
        if (n < 0) {
            throw std::runtime_error(
                "post_bucket_chunks: tail-drain ibv_poll_cq error");
        }
        if (n > 0) {
            inflight = std::max(0, inflight - n);
        }
    }
    return cs;
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
