/*
 * test_ratio_timeout — gtest: RatioController threshold and timeout behavior
 *
 * Client posts only 240/256 chunks (skips last 16 to simulate loss).
 * Tests:
 *   1. wait_for_ratio(0.90) succeeds (240/256 = 93.75% >= 90%)
 *   2. wait_for_ratio(1.00) times out (only 240/256 can arrive)
 */

#include <gtest/gtest.h>

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "test_helpers.h"

#include <cstring>

namespace {

constexpr size_t BUF_SIZE        = 4 * 1024 * 1024;
constexpr size_t CHUNK_BYTES     = 16 * 1024;
constexpr int    NUM_CHUNKS      = BUF_SIZE / CHUNK_BYTES;  // 256
constexpr int    CHUNKS_TO_SEND  = 240;
constexpr int    TCP_PORT        = 18521;
// Override via SEMIRDMA_DEV env var (e.g. mlx5_2 on CloudLab CX-6 Lx).
inline const char* const DEV = [](){
    const char* d = std::getenv("SEMIRDMA_DEV");
    return (d && *d) ? d : "rxe0";
}();
constexpr uint8_t FILL_BYTE      = 0x55;

TEST(RatioTimeout, PartialCompletionSucceeds)
{
    using namespace semirdma;
    using namespace semirdma::test;

    run_server_client(
        // ---- SERVER ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, 16, NUM_CHUNKS + 64);

            for (int i = 0; i < NUM_CHUNKS; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_server_exchange(TCP_PORT, local);
            engine.bring_up(remote.qp);

            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            RatioController rc(engine);
            WaitStats stats;

            // 240/256 = 93.75% >= 90% → should succeed
            bool ok = rc.wait_for_ratio(cs, 0.90, 5000, &stats);
            if (!ok) {
                fprintf(stderr, "[SERVER] Unexpected timeout: completed=%u\n",
                        stats.completed);
                return 1;
            }

            // wait_for_ratio returns as soon as 90% threshold is met,
            // so completed may be < CHUNKS_TO_SEND.  Verify the ratio.
            uint32_t min_expected = static_cast<uint32_t>(NUM_CHUNKS * 0.90);
            if (stats.completed < min_expected) {
                fprintf(stderr, "[SERVER] Below threshold: completed=%u, "
                        "min_expected=%u\n", stats.completed, min_expected);
                return 1;
            }

            fprintf(stderr, "[SERVER] ratio=0.90 OK: completed=%u/%d in %.2f ms\n",
                    stats.completed, NUM_CHUNKS, stats.latency_ms);
            return 0;
        },

        // ---- CLIENT ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, NUM_CHUNKS + 64, 16);
            std::memset(engine.local_buf(), FILL_BYTE, BUF_SIZE);

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_client_exchange("127.0.0.1", TCP_PORT, local);
            engine.bring_up(remote.qp);

            // Post only first 240 chunks
            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            for (int i = 0; i < CHUNKS_TO_SEND; i++) {
                const auto& cd = cs.chunk(i);
                engine.post_write(cd.chunk_id,
                                  cd.local_offset, cd.remote_offset,
                                  cd.length, remote.mr,
                                  true, cd.chunk_id);
            }

            // Drain sender CQEs
            int drained = 0;
            while (drained < CHUNKS_TO_SEND) {
                auto cqes = engine.poll_cq(16, 1000);
                drained += static_cast<int>(cqes.size());
            }
            return 0;
        }
    );
}

TEST(RatioTimeout, FullCompletionTimesOut)
{
    using namespace semirdma;
    using namespace semirdma::test;

    run_server_client(
        // ---- SERVER ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, 16, NUM_CHUNKS + 64);

            for (int i = 0; i < NUM_CHUNKS; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_server_exchange(TCP_PORT, local);
            engine.bring_up(remote.qp);

            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            RatioController rc(engine);
            WaitStats stats;

            // Only 240/256 arrive → ratio=1.00 should timeout
            bool ok = rc.wait_for_ratio(cs, 1.00, 1000, &stats);
            if (ok) {
                fprintf(stderr, "[SERVER] Unexpected success for ratio=1.00\n");
                return 1;
            }
            if (!stats.timed_out) {
                fprintf(stderr, "[SERVER] Expected timed_out=true\n");
                return 1;
            }
            if (stats.completed < static_cast<uint32_t>(CHUNKS_TO_SEND)) {
                fprintf(stderr, "[SERVER] completed=%u < %d\n",
                        stats.completed, CHUNKS_TO_SEND);
                return 1;
            }

            fprintf(stderr, "[SERVER] ratio=1.00 timed out as expected: "
                    "completed=%u, latency=%.2f ms\n",
                    stats.completed, stats.latency_ms);
            return 0;
        },

        // ---- CLIENT ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, NUM_CHUNKS + 64, 16);
            std::memset(engine.local_buf(), FILL_BYTE, BUF_SIZE);

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_client_exchange("127.0.0.1", TCP_PORT, local);
            engine.bring_up(remote.qp);

            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            for (int i = 0; i < CHUNKS_TO_SEND; i++) {
                const auto& cd = cs.chunk(i);
                engine.post_write(cd.chunk_id,
                                  cd.local_offset, cd.remote_offset,
                                  cd.length, remote.mr,
                                  true, cd.chunk_id);
            }

            int drained = 0;
            while (drained < CHUNKS_TO_SEND) {
                auto cqes = engine.poll_cq(16, 1000);
                drained += static_cast<int>(cqes.size());
            }
            return 0;
        }
    );
}

} // anonymous namespace
