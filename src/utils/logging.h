/*
 * logging.h — Simple stderr logging macros for SemiRDMA
 *
 * Uses SEMIRDMA_ prefix to avoid collision with Phase 1's LOG_INFO/LOG_ERR
 * in rdma_common.h.  No external dependencies (no spdlog).
 */

#pragma once

#include <cstdio>

#define SEMIRDMA_LOG_INFO(fmt, ...) \
    fprintf(stderr, "[INFO]  " fmt "\n", ##__VA_ARGS__)

#define SEMIRDMA_LOG_WARN(fmt, ...) \
    fprintf(stderr, "[WARN]  %s:%d: " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__)

#define SEMIRDMA_LOG_ERR(fmt, ...) \
    fprintf(stderr, "[ERROR] %s:%d: " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__)
