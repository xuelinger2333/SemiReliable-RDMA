/*
 * finalizer.cpp — see finalizer.h
 */

#include "finalizer.h"

#include <cstring>

namespace semirdma::clear {

namespace {

inline size_t bitmap_bytes_for(uint32_t n_chunks) {
    return (static_cast<size_t>(n_chunks) + 7u) >> 3;
}

// Build a "final mask" bitmap that records which chunks are *present* in
// the buffer that downstream SGD will consume. Semantics by decision:
//   DELIVERED — all bits set.
//   REPAIRED  — all bits set (repair filled the holes).
//   MASKED    — same as recv_bitmap (zero where missing).
//   STALE     — same as recv_bitmap (caller will fill missing from prev step).
//   FALLBACK_RC — all bits set (RC will resend everything).
// The caller's apply_mask_ callback is responsible for actually zeroing or
// stale-filling the underlying buffer; this bitmap just describes intent.
std::vector<uint8_t> build_final_mask(FinalizeDecision decision,
                                      const uint8_t* recv_bitmap,
                                      size_t bitmap_bytes,
                                      uint32_t n_chunks) {
    const size_t expected = bitmap_bytes_for(n_chunks);
    std::vector<uint8_t> out(expected, 0);
    switch (decision) {
    case FinalizeDecision::DELIVERED:
    case FinalizeDecision::REPAIRED:
    case FinalizeDecision::FALLBACK_RC:
        for (uint32_t i = 0; i < n_chunks; ++i) bitmap_set(out.data(), i);
        return out;
    case FinalizeDecision::MASKED:
    case FinalizeDecision::STALE: {
        const size_t copy_bytes = std::min(expected, bitmap_bytes);
        if (copy_bytes > 0) std::memcpy(out.data(), recv_bitmap, copy_bytes);
        // Trim any trailing bits past n_chunks.
        uint32_t tail = n_chunks & 7u;
        if (tail && expected > 0) {
            out.back() &= static_cast<uint8_t>((1u << tail) - 1u);
        }
        return out;
    }
    }
    return out;  // unreachable
}

}  // namespace

// ---------------------------------------------------------------------------
// Pure decision kernel
// ---------------------------------------------------------------------------

DecideResult decide_finalize(uint32_t n_chunks,
                             const uint8_t* recv_bitmap,
                             size_t bitmap_bytes,
                             uint32_t chunk_bytes,
                             Policy policy,
                             uint64_t repair_budget_bytes,
                             uint64_t max_repair_bytes_per_uid) {
    DecideResult r{};
    const uint32_t recv_count =
        bitmap_popcount(recv_bitmap,
                        static_cast<uint32_t>(
                            std::min<size_t>(bitmap_bytes * 8u, n_chunks)));
    r.missing_count = n_chunks - recv_count;
    r.missing_bytes = static_cast<uint64_t>(r.missing_count) *
                      static_cast<uint64_t>(chunk_bytes);

    if (r.missing_count == 0) {
        r.decision = FinalizeDecision::DELIVERED;
        return r;
    }

    switch (policy) {
    case Policy::REPAIR_FIRST: {
        const uint64_t cap = (max_repair_bytes_per_uid > 0)
            ? std::min(max_repair_bytes_per_uid, repair_budget_bytes)
            : repair_budget_bytes;
        if (r.missing_bytes <= cap) {
            r.decision = FinalizeDecision::REPAIRED;  // pending; emit REPAIR_REQ
            r.repair_ranges = compute_missing_ranges(recv_bitmap, bitmap_bytes,
                                                     n_chunks);
            r.budget_consumed_bytes = r.missing_bytes;
        } else {
            // Budget exhausted — fall back to MASKED (safer than RC for
            // repair-first which is typically a critical layer; finalizer
            // will report budget_underruns so callers can tune).
            r.decision = FinalizeDecision::MASKED;
        }
        return r;
    }
    case Policy::MASK_FIRST:
        r.decision = FinalizeDecision::MASKED;
        return r;
    case Policy::STALE_FILL:
        r.decision = FinalizeDecision::STALE;
        return r;
    case Policy::ESTIMATOR_SCALE:
        // Estimator-scale is a downstream operation: at finalize time we
        // surface MASKED (zeros for missing); the apply_mask callback
        // re-scales the aggregated tensor by n_chunks/recv_count.
        r.decision = FinalizeDecision::MASKED;
        return r;
    }
    // Unknown policy — be safe and mask.
    r.decision = FinalizeDecision::MASKED;
    return r;
}

// ---------------------------------------------------------------------------
// Finalizer state machine
// ---------------------------------------------------------------------------

Finalizer::Finalizer(FinalizerConfig cfg)
    : cfg_(cfg), budget_remaining_(cfg.repair_budget_bytes_per_step) {}

bool Finalizer::track(uint64_t uid, uint8_t slot, uint8_t gen,
                      uint32_t n_chunks, uint32_t chunk_bytes,
                      Policy policy) {
    if (records_.find(uid) != records_.end()) return false;
    Record rec{slot, gen, n_chunks, chunk_bytes, policy, State::WAITING_WITNESS};
    records_.emplace(uid, rec);
    ++stats_.n_uids_seen;
    return true;
}

bool Finalizer::is_tracked(uint64_t uid) const {
    return records_.find(uid) != records_.end();
}

void Finalizer::terminate(uint64_t uid, const Record& r,
                          FinalizeDecision decision,
                          const uint8_t* mask_bitmap, size_t bitmap_bytes) {
    // Pick the smallest wire encoding for the mask body via witness_codec.
    auto enc = encode_witness(mask_bitmap, bitmap_bytes, r.n_chunks);

    if (send_finalize_) {
        send_finalize_(uid, decision, enc.encoding,
                       enc.body.empty() ? nullptr : enc.body.data(),
                       enc.body.size());
    }
    if (apply_mask_) {
        apply_mask_(uid, decision, mask_bitmap, bitmap_bytes, r.n_chunks);
    }
    if (send_retire_) {
        send_retire_(uid, r.slot, r.gen);
    }

    // Bookkeeping
    ++stats_.n_finalized;
    auto idx = static_cast<size_t>(decision);
    if (idx >= 1 && idx < 6) ++stats_.n_decisions[idx];
}

FinalizeDecision Finalizer::on_witness(uint64_t uid,
                                       const uint8_t* recv_bitmap,
                                       size_t bitmap_bytes) {
    auto it = records_.find(uid);
    if (it == records_.end()) return FinalizeDecision::FALLBACK_RC;
    Record& rec = it->second;
    if (rec.state != State::WAITING_WITNESS) {
        // Idempotent: a second WITNESS for the same uid is ignored. Caller
        // should not normally do this; we don't change state and report
        // the previous state via FALLBACK_RC sentinel here. Tests treat
        // this as a soft assertion — see test_finalizer.
        return FinalizeDecision::FALLBACK_RC;
    }

    auto dr = decide_finalize(rec.n_chunks, recv_bitmap, bitmap_bytes,
                              rec.chunk_bytes, rec.policy,
                              budget_remaining_, cfg_.max_repair_bytes_per_uid);

    if (dr.decision == FinalizeDecision::REPAIRED) {
        // Pending: emit REPAIR_REQ; FINALIZE(REPAIRED) is sent on
        // on_repair_complete.
        budget_remaining_ -= dr.budget_consumed_bytes;
        stats_.total_repair_bytes += dr.budget_consumed_bytes;
        rec.state = State::REPAIRING;
        if (send_repair_req_) {
            send_repair_req_(uid, dr.repair_ranges.data(),
                             static_cast<uint16_t>(dr.repair_ranges.size()));
        }
        return FinalizeDecision::REPAIRED;
    }

    // Terminal decision: build mask + emit FINALIZE + RETIRE.
    if (rec.policy == Policy::REPAIR_FIRST &&
        dr.decision == FinalizeDecision::MASKED) {
        ++stats_.budget_underruns;
    }
    auto mask = build_final_mask(dr.decision, recv_bitmap, bitmap_bytes,
                                 rec.n_chunks);
    rec.state = State::DONE;
    terminate(uid, rec, dr.decision, mask.data(), mask.size());
    records_.erase(it);
    return dr.decision;
}

bool Finalizer::on_repair_complete(uint64_t uid) {
    auto it = records_.find(uid);
    if (it == records_.end()) return false;
    Record& rec = it->second;
    if (rec.state != State::REPAIRING) return false;

    // Build an all-present mask (length = n_chunks, all bits set).
    std::vector<uint8_t> mask((rec.n_chunks + 7u) >> 3, 0);
    for (uint32_t i = 0; i < rec.n_chunks; ++i) bitmap_set(mask.data(), i);
    rec.state = State::DONE;
    terminate(uid, rec, FinalizeDecision::REPAIRED, mask.data(), mask.size());
    records_.erase(it);
    return true;
}

void Finalizer::on_step_boundary() {
    budget_remaining_ = cfg_.repair_budget_bytes_per_step;
    ++stats_.budget_refills;
}

}  // namespace semirdma::clear
