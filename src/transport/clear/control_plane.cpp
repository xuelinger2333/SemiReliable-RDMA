/*
 * control_plane.cpp — see control_plane.h
 */

#include "control_plane.h"

#include <cstring>
#include <stdexcept>

namespace semirdma::clear {

namespace {

constexpr size_t kSlotBytes = kMaxMessageBytes;

// Bump stats[type] safely (type is 1-based).
inline void bump_by_type(uint64_t (&counters)[7], MsgType t) {
    auto idx = static_cast<size_t>(t);
    if (idx >= 1 && idx < 7) ++counters[idx];
}

}  // namespace

ControlPlane::ControlPlane(const ControlPlaneConfig& cfg)
    : cfg_(cfg),
      send_busy_(cfg.send_slots, false) {
    const size_t recv_bytes = static_cast<size_t>(cfg.recv_slots) * kSlotBytes;
    const size_t send_bytes = static_cast<size_t>(cfg.send_slots) * kSlotBytes;
    const size_t total      = recv_bytes + send_bytes;

    // SQ depth needs at least send_slots; RQ depth needs at least recv_slots.
    // Add a small safety margin.
    const int sq_depth = static_cast<int>(cfg.send_slots) + 4;
    const int rq_depth = static_cast<int>(cfg.recv_slots) + 4;

    engine_ = std::make_unique<UCQPEngine>(
        cfg.dev_name,
        total,
        sq_depth,
        rq_depth,
        cfg.gid_index,
        /*qp_type=*/"rc",
        cfg.rc_timeout,
        cfg.rc_retry_cnt,
        cfg.rc_rnr_retry,
        cfg.rc_min_rnr_timer,
        /*rc_max_rd_atomic=*/1);

    recv_pool_offset_ = 0;
    send_pool_offset_ = recv_bytes;
}

ControlPlane::~ControlPlane() = default;

void ControlPlane::bring_up(const RemoteQpInfo& peer) {
    if (up_) {
        throw std::runtime_error("ControlPlane::bring_up called twice");
    }
    engine_->bring_up(peer);
    // Pre-post every recv slot before flagging up_.
    for (uint16_t slot = 0; slot < cfg_.recv_slots; ++slot) {
        repost_recv(slot);
    }
    up_ = true;
}

// ---------- send_* helpers --------------------------------------------------

int ControlPlane::acquire_send_slot() {
    for (uint16_t i = 0; i < cfg_.send_slots; ++i) {
        uint16_t slot = static_cast<uint16_t>(
            (next_send_slot_ + i) % cfg_.send_slots);
        if (!send_busy_[slot]) {
            next_send_slot_ = static_cast<uint16_t>((slot + 1) % cfg_.send_slots);
            return slot;
        }
    }
    return -1;
}

bool ControlPlane::commit_send_slot(int slot, MsgType type, size_t length) {
    if (slot < 0) return false;
    send_busy_[slot] = true;
    const size_t off = send_pool_offset_ + static_cast<size_t>(slot) * kSlotBytes;
    engine_->post_send(make_send_wr_id(static_cast<uint16_t>(slot)),
                       off, length);
    ++stats_.sent_total;
    bump_by_type(stats_.sent_by_type, type);
    return true;
}

bool ControlPlane::send_begin(uint64_t uid, const BeginPayload& p) {
    int slot = acquire_send_slot();
    if (slot < 0) { ++stats_.recv_dropped_full; return false; }
    uint8_t* buf = engine_->local_buf() + send_pool_offset_ +
                   static_cast<size_t>(slot) * kSlotBytes;
    size_t n = encode_begin(uid, p, buf, kSlotBytes);
    if (n == 0) { send_busy_[slot] = false; return false; }
    return commit_send_slot(slot, MsgType::BEGIN, n);
}

bool ControlPlane::send_witness(uint64_t uid, uint32_t recv_count,
                                WitnessEncoding encoding,
                                const uint8_t* body, size_t body_len) {
    int slot = acquire_send_slot();
    if (slot < 0) { ++stats_.recv_dropped_full; return false; }
    uint8_t* buf = engine_->local_buf() + send_pool_offset_ +
                   static_cast<size_t>(slot) * kSlotBytes;
    size_t n = encode_witness(uid, recv_count, encoding, body, body_len,
                              buf, kSlotBytes);
    if (n == 0) { send_busy_[slot] = false; return false; }
    return commit_send_slot(slot, MsgType::WITNESS, n);
}

bool ControlPlane::send_repair_req(uint64_t uid,
                                   const Range* ranges, uint16_t n_ranges) {
    int slot = acquire_send_slot();
    if (slot < 0) { ++stats_.recv_dropped_full; return false; }
    uint8_t* buf = engine_->local_buf() + send_pool_offset_ +
                   static_cast<size_t>(slot) * kSlotBytes;
    size_t n = encode_repair_req(uid, ranges, n_ranges, buf, kSlotBytes);
    if (n == 0) { send_busy_[slot] = false; return false; }
    return commit_send_slot(slot, MsgType::REPAIR_REQ, n);
}

bool ControlPlane::send_finalize(uint64_t uid, FinalizeDecision decision,
                                 WitnessEncoding mask_encoding,
                                 const uint8_t* mask_body, size_t mask_body_len) {
    int slot = acquire_send_slot();
    if (slot < 0) { ++stats_.recv_dropped_full; return false; }
    uint8_t* buf = engine_->local_buf() + send_pool_offset_ +
                   static_cast<size_t>(slot) * kSlotBytes;
    size_t n = encode_finalize(uid, decision, mask_encoding,
                               mask_body, mask_body_len, buf, kSlotBytes);
    if (n == 0) { send_busy_[slot] = false; return false; }
    return commit_send_slot(slot, MsgType::FINALIZE, n);
}

bool ControlPlane::send_retire(uint64_t uid, const RetirePayload& p) {
    int slot = acquire_send_slot();
    if (slot < 0) { ++stats_.recv_dropped_full; return false; }
    uint8_t* buf = engine_->local_buf() + send_pool_offset_ +
                   static_cast<size_t>(slot) * kSlotBytes;
    size_t n = encode_retire(uid, p, buf, kSlotBytes);
    if (n == 0) { send_busy_[slot] = false; return false; }
    return commit_send_slot(slot, MsgType::RETIRE, n);
}

bool ControlPlane::send_backpressure(uint64_t uid,
                                     const BackpressurePayload& p) {
    int slot = acquire_send_slot();
    if (slot < 0) { ++stats_.recv_dropped_full; return false; }
    uint8_t* buf = engine_->local_buf() + send_pool_offset_ +
                   static_cast<size_t>(slot) * kSlotBytes;
    size_t n = encode_backpressure(uid, p, buf, kSlotBytes);
    if (n == 0) { send_busy_[slot] = false; return false; }
    return commit_send_slot(slot, MsgType::BACKPRESSURE, n);
}

// ---------- recv path ------------------------------------------------------

void ControlPlane::repost_recv(uint16_t slot) {
    const size_t off = recv_pool_offset_ + static_cast<size_t>(slot) * kSlotBytes;
    engine_->post_recv_buffer(make_recv_wr_id(slot), off, kSlotBytes);
}

void ControlPlane::dispatch(const ParsedMessage& msg) {
    ++stats_.recv_total;
    bump_by_type(stats_.recv_by_type, msg.type);
    switch (msg.type) {
    case MsgType::BEGIN:
        if (begin_handler_) begin_handler_(msg.begin);
        break;
    case MsgType::WITNESS:
        if (witness_handler_) witness_handler_(msg.witness);
        break;
    case MsgType::REPAIR_REQ:
        if (repair_req_handler_) repair_req_handler_(msg.repair_req);
        break;
    case MsgType::FINALIZE:
        if (finalize_handler_) finalize_handler_(msg.finalize);
        break;
    case MsgType::RETIRE:
        if (retire_handler_) retire_handler_(msg.retire);
        break;
    case MsgType::BACKPRESSURE:
        if (backpressure_handler_) backpressure_handler_(msg.backpressure);
        break;
    }
}

int ControlPlane::poll_once(int max_completions, int timeout_ms) {
    auto cs = engine_->poll_cq(max_completions, timeout_ms);
    for (const Completion& c : cs) {
        const uint16_t slot = wr_id_slot(c.wr_id);
        const bool is_recv  = is_recv_wr_id(c.wr_id);

        if (c.status != IBV_WC_SUCCESS) {
            if (is_recv) {
                ++stats_.recv_decode_errors;
                repost_recv(slot);
            } else {
                ++stats_.send_completion_errors;
                if (slot < cfg_.send_slots) send_busy_[slot] = false;
            }
            continue;
        }

        if (is_recv) {
            const uint8_t* buf = engine_->local_buf() +
                                 recv_pool_offset_ +
                                 static_cast<size_t>(slot) * kSlotBytes;
            ParsedMessage msg;
            if (decode(buf, kSlotBytes, msg)) {
                dispatch(msg);
            } else {
                ++stats_.recv_decode_errors;
            }
            repost_recv(slot);
        } else {
            if (slot < cfg_.send_slots) send_busy_[slot] = false;
        }
    }
    return static_cast<int>(cs.size());
}

}  // namespace semirdma::clear
