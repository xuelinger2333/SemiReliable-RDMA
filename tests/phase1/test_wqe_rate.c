/*
 * Test 3: WQE Rate Micro-benchmark
 *
 * Measures RDMA Write throughput at different chunk sizes to determine
 * the WQE posting rate ceiling.  Uses plain RDMA Write (no Immediate)
 * so the receiver needs no Receive WRs.
 *
 * Chunk sizes: 4 KB, 16 KB, 64 KB, 256 KB, 1 MB
 * Iterations:  1000 per size (after 10 warmup)
 *
 * Usage:
 *   ./test_wqe_rate server [device]
 *   ./test_wqe_rate client [server_ip] [device]
 */

#include "rdma_common.h"

#define LARGE_BUF       (16 * 1024 * 1024)  /* 16 MB */
#define NUM_ITERS       1000
#define WARMUP_ITERS    10
#define SIG_INTERVAL    64                  /* signal every Nth WQE */
#define TCP_PORT_BENCH  18518

static const size_t CHUNKS[] = {
    4   * 1024,
    16  * 1024,
    64  * 1024,
    256 * 1024,
    1024* 1024,
};
#define N_CHUNKS (sizeof(CHUNKS)/sizeof(CHUNKS[0]))

static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* Format size for display */
static const char *fmt_size(size_t bytes, char *out, size_t out_len)
{
    if (bytes >= 1024*1024)
        snprintf(out, out_len, "%zu MB", bytes / (1024*1024));
    else
        snprintf(out, out_len, "%zu KB", bytes / 1024);
    return out;
}

/* ── Server: just provide a target buffer, wait for client ── */

static void run_server(const char *dev_name)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;

    rdma_init_ctx(&ctx, dev_name, LARGE_BUF, 16, 16);
    memset(ctx.buf, 0, ctx.buf_size);

    rdma_modify_qp_to_init(&ctx);
    tcp_server_exchange(TCP_PORT_BENCH, &ctx.local_info, &remote);
    rdma_modify_qp_to_rtr(&ctx, &remote);

    /* Wait for client to signal completion */
    printf("[Server] Waiting for benchmark to finish ...\n");
    {
        int lfd = socket(AF_INET, SOCK_STREAM, 0);
        int opt = 1;
        setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
        struct sockaddr_in a = {
            .sin_family = AF_INET, .sin_addr.s_addr = INADDR_ANY,
            .sin_port = htons(TCP_PORT_BENCH + 1),
        };
        bind(lfd, (struct sockaddr *)&a, sizeof(a));
        listen(lfd, 1);
        int cfd = accept(lfd, NULL, NULL);
        uint8_t b; read(cfd, &b, 1);
        close(cfd); close(lfd);
    }
    printf("[Server] Done.\n");
    rdma_cleanup(&ctx);
}

/* ── Client: run the benchmark ── */

static void run_client(const char *dev_name, const char *server_ip)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, LARGE_BUF, 256, 16);
    memset(ctx.buf, 0x42, ctx.buf_size);

    rdma_modify_qp_to_init(&ctx);
    tcp_client_exchange(server_ip, TCP_PORT_BENCH, &ctx.local_info, &remote);
    rdma_modify_qp_to_rtr(&ctx, &remote);
    rdma_modify_qp_to_rts(&ctx, 0);

    printf("\n========== TEST 3: WQE Rate Benchmark ==========\n");
    printf("  Iters: %d  |  Warmup: %d  |  Signal every: %d\n",
           NUM_ITERS, WARMUP_ITERS, SIG_INTERVAL);
    printf("  Buffer: %d MB  |  QP type: UC\n\n", LARGE_BUF / (1024*1024));
    printf("  %-10s  %10s  %12s  %12s\n",
           "Chunk", "Time(ms)", "WQE/s", "Throughput");
    printf("  %-10s  %10s  %12s  %12s\n",
           "------", "--------", "-----", "----------");

    for (size_t ci = 0; ci < N_CHUNKS; ci++) {
        size_t chunk = CHUNKS[ci];
        char sz[16];
        fmt_size(chunk, sz, sizeof(sz));

        /* ── Warmup ── */
        for (int i = 0; i < WARMUP_ITERS; i++) {
            rdma_post_write(&ctx, &remote, chunk, i, true);
            rdma_poll_cq_spin(ctx.cq, &wc);
            if (wc.status != IBV_WC_SUCCESS) {
                printf("  %-10s  WARMUP FAILED (%s)\n",
                       sz, ibv_wc_status_str(wc.status));
                goto next_chunk;
            }
        }

        /* ── Timed run ── */
        {
            double t0 = now_sec();

            for (int i = 0; i < NUM_ITERS; i++) {
                bool sig = ((i + 1) % SIG_INTERVAL == 0) || (i == NUM_ITERS - 1);
                int ret = rdma_post_write(&ctx, &remote, chunk, i, sig);
                if (ret) {
                    printf("  %-10s  POST FAILED at iter %d\n", sz, i);
                    goto next_chunk;
                }
                if (sig) {
                    rdma_poll_cq_spin(ctx.cq, &wc);
                    if (wc.status != IBV_WC_SUCCESS) {
                        printf("  %-10s  CQE ERROR at iter %d (%s)\n",
                               sz, i, ibv_wc_status_str(wc.status));
                        goto next_chunk;
                    }
                }
            }

            double elapsed = now_sec() - t0;
            double wps = NUM_ITERS / elapsed;
            double mbps = (NUM_ITERS * (double)chunk) / elapsed / (1024.0*1024.0);

            char tp[32];
            if (mbps >= 1024.0)
                snprintf(tp, sizeof(tp), "%.1f GB/s", mbps / 1024.0);
            else
                snprintf(tp, sizeof(tp), "%.1f MB/s", mbps);

            printf("  %-10s  %10.1f  %12.0f  %12s\n", sz, elapsed*1000, wps, tp);
        }
next_chunk:;
    }

    printf("\n  Design impact:\n");
    printf("  - If 4KB WQE/s >> gradient_size/4KB, fine-grained chunking is viable.\n");
    printf("  - If WQE/s plateaus at small chunks, set chunk-size floor accordingly.\n");
    printf("================================================\n\n");

    /* Signal server to exit */
    {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        struct sockaddr_in a = {
            .sin_family = AF_INET, .sin_port = htons(TCP_PORT_BENCH + 1),
        };
        inet_pton(AF_INET, server_ip, &a.sin_addr);
        connect(fd, (struct sockaddr *)&a, sizeof(a));
        uint8_t b = 1; write(fd, &b, 1);
        close(fd);
    }

    rdma_cleanup(&ctx);
}

/* ── Main ── */

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr,
            "Usage:\n"
            "  %s server [device]\n"
            "  %s client [server_ip] [device]\n",
            argv[0], argv[0]);
        return 1;
    }

    const char *role = argv[1];
    if (strcmp(role, "server") == 0) {
        run_server((argc >= 3) ? argv[2] : DEFAULT_DEV_NAME);
    } else if (strcmp(role, "client") == 0) {
        const char *ip  = (argc >= 3) ? argv[2] : "127.0.0.1";
        const char *dev = (argc >= 4) ? argv[3] : DEFAULT_DEV_NAME;
        run_client(dev, ip);
    } else {
        fprintf(stderr, "Unknown role '%s'\n", role);
        return 1;
    }
    return 0;
}
