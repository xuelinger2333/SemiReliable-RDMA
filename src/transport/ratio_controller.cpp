/*
 * ratio_controller.cpp — RatioController implementation
 *
 * Single-threaded busy-poll loop.  Phase 2 does not use event channels;
 * P0 confirmed ibv_poll_cq latency is in the microsecond range on SoftRoCE.
 */

#include "transport/ratio_controller.h"
#include "utils/timing.h"

#include <cstdint>

namespace semirdma {

namespace {

// PR-C wire encoding helpers (see ratio_controller.h header for design).
constexpr uint32_t IMM_BUCKET_SHIFT = 24;
constexpr uint32_t IMM_CHUNK_MASK   = 0x00FFFFFFu;

inline uint8_t  bucket_of(uint32_t imm) {
    return static_cast<uint8_t>((imm >> IMM_BUCKET_SHIFT) & 0xFFu);
}
inline uint32_t chunk_of(uint32_t imm) {
    return imm & IMM_CHUNK_MASK;
}

} // anonymous namespace

size_t RatioController::drain_pending(ChunkSet& cs, uint8_t expected_bucket_id)
{
    auto it = pending_cqes_.find(expected_bucket_id);
    if (it == pending_cqes_.end()) {
        return 0;
    }
    size_t drained = 0;
    for (uint32_t chunk_id : it->second) {
        if (cs.mark_completed(chunk_id)) {
            ++drained;
        }
        // mark_completed returning false means chunk_id is out of range
        // for ``cs`` — silently dropped (defensive: a stale entry from
        // a wrap-around could land here).
    }
    pending_cqes_.erase(it);
    return drained;
}

void RatioController::stash_foreign(uint8_t bucket_id, uint32_t chunk_id)
{
    pending_cqes_[bucket_id].push_back(chunk_id & IMM_CHUNK_MASK);
}

size_t RatioController::pending_size() const
{
    size_t total = 0;
    for (const auto& kv : pending_cqes_) {
        total += kv.second.size();
    }
    return total;
}

size_t RatioController::pending_size_for(uint8_t bucket_id) const
{
    auto it = pending_cqes_.find(bucket_id);
    return (it == pending_cqes_.end()) ? 0u : it->second.size();
}

void RatioController::clear_pending()
{
    pending_cqes_.clear();
}

bool RatioController::wait_for_ratio(ChunkSet&  cs,
                                     double     ratio,
                                     int        timeout_ms,
                                     uint8_t    expected_bucket_id,
                                     WaitStats* stats)
{
    Stopwatch sw;
    uint32_t poll_count = 0;

    // Step 0: drain any pending CQEs that match the expected bucket.
    // No-op when the queue is empty (overwhelmingly the common case
    // for legacy callers using bucket_id=0 with one bucket per step).
    drain_pending(cs, expected_bucket_id);

    while (cs.completion_ratio() < ratio) {
        if (sw.elapsed_ms() >= static_cast<double>(timeout_ms)) {
            // Timeout
            if (stats) {
                stats->latency_ms = sw.elapsed_ms();
                stats->poll_count = poll_count;
                stats->completed  = static_cast<uint32_t>(cs.num_completed());
                stats->timed_out  = true;
            }
            return false;
        }

        // Non-blocking batch poll (timeout_ms=0 → single ibv_poll_cq call)
        auto completions = engine_.poll_cq(16, 0);
        poll_count++;

        for (const auto& c : completions) {
            // Only process receiver-side Write-with-Imm CQEs.
            // Sender CQEs (IBV_WC_RDMA_WRITE) are silently consumed.
            if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM &&
                c.status == IBV_WC_SUCCESS) {
                uint8_t  bid = bucket_of(c.imm_data);
                uint32_t cid = chunk_of(c.imm_data);
                if (bid == expected_bucket_id) {
                    cs.mark_completed(cid);
                } else {
                    // Foreign bucket — stash for a future wait_for_ratio
                    // (or external drain_pending) call to claim.
                    pending_cqes_[bid].push_back(cid);
                }
            }
        }
    }

    // Reached target ratio
    if (stats) {
        stats->latency_ms = sw.elapsed_ms();
        stats->poll_count = poll_count;
        stats->completed  = static_cast<uint32_t>(cs.num_completed());
        stats->timed_out  = false;
    }
    return true;
}

} // namespace semirdma
