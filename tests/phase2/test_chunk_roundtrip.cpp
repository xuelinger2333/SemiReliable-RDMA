/*
 * test_chunk_roundtrip — gtest: full-stack chunk transmission verification
 *
 * 4 MB buffer, 16 KB chunks = 256 chunks, loss rate = 0%.
 * Server receives all chunks via RatioController, verifies buffer content.
 *
 * Uses fork-based harness: parent = server, child = client.
 */

#include <gtest/gtest.h>

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "test_helpers.h"

#include <cstring>

namespace {

constexpr size_t BUF_SIZE    = 4 * 1024 * 1024;   // 4 MB
constexpr size_t CHUNK_BYTES = 16 * 1024;          // 16 KB
constexpr int    NUM_CHUNKS  = BUF_SIZE / CHUNK_BYTES;  // 256
constexpr int    TCP_PORT    = 18520;
// Override via SEMIRDMA_DEV env var (e.g. mlx5_2 on CloudLab CX-6 Lx).
inline const char* const DEV = [](){
    const char* d = std::getenv("SEMIRDMA_DEV");
    return (d && *d) ? d : "rxe0";
}();
constexpr uint8_t FILL_BYTE  = 0x42;

TEST(ChunkRoundtrip, AllChunksArrive)
{
    using namespace semirdma;
    using namespace semirdma::test;

    run_server_client(
        // ---- SERVER (parent) ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, 16, NUM_CHUNKS + 64);

            // Pre-post Recv WRs (one per expected chunk)
            for (int i = 0; i < NUM_CHUNKS; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            // TCP exchange
            ExchangeData local;
            local.qp = engine.local_qp_info();
            local.mr = engine.local_mr_info();
            ExchangeData remote = tcp_server_exchange(TCP_PORT, local);

            engine.bring_up(remote.qp);

            // Wait for all chunks
            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            RatioController rc(engine);
            WaitStats stats;
            bool ok = rc.wait_for_ratio(cs, 1.0, 10000, &stats);
            if (!ok) {
                fprintf(stderr, "[SERVER] Timeout: completed=%u/%d\n",
                        stats.completed, NUM_CHUNKS);
                return 1;
            }

            fprintf(stderr, "[SERVER] All %d chunks received in %.2f ms "
                    "(polls=%u)\n",
                    NUM_CHUNKS, stats.latency_ms, stats.poll_count);

            // Verify buffer content
            const uint8_t* buf = engine.local_buf();
            for (size_t i = 0; i < BUF_SIZE; i++) {
                if (buf[i] != FILL_BYTE) {
                    fprintf(stderr, "[SERVER] Mismatch at byte %zu: "
                            "expected 0x%02X, got 0x%02X\n",
                            i, FILL_BYTE, buf[i]);
                    return 1;
                }
            }

            fprintf(stderr, "[SERVER] Buffer content verified OK\n");
            return 0;
        },

        // ---- CLIENT (child) ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, NUM_CHUNKS + 64, 16);

            // Fill buffer with known pattern
            std::memset(engine.local_buf(), FILL_BYTE, BUF_SIZE);

            // TCP exchange
            ExchangeData local;
            local.qp = engine.local_qp_info();
            local.mr = engine.local_mr_info();
            ExchangeData remote = tcp_client_exchange("127.0.0.1", TCP_PORT, local);

            engine.bring_up(remote.qp);

            // Post all chunks as Write-with-Immediate
            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            for (size_t i = 0; i < cs.size(); i++) {
                const auto& cd = cs.chunk(i);
                engine.post_write(cd.chunk_id,
                                  cd.local_offset,
                                  cd.remote_offset,
                                  cd.length,
                                  remote.mr,
                                  true,          // with_imm
                                  cd.chunk_id);  // imm_data = chunk_id
            }

            // Drain sender CQEs
            int drained = 0;
            while (drained < NUM_CHUNKS) {
                auto cqes = engine.poll_cq(16, 1000);
                drained += static_cast<int>(cqes.size());
            }

            fprintf(stderr, "[CLIENT] Sent and drained %d WRs\n", drained);
            return 0;
        }
    );
}

} // anonymous namespace
