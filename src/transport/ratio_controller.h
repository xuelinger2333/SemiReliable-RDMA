/*
 * ratio_controller.h — CQE-driven completion ratio polling
 *
 * Given a ChunkSet, polls the engine's CQ in a loop.  Each CQE's imm_data
 * identifies the completed chunk (via ChunkSet::mark_completed).  Returns
 * when completion_ratio() >= target or timeout expires.
 *
 * Key invariant: only looks at CQE, never scans the buffer (P0 conclusion ②).
 */

#pragma once

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"

#include <cstdint>

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
    // Returns true if ratio was reached, false on timeout.
    // stats (optional): filled with performance counters for RQ4 experiments.
    bool wait_for_ratio(ChunkSet&  cs,
                        double     ratio,
                        int        timeout_ms,
                        WaitStats* stats = nullptr);

private:
    UCQPEngine& engine_;
};

} // namespace semirdma
