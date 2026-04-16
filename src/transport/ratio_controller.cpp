/*
 * ratio_controller.cpp — RatioController implementation
 *
 * Single-threaded busy-poll loop.  Phase 2 does not use event channels;
 * P0 confirmed ibv_poll_cq latency is in the microsecond range on SoftRoCE.
 */

#include "transport/ratio_controller.h"
#include "utils/timing.h"

namespace semirdma {

bool RatioController::wait_for_ratio(ChunkSet&  cs,
                                     double     ratio,
                                     int        timeout_ms,
                                     WaitStats* stats)
{
    Stopwatch sw;
    uint32_t poll_count = 0;

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
                cs.mark_completed(c.imm_data);
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
