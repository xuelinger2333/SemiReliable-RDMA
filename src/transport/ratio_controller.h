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
 *
 * Phase 5 W2.1 — CLEAR mode (additive)
 * ------------------------------------
 *   imm_data = (slot_id << 24) | (chunk_idx << 4) | gen
 *               8 bits             20 bits           4 bits
 *
 * For CLEAR transfers, callers use ``wait_for_ratio_clear`` with
 * (slot_id, gen). Foreign (slot, gen) CQEs are stashed into a separate
 * pending map keyed by ``lease_key(slot, gen)``. The PR-C bucket_id
 * pending map is untouched; CLEAR and PR-C transfers can coexist on the
 * same engine, but a single bucket transfer must use one mode end-to-end.
 *
 * The CLEAR-mode methods do not finalize the ChunkSet themselves: they
 * mark received chunks and report the exit reason via RatioExitReason
 * so the higher-level finalizer (Phase 5 W2.2) decides between
 * deliver/repair/mask. See docs/phase5/clear-design.md §3.
 */

#pragma once

#include "transport/clear/imm_codec.h"
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
    // Count of CQEs polled with status != IBV_WC_SUCCESS during the wait.
    // Includes RC RNR/RETRY_EXC/TIMEOUT, UC LOC_PROT/LOC_LEN/REM_ACCESS, etc.
    // Surfaced so RC-Lossy / RC-Baseline experiments can distinguish a real
    // QP error from a delivery timeout (otherwise both paths would just see
    // ``timed_out=true``). Last-seen status is reported in ``last_wc_status``.
    uint32_t wc_errors      = 0;
    uint32_t last_wc_status = 0;  // ibv_wc_status enum value, 0 if no error
};

// Phase 5 CLEAR-mode exit classification. Reported by wait_for_ratio_clear
// alongside WaitStats so the finalizer can choose the right policy:
//   DELIVERED — every chunk arrived (recv_count == n_chunks).
//   RATIO_MET — ratio threshold reached but some chunks still missing;
//               the missing chunks become candidates for WITNESS / repair.
//   DEADLINE  — timeout fired before ratio was met; usual path on a
//               lossy wire.
//   WC_ERROR  — at least one CQE returned status != IBV_WC_SUCCESS during
//               the wait. The wait still respects the ratio target — this
//               flag is informational and reported via out_reason so the
//               higher-level path (RC fallback, finalizer) can react. The
//               bool return value is unaffected (true if ratio met).
enum class RatioExitReason : uint8_t {
    DELIVERED = 0,
    RATIO_MET = 1,
    DEADLINE  = 2,
    WC_ERROR  = 3,
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

    // ----- Phase 5 CLEAR-mode API (additive) ---------------------------
    // wait_for_ratio_clear: block until cs.completion_ratio() >= ratio
    // OR every chunk arrived (DELIVERED) OR timeout_ms expires (DEADLINE).
    // CQEs decoded with the CLEAR imm_data layout (slot:8|chunk:20|gen:4).
    // CQEs whose (slot, gen) does not match the expected pair are stashed
    // in clr_pending_cqes_ for a later wait_for_ratio_clear with that
    // pair, or for an external clr_drain_pending call.
    //
    // out_reason is required (the finalizer needs it to choose between
    // DELIVERED / WITNESS+repair / WITNESS+mask). stats is optional.
    bool wait_for_ratio_clear(ChunkSet&         cs,
                              double            ratio,
                              int               timeout_ms,
                              uint8_t           expected_slot_id,
                              uint8_t           expected_gen,
                              RatioExitReason*  out_reason,
                              WaitStats*        stats = nullptr);

    // Drain pending CQEs for (slot, gen) onto cs. Returns count drained.
    size_t clr_drain_pending(ChunkSet& cs, uint8_t slot_id, uint8_t gen);

    // Stash a (slot, gen, chunk_idx) seen by an external poller.
    void clr_stash_foreign(uint8_t slot_id, uint8_t gen, uint32_t chunk_idx);

    size_t clr_pending_size() const;
    size_t clr_pending_size_for(uint8_t slot_id, uint8_t gen) const;
    void   clr_clear_pending();

    // Test-only: advance the internal wait counter without driving the QP.
    // Used by aliasing-protection unit tests that cannot wait_for_ratio()
    // 256 times against a real engine. NOT for production callers.
    void advance_wait_seq_for_tests(uint64_t n) { wait_seq_ += n; }

    // Pending entries older than this many wait_for_ratio calls are evicted
    // by drain_pending before being claimed. Set to one full bucket_id cycle
    // so that an entry stashed under bucket_id=K cannot be falsely claimed by
    // a future wait_for_ratio(bucket_id=K) after K has wrapped around. See
    // P2-① in plans/p1cqe-dismiss-wait-for-ratio-zippy-engelbart.md.
    static constexpr uint64_t kPendingMaxAgeWaits = 256;

private:
    UCQPEngine& engine_;
    // PR-C: bucket_id (mod 256) → list of (LOCAL chunk_id, deposit_seq).
    // ``deposit_seq`` is the value of ``wait_seq_`` at the moment the entry
    // was stashed. drain_pending evicts entries whose age exceeds
    // kPendingMaxAgeWaits before claiming, which prevents 8-bit bucket_id
    // wrap from causing stale CQEs to be applied to a fresh ChunkSet.
    struct PendingEntry {
        uint32_t chunk_id;
        uint64_t deposit_seq;
    };
    std::unordered_map<uint8_t, std::vector<PendingEntry>> pending_cqes_;
    uint64_t wait_seq_ = 0;

    // CLEAR (W2.1): (slot, gen) lease_key → list of LOCAL chunk_idx
    // (20-bit) seen but not yet claimed. Separate map so PR-C and CLEAR
    // pending entries cannot alias.
    std::unordered_map<uint16_t, std::vector<uint32_t>> clr_pending_cqes_;
};

} // namespace semirdma
