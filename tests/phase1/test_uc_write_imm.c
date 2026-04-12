/*
 * Test 1: UC QP + RDMA Write-with-Immediate
 *
 * Validates that UC QP Write-with-Immediate generates a CQE on the
 * receiver side when using SoftRoCE.  This is the CRITICAL test for
 * SemiRDMA's CQE-driven ratio control (RQ4).
 *
 * Verification points:
 *   1. Receiver CQE opcode == IBV_WC_RECV_RDMA_WITH_IMM
 *   2. imm_data == 0xDEADBEEF
 *   3. Buffer changes from 0xAA to 0x42 (zero-copy write confirmed)
 *
 * Usage:
 *   ./test_uc_write_imm server [device]
 *   ./test_uc_write_imm client [server_ip] [device]
 */

#include "rdma_common.h"

#define TEST_IMM_DATA  0xDEADBEEF
#define FILL_BEFORE    0xAA
#define FILL_AFTER     0x42

/* ── Server ──────────────────────────────────────── */

static void run_server(const char *dev_name)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, BUF_4KB, 16, 16);

    /* Pre-fill buffer */
    memset(ctx.buf, FILL_BEFORE, ctx.buf_size);
    printf("\n[Server] Buffer pre-filled with 0x%02X\n", FILL_BEFORE);
    hex_dump("Buffer BEFORE", ctx.buf, 64);

    rdma_modify_qp_to_init(&ctx);

    /* Post Receive WR — required to get CQE for Write-with-Immediate */
    int ret = rdma_post_recv(&ctx, 1);
    CHECK(ret == 0, "post_recv failed");
    LOG_INFO("Posted Receive WR (wr_id=1)");

    /* Exchange QP info via TCP */
    tcp_server_exchange(DEFAULT_TCP_PORT, &ctx.local_info, &remote);

    rdma_modify_qp_to_rtr(&ctx, &remote);

    /* Poll CQ for the Write-with-Immediate completion */
    printf("\n[Server] Polling CQ (timeout 10s)...\n");
    int n = rdma_poll_cq(ctx.cq, &wc, 10000);

    /* Small delay so RDMA data is visible in buffer */
    usleep(50000);

    /* ── Results ── */
    printf("\n============ TEST 1 RESULTS (Server) ============\n");
    if (n > 0) {
        uint32_t imm = ntohl(wc.imm_data);
        uint8_t  b0  = ((uint8_t *)ctx.buf)[0];
        bool pass_status = (wc.status == IBV_WC_SUCCESS);
        bool pass_opcode = (wc.opcode == IBV_WC_RECV_RDMA_WITH_IMM);
        bool pass_imm    = (imm == TEST_IMM_DATA);
        bool pass_buf    = (b0 == FILL_AFTER);

        printf("  CQE received:  YES\n");
        printf("  Status:        %s  %s\n",
               ibv_wc_status_str(wc.status), pass_status ? "[OK]" : "[FAIL]");
        printf("  Opcode:        %s  %s\n",
               wc_opcode_str(wc.opcode), pass_opcode ? "[OK]" : "[FAIL]");
        printf("  imm_data:      0x%08X (expect 0x%08X)  %s\n",
               imm, TEST_IMM_DATA, pass_imm ? "[OK]" : "[FAIL]");
        printf("  Buffer[0]:     0x%02X (expect 0x%02X)  %s\n",
               b0, FILL_AFTER, pass_buf ? "[OK]" : "[FAIL]");

        hex_dump("Buffer AFTER", ctx.buf, 64);

        if (pass_status && pass_opcode && pass_imm && pass_buf) {
            printf("\n  >>> TEST 1: PASS <<<\n");
            printf("  CQE-driven ratio control (RQ4) is feasible on SoftRoCE.\n");
        } else {
            printf("\n  >>> TEST 1: PARTIAL (see details above) <<<\n");
        }
    } else {
        printf("  CQE received:  NO (timeout)\n");
        uint8_t b0 = ((uint8_t *)ctx.buf)[0];
        if (b0 == FILL_AFTER) {
            printf("  Buffer[0]:     0x%02X — data written, but no CQE.\n", b0);
            printf("  Impact: Cannot use CQE for completion tracking;\n");
            printf("          need canary-value or buffer-polling fallback.\n");
        } else {
            printf("  Buffer[0]:     0x%02X — write also failed.\n", b0);
        }
        printf("\n  >>> TEST 1: FAIL <<<\n");
    }
    printf("=================================================\n\n");

    rdma_cleanup(&ctx);
}

/* ── Client ──────────────────────────────────────── */

static void run_client(const char *dev_name, const char *server_ip)
{
    struct rdma_ctx ctx;
    struct qp_info  remote;
    struct ibv_wc   wc;

    rdma_init_ctx(&ctx, dev_name, BUF_4KB, 16, 16);

    memset(ctx.buf, FILL_AFTER, ctx.buf_size);
    printf("\n[Client] Send buffer filled with 0x%02X\n", FILL_AFTER);

    rdma_modify_qp_to_init(&ctx);

    tcp_client_exchange(server_ip, DEFAULT_TCP_PORT, &ctx.local_info, &remote);

    rdma_modify_qp_to_rtr(&ctx, &remote);
    rdma_modify_qp_to_rts(&ctx, 0);

    printf("[Client] Posting Write-with-Immediate (imm=0x%08X) ...\n", TEST_IMM_DATA);
    int ret = rdma_post_write_imm(&ctx, &remote, ctx.buf_size,
                                  TEST_IMM_DATA, 1, true);
    CHECK(ret == 0, "post_write_imm failed");

    int n = rdma_poll_cq(ctx.cq, &wc, 5000);

    printf("\n============ TEST 1 RESULTS (Client) ============\n");
    if (n > 0 && wc.status == IBV_WC_SUCCESS)
        printf("  Send CQE:  SUCCESS  (opcode=%s)\n", wc_opcode_str(wc.opcode));
    else if (n > 0)
        printf("  Send CQE:  ERROR    (%s)\n", ibv_wc_status_str(wc.status));
    else
        printf("  Send CQE:  TIMEOUT\n");
    printf("=================================================\n\n");

    rdma_cleanup(&ctx);
}

/* ── Main ────────────────────────────────────────── */

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr,
            "Usage:\n"
            "  %s server [device]\n"
            "  %s client [server_ip] [device]\n"
            "Defaults: device=%s  server_ip=127.0.0.1\n",
            argv[0], argv[0], DEFAULT_DEV_NAME);
        return 1;
    }

    const char *role = argv[1];

    if (strcmp(role, "server") == 0) {
        const char *dev = (argc >= 3) ? argv[2] : DEFAULT_DEV_NAME;
        run_server(dev);
    } else if (strcmp(role, "client") == 0) {
        const char *ip  = (argc >= 3) ? argv[2] : "127.0.0.1";
        const char *dev = (argc >= 4) ? argv[3] : DEFAULT_DEV_NAME;
        run_client(dev, ip);
    } else {
        fprintf(stderr, "Unknown role '%s' — use 'server' or 'client'\n", role);
        return 1;
    }
    return 0;
}
