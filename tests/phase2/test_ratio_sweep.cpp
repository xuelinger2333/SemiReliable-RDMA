/*
 * test_ratio_sweep — RQ4 CQE-driven ratio / timeout parameter sweep
 *
 * Sweeps (ratio, timeout_ms) while holding chunk size (16 KB / 256 chunks),
 * buffer size (4 MB), and loss rate (1%) fixed.  For each cell, measures:
 *   - wait_latency_ms (mean / p50 / p99)  — RatioController::wait_for_ratio duration
 *   - achieved_ratio                      — completed / NUM_CHUNKS at return time
 *   - cqe_poll_count                      — number of ibv_poll_cq calls per wait
 *   - timeout_rate                        — fraction of rounds returning timed_out=true
 *
 * Important timing choice (differs from RQ2):
 *   The server calls wait_for_ratio BEFORE it receives "client done" over TCP.
 *   TCP signal "ready" is sent, then server immediately begins polling the CQ
 *   while the client posts Writes in parallel.  This gives realistic wait
 *   latencies; if the server first waited for client-done (as RQ2 does for
 *   determinism), wait_latency would collapse to ~0 because CQEs are already
 *   delivered before polling begins.
 *
 * Loss injection: per-chunk Bernoulli at client (1%) — same pattern as RQ2.
 *
 * Usage (two terminals, loopback):
 *   ./test_ratio_sweep server [device] [rounds] [perround_csv_path]
 *   ./test_ratio_sweep client <server_ip> [device] [rounds] [seed]
 *
 * Main CSV → stdout, per-round CSV → file path (default ./rq4_perround.csv).
 */

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "utils/logging.h"
#include "utils/timing.h"
#include "test_helpers.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using namespace semirdma;
using namespace semirdma::test;

// ================================================================
//  Fixed experiment parameters
// ================================================================

constexpr size_t BUF_SIZE     = 4 * 1024 * 1024;   // 4 MB per "layer"
constexpr size_t CHUNK_BYTES  = 16 * 1024;         // 16 KB
constexpr int    NUM_CHUNKS   = static_cast<int>(BUF_SIZE / CHUNK_BYTES); // 256

constexpr double LOSS_RATE      = 0.010;           // p = 1%  (fixed)
constexpr int    DEFAULT_ROUNDS = 500;
constexpr int    TCP_PORT       = 18527;           // separate from RQ1 (18525), RQ2 (18526)

constexpr unsigned BASE_SEED = 42;

// Sweep axes (design-core-transport.md §2.3)
static const double  RATIOS[]    = { 0.90, 0.95, 0.99, 1.00 };
static const int     TIMEOUTS[]  = { 1, 5, 20, 100 };
constexpr int NUM_RATIOS   = sizeof(RATIOS)   / sizeof(RATIOS[0]);
constexpr int NUM_TIMEOUTS = sizeof(TIMEOUTS) / sizeof(TIMEOUTS[0]);

// ================================================================
//  Helpers
// ================================================================

static double percentile(std::vector<double> v, double q)
{
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    size_t idx = static_cast<size_t>(v.size() * q);
    if (idx >= v.size()) idx = v.size() - 1;
    return v[idx];
}

static double percentile_u32(std::vector<uint32_t> v, double q)
{
    if (v.empty()) return 0.0;
    std::sort(v.begin(), v.end());
    size_t idx = static_cast<size_t>(v.size() * q);
    if (idx >= v.size()) idx = v.size() - 1;
    return static_cast<double>(v[idx]);
}

// Drain any stragglers the ratio wait didn't consume, so they don't leak into
// the next round.  No timing — best-effort for correctness.
static void drain_stragglers(UCQPEngine& engine, ChunkSet& cs, int budget_ms)
{
    Stopwatch sw;
    while (sw.elapsed_ms() < budget_ms) {
        auto cqes = engine.poll_cq(64, 0);
        if (cqes.empty()) {
            usleep(100);
            continue;
        }
        for (const auto& c : cqes) {
            if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM &&
                c.status == IBV_WC_SUCCESS) {
                cs.mark_completed(c.imm_data);
            }
        }
    }
}

// ================================================================
//  Server
// ================================================================

static void run_server(const char* dev_name, int rounds,
                       const char* perround_path)
{
    FILE* perround = std::fopen(perround_path, "w");
    if (!perround) {
        fprintf(stderr, "[SERVER] Cannot open %s for writing\n", perround_path);
        return;
    }
    fprintf(perround,
        "ratio,timeout_ms,round_id,sent_count,completed,"
        "achieved_ratio,wait_ms,poll_count,timed_out\n");

    // Main CSV header → stdout
    printf(
        "ratio,timeout_ms,rounds,loss_pct,"
        "mean_sent_count,mean_completed,mean_achieved_ratio,timeout_rate,"
        "mean_wait_ms,p50_wait_ms,p99_wait_ms,"
        "mean_poll_count,p99_poll_count\n");
    fflush(stdout);

    const int max_rq = NUM_CHUNKS + 64;

    for (int ri = 0; ri < NUM_RATIOS; ri++) {
        for (int ti = 0; ti < NUM_TIMEOUTS; ti++) {
            const double target_ratio = RATIOS[ri];
            const int    timeout_ms   = TIMEOUTS[ti];

            fprintf(stderr,
                "\n==== ratio=%.2f  timeout=%d ms  loss=%.2f%%  rounds=%d ====\n",
                target_ratio, timeout_ms, LOSS_RATE * 100.0, rounds);

            UCQPEngine engine(dev_name, BUF_SIZE, 16, max_rq);

            int tcp_fd = tcp_listen_accept(TCP_PORT);
            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_exchange_on_fd_server(tcp_fd, local);
            engine.bring_up(remote.qp);

            // Pre-post Recv WRs (one round's worth).  After each round we
            // re-post exactly the number consumed, keeping outstanding ≈ NUM_CHUNKS.
            for (int i = 0; i < NUM_CHUNKS; i++) {
                engine.post_recv(static_cast<uint64_t>(i));
            }

            std::vector<double>   wait_list;        wait_list.reserve(rounds);
            std::vector<uint32_t> poll_list;        poll_list.reserve(rounds);
            std::vector<uint32_t> completed_list;   completed_list.reserve(rounds);
            std::vector<int32_t>  sent_list;        sent_list.reserve(rounds);
            int timeout_count = 0;

            for (int r = 0; r < rounds; r++) {
                ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
                RatioController rc(engine);
                WaitStats stats;

                // Tell client we're ready to receive; start the clock immediately.
                tcp_signal(tcp_fd);

                rc.wait_for_ratio(cs, target_ratio, timeout_ms, &stats);

                // After wait (success or timeout), synchronize with client
                // so we can drain stragglers deterministically.
                tcp_wait(tcp_fd);
                int32_t sent_count = 0;
                ssize_t nr = read(tcp_fd, &sent_count, sizeof(sent_count));
                (void)nr;

                // Drain any remaining CQEs before next round (not timed).
                drain_stragglers(engine, cs, 20);

                size_t final_completed = cs.num_completed();

                wait_list.push_back(stats.latency_ms);
                poll_list.push_back(stats.poll_count);
                completed_list.push_back(stats.completed);
                sent_list.push_back(sent_count);
                if (stats.timed_out) timeout_count++;

                double achieved = static_cast<double>(stats.completed)
                                / static_cast<double>(NUM_CHUNKS);

                fprintf(perround,
                    "%.2f,%d,%d,%d,%u,%.6f,%.6f,%u,%d\n",
                    target_ratio, timeout_ms, r,
                    sent_count, stats.completed, achieved,
                    stats.latency_ms, stats.poll_count,
                    stats.timed_out ? 1 : 0);

                // Re-post recvs for next round.  We consumed final_completed
                // (wait-time CQEs + drained stragglers), so re-post that many.
                for (size_t i = 0; i < final_completed; i++) {
                    engine.post_recv(0);
                }
            }

            close(tcp_fd);
            fflush(perround);

            // Cell aggregate
            double sum_wait = 0.0;
            for (double v : wait_list) sum_wait += v;
            double sum_poll = 0.0;
            for (uint32_t v : poll_list) sum_poll += v;
            double sum_completed = 0.0;
            for (uint32_t v : completed_list) sum_completed += v;
            double sum_sent = 0.0;
            for (int32_t v : sent_list) sum_sent += v;

            double mean_wait      = sum_wait      / rounds;
            double mean_poll      = sum_poll      / rounds;
            double mean_completed = sum_completed / rounds;
            double mean_sent      = sum_sent      / rounds;
            double mean_achieved  = mean_completed / NUM_CHUNKS;
            double timeout_rate   = static_cast<double>(timeout_count) / rounds;

            double p50_wait = percentile(wait_list, 0.50);
            double p99_wait = percentile(wait_list, 0.99);
            double p99_poll = percentile_u32(poll_list, 0.99);

            printf("%.2f,%d,%d,%.2f,"
                   "%.3f,%.3f,%.6f,%.6f,"
                   "%.6f,%.6f,%.6f,"
                   "%.3f,%.0f\n",
                   target_ratio, timeout_ms, rounds, LOSS_RATE * 100.0,
                   mean_sent, mean_completed, mean_achieved, timeout_rate,
                   mean_wait, p50_wait, p99_wait,
                   mean_poll, p99_poll);
            fflush(stdout);

            fprintf(stderr,
                "  mean_achieved=%.4f  timeout_rate=%.3f  "
                "mean_wait=%.4f ms  p99_wait=%.4f ms  mean_poll=%.0f\n",
                mean_achieved, timeout_rate, mean_wait, p99_wait, mean_poll);
        }
    }

    std::fclose(perround);
}

// ================================================================
//  Client
// ================================================================

static void run_client(const char* dev_name, const char* server_ip,
                       int rounds, unsigned seed)
{
    const int max_sq = NUM_CHUNKS + 64;
    unsigned loss_rng = seed;

    for (int ri = 0; ri < NUM_RATIOS; ri++) {
        for (int ti = 0; ti < NUM_TIMEOUTS; ti++) {
            UCQPEngine engine(dev_name, BUF_SIZE, max_sq, 16);

            int tcp_fd = tcp_connect_to(server_ip, TCP_PORT);
            ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
            ExchangeData remote = tcp_exchange_on_fd_client(tcp_fd, local);
            engine.bring_up(remote.qp);

            // Fill client buffer with a simple pattern (content doesn't matter
            // for RQ4 — we only care about CQE timing).
            std::memset(engine.local_buf(), 0x55, BUF_SIZE);

            for (int r = 0; r < rounds; r++) {
                // Wait for server-ready signal (which also starts server's clock).
                tcp_wait(tcp_fd);

                // Per-chunk Bernoulli loss injection.
                ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
                int sent_count = 0;
                for (size_t i = 0; i < cs.size(); i++) {
                    double u = static_cast<double>(rand_r(&loss_rng))
                             / static_cast<double>(RAND_MAX);
                    if (u < LOSS_RATE) continue;

                    const auto& cd = cs.chunk(i);
                    engine.post_write(cd.chunk_id,
                                      cd.local_offset, cd.remote_offset,
                                      cd.length, remote.mr,
                                      true, cd.chunk_id);
                    sent_count++;
                }

                // Drain sender CQEs.
                int drained = 0;
                while (drained < sent_count) {
                    auto cqes = engine.poll_cq(32, 2000);
                    drained += static_cast<int>(cqes.size());
                }

                // Signal server done + report sent_count so server can drain.
                tcp_signal(tcp_fd);
                int32_t sc = sent_count;
                ssize_t nw = write(tcp_fd, &sc, sizeof(sc));
                (void)nw;
            }

            close(tcp_fd);

            fprintf(stderr, "[CLIENT] Done: ratio=%.2f  timeout=%d ms  %d rounds\n",
                    RATIOS[ri], TIMEOUTS[ti], rounds);
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
            "  %s server [device] [rounds] [perround_csv_path]\n"
            "  %s client <server_ip> [device] [rounds] [seed]\n",
            argv[0], argv[0]);
        return 1;
    }

    const char* role = argv[1];

    if (strcmp(role, "server") == 0) {
        const char* dev    = (argc >= 3) ? argv[2] : "rxe0";
        int         rounds = (argc >= 4) ? atoi(argv[3]) : DEFAULT_ROUNDS;
        const char* path   = (argc >= 5) ? argv[4] : "rq4_perround.csv";
        run_server(dev, rounds, path);
    } else if (strcmp(role, "client") == 0) {
        if (argc < 3) {
            fprintf(stderr, "client role requires <server_ip>\n");
            return 1;
        }
        const char* ip     = argv[2];
        const char* dev    = (argc >= 4) ? argv[3] : "rxe0";
        int         rounds = (argc >= 5) ? atoi(argv[4]) : DEFAULT_ROUNDS;
        unsigned    seed   = (argc >= 6) ? static_cast<unsigned>(atoi(argv[5])) : BASE_SEED;
        run_client(dev, ip, rounds, seed);
    } else {
        fprintf(stderr, "Unknown role '%s'\n", role);
        return 1;
    }

    return 0;
}
