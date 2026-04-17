/*
 * test_rms_error — RQ2 ghost-gradient masking RMS error experiment
 *
 * Compares two post-processing paths on the receiver:
 *   - raw    : buffer used as-is (ghost region keeps stale N(0,1) from pre-fill)
 *   - masked : GhostMask::apply zeroes ghost chunks
 * Both are compared against the ground truth (what client filled this round).
 *
 * Ground truth sync: both sides draw from std::mt19937 seeded
 * with BASE_SEED + 2000 + round_id; server generates gt locally (no TCP).
 * Stale pre-fill: std::mt19937 seeded with BASE_SEED + 1000 + round_id
 * (independent stream) — ensures ghost region values are independent of
 * truth, so raw and masked differ in a measurable way.
 *
 * Usage (two terminals, loopback):
 *   ./test_rms_error server [device] [rounds] [perround_csv_path]
 *   ./test_rms_error client <server_ip> [device] [rounds] [seed]
 *
 * Main CSV → stdout, per-round CSV → file path (default ./rq2_perround.csv).
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
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
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

static const double LOSS_RATES[] = { 0.000, 0.010, 0.050 };
constexpr int    NUM_LOSS_RATES = sizeof(LOSS_RATES) / sizeof(LOSS_RATES[0]);

constexpr int    DEFAULT_ROUNDS = 200;
constexpr int    TCP_PORT       = 18526;   // separate from RQ1 (18525)
constexpr int    WAIT_TIMEOUT   = 5000;

constexpr unsigned BASE_SEED = 42;

// Seed offsets for the two independent streams
constexpr unsigned STALE_SEED_OFFSET = 1000;
constexpr unsigned GT_SEED_OFFSET    = 2000;

// ================================================================
//  Helpers
// ================================================================

// Fill `n_floats` floats into `dst` using mt19937 + N(0,1).
static void fill_normal(float* dst, size_t n_floats, unsigned seed)
{
    std::mt19937 rng(seed);
    std::normal_distribution<float> nd(0.0f, 1.0f);
    for (size_t i = 0; i < n_floats; i++) {
        dst[i] = nd(rng);
    }
}

// RMS error between a byte buffer (reinterpreted as floats) and ground truth.
static double rms_error(const uint8_t* buf, const std::vector<float>& gt)
{
    const float* f = reinterpret_cast<const float*>(buf);
    double sum_sq = 0.0;
    for (size_t i = 0; i < gt.size(); i++) {
        double d = static_cast<double>(f[i]) - static_cast<double>(gt[i]);
        sum_sq += d * d;
    }
    return std::sqrt(sum_sq / static_cast<double>(gt.size()));
}

// ================================================================
//  Server
// ================================================================

static void run_server(const char* dev_name, int rounds,
                       const char* perround_path)
{
    // Open per-round CSV file
    FILE* perround = std::fopen(perround_path, "w");
    if (!perround) {
        fprintf(stderr, "[SERVER] Cannot open %s for writing\n", perround_path);
        return;
    }
    fprintf(perround, "loss_pct,round_id,ghost_ratio,raw_rms,masked_rms\n");

    // Main CSV header → stdout
    printf("loss_pct,rounds,mean_ghost_ratio,mean_raw_rms,mean_masked_rms,"
           "rms_ratio,p50_raw_rms,p99_raw_rms,p50_masked_rms,p99_masked_rms\n");
    fflush(stdout);

    const size_t n_floats = BUF_SIZE / sizeof(float);
    std::vector<float>   gt(n_floats);               // ground truth
    std::vector<uint8_t> raw_copy(BUF_SIZE);         // pre-mask snapshot

    const int max_rq = NUM_CHUNKS + 64;

    for (int li = 0; li < NUM_LOSS_RATES; li++) {
        const double loss_rate = LOSS_RATES[li];

        fprintf(stderr, "\n==== loss=%.2f%%  chunks/round=%d  rounds=%d ====\n",
                loss_rate * 100.0, NUM_CHUNKS, rounds);

        UCQPEngine engine(dev_name, BUF_SIZE, 16, max_rq);

        int tcp_fd = tcp_listen_accept(TCP_PORT);
        ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
        ExchangeData remote = tcp_exchange_on_fd_server(tcp_fd, local);
        engine.bring_up(remote.qp);

        // Pre-post Recv WRs (one round's worth)
        for (int i = 0; i < NUM_CHUNKS; i++) {
            engine.post_recv(static_cast<uint64_t>(i));
        }

        std::vector<double> raw_rms_list;    raw_rms_list.reserve(rounds);
        std::vector<double> masked_rms_list; masked_rms_list.reserve(rounds);
        double total_ghost_ratio = 0.0;

        for (int r = 0; r < rounds; r++) {
            // 1. Pre-fill server buffer with independent stale N(0,1)
            fill_normal(reinterpret_cast<float*>(engine.local_buf()),
                        n_floats, BASE_SEED + STALE_SEED_OFFSET + r);

            // 2. Generate ground truth locally (matches client's seed)
            fill_normal(gt.data(), n_floats, BASE_SEED + GT_SEED_OFFSET + r);

            // 3. TCP sync: tell client we're ready, wait for client done
            tcp_signal(tcp_fd);
            tcp_wait(tcp_fd);

            int32_t sent_count = 0;
            ssize_t nr = read(tcp_fd, &sent_count, sizeof(sent_count));
            (void)nr;

            // 4. Wait for CQEs at the exact ratio that client sent
            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            RatioController rc(engine);
            WaitStats stats;

            if (sent_count > 0) {
                double target = static_cast<double>(sent_count)
                              / static_cast<double>(NUM_CHUNKS);
                rc.wait_for_ratio(cs, target, WAIT_TIMEOUT, &stats);

                // Extra drain for stragglers
                usleep(5000);
                auto extra = engine.poll_cq(64, 50);
                for (const auto& c : extra) {
                    if (c.opcode == IBV_WC_RECV_RDMA_WITH_IMM &&
                        c.status == IBV_WC_SUCCESS) {
                        cs.mark_completed(c.imm_data);
                    }
                }
            }

            size_t completed = cs.num_completed();
            double ghost_ratio = 1.0 - static_cast<double>(completed)
                                     / static_cast<double>(NUM_CHUNKS);
            total_ghost_ratio += ghost_ratio;

            // 5. Snapshot pre-mask buffer, then apply GhostMask
            std::memcpy(raw_copy.data(), engine.local_buf(), BUF_SIZE);
            GhostMask::apply(engine.local_buf(), cs);

            // 6. Compute both RMS errors
            double raw_rms    = rms_error(raw_copy.data(), gt);
            double masked_rms = rms_error(engine.local_buf(), gt);

            raw_rms_list.push_back(raw_rms);
            masked_rms_list.push_back(masked_rms);

            fprintf(perround, "%.2f,%d,%.6f,%.6e,%.6e\n",
                    loss_rate * 100.0, r, ghost_ratio, raw_rms, masked_rms);

            // 7. Refill RQ: re-post exactly `completed` WRs
            for (size_t i = 0; i < completed; i++) {
                engine.post_recv(0);
            }
        }

        close(tcp_fd);
        fflush(perround);

        // Cell aggregate
        double mean_ghost  = total_ghost_ratio / static_cast<double>(rounds);
        double sum_raw     = 0.0, sum_masked = 0.0;
        for (double v : raw_rms_list)    sum_raw    += v;
        for (double v : masked_rms_list) sum_masked += v;
        double mean_raw    = sum_raw    / static_cast<double>(rounds);
        double mean_masked = sum_masked / static_cast<double>(rounds);
        double ratio = (mean_raw > 0.0) ? (mean_masked / mean_raw) : 0.0;

        auto pct = [](std::vector<double> v, double q) {
            if (v.empty()) return 0.0;
            std::sort(v.begin(), v.end());
            size_t idx = static_cast<size_t>(v.size() * q);
            if (idx >= v.size()) idx = v.size() - 1;
            return v[idx];
        };
        double p50_raw    = pct(raw_rms_list,    0.50);
        double p99_raw    = pct(raw_rms_list,    0.99);
        double p50_masked = pct(masked_rms_list, 0.50);
        double p99_masked = pct(masked_rms_list, 0.99);

        printf("%.2f,%d,%.6f,%.6e,%.6e,%.6f,%.6e,%.6e,%.6e,%.6e\n",
               loss_rate * 100.0, rounds,
               mean_ghost, mean_raw, mean_masked, ratio,
               p50_raw, p99_raw, p50_masked, p99_masked);
        fflush(stdout);

        fprintf(stderr, "  mean_ghost=%.4f  mean_raw_rms=%.4e  "
                "mean_masked_rms=%.4e  ratio=%.4f\n",
                mean_ghost, mean_raw, mean_masked, ratio);
    }

    std::fclose(perround);
}

// ================================================================
//  Client
// ================================================================

static void run_client(const char* dev_name, const char* server_ip,
                       int rounds, unsigned seed)
{
    const size_t n_floats = BUF_SIZE / sizeof(float);
    const int max_sq = NUM_CHUNKS + 64;

    // loss-injection RNG (separate from N(0,1) stream)
    unsigned loss_rng = seed;

    for (int li = 0; li < NUM_LOSS_RATES; li++) {
        const double loss_rate = LOSS_RATES[li];

        UCQPEngine engine(dev_name, BUF_SIZE, max_sq, 16);

        int tcp_fd = tcp_connect_to(server_ip, TCP_PORT);
        ExchangeData local{engine.local_qp_info(), engine.local_mr_info()};
        ExchangeData remote = tcp_exchange_on_fd_client(tcp_fd, local);
        engine.bring_up(remote.qp);

        for (int r = 0; r < rounds; r++) {
            // Fill client buffer with round's ground truth
            fill_normal(reinterpret_cast<float*>(engine.local_buf()),
                        n_floats, BASE_SEED + GT_SEED_OFFSET + r);

            // Wait for server ready
            tcp_wait(tcp_fd);

            // Per-chunk loss injection
            ChunkSet cs(0, BUF_SIZE, CHUNK_BYTES);
            int sent_count = 0;

            for (size_t i = 0; i < cs.size(); i++) {
                double u = static_cast<double>(rand_r(&loss_rng))
                         / static_cast<double>(RAND_MAX);
                if (u < loss_rate) continue;

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

            // Signal server done + report sent_count
            tcp_signal(tcp_fd);
            int32_t sc = sent_count;
            ssize_t nw = write(tcp_fd, &sc, sizeof(sc));
            (void)nw;
        }

        close(tcp_fd);

        fprintf(stderr, "[CLIENT] Done: loss=%.2f%%  %d rounds\n",
                loss_rate * 100.0, rounds);
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
        const char* path   = (argc >= 5) ? argv[4] : "rq2_perround.csv";
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
