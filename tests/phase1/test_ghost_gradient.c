/*
 * Test 2: Ghost Gradient Verification
 *
 * Demonstrates the "ghost gradient" problem unique to UC QP:
 *   - Round 1: successful Write-with-Immediate  (buffer 0xAA → 0x42)
 *   - Round 2: server does NOT post a Receive WR, client writes 0xFF
 *   - Expected: buffer still 0x42 (stale data = ghost gradient)
 *
 * A persistent TCP connection synchronizes the two rounds.
 *
 * Usage:
 *   ./test_ghost_gradient server [device]
 *   ./test_ghost_gradient client [server_ip] [device]
 */

#include "rdma_common.h"

#define FILL_INITIAL  0xAA
#define FILL_ROUND1   0x42
#define FILL_ROUND2   0xFF
#define IMM_ROUND1    0x11111111
#define IMM_ROUND2    0x22222222
#define TCP_PORT      18516  /* separate from test 1 */

/* ================================================================
 *  Persistent TCP helpers  (keep connection open across rounds)
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
    struct sockaddr_in addr = {
        .sin_family = AF_INET, .sin_port = htons(port),
    };
    inet_pton(AF_INET, ip, &addr.sin_addr);
    CHECK(connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0,
          "connect(%s:%d): %s", ip, port, strerror(errno));
    return fd;
}

/* Blocking one-byte send / recv for round synchronization */
static void tcp_signal(int fd) { uint8_t b = 1; ssize_t r = write(fd, &b, 1); (void)r; }
static void tcp_wait(int fd)   { uint8_t b;     ssize_t r = read(fd, &b, 1);  (void)r; }

/* ── Server ──────────────────────────────────────── */

static void run_server(const char *dev_name)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, BUF_4KB, 16, 16);
    memset(ctx.buf, FILL_INITIAL, ctx.buf_size);
    printf("\n[Server] Buffer initialized: 0x%02X\n", FILL_INITIAL);

    rdma_modify_qp_to_init(&ctx);

    /* Post Receive WR for Round 1 only */
    rdma_post_recv(&ctx, 1);
    LOG_INFO("Round 1: Receive WR posted");

    /* Persistent TCP — exchange QP info, keep connection open */
    int tcp = tcp_listen_accept(TCP_PORT);
    ssize_t nw = write(tcp, &ctx.local_info, sizeof(ctx.local_info));
    CHECK(nw == (ssize_t)sizeof(ctx.local_info), "TCP write failed");
    ssize_t nr = read(tcp,  &remote,         sizeof(remote));
    CHECK(nr == (ssize_t)sizeof(remote), "TCP read failed");
    LOG_INFO("QP exchange done (remote qpn=%u)", remote.qpn);

    rdma_modify_qp_to_rtr(&ctx, &remote);

    /* Signal client: server ready for Round 1 */
    tcp_signal(tcp);

    /* ── Round 1 ── */
    printf("\n[Server] Round 1: waiting for CQE ...\n");
    int n = rdma_poll_cq(ctx.cq, &wc, 10000);
    usleep(50000);

    uint8_t *buf = (uint8_t *)ctx.buf;
    printf("--- Round 1 ---\n");
    if (n > 0 && wc.status == IBV_WC_SUCCESS) {
        printf("  CQE:       %s  imm=0x%08X\n",
               wc_opcode_str(wc.opcode), ntohl(wc.imm_data));
        printf("  Buffer[0]: 0x%02X (expect 0x%02X)  %s\n",
               buf[0], FILL_ROUND1,
               buf[0] == FILL_ROUND1 ? "[OK]" : "[FAIL]");
    } else {
        printf("  CQE: FAILED / TIMEOUT  — cannot proceed.\n");
        close(tcp);
        rdma_cleanup(&ctx);
        return;
    }

    /* ── Round 2: deliberately do NOT post Receive WR ── */
    printf("\n[Server] Round 2: NOT posting Receive WR  (ghost gradient scenario)\n");
    hex_dump("Buffer before Round 2", ctx.buf, 32);

    /* Signal client: go ahead with Round 2 */
    tcp_signal(tcp);

    /* Wait for client to confirm Round 2 write posted */
    tcp_wait(tcp);

    /* Brief delay then check CQ (should be empty) */
    usleep(200000);  /* 200 ms */
    n = rdma_poll_cq(ctx.cq, &wc, 2000);

    /* ── Results ── */
    printf("\n========== TEST 2 RESULTS ==========\n");
    if (n == 0) {
        /* No CQE — expected */
        printf("  Round 2 CQE:     NO  (expected: no Receive WR posted)\n");
        if (buf[0] == FILL_ROUND1) {
            printf("  Buffer[0]:       0x%02X  (still Round 1 value)\n", buf[0]);
            printf("  Ghost gradient:  CONFIRMED\n");
            printf("\n  >>> TEST 2: PASS <<<\n");
            printf("  Stale data persists when UC Write-with-Immediate is\n");
            printf("  silently dropped.  Masked aggregation (RQ2) is needed.\n");
        } else if (buf[0] == FILL_ROUND2) {
            printf("  Buffer[0]:       0x%02X  (Round 2 data written despite no RQ WR!)\n", buf[0]);
            printf("  Ghost gradient:  DIFFERENT VARIANT\n");
            printf("\n  >>> TEST 2: PARTIAL <<<\n");
            printf("  Data arrived but receiver has no CQE.\n");
            printf("  Ghost gradient from packet loss must be tested with tc netem.\n");
        } else {
            printf("  Buffer[0]:       0x%02X  (unexpected)\n", buf[0]);
            printf("\n  >>> TEST 2: UNEXPECTED <<<\n");
        }
    } else if (n > 0) {
        printf("  Round 2 CQE:     YES  (unexpected! status=%s)\n",
               ibv_wc_status_str(wc.status));
        printf("\n  >>> TEST 2: UNEXPECTED <<<\n");
    }
    hex_dump("Buffer after Round 2", ctx.buf, 32);
    printf("====================================\n\n");

    close(tcp);
    rdma_cleanup(&ctx);
}

/* ── Client ──────────────────────────────────────── */

static void run_client(const char *dev_name, const char *server_ip)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, BUF_4KB, 16, 16);
    rdma_modify_qp_to_init(&ctx);

    /* Persistent TCP — exchange QP info */
    int tcp = tcp_connect_to(server_ip, TCP_PORT);
    ssize_t nr = read(tcp,  &remote,         sizeof(remote));
    CHECK(nr == (ssize_t)sizeof(remote), "TCP read failed");
    ssize_t nw = write(tcp, &ctx.local_info, sizeof(ctx.local_info));
    CHECK(nw == (ssize_t)sizeof(ctx.local_info), "TCP write failed");
    LOG_INFO("QP exchange done (remote qpn=%u)", remote.qpn);

    rdma_modify_qp_to_rtr(&ctx, &remote);
    rdma_modify_qp_to_rts(&ctx, 0);

    /* Wait for server ready */
    tcp_wait(tcp);

    /* ── Round 1 ── */
    memset(ctx.buf, FILL_ROUND1, ctx.buf_size);
    printf("\n[Client] Round 1: Write-with-Immediate  (data=0x%02X, imm=0x%08X)\n",
           FILL_ROUND1, IMM_ROUND1);
    rdma_post_write_imm(&ctx, &remote, ctx.buf_size, IMM_ROUND1, 1, true);
    int n = rdma_poll_cq(ctx.cq, &wc, 5000);
    printf("[Client] Round 1 send: %s\n",
           (n > 0 && wc.status == IBV_WC_SUCCESS) ? "SUCCESS" : "FAILED");

    /* Wait for server to be ready for Round 2 */
    tcp_wait(tcp);

    /* ── Round 2: server has NOT posted Receive WR ── */
    memset(ctx.buf, FILL_ROUND2, ctx.buf_size);
    printf("[Client] Round 2: Write-with-Immediate  (data=0x%02X, imm=0x%08X)\n",
           FILL_ROUND2, IMM_ROUND2);
    printf("         (server has NO Receive WR — expecting silent drop)\n");
    rdma_post_write_imm(&ctx, &remote, ctx.buf_size, IMM_ROUND2, 2, true);
    n = rdma_poll_cq(ctx.cq, &wc, 5000);
    printf("[Client] Round 2 send: %s\n",
           (n > 0 && wc.status == IBV_WC_SUCCESS)
               ? "SUCCESS (UC: sender always succeeds)" : "FAILED");
    printf("[Client] Note: UC sender CQE is always SUCCESS regardless of receiver.\n\n");

    /* Signal server: Round 2 write done */
    tcp_signal(tcp);

    close(tcp);
    rdma_cleanup(&ctx);
}

/* ── Main ────────────────────────────────────────── */

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
