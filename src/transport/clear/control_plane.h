/*
 * control_plane.h — RC QP wrapper carrying CLEAR control messages
 *
 * One ControlPlane instance per peer pair. It owns:
 *   - A UCQPEngine in RC mode (HW-reliable delivery for control msgs).
 *   - A pre-divided recv-slot pool (default 64 × kMaxMessageBytes).
 *   - A small send-slot ring (default 16 × kMaxMessageBytes), round-robin.
 *
 * Send path (any rank): caller invokes one of the send_*() methods. The
 * message is encoded by control_plane_codec into the next free send slot,
 * then UCQPEngine::post_send fires it on the RC QP. Send completions are
 * drained inside poll_once() to recycle send slots.
 *
 * Receive path: the constructor pre-posts every recv slot. poll_once()
 * polls the RC CQ; for each IBV_WC_RECV completion it decodes the buffer
 * via control_plane_codec::decode and dispatches to the registered
 * callback for that MsgType. The slot is then reposted so the RQ never
 * drains.
 *
 * Threading: not thread-safe by itself. Phase 5 callers serialize through
 * the existing transport mutex (same model as UCQPEngine).
 *
 * The RC QP handshake (QPN + GID exchange) is the caller's job — same
 * pattern as Phase 4. Use local_qp_info() and local_mr_info() to publish
 * one's own params; pass the peer's into bring_up().
 */

#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include "control_plane_codec.h"
#include "messages.h"
#include "transport/uc_qp_engine.h"

namespace semirdma::clear {

struct ControlPlaneConfig {
    std::string dev_name;          // e.g. "mlx5_0"
    int         gid_index   = -1;  // -1 → auto-discover
    uint16_t    recv_slots  = 64;  // pre-posted recv buffer count
    uint16_t    send_slots  = 16;  // outstanding-send ring depth
    int         rc_timeout      = 14;  // log2(4.096us); 14 ≈ 67 ms
    int         rc_retry_cnt    = 7;
    int         rc_rnr_retry    = 7;
    int         rc_min_rnr_timer = 12;
};

// Counters surfaced for tests and metrics.
struct ControlPlaneStats {
    uint64_t sent_total          = 0;
    uint64_t sent_by_type[7]     = {};   // index = MsgType (1..6); slot 0 unused
    uint64_t recv_total          = 0;
    uint64_t recv_by_type[7]     = {};
    uint64_t recv_decode_errors  = 0;
    uint64_t send_completion_errors = 0;
    uint64_t recv_dropped_full   = 0;    // send-slot ring full → dropped send
};

class ControlPlane {
public:
    using BeginHandler        = std::function<void(const ParsedBegin&)>;
    using WitnessHandler      = std::function<void(const ParsedWitness&)>;
    using RepairReqHandler    = std::function<void(const ParsedRepairReq&)>;
    using FinalizeHandler     = std::function<void(const ParsedFinalize&)>;
    using RetireHandler       = std::function<void(const ParsedRetire&)>;
    using BackpressureHandler = std::function<void(const ParsedBackpressure&)>;

    explicit ControlPlane(const ControlPlaneConfig& cfg);
    ~ControlPlane();

    ControlPlane(const ControlPlane&)            = delete;
    ControlPlane& operator=(const ControlPlane&) = delete;

    // RC QP handshake. Caller must invoke this exactly once before any send_*
    // / poll_once. After this returns, all recv slots are pre-posted.
    void bring_up(const RemoteQpInfo& peer);

    // Local QP / MR descriptors for the peer-side handshake.
    RemoteQpInfo local_qp_info() const { return engine_->local_qp_info(); }
    RemoteMR     local_mr_info() const { return engine_->local_mr_info(); }

    // ----- Senders -----------------------------------------------------------
    // Each returns true on success; false if the send-slot ring is full or
    // the encoder rejected the inputs. Stats are bumped accordingly.

    bool send_begin(uint64_t uid, const BeginPayload& p);
    bool send_witness(uint64_t uid, uint32_t recv_count,
                      WitnessEncoding encoding,
                      const uint8_t* body, size_t body_len);
    bool send_repair_req(uint64_t uid,
                         const Range* ranges, uint16_t n_ranges);
    bool send_finalize(uint64_t uid, FinalizeDecision decision,
                       WitnessEncoding mask_encoding,
                       const uint8_t* mask_body, size_t mask_body_len);
    bool send_retire(uint64_t uid, const RetirePayload& p);
    bool send_backpressure(uint64_t uid, const BackpressurePayload& p);

    // ----- Receiver-side dispatch -------------------------------------------
    // Drains up to max_completions CQEs, dispatches recv'd messages to
    // their callbacks, recycles send slots on send completions, reposts
    // recv slots. Returns the number of completions processed.
    int poll_once(int max_completions = 32, int timeout_ms = 0);

    // ----- Callback registration --------------------------------------------
    void on_begin(BeginHandler h)               { begin_handler_       = std::move(h); }
    void on_witness(WitnessHandler h)           { witness_handler_     = std::move(h); }
    void on_repair_req(RepairReqHandler h)      { repair_req_handler_  = std::move(h); }
    void on_finalize(FinalizeHandler h)         { finalize_handler_    = std::move(h); }
    void on_retire(RetireHandler h)             { retire_handler_      = std::move(h); }
    void on_backpressure(BackpressureHandler h) { backpressure_handler_= std::move(h); }

    const ControlPlaneStats& stats() const { return stats_; }

private:
    // Acquire the next free send slot. Returns slot index in [0, send_slots),
    // or -1 if all slots are still in flight. The caller fills the slot,
    // then calls commit_send_slot() with the encoded byte length.
    int  acquire_send_slot();
    bool commit_send_slot(int slot, MsgType type, size_t length);

    void dispatch(const ParsedMessage& msg);
    void repost_recv(uint16_t slot);

    // wr_id encoding lets poll_once() tell SENDs and RECVs apart and recover
    // which slot a completion belongs to. We pack:
    //   bit 63 = direction (0 = SEND, 1 = RECV)
    //   bits 0..15 = slot index
    static constexpr uint64_t kSendBit = 0ull;
    static constexpr uint64_t kRecvBit = 1ull << 63;
    static uint64_t make_send_wr_id(uint16_t slot) { return kSendBit | slot; }
    static uint64_t make_recv_wr_id(uint16_t slot) { return kRecvBit | slot; }
    static bool     is_recv_wr_id(uint64_t id) { return (id & kRecvBit) != 0; }
    static uint16_t wr_id_slot(uint64_t id) { return static_cast<uint16_t>(id & 0xFFFFu); }

    ControlPlaneConfig             cfg_;
    std::unique_ptr<UCQPEngine>    engine_;
    bool                           up_ = false;

    // Buffer layout inside engine_->local_buf():
    //   [0 .. recv_slots * kMaxMessageBytes)              recv pool
    //   [recv_offset .. recv_offset + send_slots * kMax)  send pool
    size_t recv_pool_offset_ = 0;
    size_t send_pool_offset_ = 0;

    // Send-slot in-flight tracking. busy_[i]=true when slot i has an
    // outstanding SEND that hasn't been completed yet.
    std::vector<bool> send_busy_;
    uint16_t          next_send_slot_ = 0;

    BeginHandler        begin_handler_;
    WitnessHandler      witness_handler_;
    RepairReqHandler    repair_req_handler_;
    FinalizeHandler     finalize_handler_;
    RetireHandler       retire_handler_;
    BackpressureHandler backpressure_handler_;

    ControlPlaneStats stats_{};
};

}  // namespace semirdma::clear
