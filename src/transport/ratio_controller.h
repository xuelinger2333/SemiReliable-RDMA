/*
 * ratio_controller.h — CQE-driven completion ratio polling
 *
 * Given a ChunkSet, polls the engine's CQ in a loop.  Each CQE's imm_data
 * identifies the completed chunk (via ChunkSet::mark_completed).  Returns
 * when completion_ratio() >= target or timeout expires.
 *
 * Key invariant: only looks at CQE, never scans the buffer (P0 conclusion ②).
 *
 * PR-C (2026-04-28): per-bucket routing via imm_data bucket_id encoding
 * --------------------------------------------------------------------
 *   imm_data = (bucket_id_mod256 << 24) | (chunk_id & 0xFFFFFF)
 *                8 bits                       24 bits  (16 M chunks/bucket)
 *
 * The sender encodes ``bucket_id`` mod 256 into the high 8 bits of
 * imm_data.  ``wait_for_ratio`` takes ``expected_bucket_id`` and routes
 * any foreign-bucket CQEs (bid != expected) to a per-bucket pending queue.
 * A subsequent ``wait_for_ratio(cs', ratio, t, expected_bucket_id=K')``
 * drains pending entries for ``K'`` onto ``cs'`` before entering the
 * poll loop.
 *
 * Backwards compat: ``expected_bucket_id`` defaults to 0; when neither
 * sender nor receiver passes a bucket_id, imm_data == chunk_id (high 8
 * bits zero) — wire encoding is bit-identical to pre-PR-C.
 */

#pragma once

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"

#include <cstdint>
#include <unordered_map>
#include <vector>

namespace semirdma {

struct WaitStats {
    double   latency_ms  = 0.0;   // Wall-clock time from call to return
    uint32_t poll_count  = 0;     // Number of ibv_poll_cq calls
    uint32_t completed   = 0;     // Chunks completed at return time
    bool     timed_out   = false;
};

class RatioController {
public:
    explicit RatioController(UCQPEngine& engine) : engine_(engine) {}

    // Block until cs.completion_ratio() >= ratio or timeout_ms expires.
    //
    // bucket_id semantics (PR-C):
    //   - On entry: pending entries with bucket_id == expected_bucket_id
    //     are drained onto ``cs`` first (see drain_pending).
    //   - In the poll loop: a CQE whose imm_data decodes to bucket_id ==
    //     expected_bucket_id is marked on ``cs``; otherwise it is stashed
    //     in the per-bucket pending queue for a later wait_for_ratio call.
    //   - Backwards compat: default ``expected_bucket_id = 0`` matches
    //     pre-PR-C behavior bit-exactly when senders also use bucket_id=0
    //     (imm_data == chunk_id, high 8 bits zero).
    //
    // Returns true if ratio was reached, false on timeout.
    // stats (optional): filled with performance counters for RQ4 experiments.
    bool wait_for_ratio(ChunkSet&  cs,
                        double     ratio,
                        int        timeout_ms,
                        uint8_t    expected_bucket_id,
                        WaitStats* stats = nullptr);

    // Backwards-compat overload: equivalent to expected_bucket_id=0.
    // Kept so existing callers (Phase 2 tests, pre-PR-C bindings) compile
    // unchanged.  New code should pass the bucket_id explicitly.
    bool wait_for_ratio(ChunkSet&  cs,
                        double     ratio,
                        int        timeout_ms,
                        WaitStats* stats = nullptr) {
        return wait_for_ratio(cs, ratio, timeout_ms,
                              /*expected_bucket_id=*/0, stats);
    }

    // Drain queued (bucket_id == expected_bucket_id) CQEs onto ``cs``.
    // Returns the number of entries drained.  Idempotent.
    //
    // Public so the Python ``await_gradient`` leftover-drain (which
    // already polls the CQ outside of wait_for_ratio for late CQEs) can
    // re-feed the same pending store; see SemiRDMATransport.await_gradient.
    size_t drain_pending(ChunkSet& cs, uint8_t expected_bucket_id);

    // Stash a foreign-bucket CQE seen by an external poller.  Used by
    // SemiRDMATransport.await_gradient's leftover-drain (Python side
    // polls the CQ post-wait for late arrivals).
    //
    // chunk_id is the LOCAL (24-bit) chunk index; do not pass the raw
    // 32-bit imm_data.
    void stash_foreign(uint8_t bucket_id, uint32_t chunk_id);

    // Total pending entries across all bucket_ids.
    size_t pending_size() const;

    // Pending entries for one specific bucket_id.
    size_t pending_size_for(uint8_t bucket_id) const;

    // Drop all pending entries (e.g. for clean shutdown / test teardown).
    void clear_pending();

private:
    UCQPEngine& engine_;
    // bucket_id (mod 256) → list of LOCAL chunk_ids (24-bit) seen but not
    // yet claimed.  std::unordered_map keeps insertion fast; per-bucket
    // vectors are append-only until drained, then the entry is erased.
    std::unordered_map<uint8_t, std::vector<uint32_t>> pending_cqes_;
};

} // namespace semirdma
