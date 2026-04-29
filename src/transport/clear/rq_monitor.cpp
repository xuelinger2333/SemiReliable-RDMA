/*
 * rq_monitor.cpp — see rq_monitor.h
 */

#include "rq_monitor.h"

namespace semirdma::clear {

RQMonitor::RQMonitor(RQMonitorConfig cfg) : cfg_(cfg) {}

void RQMonitor::register_peer(uint16_t peer_edge) {
    credits_.emplace(peer_edge, cfg_.initial_credits);
}

void RQMonitor::record_consumed(uint16_t peer_edge, int32_t n) {
    if (n <= 0) return;
    auto& c = credits_[peer_edge];   // value-initializes to 0 if absent
    c -= n;
    stats_.total_consumed += n;

    if (c <= cfg_.low_watermark) {
        ++stats_.low_watermark_events;
        if (low_watermark_) low_watermark_(peer_edge, c);
        const int32_t to_post = cfg_.refill_target - c;
        if (to_post > 0 && replenish_) {
            ++stats_.replenish_events;
            replenish_(peer_edge, to_post);
        }
    }
}

void RQMonitor::record_posted(uint16_t peer_edge, int32_t n) {
    if (n <= 0) return;
    credits_[peer_edge] += n;
    stats_.total_posted += n;
}

int32_t RQMonitor::credits(uint16_t peer_edge) const {
    auto it = credits_.find(peer_edge);
    return it == credits_.end() ? 0 : it->second;
}

bool RQMonitor::is_low(uint16_t peer_edge) const {
    return credits(peer_edge) <= cfg_.low_watermark;
}

}  // namespace semirdma::clear
