/*
 * test_ghost_mask — gtest: GhostMask zeroing and no-op control group
 *
 * Client posts 240/256 chunks with pattern 0x42.
 * Server pre-fills buffer with 0xDE ("old stale data"), receives,
 * then applies GhostMask:
 *   - Chunks 0-239:  0x42 (received, preserved)
 *   - Chunks 240-255: 0x00 (ghost, zeroed by GhostMask)
 *
 * Control test: apply_noop leaves ghost chunks as 0xDE.
 */

#include <gtest/gtest.h>

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "transport/ghost_mask.h"
#include "test_helpers.h"

#include <cstring>

namespace {

constexpr size_t BUF_SIZE        = 4 * 1024 * 1024;
constexpr size_t CHUNK_BYTES     = 16 * 1024;
constexpr int    NUM_CHUNKS      = BUF_SIZE / CHUNK_BYTES;
constexpr int    CHUNKS_TO_SEND  = 240;
constexpr int    TCP_PORT        = 18522;
constexpr const char* DEV        = "rxe0";
constexpr uint8_t NEW_PATTERN    = 0x42;
constexpr uint8_t OLD_PATTERN    = 0xDE;

TEST(GhostMask, ZerosGhostChunks)
{
    using namespace semirdma;
    using namespace semirdma::test;

    run_server_client(
        // ---- SERVER ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, 16, NUM_CHUNKS + 64);

            // Fill buffer with "old stale data" to simulate ghost gradient
            std::memset(engine.local_buf(), OLD_PATTERN, BUF_SIZE);

            for (int i = 0; i < NUM_CHUNKS; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_server_exchange(TCP_PORT, local);
            engine.bring_up(remote.qp);

            // Wait for the 240 chunks that will arrive
            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            RatioController rc(engine);
            WaitStats stats;
            // 240/256 = 93.75%, wait for 90% with generous timeout
            rc.wait_for_ratio(cs, 0.90, 5000, &stats);

            // Drain any remaining CQEs with a short extra poll
            usleep(100000);
            auto extra = engine.poll_cq(64, 100);
            for (const auto& c : extra) {
                if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM &&
                    c.status == IBV_WC_SUCCESS) {
                    cs.mark_completed(c.imm_data);
                }
            }

            fprintf(stderr, "[SERVER] Completed %zu/%d chunks\n",
                    cs.num_completed(), NUM_CHUNKS);

            // Apply GhostMask
            GhostMask::apply(engine.local_buf(), cs);

            // Verify received chunks (0-239) contain NEW_PATTERN
            const uint8_t* buf = engine.local_buf();
            for (int i = 0; i < CHUNKS_TO_SEND; i++) {
                size_t off = cs.chunk(i).local_offset;
                size_t len = cs.chunk(i).length;
                for (size_t j = 0; j < len; j++) {
                    if (buf[off + j] != NEW_PATTERN) {
                        fprintf(stderr, "[SERVER] Chunk %d byte %zu: "
                                "expected 0x%02X, got 0x%02X\n",
                                i, j, NEW_PATTERN, buf[off + j]);
                        return 1;
                    }
                }
            }

            // Verify ghost chunks (240-255) are zeroed
            for (int i = CHUNKS_TO_SEND; i < NUM_CHUNKS; i++) {
                size_t off = cs.chunk(i).local_offset;
                size_t len = cs.chunk(i).length;
                for (size_t j = 0; j < len; j++) {
                    if (buf[off + j] != 0x00) {
                        fprintf(stderr, "[SERVER] Ghost chunk %d byte %zu: "
                                "expected 0x00, got 0x%02X\n",
                                i, j, buf[off + j]);
                        return 1;
                    }
                }
            }

            fprintf(stderr, "[SERVER] GhostMask verification OK: "
                    "%d received + %d zeroed\n",
                    CHUNKS_TO_SEND, NUM_CHUNKS - CHUNKS_TO_SEND);
            return 0;
        },

        // ---- CLIENT ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, NUM_CHUNKS + 64, 16);
            std::memset(engine.local_buf(), NEW_PATTERN, BUF_SIZE);

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

TEST(GhostMask, NoopPreservesOldData)
{
    using namespace semirdma;
    using namespace semirdma::test;

    run_server_client(
        // ---- SERVER ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, 16, NUM_CHUNKS + 64);
            std::memset(engine.local_buf(), OLD_PATTERN, BUF_SIZE);

            for (int i = 0; i < NUM_CHUNKS; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_server_exchange(TCP_PORT, local);
            engine.bring_up(remote.qp);

            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            RatioController rc(engine);
            rc.wait_for_ratio(cs, 0.90, 5000);

            usleep(100000);
            auto extra = engine.poll_cq(64, 100);
            for (const auto& c : extra) {
                if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM &&
                    c.status == IBV_WC_SUCCESS) {
                    cs.mark_completed(c.imm_data);
                }
            }

            // Apply no-op masking (control group)
            GhostMask::apply_noop(engine.local_buf(), cs);

            // Ghost chunks should still have OLD_PATTERN
            const uint8_t* buf = engine.local_buf();
            for (int i = CHUNKS_TO_SEND; i < NUM_CHUNKS; i++) {
                size_t off = cs.chunk(i).local_offset;
                size_t len = cs.chunk(i).length;
                for (size_t j = 0; j < len; j++) {
                    if (buf[off + j] != OLD_PATTERN) {
                        fprintf(stderr, "[SERVER] Ghost chunk %d byte %zu: "
                                "expected 0x%02X (old), got 0x%02X\n",
                                i, j, OLD_PATTERN, buf[off + j]);
                        return 1;
                    }
                }
            }

            fprintf(stderr, "[SERVER] apply_noop control OK: "
                    "ghost chunks still 0x%02X\n", OLD_PATTERN);
            return 0;
        },

        // ---- CLIENT (same as above) ----
        []() -> int {
            UCQPEngine engine(DEV, BUF_SIZE, NUM_CHUNKS + 64, 16);
            std::memset(engine.local_buf(), NEW_PATTERN, BUF_SIZE);

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
