/*
 * messages.h — CLEAR control-plane wire format (Phase 5)
 *
 * Five POD message types travel over a per-peer RC QP:
 *   BEGIN, WITNESS, REPAIR_REQ, FINALIZE, RETIRE
 * Plus one out-of-band signal:
 *   BACKPRESSURE (sent on RQ low-watermark)
 *
 * All messages share an 8-byte MsgHeader (type, version, payload_len, uid).
 * Payload layout is fixed for non-WITNESS / non-REPAIR_REQ / non-FINALIZE
 * messages and variable-length for those three (compressed bitmap / range list).
 *
 * Byte order: native little-endian. Both ends are x86_64 in Phase 5.
 * If we ever need cross-arch interop, swap to explicit le32/le64 helpers.
 *
 * Design note: this header is dependency-free (no ibverbs, no STL containers
 * in the wire structs) so it can be unit-tested standalone.
 *
 * See docs/phase5/clear-design.md §2 for protocol-level details.
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace semirdma::clear {

constexpr uint8_t kProtocolVersion = 1;

enum class MsgType : uint8_t {
    BEGIN        = 1,
    WITNESS      = 2,
    REPAIR_REQ   = 3,
    FINALIZE     = 4,
    RETIRE       = 5,
    BACKPRESSURE = 6,
};

enum class Policy : uint8_t {
    REPAIR_FIRST    = 1,
    MASK_FIRST      = 2,
    STALE_FILL      = 3,
    ESTIMATOR_SCALE = 4,
};

enum class FinalizeDecision : uint8_t {
    DELIVERED    = 1,  // recv_count == n_chunks; no mask needed
    REPAIRED     = 2,  // recovered via REPAIR_REQ within budget
    MASKED       = 3,  // missing chunks zero-masked
    STALE        = 4,  // missing chunks filled from previous step
    FALLBACK_RC  = 5,  // bucket resent over RC
};

enum class WitnessEncoding : uint8_t {
    FULL_ALL_PRESENT = 1,  // payload empty
    FULL_ALL_ABSENT  = 2,  // payload empty
    RAW              = 3,  // ceil(n_bits/8) bytes; bit i = recv_bitmap[i]
    RANGE_MISSING    = 4,  // u16 n_ranges; then n_ranges × {u32 start, u32 len}
};

// Common 16-byte header. payload_len excludes this header.
#pragma pack(push, 1)
struct MsgHeader {
    uint8_t  type;          // MsgType
    uint8_t  version;       // == kProtocolVersion
    uint16_t payload_len;   // bytes after header
    uint32_t reserved;      // alignment padding; must be zero
    uint64_t uid;           // transfer identifier
};
static_assert(sizeof(MsgHeader) == 16, "MsgHeader must be 16 bytes");

// BEGIN payload (40 bytes)
struct BeginPayload {
    uint8_t  slot_id;
    uint8_t  gen;
    uint8_t  phase_id;
    uint8_t  policy;          // Policy enum
    uint16_t peer_edge;
    uint16_t reserved;        // must be zero
    uint32_t step_seq;
    uint32_t bucket_seq;
    uint32_t n_chunks;
    uint32_t deadline_us;
    uint32_t chunk_bytes;
    uint32_t checksum_seed;
    uint64_t reserved2;       // future use; must be zero
};
static_assert(sizeof(BeginPayload) == 40, "BeginPayload must be 40 bytes");

// WITNESS payload header (8 bytes); variable witness body follows.
struct WitnessPayloadHead {
    uint32_t recv_count;
    uint8_t  encoding;        // WitnessEncoding enum
    uint8_t  reserved;
    uint16_t body_len;        // bytes after this struct
};
static_assert(sizeof(WitnessPayloadHead) == 8, "WitnessPayloadHead must be 8 bytes");

// REPAIR_REQ payload header (8 bytes); n_ranges × Range follows.
struct RepairReqPayloadHead {
    uint16_t n_ranges;
    uint16_t reserved;
    uint32_t reserved2;
};
static_assert(sizeof(RepairReqPayloadHead) == 8,
              "RepairReqPayloadHead must be 8 bytes");

struct Range {
    uint32_t start;   // chunk_idx of range start
    uint32_t length;  // number of consecutive chunks
};
static_assert(sizeof(Range) == 8, "Range must be 8 bytes");

// FINALIZE payload header (8 bytes); variable mask body follows.
struct FinalizePayloadHead {
    uint8_t  decision;        // FinalizeDecision enum
    uint8_t  mask_encoding;   // WitnessEncoding enum (reused for mask)
    uint16_t body_len;        // bytes after this struct
    uint32_t reserved;
};
static_assert(sizeof(FinalizePayloadHead) == 8,
              "FinalizePayloadHead must be 8 bytes");

// RETIRE payload (8 bytes)
struct RetirePayload {
    uint8_t  slot_id;
    uint8_t  gen;
    uint16_t reserved;
    uint32_t reserved2;
};
static_assert(sizeof(RetirePayload) == 8, "RetirePayload must be 8 bytes");

// BACKPRESSURE payload (8 bytes)
struct BackpressurePayload {
    uint16_t peer_edge;
    uint16_t requested_credits;
    uint32_t reserved;
};
static_assert(sizeof(BackpressurePayload) == 8,
              "BackpressurePayload must be 8 bytes");
#pragma pack(pop)

// Bound the total wire size of any message we will ever post on the RC QP.
// At bucket_cap_mb=1 with 4 KiB chunks → 256 chunks → RAW witness ≤ 32 bytes,
// RANGE_MISSING worst-case ≤ 256 × 8 = 2048 bytes. Pick 4 KiB as a generous
// upper bound and pre-post RC receive buffers of this size.
constexpr size_t kMaxMessageBytes = 4096;

// Encode a full message (header + payload) into out_buf. Returns total bytes
// written, or 0 on overflow. The caller is responsible for posting the buffer
// as an RC SEND.
//
// These are tiny one-line wrappers; the heavy lifting (variable bodies) is
// in witness_codec.cpp / finalizer.cpp before they call into us.
inline size_t encode_header(MsgType type, uint64_t uid, uint16_t payload_len,
                            void* out_buf, size_t out_cap) {
    if (out_cap < sizeof(MsgHeader)) return 0;
    MsgHeader h{};
    h.type        = static_cast<uint8_t>(type);
    h.version     = kProtocolVersion;
    h.payload_len = payload_len;
    h.reserved    = 0;
    h.uid         = uid;
    std::memcpy(out_buf, &h, sizeof(h));
    return sizeof(h);
}

// Returns false if the buffer is too small to even hold a header, the version
// mismatches, or the declared payload_len exceeds the buffer.
inline bool decode_header(const void* in_buf, size_t in_len,
                          MsgHeader& out_header) {
    if (in_len < sizeof(MsgHeader)) return false;
    std::memcpy(&out_header, in_buf, sizeof(out_header));
    if (out_header.version != kProtocolVersion) return false;
    if (sizeof(MsgHeader) + out_header.payload_len > in_len) return false;
    return true;
}

}  // namespace semirdma::clear
