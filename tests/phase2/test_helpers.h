/*
 * test_helpers.h — TCP exchange, persistent sync, and fork-based test harness
 *
 * C++ rewrite of Phase 1's TCP exchange (rdma_common.h:252-308) plus
 * the persistent TCP sync pattern from test_netem_loss.c:58-90.
 *
 * The fork-based harness runs server (parent) and client (child) in a
 * single gtest binary.  RDMA resources are created AFTER fork() to avoid
 * ibverbs fork-safety issues.
 */

#pragma once

#include "transport/uc_qp_engine.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <stdexcept>
#include <string>

namespace semirdma { namespace test {

// ================================================================
//  Exchange data: QP info + MR info bundled for TCP exchange
// ================================================================

struct ExchangeData {
    RemoteQpInfo qp;
    RemoteMR     mr;
};

// ================================================================
//  One-shot TCP exchange (mirrors rdma_common.h server/client pattern)
// ================================================================

inline ExchangeData tcp_server_exchange(int port, const ExchangeData& local)
{
    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    if (lfd < 0) throw std::runtime_error("socket() failed");

    int opt = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port        = htons(port);

    if (bind(lfd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        close(lfd);
        throw std::runtime_error(std::string("bind failed: ") + strerror(errno));
    }
    listen(lfd, 1);

    int cfd = accept(lfd, nullptr, nullptr);
    close(lfd);
    if (cfd < 0) throw std::runtime_error("accept failed");

    // Server writes first, client reads first (same as Phase 1)
    ssize_t nw = write(cfd, &local, sizeof(local));
    if (nw != static_cast<ssize_t>(sizeof(local))) {
        close(cfd); throw std::runtime_error("TCP write failed");
    }

    ExchangeData remote;
    ssize_t nr = read(cfd, &remote, sizeof(remote));
    if (nr != static_cast<ssize_t>(sizeof(remote))) {
        close(cfd); throw std::runtime_error("TCP read failed");
    }

    close(cfd);
    return remote;
}

inline ExchangeData tcp_client_exchange(const char* server_ip, int port,
                                        const ExchangeData& local)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error("socket() failed");

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(port);
    inet_pton(AF_INET, server_ip, &addr.sin_addr);

    if (connect(fd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        close(fd);
        throw std::runtime_error(std::string("connect failed: ") + strerror(errno));
    }

    // Client reads first (server sends first)
    ExchangeData remote;
    ssize_t nr = read(fd, &remote, sizeof(remote));
    if (nr != static_cast<ssize_t>(sizeof(remote))) {
        close(fd); throw std::runtime_error("TCP read failed");
    }

    ssize_t nw = write(fd, &local, sizeof(local));
    if (nw != static_cast<ssize_t>(sizeof(local))) {
        close(fd); throw std::runtime_error("TCP write failed");
    }

    close(fd);
    return remote;
}

// ================================================================
//  Persistent TCP helpers (for multi-round sync, mirrors test_netem_loss.c)
// ================================================================

inline int tcp_listen_accept(int port)
{
    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    int opt = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port        = htons(port);

    if (bind(lfd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
        close(lfd);
        throw std::runtime_error(std::string("bind: ") + strerror(errno));
    }
    listen(lfd, 1);

    int cfd = accept(lfd, nullptr, nullptr);
    close(lfd);
    if (cfd < 0) throw std::runtime_error("accept failed");
    return cfd;
}

inline int tcp_connect_to(const char* ip, int port, int max_retries = 20)
{
    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port   = htons(port);
    inet_pton(AF_INET, ip, &addr.sin_addr);

    for (int attempt = 0; attempt < max_retries; attempt++) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        if (connect(fd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) == 0) {
            return fd;
        }
        close(fd);
        // Retry with backoff: 50ms, 100ms, 150ms, ...
        usleep(50000 * (attempt + 1));
    }
    throw std::runtime_error(std::string("connect to ") + ip + ":" +
                             std::to_string(port) + " failed after retries");
}

inline void tcp_signal(int fd) { uint8_t b = 1; ssize_t r = write(fd, &b, 1); (void)r; }
inline void tcp_wait(int fd)   { uint8_t b;     ssize_t r = read(fd, &b, 1);  (void)r; }

// ================================================================
//  Persistent TCP exchange on an already-connected fd
// ================================================================

inline ExchangeData tcp_exchange_on_fd_server(int fd, const ExchangeData& local)
{
    ssize_t nw = write(fd, &local, sizeof(local));
    if (nw != static_cast<ssize_t>(sizeof(local)))
        throw std::runtime_error("TCP write failed");

    ExchangeData remote;
    ssize_t nr = read(fd, &remote, sizeof(remote));
    if (nr != static_cast<ssize_t>(sizeof(remote)))
        throw std::runtime_error("TCP read failed");
    return remote;
}

inline ExchangeData tcp_exchange_on_fd_client(int fd, const ExchangeData& local)
{
    ExchangeData remote;
    ssize_t nr = read(fd, &remote, sizeof(remote));
    if (nr != static_cast<ssize_t>(sizeof(remote)))
        throw std::runtime_error("TCP read failed");

    ssize_t nw = write(fd, &local, sizeof(local));
    if (nw != static_cast<ssize_t>(sizeof(local)))
        throw std::runtime_error("TCP write failed");
    return remote;
}

// ================================================================
//  Fork-based server/client harness for gtest
// ================================================================

// Runs server_fn in parent and client_fn in forked child.
// Both return 0 on success, non-zero on failure.
// Parent asserts child exited 0.
//
// IMPORTANT: Do NOT create any RDMA resources before calling this.
// All UCQPEngine construction must happen inside server_fn / client_fn.
inline void run_server_client(std::function<int()> server_fn,
                              std::function<int()> client_fn)
{
    pid_t pid = fork();
    if (pid < 0) {
        throw std::runtime_error("fork() failed");
    }

    if (pid == 0) {
        // ----- Child = client -----
        // Give server time to bind + listen
        usleep(300000);  // 300ms

        int rc = 1;
        try {
            rc = client_fn();
        } catch (const std::exception& e) {
            fprintf(stderr, "[CLIENT ERROR] %s\n", e.what());
        }
        _exit(rc);
    }

    // ----- Parent = server -----
    int server_rc = 1;
    try {
        server_rc = server_fn();
    } catch (const std::exception& e) {
        fprintf(stderr, "[SERVER ERROR] %s\n", e.what());
    }

    // Wait for child
    int status = 0;
    waitpid(pid, &status, 0);

    if (server_rc != 0) {
        fprintf(stderr, "[FAIL] Server returned %d\n", server_rc);
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "[FAIL] Client exited with status %d\n",
                WIFEXITED(status) ? WEXITSTATUS(status) : -1);
    }

    // Use assert-style checks that work in gtest context (parent only)
    if (server_rc != 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        throw std::runtime_error("Server/client test failed");
    }
}

}} // namespace semirdma::test
