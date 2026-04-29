/*
 * control_plane_codec.h — Wire-format encode/decode for CLEAR RC messages
 *
 * The control plane carries 6 message types over an RC QP:
 *     BEGIN, WITNESS, REPAIR_REQ, FINALIZE, RETIRE, BACKPRESSURE
 *
 * This module is the *pure-logic* half of the control plane: every message
 * is packed into a contiguous byte buffer (bounded by kMaxMessageBytes) by
 * an encode_*() function and unpacked by a single decode() that returns a
 * tagged ParsedMessage. No ibverbs, no syscalls, no allocation beyond
 * std::vector when the caller passes one in.
 *
 * The actual RC QP send/recv path (W1.3b) sits on top of this and only
 * cares about (buffer, length) pairs. Splitting the codec out lets us
 * unit-test wire-format correctness without an RDMA device.
 *
 * See docs/phase5/clear-design.md §2.2 for the protocol-level definition.
 */

#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

#include "messages.h"

namespace semirdma::clear {

// ---------------------------------------------------------------------------
// Encoders. Each writes a complete wire message (header + payload) into
// out_buf. Returns the byte count written, or 0 on overflow / invalid input.
// out_cap MUST be at least kMaxMessageBytes for variable-length messages.
// ---------------------------------------------------------------------------

size_t encode_begin(uint64_t uid,
                    const BeginPayload& payload,
                    void* out_buf, size_t out_cap);

// `body` is the witness body produced by encode_witness() in witness_codec.h.
// `body_len` may be zero for FULL_ALL_PRESENT / FULL_ALL_ABSENT.
size_t encode_witness(uint64_t uid,
                      uint32_t recv_count,
                      WitnessEncoding encoding,
                      const uint8_t* body, size_t body_len,
                      void* out_buf, size_t out_cap);

size_t encode_repair_req(uint64_t uid,
                         const Range* ranges, uint16_t n_ranges,
                         void* out_buf, size_t out_cap);

// `mask_body` is the finalized mask, encoded the same way as a witness body.
size_t encode_finalize(uint64_t uid,
                       FinalizeDecision decision,
                       WitnessEncoding mask_encoding,
                       const uint8_t* mask_body, size_t mask_body_len,
                       void* out_buf, size_t out_cap);

size_t encode_retire(uint64_t uid,
                     const RetirePayload& payload,
                     void* out_buf, size_t out_cap);

size_t encode_backpressure(uint64_t uid,
                           const BackpressurePayload& payload,
                           void* out_buf, size_t out_cap);

// ---------------------------------------------------------------------------
// Decoder. Returns true on success and fills exactly one variant of the
// output struct. False on any of: short buffer, version mismatch, declared
// payload_len exceeds buffer, struct size mismatch for fixed-payload msgs.
//
// For variable-payload msgs (WITNESS / REPAIR_REQ / FINALIZE), the body
// pointer in ParsedMessage is a *non-owning view* into the input buffer;
// callers that need to retain the body past the next poll must copy it.
// ---------------------------------------------------------------------------

struct ParsedBegin {
    uint64_t uid;
    BeginPayload payload;
};

struct ParsedWitness {
    uint64_t uid;
    uint32_t recv_count;
    WitnessEncoding encoding;
    const uint8_t* body;     // non-owning; null when body_len == 0
    uint16_t body_len;
};

struct ParsedRepairReq {
    uint64_t uid;
    const Range* ranges;     // non-owning; null when n_ranges == 0
    uint16_t n_ranges;
};

struct ParsedFinalize {
    uint64_t uid;
    FinalizeDecision decision;
    WitnessEncoding mask_encoding;
    const uint8_t* mask_body;
    uint16_t mask_body_len;
};

struct ParsedRetire {
    uint64_t uid;
    RetirePayload payload;
};

struct ParsedBackpressure {
    uint64_t uid;
    BackpressurePayload payload;
};

struct ParsedMessage {
    MsgType type;
    union {
        ParsedBegin        begin;
        ParsedWitness      witness;
        ParsedRepairReq    repair_req;
        ParsedFinalize     finalize;
        ParsedRetire       retire;
        ParsedBackpressure backpressure;
    };

    ParsedMessage() : type(MsgType::BEGIN), begin{0, {}} {}
};

bool decode(const void* in_buf, size_t in_len, ParsedMessage& out);

}  // namespace semirdma::clear
