/*
 * Test 4: Packet-Loss-Induced Ghost Gradient (tc netem)
 *
 * Purpose: verify the real ghost-gradient mechanism on UC QP under
 *          packet loss — partial write + PSN out-of-order + missing CQE.
 *
 * Protocol:
 *   - Buffer size 256 KB (= 256 x 1024B MTU packets per Write on SoftRoCE).
 *   - Each round:
 *       * server resets buf to OLD_PATTERN (0xDEADBEEF per 32-bit word)
 *       * client fills buf with NEW pattern = round_id (uint32 per word)
 *       * client posts Write-with-Immediate (imm_data = round_id)
 *       * server polls CQ with timeout, then scans buf word-by-word
 *   - Classification per round:
 *       FULL     : all words == round_id                (CQE expected YES)
 *       PARTIAL  : some new prefix then old suffix       (CQE expected NO)
 *       NONE     : all words == OLD_PATTERN              (CQE expected NO)
 *       CORRUPT  : any word != OLD and != round_id       (unexpected on UC)
 *
 * Loss rate is applied externally via tc netem on the netdev backing rxe0
 * (see scripts/run_netem_test.sh).  This binary is loss-agnostic and just
 * runs a fixed number of rounds; the surrounding script sweeps loss rates.
 *
 * Usage:
 *   ./test_netem_loss server [device] [rounds]
 *   ./test_netem_loss client [server_ip] [device] [rounds]
 *
 * Output (server only):
 *   Human-readable summary to stderr.
 *   Machine-readable single-line CSV record to stdout:
 *     rounds,full,partial,none,corrupt,cqe_yes,avg_new_words,avg_first_old_off
 */

#include "rdma_common.h"

#define BUF_SIZE        (256 * 1024)     /* 256 KB — ~256 MTU packets       */
#define OLD_PATTERN     0xDEADBEEFu
#define TCP_PORT        18517
#define DEFAULT_ROUNDS  500
#define CQE_TIMEOUT_MS  200

/* ================================================================
 *  Persistent TCP helpers (mirrors test_ghost_gradient.c)
 * ================================================================ */

static int tcp_listen_accept(int port)
{
    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family = AF_INET, .sin_addr.s_addr = INADDR_ANY,
        .sin_port = htons(port),
    };
    CHECK(bind(lfd, (struct sockaddr *)&addr, sizeof(addr)) == 0,
          "bind(%d): %s", port, strerror(errno));
    listen(lfd, 1);
    LOG_INFO("TCP listening on %d ...", port);

    int cfd = accept(lfd, NULL, NULL);
    CHECK(cfd >= 0, "accept: %s", strerror(errno));
    close(lfd);
    return cfd;
}

static int tcp_connect_to(const char *ip, int port)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr = { .sin_family = AF_INET, .sin_port = htons(port) };
    inet_pton(AF_INET, ip, &addr.sin_addr);
    CHECK(connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0,
          "connect(%s:%d): %s", ip, port, strerror(errno));
    return fd;
}

static void tcp_signal(int fd) { uint8_t b = 1; ssize_t r = write(fd, &b, 1); (void)r; }
static void tcp_wait(int fd)   { uint8_t b;     ssize_t r = read(fd, &b, 1);  (void)r; }

/* ================================================================
 *  Buffer pattern helpers
 * ================================================================ */

static void fill_pattern(void *buf, size_t len, uint32_t word)
{
    uint32_t *p = (uint32_t *)buf;
    size_t n = len / 4;
    for (size_t i = 0; i < n; i++) p[i] = word;
}

/* Classify buffer and return stats.
 *   new_words        : count of words == round_id
 *   old_words        : count of words == OLD_PATTERN
 *   corrupt_words    : count of neither
 *   first_old_offset : index of first old word (-1 if none)
 *                      (= length of new-prefix)
 *   last_new_offset  : index of last new word (-1 if none)
 */
struct scan_result {
    size_t new_words;
    size_t old_words;
    size_t corrupt_words;
    long   first_old_offset;
    long   last_new_offset;
};

static struct scan_result scan_buffer(const void *buf, size_t len, uint32_t round_id)
{
    const uint32_t *p = (const uint32_t *)buf;
    size_t n = len / 4;
    struct scan_result r = { 0, 0, 0, -1, -1 };

    for (size_t i = 0; i < n; i++) {
        if (p[i] == round_id) {
            r.new_words++;
            r.last_new_offset = (long)i;
        } else if (p[i] == OLD_PATTERN) {
            r.old_words++;
            if (r.first_old_offset < 0) r.first_old_offset = (long)i;
        } else {
            r.corrupt_words++;
        }
    }
    return r;
}

/* ================================================================
 *  Server
 * ================================================================ */

static void run_server(const char *dev_name, int rounds)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, BUF_SIZE, 16, rounds + 16);

    rdma_modify_qp_to_init(&ctx);

    /* Pre-post one Receive WR per round (+ some slack). Unused WRs are
     * torn down with the QP at the end. */
    for (int i = 0; i < rounds; i++) {
        rdma_post_recv(&ctx, (uint64_t)i);
    }
    LOG_INFO("Pre-posted %d Receive WRs", rounds);

    /* TCP: exchange QP info, keep connection open */
    int tcp = tcp_listen_accept(TCP_PORT);
    ssize_t nw = write(tcp, &ctx.local_info, sizeof(ctx.local_info));
    CHECK(nw == (ssize_t)sizeof(ctx.local_info), "TCP write failed");
    ssize_t nr = read(tcp, &remote, sizeof(remote));
    CHECK(nr == (ssize_t)sizeof(remote), "TCP read failed");
    LOG_INFO("QP exchange done (remote qpn=%u)", remote.qpn);

    rdma_modify_qp_to_rtr(&ctx, &remote);

    /* Counters */
    int cnt_full = 0, cnt_partial = 0, cnt_none = 0, cnt_corrupt = 0;
    int cnt_cqe  = 0;
    double sum_new_words       = 0;
    double sum_first_old_off   = 0;
    int    first_old_samples   = 0;

    const size_t total_words = BUF_SIZE / 4;

    fprintf(stderr, "\n[Server] Starting %d rounds, %zu bytes/round, MTU-class packets ~%zu\n",
            rounds, (size_t)BUF_SIZE, (size_t)BUF_SIZE / 1024);

    for (int r = 0; r < rounds; r++) {
        /* Reset buffer to OLD_PATTERN */
        fill_pattern(ctx.buf, BUF_SIZE, OLD_PATTERN);

        /* Signal client: ready for this round */
        tcp_signal(tcp);

        /* Wait for client to confirm it has posted the Write */
        tcp_wait(tcp);

        /* Give packets time to propagate through netem */
        usleep(20000);  /* 20 ms */

        /* Try to collect a CQE for this round */
        int n = rdma_poll_cq(ctx.cq, &wc, CQE_TIMEOUT_MS);
        int cqe_received = 0;
        uint32_t imm_val = 0;
        if (n > 0 && wc.status == IBV_WC_SUCCESS &&
            wc.opcode == IBV_WC_RECV_RDMA_WITH_IMM) {
            cqe_received = 1;
            imm_val = ntohl(wc.imm_data);
        }

        /* Scan buffer with round_id as the expected new value */
        uint32_t round_id = (uint32_t)(r + 1); /* avoid 0 */
        struct scan_result s = scan_buffer(ctx.buf, BUF_SIZE, round_id);

        /* Classify */
        const char *cls;
        if (s.corrupt_words > 0) {
            cnt_corrupt++; cls = "CORRUPT";
        } else if (s.new_words == total_words) {
            cnt_full++;    cls = "FULL";
        } else if (s.new_words == 0) {
            cnt_none++;    cls = "NONE";
        } else {
            cnt_partial++; cls = "PARTIAL";
        }

        if (cqe_received) cnt_cqe++;
        sum_new_words += (double)s.new_words;
        if (s.first_old_offset >= 0 && s.new_words > 0) {
            sum_first_old_off += (double)s.first_old_offset;
            first_old_samples++;
        }

        if (r < 5 || r % 50 == 0) {
            fprintf(stderr,
                "  round %4d: %-8s  cqe=%s imm=0x%08X  new=%zu/%zu  first_old=%ld\n",
                r, cls, cqe_received ? "YES" : "no ", imm_val,
                s.new_words, total_words, s.first_old_offset);
        }

        /* Sanity: CQE should only arrive on FULL delivery under UC */
        if (cqe_received && s.new_words != total_words) {
            fprintf(stderr, "  [WARN] round %d: CQE but partial buffer (%zu/%zu)\n",
                    r, s.new_words, total_words);
        }
    }

    /* Summary */
    double full_pct    = 100.0 * cnt_full    / rounds;
    double partial_pct = 100.0 * cnt_partial / rounds;
    double none_pct    = 100.0 * cnt_none    / rounds;
    double cqe_pct     = 100.0 * cnt_cqe     / rounds;
    double avg_new     = sum_new_words / rounds;
    double avg_new_pct = 100.0 * avg_new / (double)total_words;
    double avg_first_old_off = first_old_samples > 0
        ? sum_first_old_off / first_old_samples : -1.0;

    fprintf(stderr, "\n========== TEST 4 RESULTS ==========\n");
    fprintf(stderr, "  Rounds:           %d\n", rounds);
    fprintf(stderr, "  FULL delivery:    %5d  (%6.2f%%)\n", cnt_full,    full_pct);
    fprintf(stderr, "  PARTIAL delivery: %5d  (%6.2f%%)\n", cnt_partial, partial_pct);
    fprintf(stderr, "  NONE delivered:   %5d  (%6.2f%%)\n", cnt_none,    none_pct);
    fprintf(stderr, "  CORRUPT:          %5d\n", cnt_corrupt);
    fprintf(stderr, "  CQE received:     %5d  (%6.2f%%)\n", cnt_cqe,     cqe_pct);
    fprintf(stderr, "  Avg bytes delivered / round: %.1f%%\n", avg_new_pct);
    fprintf(stderr, "  Avg first-old offset (partial rounds only): %.1f words\n",
            avg_first_old_off);
    fprintf(stderr, "====================================\n\n");

    /* Machine-readable CSV on stdout (one line) */
    printf("%d,%d,%d,%d,%d,%d,%.2f,%.1f\n",
           rounds, cnt_full, cnt_partial, cnt_none, cnt_corrupt, cnt_cqe,
           avg_new_pct, avg_first_old_off);
    fflush(stdout);

    close(tcp);
    rdma_cleanup(&ctx);
}

/* ================================================================
 *  Client
 * ================================================================ */

static void run_client(const char *dev_name, const char *server_ip, int rounds)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, BUF_SIZE, rounds + 16, 16);
    rdma_modify_qp_to_init(&ctx);

    int tcp = tcp_connect_to(server_ip, TCP_PORT);
    ssize_t nr = read(tcp, &remote, sizeof(remote));
    CHECK(nr == (ssize_t)sizeof(remote), "TCP read failed");
    ssize_t nw = write(tcp, &ctx.local_info, sizeof(ctx.local_info));
    CHECK(nw == (ssize_t)sizeof(ctx.local_info), "TCP write failed");
    LOG_INFO("QP exchange done (remote qpn=%u)", remote.qpn);

    rdma_modify_qp_to_rtr(&ctx, &remote);
    rdma_modify_qp_to_rts(&ctx, 0);

    fprintf(stderr, "[Client] Running %d rounds, %d bytes each\n", rounds, BUF_SIZE);

    for (int r = 0; r < rounds; r++) {
        uint32_t round_id = (uint32_t)(r + 1);
        fill_pattern(ctx.buf, BUF_SIZE, round_id);

        /* Wait for server-ready */
        tcp_wait(tcp);

        /* Post Write-with-Immediate.  Signal every time so we can drain
         * the send CQ and keep the SQ from overflowing. */
        int ret = rdma_post_write_imm(&ctx, &remote, BUF_SIZE,
                                      round_id, (uint64_t)round_id, true);
        CHECK(ret == 0, "post_write_imm failed at round %d", r);

        /* Drain sender CQE (UC sender always SUCCESS regardless of loss) */
        int n = rdma_poll_cq(ctx.cq, &wc, 5000);
        if (n <= 0 || wc.status != IBV_WC_SUCCESS) {
            LOG_WARN("round %d: sender CQE unexpected (n=%d status=%s)",
                     r, n, n > 0 ? ibv_wc_status_str(wc.status) : "timeout");
        }

        /* Tell server: Write has been posted */
        tcp_signal(tcp);
    }

    fprintf(stderr, "[Client] Done.\n");
    close(tcp);
    rdma_cleanup(&ctx);
}

/* ================================================================
 *  Main
 * ================================================================ */

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr,
            "Usage:\n"
            "  %s server [device] [rounds]\n"
            "  %s client [server_ip] [device] [rounds]\n",
            argv[0], argv[0]);
        return 1;
    }

    const char *role = argv[1];
    if (strcmp(role, "server") == 0) {
        const char *dev    = (argc >= 3) ? argv[2] : DEFAULT_DEV_NAME;
        int         rounds = (argc >= 4) ? atoi(argv[3]) : DEFAULT_ROUNDS;
        run_server(dev, rounds);
    } else if (strcmp(role, "client") == 0) {
        const char *ip     = (argc >= 3) ? argv[2] : "127.0.0.1";
        const char *dev    = (argc >= 4) ? argv[3] : DEFAULT_DEV_NAME;
        int         rounds = (argc >= 5) ? atoi(argv[4]) : DEFAULT_ROUNDS;
        run_client(dev, ip, rounds);
    } else {
        fprintf(stderr, "Unknown role '%s'\n", role);
        return 1;
    }
    return 0;
}
