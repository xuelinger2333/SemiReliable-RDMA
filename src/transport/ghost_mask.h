/*
 * ghost_mask.h — Zero out buffer regions for chunks without CQE
 *
 * P0 conclusion ③: contamination is pure prefix truncation.  Per-chunk
 * masking is "all or nothing" — if CQE received, keep entire chunk;
 * otherwise memset to zero.
 *
 * Static methods only (no state).  GhostMask is an optional post-processing
 * step — decoupled from RatioController so RQ2 experiments can run a
 * no-masking control group via apply_noop.
 */

#pragma once

#include "transport/chunk_manager.h"

#include <cstdint>

namespace semirdma {

class GhostMask {
public:
    // For each chunk without CQE, zero out the corresponding buffer region.
    // buf must point to the base of the registered MR buffer.
    static void apply(uint8_t* buf, const ChunkSet& cs);

    // No-op variant for RQ2 control group (raw aggregation without masking).
    static void apply_noop(uint8_t* buf, const ChunkSet& cs) {
        (void)buf;
        (void)cs;
    }
};

} // namespace semirdma
