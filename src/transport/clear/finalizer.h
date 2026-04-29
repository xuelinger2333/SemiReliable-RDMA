/*
 * finalizer.h — Per-uid CLEAR finalize state machine
 *
 * The finalizer is the brain of CLEAR semantic erasure. Given a receiver's
 * recv_bitmap (delivered by WITNESS) and the bucket's policy, it picks one
 * of five FinalizeDecisions and emits exactly one FINALIZE message that
 * every rank applies before SGD:
 *   DELIVERED   — every chunk arrived; no mask needed.
 *   REPAIRED    — missing chunks were re-sent over RC; mask is all-present.
 *   MASKED      — missing chunks zero-masked.
 *   STALE       — missing chunks reuse last iter's value.
 *   FALLBACK_RC — bucket too damaged for repair budget; resent on RC.
 *
 * Pure-logic core: ``decide_finalize`` is a stateless function that, given
 * (n_chunks, recv_bitmap, policy, budget), returns a DecideResult — the
 * decision plus repair_ranges (when applicable) plus byte cost. This is
 * the unit-testable kernel.
 *
 * Stateful wrapper: ``Finalizer`` holds per-uid records, a step-scoped
 * repair budget, and four caller-installed callbacks (send REPAIR_REQ,
 * send FINALIZE, send RETIRE, apply final mask locally). The state machine
 * is single-threaded; callers serialize via the existing transport mutex.
 *
 * See docs/phase5/clear-design.md §3.3 + §5 for protocol-level details.
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <unordered_map>
#include <vector>

#include "control_plane_codec.h"
#include "messages.h"
#include "witness_codec.h"

namespace semirdma::clear {

// ---------------------------------------------------------------------------
// Pure decision kernel.
//
// Inputs:
//   n_chunks               — total chunks in the bucket
//   recv_bitmap            — bit-packed LSB-first; bit i set iff chunk i
//                            was observed locally (or via WITNESS aggregate)
//   bitmap_bytes           — must be >= ceil(n_chunks / 8)
//   chunk_bytes            — bytes per chunk (used to budget repair)
//   policy                 — bucket policy from the application registry
//   repair_budget_bytes    — bytes still available in this step's repair pool
//   max_repair_bytes_per_uid — cap on a single uid's repair (0 = no cap)
//
// Output:
//   decision               — one of FinalizeDecision values. If REPAIRED,
//                            the caller must emit REPAIR_REQ for the
//                            returned repair_ranges; the actual FINALIZE
//                            with REPAIRED is emitted later when the repair
//                            data has been delivered.
//   repair_ranges          — populated iff decision == REPAIRED (pending);
//                            otherwise empty.
//   missing_count          — number of chunks where the bit is 0
//   missing_bytes          — missing_count * chunk_bytes
//   budget_consumed_bytes  — bytes the caller MUST subtract from the
//                            step-level repair budget; 0 unless decision
//                            == REPAIRED.
// ---------------------------------------------------------------------------

struct DecideResult {
    FinalizeDecision     decision;
    std::vector<Range>   repair_ranges;
    uint32_t             missing_count;
    uint64_t             missing_bytes;
    uint64_t             budget_consumed_bytes;
};

DecideResult decide_finalize(uint32_t n_chunks,
                             const uint8_t* recv_bitmap,
                             size_t bitmap_bytes,
                             uint32_t chunk_bytes,
                             Policy policy,
                             uint64_t repair_budget_bytes,
                             uint64_t max_repair_bytes_per_uid);

// ---------------------------------------------------------------------------
// Stateful wrapper.
// ---------------------------------------------------------------------------

struct FinalizerConfig {
    uint64_t repair_budget_bytes_per_step = 16ull * 1024 * 1024;  // 16 MiB
    uint64_t max_repair_bytes_per_uid     = 0;                    // 0 = no cap
};

struct FinalizerStats {
    uint64_t n_uids_seen          = 0;
    uint64_t n_finalized          = 0;
    uint64_t n_decisions[6]       = {};   // index by FinalizeDecision (1..5)
    uint64_t total_repair_bytes   = 0;
    uint64_t budget_refills       = 0;
    uint64_t budget_underruns     = 0;    // wanted REPAIR but no budget left
};

class Finalizer {
public:
    using SendRepairReqFn = std::function<void(uint64_t uid,
                                               const Range* ranges,
                                               uint16_t n_ranges)>;
    using SendFinalizeFn  = std::function<void(uint64_t uid,
                                               FinalizeDecision decision,
                                               WitnessEncoding mask_encoding,
                                               const uint8_t* mask_body,
                                               size_t mask_body_len)>;
    using SendRetireFn    = std::function<void(uint64_t uid,
                                               uint8_t slot,
                                               uint8_t gen)>;
    using ApplyMaskFn     = std::function<void(uint64_t uid,
                                               FinalizeDecision decision,
                                               const uint8_t* mask_bitmap,
                                               size_t bitmap_bytes,
                                               uint32_t n_chunks)>;

    explicit Finalizer(FinalizerConfig cfg = {});

    void on_send_repair_req(SendRepairReqFn h) { send_repair_req_ = std::move(h); }
    void on_send_finalize(SendFinalizeFn h)    { send_finalize_   = std::move(h); }
    void on_send_retire(SendRetireFn h)        { send_retire_     = std::move(h); }
    void on_apply_mask(ApplyMaskFn h)          { apply_mask_      = std::move(h); }

    // Register a uid that this finalizer should track. Invoked on BEGIN.
    // Returns false if the uid is already registered.
    bool track(uint64_t uid, uint8_t slot, uint8_t gen,
               uint32_t n_chunks, uint32_t chunk_bytes,
               Policy policy);

    // Drive the state machine when WITNESS arrives (or is locally produced).
    // recv_bitmap is bit-packed LSB-first of length n_chunks (registered via
    // track()). May trigger send_repair_req_ (REPAIR_FIRST policy + budget
    // OK) OR send_finalize_ + send_retire_ + apply_mask_ (terminal).
    // Returns the decision taken.
    FinalizeDecision on_witness(uint64_t uid,
                                const uint8_t* recv_bitmap,
                                size_t bitmap_bytes);

    // Called when the REPAIR phase has fully delivered (i.e. all REPAIR_REQ
    // ranges' chunks have arrived). Emits FINALIZE(REPAIRED) + RETIRE +
    // apply_mask(all-present). Returns false if the uid is not in REPAIRING.
    bool on_repair_complete(uint64_t uid);

    // Step boundary — refill repair budget.
    void on_step_boundary();

    bool is_tracked(uint64_t uid) const;
    uint64_t repair_budget_remaining_bytes() const { return budget_remaining_; }
    const FinalizerStats& stats() const { return stats_; }

private:
    enum class State : uint8_t {
        WAITING_WITNESS,
        REPAIRING,
        DONE,
    };

    struct Record {
        uint8_t  slot;
        uint8_t  gen;
        uint32_t n_chunks;
        uint32_t chunk_bytes;
        Policy   policy;
        State    state;
    };

    void terminate(uint64_t uid, const Record& r,
                   FinalizeDecision decision,
                   const uint8_t* mask_bitmap, size_t bitmap_bytes);

    FinalizerConfig cfg_;
    std::unordered_map<uint64_t, Record> records_;
    uint64_t        budget_remaining_ = 0;
    SendRepairReqFn send_repair_req_;
    SendFinalizeFn  send_finalize_;
    SendRetireFn    send_retire_;
    ApplyMaskFn     apply_mask_;
    FinalizerStats  stats_{};
};

}  // namespace semirdma::clear
