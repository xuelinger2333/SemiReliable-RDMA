/*
 * rq_monitor.h — Per-peer Receive-Queue credit tracker
 *
 * UC Write-with-Imm is *fatal* at the receiver if no Receive WR is posted
 * (DOCA documentation; observed empirically on CX-5). RQMonitor watches
 * the running credit balance for every peer the local rank receives from
 * and fires:
 *   - on_low_watermark(peer)  when credits drop below cfg.low_watermark.
 *     Caller's policy: send a BACKPRESSURE control message OR force
 *     fallback for in-flight buckets to RC.
 *   - on_replenish_request(peer, k) when the monitor recommends posting
 *     k more recv WRs to keep credits at cfg.refill_target.
 *
 * Pure-logic; no ibverbs. Same single-threaded model as Finalizer.
 *
 * See docs/phase5/clear-design.md §6.
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <unordered_map>

namespace semirdma::clear {

struct RQMonitorConfig {
    int32_t low_watermark   = 16;   // fire when credits <= this
    int32_t refill_target   = 64;   // recommend topping up to this
    int32_t initial_credits = 64;
};

struct RQMonitorStats {
    uint64_t low_watermark_events = 0;
    uint64_t replenish_events     = 0;
    int64_t  total_consumed       = 0;
    int64_t  total_posted         = 0;
};

class RQMonitor {
public:
    using LowWatermarkFn      = std::function<void(uint16_t peer_edge,
                                                   int32_t credits_remaining)>;
    using ReplenishRequestFn  = std::function<void(uint16_t peer_edge,
                                                   int32_t k_to_post)>;

    explicit RQMonitor(RQMonitorConfig cfg = {});

    void on_low_watermark(LowWatermarkFn h)         { low_watermark_ = std::move(h); }
    void on_replenish_request(ReplenishRequestFn h) { replenish_     = std::move(h); }

    // Register a peer with `initial_credits` worth of pre-posted recv WRs.
    // Idempotent: if the peer already exists this is a no-op.
    void register_peer(uint16_t peer_edge);

    // Record that the given peer consumed `n` recv WRs (i.e. n CQEs were
    // drained). Fires on_low_watermark when the post-decrement credit is
    // <= cfg.low_watermark, AND fires on_replenish_request to suggest
    // topping back up to cfg.refill_target.
    void record_consumed(uint16_t peer_edge, int32_t n = 1);

    // Record that the caller posted `n` fresh recv WRs to the QP.
    void record_posted(uint16_t peer_edge, int32_t n);

    int32_t credits(uint16_t peer_edge) const;
    bool    is_low(uint16_t peer_edge) const;
    const RQMonitorStats& stats() const { return stats_; }

private:
    RQMonitorConfig cfg_;
    std::unordered_map<uint16_t, int32_t> credits_;
    LowWatermarkFn      low_watermark_;
    ReplenishRequestFn  replenish_;
    RQMonitorStats      stats_{};
};

}  // namespace semirdma::clear
