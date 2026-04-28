/*
 * test_bucket_id_isolation.cpp — PR-C: cross-bucket CQE aliasing
 *
 * Background
 * ----------
 * Pre-PR-C, ``imm_data == chunk_id`` (24-bit local index inside the ChunkSet).
 * When two buckets are in flight simultaneously (e.g. ResNet-18 with
 * ``bucket_cap_mb=1`` ≈ 50 buckets/step), bucket K+1's chunk_id=N CQE arrives
 * while the receiver is still waiting for bucket K's ratio. The old
 * ``RatioController`` would mark that CQE on bucket K's ``cs`` (because the
 * imm_data values alias), corrupting K's completion bitmap and leaving K+1's
 * ``cs`` empty (so bucket K+1's await would time out and ghost-mask zero
 * everything).
 *
 * Fix (PR-C): ``imm_data = (bucket_id_mod256 << 24) | (chunk_id & 0xffffff)``.
 * ``RatioController::wait_for_ratio`` takes ``expected_bucket_id`` and routes
 * foreign CQEs to a per-(bucket_id) pending queue. Subsequent
 * ``wait_for_ratio`` for that bucket drains the queue first.
 *
 * What this test exercises
 * ------------------------
 * Client posts buckets 0 and 1 back-to-back (each 16 chunks, encoded via
 * ``(bucket_id<<24) | chunk_id``). Server runs two ``wait_for_ratio`` calls
 * in sequence:
 *
 *   wait_for_ratio(cs_b0, ratio=1.0, timeout=2000ms, expected_bucket_id=0)
 *      — must succeed (16/16 bucket-0 CQEs marked on cs_b0; bucket-1 CQEs
 *        seen during the poll loop are stashed in pending_cqes_).
 *
 *   wait_for_ratio(cs_b1, ratio=1.0, timeout=200ms, expected_bucket_id=1)
 *      — must succeed quickly (drain_pending feeds all 16 stashed entries
 *        onto cs_b1 before the poll loop even starts).
 *
 * Invariants verified:
 *   - cs_b0.completion_ratio() == 1.0 after first wait
 *   - cs_b1.completion_ratio() == 1.0 after second wait
 *   - rc.pending_size() == 0 after both waits (all CQEs consumed)
 *   - rc.pending_size() > 0 between the two waits (some bucket-1 CQEs
 *     stashed during bucket-0's wait — only meaningful if the poll loop
 *     actually saw any bucket-1 CQEs interleaved; if not, this just
 *     verifies the queue is empty after bucket-1's drain)
 */

#include <gtest/gtest.h>

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "test_helpers.h"

#include <cstring>

namespace {

// Two buckets, 16 chunks each. Small enough to finish fast, large enough
// that interleaved arrival is plausible on SoftRoCE.
constexpr size_t BUF_SIZE       = 4 * 1024 * 1024;
constexpr size_t BUCKET_BYTES   = 64 * 1024;        // 16 chunks × 4 KiB
constexpr size_t CHUNK_BYTES    = 4 * 1024;
constexpr int    CHUNKS_PER_BKT = BUCKET_BYTES / CHUNK_BYTES;  // 16
constexpr int    N_BUCKETS      = 2;
constexpr int    TCP_PORT       = 18525;

inline const char* const DEV = [](){
    const char* d = std::getenv("SEMIRDMA_DEV");
    return (d && *d) ? d : "rxe0";
}();

constexpr uint8_t FILL_BYTE = 0xAB;

inline uint32_t encode_imm(uint8_t bucket_id, uint32_t chunk_id) {
    return (static_cast<uint32_t>(bucket_id) << 24)
         | (chunk_id & 0xFFFFFFu);
}

TEST(BucketIdIsolation, TwoBucketsBackToBack)
{
    using namespace semirdma;
    using namespace semirdma::test;

    run_server_client(
        // ---- SERVER (receiver) ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, 16,
                              N_BUCKETS * CHUNKS_PER_BKT + 64);

            // Pre-post enough recvs for both buckets.
            for (int i = 0; i < N_BUCKETS * CHUNKS_PER_BKT; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_server_exchange(TCP_PORT, local);
            engine.bring_up(remote.qp);

            RatioController rc(engine);

            // Each bucket lives in a distinct slot of the MR so wires
            // don't collide on writes.
            ChunkSet cs_b0(0,            BUCKET_BYTES, CHUNK_BYTES);
            ChunkSet cs_b1(BUCKET_BYTES, BUCKET_BYTES, CHUNK_BYTES);

            WaitStats stats0, stats1;

            // ---- Wait for bucket 0 ----
            // Must succeed at ratio=1.0 within 2 s (16 chunks @ SoftRoCE).
            // During the poll loop, ANY bucket-1 CQEs that arrive get
            // stashed in rc.pending_cqes_ rather than incorrectly marked
            // on cs_b0.
            bool ok0 = rc.wait_for_ratio(cs_b0, 1.0, 2000,
                                         /*expected_bucket_id=*/0, &stats0);
            if (!ok0) {
                fprintf(stderr,
                    "[SERVER] bucket-0 wait FAILED: completed=%u/%d "
                    "timed_out=%d latency=%.2fms\n",
                    stats0.completed, CHUNKS_PER_BKT,
                    stats0.timed_out, stats0.latency_ms);
                return 1;
            }
            if (cs_b0.completion_ratio() < 1.0) {
                fprintf(stderr,
                    "[SERVER] bucket-0 ratio %.3f < 1.0 (completed=%zu/%zu)\n",
                    cs_b0.completion_ratio(),
                    cs_b0.num_completed(), cs_b0.size());
                return 2;
            }
            fprintf(stderr,
                "[SERVER] bucket-0 OK: %u/%d in %.2fms; pending after=%zu\n",
                stats0.completed, CHUNKS_PER_BKT, stats0.latency_ms,
                rc.pending_size());

            // ---- Wait for bucket 1 ----
            // Tight 200 ms timeout. Must succeed because drain_pending
            // pulls all bucket-1 CQEs (already in flight or already
            // queued) onto cs_b1 before the poll loop. Without PR-C,
            // these CQEs were lost to cs_b0 → bucket-1 would timeout
            // with completed=0.
            bool ok1 = rc.wait_for_ratio(cs_b1, 1.0, 200,
                                         /*expected_bucket_id=*/1, &stats1);
            if (!ok1) {
                fprintf(stderr,
                    "[SERVER] bucket-1 wait FAILED: completed=%u/%d "
                    "timed_out=%d latency=%.2fms (regression: pre-PR-C "
                    "behavior)\n",
                    stats1.completed, CHUNKS_PER_BKT,
                    stats1.timed_out, stats1.latency_ms);
                return 3;
            }
            if (cs_b1.completion_ratio() < 1.0) {
                fprintf(stderr,
                    "[SERVER] bucket-1 ratio %.3f < 1.0 (completed=%zu/%zu)\n",
                    cs_b1.completion_ratio(),
                    cs_b1.num_completed(), cs_b1.size());
                return 4;
            }
            fprintf(stderr,
                "[SERVER] bucket-1 OK: %u/%d in %.2fms; pending after=%zu\n",
                stats1.completed, CHUNKS_PER_BKT, stats1.latency_ms,
                rc.pending_size());

            // All CQEs accounted for → pending queue empty.
            if (rc.pending_size() != 0) {
                fprintf(stderr,
                    "[SERVER] pending queue NOT empty: %zu entries left\n",
                    rc.pending_size());
                return 5;
            }
            return 0;
        },

        // ---- CLIENT (sender) ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE,
                              N_BUCKETS * CHUNKS_PER_BKT + 64, 16);
            std::memset(engine.local_buf(), FILL_BYTE, BUF_SIZE);

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_client_exchange("127.0.0.1", TCP_PORT,
                                                     local);
            engine.bring_up(remote.qp);

            // Build two ChunkSets in distinct MR slots and post them
            // back-to-back so receiver sees interleaved CQEs.
            ChunkSet cs_b0(0,            BUCKET_BYTES, CHUNK_BYTES);
            ChunkSet cs_b1(BUCKET_BYTES, BUCKET_BYTES, CHUNK_BYTES);

            uint64_t wr_seq = 1;
            for (uint8_t bid = 0; bid < N_BUCKETS; bid++) {
                const ChunkSet& cs = (bid == 0) ? cs_b0 : cs_b1;
                for (int i = 0; i < CHUNKS_PER_BKT; i++) {
                    const auto& cd = cs.chunk(i);
                    uint32_t imm = encode_imm(bid, cd.chunk_id);
                    engine.post_write(wr_seq++,
                                      cd.local_offset, cd.remote_offset,
                                      cd.length, remote.mr,
                                      /*with_imm=*/true,
                                      imm);
                }
            }

            // Drain sender CQEs to keep the SQ from filling up.
            int drained = 0;
            int target  = N_BUCKETS * CHUNKS_PER_BKT;
            while (drained < target) {
                auto cqes = engine.poll_cq(32, 1000);
                drained += static_cast<int>(cqes.size());
            }
            return 0;
        }
    );
}

} // anonymous namespace
