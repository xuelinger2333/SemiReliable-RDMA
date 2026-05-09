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
    for (const PendingEntry& e : it->second) {
        // Evict entries whose age has reached one bucket_id cycle. Strict
        // ``>=`` (not ``>``) is required: with 8-bit bucket_id, age == 256
        // is precisely the first wait at which the same bucket_id can be
        // legitimately reused for a *new* ChunkSet; allowing an entry of
        // exactly that age through would let the previous cycle's stale
        // CQE be marked on the new set.
        if (wait_seq_ - e.deposit_seq >= kPendingMaxAgeWaits) continue;
        if (cs.mark_completed(e.chunk_id)) {
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
    pending_cqes_[bucket_id].push_back(
        PendingEntry{chunk_id & IMM_CHUNK_MASK, wait_seq_});
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

// ---------------------------------------------------------------------------
// Phase 5 CLEAR-mode helpers (additive). Use the slot/chunk/gen imm layout
// from src/transport/clear/imm_codec.h. Pending map is keyed by
// lease_key(slot, gen) so a future wait_for_ratio_clear call with the
// same (slot, gen) can drain it.
// ---------------------------------------------------------------------------

size_t RatioController::clr_drain_pending(ChunkSet& cs,
                                          uint8_t slot_id, uint8_t gen)
{
    const uint16_t key = clear::lease_key(slot_id, gen);
    auto it = clr_pending_cqes_.find(key);
    if (it == clr_pending_cqes_.end()) return 0;
    size_t drained = 0;
    for (uint32_t chunk_idx : it->second) {
        if (cs.mark_completed(chunk_idx)) ++drained;
    }
    clr_pending_cqes_.erase(it);
    return drained;
}

void RatioController::clr_stash_foreign(uint8_t slot_id, uint8_t gen,
                                        uint32_t chunk_idx)
{
    const uint16_t key = clear::lease_key(slot_id, gen);
    clr_pending_cqes_[key].push_back(chunk_idx & clear::kImmChunkMask);
}

size_t RatioController::clr_pending_size() const
{
    size_t total = 0;
    for (const auto& kv : clr_pending_cqes_) total += kv.second.size();
    return total;
}

size_t RatioController::clr_pending_size_for(uint8_t slot_id, uint8_t gen) const
{
    const uint16_t key = clear::lease_key(slot_id, gen);
    auto it = clr_pending_cqes_.find(key);
    return (it == clr_pending_cqes_.end()) ? 0u : it->second.size();
}

void RatioController::clr_clear_pending()
{
    clr_pending_cqes_.clear();
}

bool RatioController::wait_for_ratio_clear(ChunkSet&         cs,
                                           double            ratio,
                                           int               timeout_ms,
                                           uint8_t           expected_slot_id,
                                           uint8_t           expected_gen,
                                           RatioExitReason*  out_reason,
                                           WaitStats*        stats)
{
    Stopwatch sw;
    uint32_t poll_count = 0;
    uint32_t wc_errors  = 0;
    uint32_t last_wc_status = 0;
    const size_t n_chunks = cs.size();

    // Drain any pending CQEs that already match (slot, gen).
    clr_drain_pending(cs, expected_slot_id, expected_gen);

    auto record_exit = [&](RatioExitReason reason, bool reached) {
        // Escalate DEADLINE to WC_ERROR when at least one non-success WC was
        // observed during the wait. DELIVERED / RATIO_MET take precedence —
        // an error mid-flight that did not prevent the ratio from being met
        // is still flagged via stats->wc_errors but the exit reason reflects
        // the dominant outcome.
        if (reason == RatioExitReason::DEADLINE && wc_errors > 0) {
            reason = RatioExitReason::WC_ERROR;
        }
        if (out_reason) *out_reason = reason;
        if (stats) {
            stats->latency_ms = sw.elapsed_ms();
            stats->poll_count = poll_count;
            stats->completed  = static_cast<uint32_t>(cs.num_completed());
            stats->timed_out  = (reason == RatioExitReason::DEADLINE ||
                                 reason == RatioExitReason::WC_ERROR);
            stats->wc_errors      = wc_errors;
            stats->last_wc_status = last_wc_status;
        }
        return reached;
    };

    // Already DELIVERED before we even poll? (drained pending hit n_chunks)
    if (cs.num_completed() >= n_chunks) {
        return record_exit(RatioExitReason::DELIVERED, true);
    }
    if (cs.completion_ratio() >= ratio) {
        return record_exit(RatioExitReason::RATIO_MET, true);
    }

    while (true) {
        if (sw.elapsed_ms() >= static_cast<double>(timeout_ms)) {
            return record_exit(RatioExitReason::DEADLINE, false);
        }

        auto completions = engine_.poll_cq(16, 0);
        ++poll_count;

        for (const auto& c : completions) {
            if (c.status != IBV_WC_SUCCESS) {
                ++wc_errors;
                last_wc_status = static_cast<uint32_t>(c.status);
                continue;
            }
            if (c.opcode != IBV_WC_RECV_RDMA_WITH_IMM) continue;
            uint8_t  slot     = clear::imm_slot(c.imm_data);
            uint32_t chunk_id = clear::imm_chunk(c.imm_data);
            uint8_t  gen      = clear::imm_gen(c.imm_data);
            if (slot == expected_slot_id && gen == expected_gen) {
                cs.mark_completed(chunk_id);
            } else {
                const uint16_t key = clear::lease_key(slot, gen);
                clr_pending_cqes_[key].push_back(chunk_id);
            }
        }

        if (cs.num_completed() >= n_chunks) {
            return record_exit(RatioExitReason::DELIVERED, true);
        }
        if (cs.completion_ratio() >= ratio) {
            return record_exit(RatioExitReason::RATIO_MET, true);
        }
    }
}

bool RatioController::wait_for_ratio(ChunkSet&  cs,
                                     double     ratio,
                                     int        timeout_ms,
                                     uint8_t    expected_bucket_id,
                                     WaitStats* stats)
{
    Stopwatch sw;
    uint32_t poll_count = 0;
    uint32_t wc_errors  = 0;
    uint32_t last_wc_status = 0;

    // Bump the wait counter before drain so age comparisons in
    // drain_pending are computed against the current epoch.
    ++wait_seq_;

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
                stats->wc_errors      = wc_errors;
                stats->last_wc_status = last_wc_status;
            }
            return false;
        }

        // Non-blocking batch poll (timeout_ms=0 → single ibv_poll_cq call)
        auto completions = engine_.poll_cq(16, 0);
        poll_count++;

        for (const auto& c : completions) {
            // Track non-success CQEs so RC fallback / RC-Lossy experiments
            // can distinguish a real QP error (RNR / RETRY_EXC / TIMEOUT /
            // LOC_PROT / LOC_LEN / REM_ACCESS) from a delivery timeout.
            // Without this counter, all of those failure modes get masked
            // as "ratio not met before deadline".
            if (c.status != IBV_WC_SUCCESS) {
                ++wc_errors;
                last_wc_status = static_cast<uint32_t>(c.status);
                continue;
            }
            // Only process receiver-side Write-with-Imm CQEs.
            // Sender CQEs (IBV_WC_RDMA_WRITE) are silently consumed.
            if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM) {
                uint8_t  bid = bucket_of(c.imm_data);
                uint32_t cid = chunk_of(c.imm_data);
                if (bid == expected_bucket_id) {
                    cs.mark_completed(cid);
                } else {
                    // Foreign bucket — stash for a future wait_for_ratio
                    // (or external drain_pending) call to claim. The
                    // deposit_seq tag lets drain_pending evict entries
                    // older than one bucket_id cycle.
                    pending_cqes_[bid].push_back(
                        PendingEntry{cid, wait_seq_});
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
        stats->wc_errors      = wc_errors;
        stats->last_wc_status = last_wc_status;
    }
    return true;
}

} // namespace semirdma
