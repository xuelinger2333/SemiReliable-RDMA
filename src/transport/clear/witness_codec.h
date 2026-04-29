/*
 * witness_codec.h — Density-aware bitmap encoding for WITNESS / FINALIZE
 *
 * The receiver reports recv_bitmap to the finalizer via WITNESS; the finalizer
 * publishes the canonical mask back to all ranks via FINALIZE. Both messages
 * carry the same kind of payload: a bitmap of length n_chunks where bit i
 * indicates "chunk i was observed" (witness) or "chunk i is present in the
 * finalized buffer" (finalize mask).
 *
 * To keep control-plane bytes ≤ 1% of payload (Phase 5 §5 PHASE5_PLAN.md),
 * we pick the smallest-on-wire encoding per call:
 *   FULL_ALL_PRESENT  : all bits 1; zero payload bytes.
 *   FULL_ALL_ABSENT   : all bits 0; zero payload bytes.
 *   RAW               : ceil(n_bits / 8) bytes; bit-packed LSB-first.
 *   RANGE_MISSING     : list of (start, length) chunk-runs that are absent.
 *                       Cheaper than RAW when the number of missing runs is
 *                       small relative to n_bits / 64.
 *
 * RLE is intentionally omitted in T2: RANGE_MISSING covers sparse-missing,
 * RAW covers dense-missing, and the breakeven for two-phase RLE is narrow.
 *
 * This module is dependency-free (only <cstdint>, <vector>) so it lives under
 * unit tests with no RDMA fixture.
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "messages.h"

namespace semirdma::clear {

struct WitnessEncodeResult {
    WitnessEncoding encoding;
    std::vector<uint8_t> body;   // empty for FULL_*
    uint32_t recv_count;          // number of bits set in input bitmap
};

// Encode an n_chunks-long bitmap into the smallest-byte representation.
// `bitmap` is interpreted as a bit-packed LSB-first array of length n_chunks
// (so bitmap.size() must be ≥ ceil(n_chunks / 8)). Bits beyond n_chunks are
// ignored.
//
// The encoder picks the encoding with the smallest body_len:
//   - if recv_count == n_chunks → FULL_ALL_PRESENT
//   - if recv_count == 0        → FULL_ALL_ABSENT
//   - else compare RAW size vs RANGE_MISSING size; pick the smaller.
WitnessEncodeResult encode_witness(const uint8_t* bitmap,
                                   size_t bitmap_bytes,
                                   uint32_t n_chunks);

// Decode a body back into a bit-packed LSB-first bitmap of length n_chunks.
// Returns false on malformed input (wrong body size, out-of-range indices,
// overlapping ranges in RANGE_MISSING). On success, out_bitmap is resized to
// ceil(n_chunks / 8) and out_recv_count is the popcount of out_bitmap.
bool decode_witness(WitnessEncoding encoding,
                    const uint8_t* body,
                    size_t body_len,
                    uint32_t n_chunks,
                    std::vector<uint8_t>& out_bitmap,
                    uint32_t& out_recv_count);

// Helpers exposed for unit tests and for the finalizer.
inline bool bitmap_get(const uint8_t* bitmap, size_t bitmap_bytes,
                       uint32_t bit) {
    size_t byte_idx = bit >> 3;
    if (byte_idx >= bitmap_bytes) return false;
    return (bitmap[byte_idx] >> (bit & 7)) & 1u;
}

inline void bitmap_set(uint8_t* bitmap, uint32_t bit) {
    bitmap[bit >> 3] |= static_cast<uint8_t>(1u << (bit & 7));
}

uint32_t bitmap_popcount(const uint8_t* bitmap, uint32_t n_bits);

}  // namespace semirdma::clear
