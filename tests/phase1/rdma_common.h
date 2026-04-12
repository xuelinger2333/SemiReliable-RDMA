/*
 * rdma_common.h — Shared RDMA utilities for SemiRDMA Phase 1 tests
 *
 * Header-only library providing:
 *   - UC QP creation and state transitions (INIT → RTR → RTS)
 *   - TCP-based QP metadata exchange (loopback server/client)
 *   - RDMA Write / Write-with-Immediate posting
 *   - CQ polling with timeout
 *   - Hex dump and debug helpers
 *
 * All functions are static inline to avoid linker issues when
 * included from multiple translation units.
 */

#pragma once

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include <time.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <infiniband/verbs.h>

/* ================================================================
 *  Logging & Error Handling
 * ================================================================ */

#define LOG_ERR(fmt, ...) \
    fprintf(stderr, "[ERROR] %s:%d: " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__)

#define LOG_INFO(fmt, ...) \
    fprintf(stderr, "[INFO]  " fmt "\n", ##__VA_ARGS__)

#define LOG_WARN(fmt, ...) \
    fprintf(stderr, "[WARN]  " fmt "\n", ##__VA_ARGS__)

#define CHECK(cond, fmt, ...) do { \
    if (!(cond)) { \
        LOG_ERR(fmt, ##__VA_ARGS__); \
        exit(EXIT_FAILURE); \
    } \
} while (0)

/* ================================================================
 *  Constants
 * ================================================================ */

#define DEFAULT_TCP_PORT  18515
#define DEFAULT_DEV_NAME  "rxe0"
#define DEFAULT_IB_PORT   1
#define DEFAULT_CQ_SIZE   256
#define BUF_4KB           4096

/* ================================================================
 *  Data Structures
 * ================================================================ */

/* Metadata exchanged between server and client via TCP */
struct qp_info {
    uint32_t      qpn;    /* QP number                        */
    uint32_t      rkey;   /* Remote key of the MR              */
    uint64_t      addr;   /* Virtual address of remote buffer  */
    union ibv_gid gid;    /* GID for RoCE addressing           */
};

/* All RDMA resources bundled together */
struct rdma_ctx {
    struct ibv_context *ctx;
    struct ibv_pd      *pd;
    struct ibv_cq      *cq;
    struct ibv_qp      *qp;
    struct ibv_mr      *mr;
    void               *buf;
    size_t              buf_size;
    int                 ib_port;
    int                 gid_index;
    struct qp_info      local_info;
};

/* ================================================================
 *  Device Operations
 * ================================================================ */

static inline struct ibv_context *
rdma_open_device(const char *dev_name)
{
    int num_devices = 0;
    struct ibv_device **dev_list = ibv_get_device_list(&num_devices);
    CHECK(dev_list && num_devices > 0, "No RDMA devices found. Is SoftRoCE configured?");

    struct ibv_context *ctx = NULL;
    for (int i = 0; i < num_devices; i++) {
        if (strcmp(ibv_get_device_name(dev_list[i]), dev_name) == 0) {
            ctx = ibv_open_device(dev_list[i]);
            break;
        }
    }
    ibv_free_device_list(dev_list);
    CHECK(ctx, "Device '%s' not found. Run: rdma link show", dev_name);
    LOG_INFO("Opened device: %s", dev_name);
    return ctx;
}

/* Find first valid GID (prefer index 1 = RoCEv2/IPv4-mapped) */
static inline int
rdma_find_gid(struct ibv_context *ctx, int port, union ibv_gid *out_gid)
{
    struct ibv_port_attr pa;
    int ret = ibv_query_port(ctx, port, &pa);
    CHECK(ret == 0, "ibv_query_port failed");

    int try_idx[] = {1, 0, 2, 3};
    union ibv_gid gid, zero;
    memset(&zero, 0, sizeof(zero));

    for (int i = 0; i < 4; i++) {
        if (try_idx[i] >= (int)pa.gid_tbl_len) continue;
        if (ibv_query_gid(ctx, port, try_idx[i], &gid) != 0) continue;
        if (memcmp(&gid, &zero, sizeof(gid)) == 0) continue;
        memcpy(out_gid, &gid, sizeof(gid));
        LOG_INFO("Using GID index %d", try_idx[i]);
        return try_idx[i];
    }
    CHECK(0, "No valid GID found on port %d", port);
    return -1; /* unreachable */
}

/* ================================================================
 *  Resource Initialization
 * ================================================================ */

static inline int
rdma_init_ctx(struct rdma_ctx *rctx, const char *dev_name,
              size_t buf_size, int sq_depth, int rq_depth)
{
    memset(rctx, 0, sizeof(*rctx));
    rctx->buf_size = buf_size;
    rctx->ib_port  = DEFAULT_IB_PORT;

    rctx->ctx = rdma_open_device(dev_name);

    rctx->pd = ibv_alloc_pd(rctx->ctx);
    CHECK(rctx->pd, "ibv_alloc_pd failed");

    rctx->cq = ibv_create_cq(rctx->ctx, DEFAULT_CQ_SIZE, NULL, NULL, 0);
    CHECK(rctx->cq, "ibv_create_cq failed");

    /* Allocate page-aligned buffer */
    rctx->buf = aligned_alloc(4096, buf_size);
    CHECK(rctx->buf, "aligned_alloc(%zu) failed", buf_size);
    memset(rctx->buf, 0, buf_size);

    int mr_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
    rctx->mr = ibv_reg_mr(rctx->pd, rctx->buf, buf_size, mr_flags);
    CHECK(rctx->mr, "ibv_reg_mr failed");

    union ibv_gid gid;
    rctx->gid_index = rdma_find_gid(rctx->ctx, rctx->ib_port, &gid);

    struct ibv_qp_init_attr qp_attr = {
        .send_cq  = rctx->cq,
        .recv_cq  = rctx->cq,
        .cap      = {
            .max_send_wr  = sq_depth,
            .max_recv_wr  = rq_depth,
            .max_send_sge = 1,
            .max_recv_sge = 1,
        },
        .qp_type  = IBV_QPT_UC,
    };
    rctx->qp = ibv_create_qp(rctx->pd, &qp_attr);
    CHECK(rctx->qp, "ibv_create_qp (UC) failed");
    LOG_INFO("Created UC QP, qpn=%u", rctx->qp->qp_num);

    rctx->local_info.qpn  = rctx->qp->qp_num;
    rctx->local_info.rkey = rctx->mr->rkey;
    rctx->local_info.addr = (uint64_t)(uintptr_t)rctx->buf;
    memcpy(&rctx->local_info.gid, &gid, sizeof(gid));

    return 0;
}

/* ================================================================
 *  QP State Transitions (UC-specific, no RC params)
 * ================================================================ */

static inline void
rdma_modify_qp_to_init(struct rdma_ctx *rctx)
{
    struct ibv_qp_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.qp_state        = IBV_QPS_INIT;
    attr.pkey_index      = 0;
    attr.port_num        = rctx->ib_port;
    attr.qp_access_flags = IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_LOCAL_WRITE;

    int flags = IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS;
    int ret = ibv_modify_qp(rctx->qp, &attr, flags);
    CHECK(ret == 0, "QP->INIT failed: %s", strerror(errno));
    LOG_INFO("QP -> INIT");
}

static inline void
rdma_modify_qp_to_rtr(struct rdma_ctx *rctx, struct qp_info *remote)
{
    struct ibv_qp_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.qp_state              = IBV_QPS_RTR;
    attr.path_mtu              = IBV_MTU_1024;
    attr.dest_qp_num           = remote->qpn;
    attr.ah_attr.is_global     = 1;
    attr.ah_attr.grh.dgid      = remote->gid;
    attr.ah_attr.grh.sgid_index = rctx->gid_index;
    attr.ah_attr.grh.hop_limit  = 1;
    attr.ah_attr.sl            = 0;
    attr.ah_attr.src_path_bits = 0;
    attr.ah_attr.port_num      = rctx->ib_port;

    int flags = IBV_QP_STATE | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN | IBV_QP_AV;
    int ret = ibv_modify_qp(rctx->qp, &attr, flags);
    CHECK(ret == 0, "QP->RTR failed: %s", strerror(errno));
    LOG_INFO("QP -> RTR (dest_qpn=%u)", remote->qpn);
}

static inline void
rdma_modify_qp_to_rts(struct rdma_ctx *rctx, uint32_t sq_psn)
{
    struct ibv_qp_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.qp_state = IBV_QPS_RTS;
    attr.sq_psn   = sq_psn;

    int flags = IBV_QP_STATE | IBV_QP_SQ_PSN;
    int ret = ibv_modify_qp(rctx->qp, &attr, flags);
    CHECK(ret == 0, "QP->RTS failed: %s", strerror(errno));
    LOG_INFO("QP -> RTS (sq_psn=%u)", sq_psn);
}

/* ================================================================
 *  TCP Metadata Exchange (single-shot, opens and closes connection)
 * ================================================================ */

static inline int
tcp_server_exchange(int port, struct qp_info *local, struct qp_info *remote)
{
    int listenfd = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(listenfd >= 0, "socket() failed");

    int opt = 1;
    setsockopt(listenfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_addr.s_addr = INADDR_ANY,
        .sin_port        = htons(port),
    };
    int ret = bind(listenfd, (struct sockaddr *)&addr, sizeof(addr));
    CHECK(ret == 0, "bind(%d) failed: %s", port, strerror(errno));
    listen(listenfd, 1);
    LOG_INFO("TCP listening on port %d ...", port);

    int connfd = accept(listenfd, NULL, NULL);
    CHECK(connfd >= 0, "accept() failed");
    close(listenfd);

    write(connfd, local, sizeof(*local));
    read(connfd, remote, sizeof(*remote));
    close(connfd);
    LOG_INFO("TCP exchange done (remote qpn=%u)", remote->qpn);
    return 0;
}

static inline int
tcp_client_exchange(const char *server_ip, int port,
                    struct qp_info *local, struct qp_info *remote)
{
    int sockfd = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(sockfd >= 0, "socket() failed");

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port   = htons(port),
    };
    inet_pton(AF_INET, server_ip, &addr.sin_addr);

    int ret = connect(sockfd, (struct sockaddr *)&addr, sizeof(addr));
    CHECK(ret == 0, "connect(%s:%d) failed: %s", server_ip, port, strerror(errno));

    /* Server sends first, client receives first */
    read(sockfd, remote, sizeof(*remote));
    write(sockfd, local, sizeof(*local));
    close(sockfd);
    LOG_INFO("TCP exchange done (remote qpn=%u)", remote->qpn);
    return 0;
}

/* ================================================================
 *  Post Operations
 * ================================================================ */

static inline int
rdma_post_recv(struct rdma_ctx *rctx, uint64_t wr_id)
{
    struct ibv_recv_wr wr, *bad_wr;
    memset(&wr, 0, sizeof(wr));
    wr.wr_id   = wr_id;
    wr.sg_list = NULL;
    wr.num_sge = 0;

    int ret = ibv_post_recv(rctx->qp, &wr, &bad_wr);
    if (ret) LOG_ERR("ibv_post_recv failed: %s", strerror(ret));
    return ret;
}

static inline int
rdma_post_write_imm(struct rdma_ctx *rctx, struct qp_info *remote,
                    size_t len, uint32_t imm_data,
                    uint64_t wr_id, bool signaled)
{
    struct ibv_sge sge = {
        .addr   = (uint64_t)(uintptr_t)rctx->buf,
        .length = (uint32_t)len,
        .lkey   = rctx->mr->lkey,
    };
    struct ibv_send_wr wr, *bad_wr;
    memset(&wr, 0, sizeof(wr));
    wr.wr_id              = wr_id;
    wr.sg_list            = &sge;
    wr.num_sge            = 1;
    wr.opcode             = IBV_WR_RDMA_WRITE_WITH_IMM;
    wr.send_flags         = signaled ? IBV_SEND_SIGNALED : 0;
    wr.imm_data           = htonl(imm_data);
    wr.wr.rdma.remote_addr = remote->addr;
    wr.wr.rdma.rkey        = remote->rkey;

    int ret = ibv_post_send(rctx->qp, &wr, &bad_wr);
    if (ret) LOG_ERR("post_send(WRITE_IMM) failed: %s", strerror(ret));
    return ret;
}

static inline int
rdma_post_write(struct rdma_ctx *rctx, struct qp_info *remote,
                size_t len, uint64_t wr_id, bool signaled)
{
    struct ibv_sge sge = {
        .addr   = (uint64_t)(uintptr_t)rctx->buf,
        .length = (uint32_t)len,
        .lkey   = rctx->mr->lkey,
    };
    struct ibv_send_wr wr, *bad_wr;
    memset(&wr, 0, sizeof(wr));
    wr.wr_id              = wr_id;
    wr.sg_list            = &sge;
    wr.num_sge            = 1;
    wr.opcode             = IBV_WR_RDMA_WRITE;
    wr.send_flags         = signaled ? IBV_SEND_SIGNALED : 0;
    wr.wr.rdma.remote_addr = remote->addr;
    wr.wr.rdma.rkey        = remote->rkey;

    int ret = ibv_post_send(rctx->qp, &wr, &bad_wr);
    if (ret) LOG_ERR("post_send(WRITE) failed: %s", strerror(ret));
    return ret;
}

/* ================================================================
 *  CQ Polling
 * ================================================================ */

/* Poll CQ with timeout (milliseconds). Returns >0 on success, 0 on timeout. */
static inline int
rdma_poll_cq(struct ibv_cq *cq, struct ibv_wc *wc, int timeout_ms)
{
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);

    for (;;) {
        int n = ibv_poll_cq(cq, 1, wc);
        if (n > 0)  return n;
        if (n < 0) { LOG_ERR("ibv_poll_cq error"); return -1; }

        clock_gettime(CLOCK_MONOTONIC, &now);
        double elapsed_ms = (now.tv_sec - start.tv_sec) * 1000.0
                          + (now.tv_nsec - start.tv_nsec) / 1e6;
        if (elapsed_ms > timeout_ms) return 0; /* timeout */
    }
}

/* Busy-spin poll (no timeout, use in benchmarks only) */
static inline int
rdma_poll_cq_spin(struct ibv_cq *cq, struct ibv_wc *wc)
{
    int n;
    while ((n = ibv_poll_cq(cq, 1, wc)) == 0)
        ;
    return n;
}

/* ================================================================
 *  Debug Helpers
 * ================================================================ */

static inline void
hex_dump(const char *label, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    printf("%s (%zu bytes):\n  ", label, len);
    for (size_t i = 0; i < len; i++) {
        printf("%02X ", p[i]);
        if ((i + 1) % 16 == 0 && i + 1 < len)
            printf("\n  ");
    }
    printf("\n");
}

static inline const char *
wc_opcode_str(enum ibv_wc_opcode op)
{
    switch (op) {
    case IBV_WC_SEND:               return "SEND";
    case IBV_WC_RDMA_WRITE:         return "RDMA_WRITE";
    case IBV_WC_RDMA_READ:          return "RDMA_READ";
    case IBV_WC_RECV:               return "RECV";
    case IBV_WC_RECV_RDMA_WITH_IMM: return "RECV_RDMA_WITH_IMM";
    default:                        return "UNKNOWN";
    }
}

static inline void
print_gid(const char *label, union ibv_gid *gid)
{
    printf("%s: %02x%02x:%02x%02x:%02x%02x:%02x%02x:"
           "%02x%02x:%02x%02x:%02x%02x:%02x%02x\n", label,
           gid->raw[0],  gid->raw[1],  gid->raw[2],  gid->raw[3],
           gid->raw[4],  gid->raw[5],  gid->raw[6],  gid->raw[7],
           gid->raw[8],  gid->raw[9],  gid->raw[10], gid->raw[11],
           gid->raw[12], gid->raw[13], gid->raw[14], gid->raw[15]);
}

/* ================================================================
 *  Cleanup
 * ================================================================ */

static inline void
rdma_cleanup(struct rdma_ctx *rctx)
{
    if (rctx->qp)  ibv_destroy_qp(rctx->qp);
    if (rctx->mr)  ibv_dereg_mr(rctx->mr);
    if (rctx->cq)  ibv_destroy_cq(rctx->cq);
    if (rctx->pd)  ibv_dealloc_pd(rctx->pd);
    if (rctx->ctx) ibv_close_device(rctx->ctx);
    free(rctx->buf);
    memset(rctx, 0, sizeof(*rctx));
}
