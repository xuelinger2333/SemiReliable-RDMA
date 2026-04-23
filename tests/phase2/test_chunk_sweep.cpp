/*
 * test_chunk_sweep — RQ1 chunk-size sweep experiment
 *
 * Sweeps chunk_size × loss_rate, measuring per-cell:
 *   - observed_ghost_ratio
 *   - effective_goodput (MB/s)
 *   - wqe_throughput (WR/s)
 *   - tail_latency_p99 (ms)
 *
 * Usage (two terminals, like Phase 1 test_netem_loss):
 *   ./test_chunk_sweep server [device] [rounds]
 *   ./test_chunk_sweep client <server_ip> [device] [rounds] [seed]
 *
 * Sweep parameters are embedded in the binary (not CLI args) for reproducibility.
 * The server outputs CSV to stdout.
 */

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "transport/ghost_mask.h"
#include "utils/logging.h"
#include "utils/timing.h"
#include "test_helpers.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <numeric>
#include <vector>

using namespace semirdma;
using namespace semirdma::test;

// ================================================================
//  Sweep parameters (fixed, matching design doc §2.1)
// ================================================================

constexpr size_t BUF_SIZE = 4 * 1024 * 1024;  // 4 MB per "layer"

static const size_t CHUNK_SIZES[] = {
    1  * 1024,    //  1 KB
    4  * 1024,    //  4 KB
    16 * 1024,    // 16 KB
    64 * 1024,    // 64 KB
    256 * 1024,   // 256 KB
};
constexpr int NUM_CHUNK_SIZES = sizeof(CHUNK_SIZES) / sizeof(CHUNK_SIZES[0]);

static const double LOSS_RATES[] = {
    0.000,   //  0%
    0.001,   //  0.1%
    0.010,   //  1%
    0.050,   //  5%
};
constexpr int NUM_LOSS_RATES = sizeof(LOSS_RATES) / sizeof(LOSS_RATES[0]);

constexpr int    DEFAULT_ROUNDS = 500;
constexpr int    TCP_PORT       = 18525;  // persistent TCP for exchange + round sync
constexpr double WAIT_RATIO     = 1.0;    // wait for all expected chunks
constexpr int    WAIT_TIMEOUT   = 5000;   // 5 seconds

// Post-wait straggler-drain timeout (ms).  Historically 50ms to let SoftRoCE
// stragglers land; on real NICs this becomes the dominant per-round cost
// (CX-6 Lx chunks arrive in <1ms, so 50ms blocks for a full 50ms).
// Override via SEMIRDMA_DRAIN_MS env var.  Recommended: 0 on real HCA.
inline int drain_ms() {
    const char* e = std::getenv("SEMIRDMA_DRAIN_MS");
    return e ? atoi(e) : 50;
}
inline int drain_settle_us() {
    const char* e = std::getenv("SEMIRDMA_SETTLE_US");
    return e ? atoi(e) : 5000;  // 5 ms pre-drain sleep (SoftRoCE default)
}

// ================================================================
//  Server
// ================================================================

static void run_server(const char* dev_name, int rounds, unsigned /*seed*/)
{
    // Print CSV header
    printf("chunk_bytes,loss_pct,rounds,"
           "ghost_ratio,effective_goodput_MBs,wqe_throughput,"
           "p50_ms,p99_ms\n");
    fflush(stdout);

    for (int ci = 0; ci < NUM_CHUNK_SIZES; ci++) {
        for (int li = 0; li < NUM_LOSS_RATES; li++) {

            const size_t chunk_bytes = CHUNK_SIZES[ci];
            const double loss_rate   = LOSS_RATES[li];
            const int num_chunks     = static_cast<int>(
                (BUF_SIZE + chunk_bytes - 1) / chunk_bytes);
            const int max_rq         = num_chunks + 64;

            fprintf(stderr, "\n==== chunk=%zuKB  loss=%.1f%%  "
                    "chunks/round=%d ====\n",
                    chunk_bytes / 1024, loss_rate * 100.0, num_chunks);

            // Create engine with enough RQ depth for one round
            UCQPEngine engine(dev_name, BUF_SIZE, 16, max_rq);

            // Persistent TCP: exchange QP info, then reuse fd for round sync
            int tcp_fd = tcp_listen_accept(TCP_PORT);
            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_exchange_on_fd_server(tcp_fd, local);
            engine.bring_up(remote.qp);

            // Per-round metrics
            std::vector<double> latencies;
            latencies.reserve(rounds);
            size_t total_ghost_chunks = 0;
            size_t total_chunks       = 0;
            double total_goodput_bytes = 0.0;

            // Pre-post Recv WRs once (one round's worth).
            // After each round we refill only the consumed count,
            // keeping outstanding Recv WRs constant and avoiding RQ overflow.
            for (int i = 0; i < num_chunks; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            for (int r = 0; r < rounds; r++) {
                // Reset buffer to detect ghost data
                std::memset(engine.local_buf(), 0, BUF_SIZE);

                // Signal client: ready
                tcp_signal(tcp_fd);
                // Wait for client: done posting
                tcp_wait(tcp_fd);

                // Read how many chunks client actually sent this round
                int32_t sent_count = 0;
                ssize_t nr = read(tcp_fd, &sent_count, sizeof(sent_count));
                (void)nr;

                // Wait for the chunks that were actually sent
                ChunkSet cs(0, BUF_SIZE, chunk_bytes);
                RatioController rc(engine);
                WaitStats stats;

                Stopwatch round_sw;
                if (sent_count > 0) {
                    double target = static_cast<double>(sent_count)
                                  / static_cast<double>(num_chunks);
                    rc.wait_for_ratio(cs, target, WAIT_TIMEOUT, &stats);

                    // Extra drain to catch stragglers.  Cost dominates round
                    // time on fast HCAs (CX-6+); set SEMIRDMA_DRAIN_MS=0 and
                    // SEMIRDMA_SETTLE_US=0 when real-hardware re-calibrating.
                    if (drain_settle_us() > 0) usleep(drain_settle_us());
                    auto extra = engine.poll_cq(64, drain_ms());
                    for (const auto& c : extra) {
                        if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM &&
                            c.status == IBV_WC_SUCCESS) {
                            cs.mark_completed(c.imm_data);
                        }
                    }
                }
                double round_ms = round_sw.elapsed_ms();

                size_t completed = cs.num_completed();
                size_t ghost     = num_chunks - completed;
                total_ghost_chunks += ghost;
                total_chunks       += num_chunks;
                total_goodput_bytes += completed * chunk_bytes;
                latencies.push_back(round_ms);

                // Refill RQ: re-post exactly the number consumed this round,
                // so outstanding Recv WRs stay at num_chunks.
                for (size_t i = 0; i < completed; i++) {
                    engine.post_recv(0);
                }
            }

            close(tcp_fd);

            // Compute aggregate metrics
            double ghost_ratio = static_cast<double>(total_ghost_chunks)
                               / static_cast<double>(total_chunks);

            double total_time_ms = 0.0;
            for (double t : latencies) total_time_ms += t;
            double effective_goodput = (total_goodput_bytes / (1024.0 * 1024.0))
                                     / (total_time_ms / 1000.0);

            double wqe_throughput = static_cast<double>(total_chunks - total_ghost_chunks)
                                  / (total_time_ms / 1000.0);

            // P50 and P99 latencies
            std::sort(latencies.begin(), latencies.end());
            double p50 = latencies[latencies.size() / 2];
            double p99 = latencies[static_cast<size_t>(latencies.size() * 0.99)];

            // CSV output
            printf("%zu,%.1f,%d,%.6f,%.2f,%.0f,%.3f,%.3f\n",
                   chunk_bytes, loss_rate * 100.0, rounds,
                   ghost_ratio, effective_goodput, wqe_throughput,
                   p50, p99);
            fflush(stdout);

            fprintf(stderr, "  ghost_ratio=%.4f  goodput=%.2f MB/s  "
                    "wqe=%.0f/s  p50=%.3f ms  p99=%.3f ms\n",
                    ghost_ratio, effective_goodput, wqe_throughput, p50, p99);
        }
    }
}

// ================================================================
//  Client
// ================================================================

static void run_client(const char* dev_name, const char* server_ip,
                       int rounds, unsigned seed)
{
    for (int ci = 0; ci < NUM_CHUNK_SIZES; ci++) {
        for (int li = 0; li < NUM_LOSS_RATES; li++) {

            const size_t chunk_bytes = CHUNK_SIZES[ci];
            const double loss_rate   = LOSS_RATES[li];
            const int num_chunks     = static_cast<int>(
                (BUF_SIZE + chunk_bytes - 1) / chunk_bytes);
            const int max_sq         = num_chunks + 64;

            UCQPEngine engine(dev_name, BUF_SIZE, max_sq, 16);

            // Persistent TCP: exchange QP info, then reuse fd for round sync
            int tcp_fd = tcp_connect_to(server_ip, TCP_PORT);
            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_exchange_on_fd_client(tcp_fd, local);
            engine.bring_up(remote.qp);

            unsigned rng = seed;

            for (int r = 0; r < rounds; r++) {
                uint32_t round_id = static_cast<uint32_t>(r + 1);

                // Fill buffer with round-specific pattern
                auto* buf32 = reinterpret_cast<uint32_t*>(engine.local_buf());
                size_t n_words = BUF_SIZE / sizeof(uint32_t);
                for (size_t w = 0; w < n_words; w++) {
                    buf32[w] = round_id;
                }

                // Wait for server ready
                tcp_wait(tcp_fd);

                // Per-chunk loss injection: each chunk independently dropped
                ChunkSet cs(0, BUF_SIZE, chunk_bytes);
                int sent_count = 0;

                for (size_t i = 0; i < cs.size(); i++) {
                    // Draw random: skip this chunk if "lost"
                    double u = static_cast<double>(rand_r(&rng))
                             / static_cast<double>(RAND_MAX);
                    if (u < loss_rate) {
                        continue;  // simulate chunk loss
                    }

                    const auto& cd = cs.chunk(i);
                    engine.post_write(cd.chunk_id,
                                      cd.local_offset, cd.remote_offset,
                                      cd.length, remote.mr,
                                      true, cd.chunk_id);
                    sent_count++;
                }

                // Drain sender CQEs
                int drained = 0;
                while (drained < sent_count) {
                    auto cqes = engine.poll_cq(32, 2000);
                    drained += static_cast<int>(cqes.size());
                }

                // Signal server: done posting
                tcp_signal(tcp_fd);
                // Tell server how many chunks we actually sent
                int32_t sc = sent_count;
                ssize_t nw = write(tcp_fd, &sc, sizeof(sc));
                (void)nw;
            }

            close(tcp_fd);

            fprintf(stderr, "[CLIENT] Done: chunk=%zuKB loss=%.1f%% "
                    "%d rounds\n",
                    chunk_bytes / 1024, loss_rate * 100.0, rounds);
        }
    }
}

// ================================================================
//  Main
// ================================================================

int main(int argc, char* argv[])
{
    if (argc < 2) {
        fprintf(stderr,
            "Usage:\n"
            "  %s server [device] [rounds]\n"
            "  %s client <server_ip> [device] [rounds] [seed]\n",
            argv[0], argv[0]);
        return 1;
    }

    const char* role = argv[1];

    if (strcmp(role, "server") == 0) {
        const char* dev    = (argc >= 3) ? argv[2] : "rxe0";
        int         rounds = (argc >= 4) ? atoi(argv[3]) : DEFAULT_ROUNDS;
        unsigned    seed   = 42;
        run_server(dev, rounds, seed);
    } else if (strcmp(role, "client") == 0) {
        const char* ip     = (argc >= 3) ? argv[2] : "127.0.0.1";
        const char* dev    = (argc >= 4) ? argv[3] : "rxe0";
        int         rounds = (argc >= 5) ? atoi(argv[4]) : DEFAULT_ROUNDS;
        unsigned    seed   = (argc >= 6) ? static_cast<unsigned>(atoi(argv[5])) : 42u;
        run_client(dev, ip, rounds, seed);
    } else {
        fprintf(stderr, "Unknown role '%s'\n", role);
        return 1;
    }

    return 0;
}
