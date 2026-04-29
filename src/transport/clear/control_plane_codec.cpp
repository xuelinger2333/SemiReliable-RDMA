/*
 * control_plane_codec.cpp — see control_plane_codec.h
 */

#include "control_plane_codec.h"

#include <cstring>

namespace semirdma::clear {

namespace {

inline uint8_t* write_after_header(void* out_buf) {
    return static_cast<uint8_t*>(out_buf) + sizeof(MsgHeader);
}

// Cap a uint16_t-typed payload_len so we never silently overflow.
inline bool fits_payload_len(size_t bytes, uint16_t& out_len) {
    if (bytes > 0xFFFFu) return false;
    out_len = static_cast<uint16_t>(bytes);
    return true;
}

}  // namespace

// ---------- Encoders --------------------------------------------------------

size_t encode_begin(uint64_t uid, const BeginPayload& payload,
                    void* out_buf, size_t out_cap) {
    constexpr size_t kTotal = sizeof(MsgHeader) + sizeof(BeginPayload);
    if (out_cap < kTotal) return 0;
    encode_header(MsgType::BEGIN, uid, sizeof(BeginPayload), out_buf, out_cap);
    std::memcpy(write_after_header(out_buf), &payload, sizeof(payload));
    return kTotal;
}

size_t encode_witness(uint64_t uid, uint32_t recv_count,
                      WitnessEncoding encoding,
                      const uint8_t* body, size_t body_len,
                      void* out_buf, size_t out_cap) {
    uint16_t body_len_u16 = 0;
    if (!fits_payload_len(body_len, body_len_u16)) return 0;
    const size_t total =
        sizeof(MsgHeader) + sizeof(WitnessPayloadHead) + body_len;
    if (total > kMaxMessageBytes || out_cap < total) return 0;

    uint16_t payload_len = 0;
    if (!fits_payload_len(sizeof(WitnessPayloadHead) + body_len, payload_len)) {
        return 0;
    }
    encode_header(MsgType::WITNESS, uid, payload_len, out_buf, out_cap);

    WitnessPayloadHead head{};
    head.recv_count = recv_count;
    head.encoding   = static_cast<uint8_t>(encoding);
    head.reserved   = 0;
    head.body_len   = body_len_u16;
    std::memcpy(write_after_header(out_buf), &head, sizeof(head));
    if (body_len > 0) {
        std::memcpy(write_after_header(out_buf) + sizeof(head),
                    body, body_len);
    }
    return total;
}

size_t encode_repair_req(uint64_t uid,
                         const Range* ranges, uint16_t n_ranges,
                         void* out_buf, size_t out_cap) {
    const size_t body_bytes = static_cast<size_t>(n_ranges) * sizeof(Range);
    const size_t total =
        sizeof(MsgHeader) + sizeof(RepairReqPayloadHead) + body_bytes;
    if (total > kMaxMessageBytes || out_cap < total) return 0;

    uint16_t payload_len = 0;
    if (!fits_payload_len(sizeof(RepairReqPayloadHead) + body_bytes,
                          payload_len)) {
        return 0;
    }
    encode_header(MsgType::REPAIR_REQ, uid, payload_len, out_buf, out_cap);

    RepairReqPayloadHead head{};
    head.n_ranges = n_ranges;
    head.reserved = 0;
    head.reserved2 = 0;
    std::memcpy(write_after_header(out_buf), &head, sizeof(head));
    if (n_ranges > 0) {
        std::memcpy(write_after_header(out_buf) + sizeof(head),
                    ranges, body_bytes);
    }
    return total;
}

size_t encode_finalize(uint64_t uid, FinalizeDecision decision,
                       WitnessEncoding mask_encoding,
                       const uint8_t* mask_body, size_t mask_body_len,
                       void* out_buf, size_t out_cap) {
    uint16_t body_len_u16 = 0;
    if (!fits_payload_len(mask_body_len, body_len_u16)) return 0;
    const size_t total = sizeof(MsgHeader) + sizeof(FinalizePayloadHead) +
                         mask_body_len;
    if (total > kMaxMessageBytes || out_cap < total) return 0;

    uint16_t payload_len = 0;
    if (!fits_payload_len(sizeof(FinalizePayloadHead) + mask_body_len,
                          payload_len)) {
        return 0;
    }
    encode_header(MsgType::FINALIZE, uid, payload_len, out_buf, out_cap);

    FinalizePayloadHead head{};
    head.decision      = static_cast<uint8_t>(decision);
    head.mask_encoding = static_cast<uint8_t>(mask_encoding);
    head.body_len      = body_len_u16;
    head.reserved      = 0;
    std::memcpy(write_after_header(out_buf), &head, sizeof(head));
    if (mask_body_len > 0) {
        std::memcpy(write_after_header(out_buf) + sizeof(head),
                    mask_body, mask_body_len);
    }
    return total;
}

size_t encode_retire(uint64_t uid, const RetirePayload& payload,
                     void* out_buf, size_t out_cap) {
    constexpr size_t kTotal = sizeof(MsgHeader) + sizeof(RetirePayload);
    if (out_cap < kTotal) return 0;
    encode_header(MsgType::RETIRE, uid, sizeof(RetirePayload), out_buf, out_cap);
    std::memcpy(write_after_header(out_buf), &payload, sizeof(payload));
    return kTotal;
}

size_t encode_backpressure(uint64_t uid, const BackpressurePayload& payload,
                           void* out_buf, size_t out_cap) {
    constexpr size_t kTotal = sizeof(MsgHeader) + sizeof(BackpressurePayload);
    if (out_cap < kTotal) return 0;
    encode_header(MsgType::BACKPRESSURE, uid, sizeof(BackpressurePayload),
                  out_buf, out_cap);
    std::memcpy(write_after_header(out_buf), &payload, sizeof(payload));
    return kTotal;
}

// ---------- Decoder ---------------------------------------------------------

bool decode(const void* in_buf, size_t in_len, ParsedMessage& out) {
    MsgHeader h{};
    if (!decode_header(in_buf, in_len, h)) return false;

    const uint8_t* body = static_cast<const uint8_t*>(in_buf) + sizeof(h);
    const uint16_t body_len = h.payload_len;
    out.type = static_cast<MsgType>(h.type);

    switch (out.type) {
    case MsgType::BEGIN: {
        if (body_len != sizeof(BeginPayload)) return false;
        out.begin.uid = h.uid;
        std::memcpy(&out.begin.payload, body, sizeof(BeginPayload));
        return true;
    }
    case MsgType::WITNESS: {
        if (body_len < sizeof(WitnessPayloadHead)) return false;
        WitnessPayloadHead head{};
        std::memcpy(&head, body, sizeof(head));
        if (sizeof(head) + head.body_len != body_len) return false;
        out.witness.uid        = h.uid;
        out.witness.recv_count = head.recv_count;
        out.witness.encoding   = static_cast<WitnessEncoding>(head.encoding);
        out.witness.body_len   = head.body_len;
        out.witness.body       = head.body_len > 0
                                 ? body + sizeof(head)
                                 : nullptr;
        return true;
    }
    case MsgType::REPAIR_REQ: {
        if (body_len < sizeof(RepairReqPayloadHead)) return false;
        RepairReqPayloadHead head{};
        std::memcpy(&head, body, sizeof(head));
        const size_t expected =
            sizeof(head) + static_cast<size_t>(head.n_ranges) * sizeof(Range);
        if (expected != body_len) return false;
        out.repair_req.uid      = h.uid;
        out.repair_req.n_ranges = head.n_ranges;
        out.repair_req.ranges   = head.n_ranges > 0
            ? reinterpret_cast<const Range*>(body + sizeof(head))
            : nullptr;
        return true;
    }
    case MsgType::FINALIZE: {
        if (body_len < sizeof(FinalizePayloadHead)) return false;
        FinalizePayloadHead head{};
        std::memcpy(&head, body, sizeof(head));
        if (sizeof(head) + head.body_len != body_len) return false;
        out.finalize.uid           = h.uid;
        out.finalize.decision      = static_cast<FinalizeDecision>(head.decision);
        out.finalize.mask_encoding =
            static_cast<WitnessEncoding>(head.mask_encoding);
        out.finalize.mask_body_len = head.body_len;
        out.finalize.mask_body     = head.body_len > 0
                                     ? body + sizeof(head)
                                     : nullptr;
        return true;
    }
    case MsgType::RETIRE: {
        if (body_len != sizeof(RetirePayload)) return false;
        out.retire.uid = h.uid;
        std::memcpy(&out.retire.payload, body, sizeof(RetirePayload));
        return true;
    }
    case MsgType::BACKPRESSURE: {
        if (body_len != sizeof(BackpressurePayload)) return false;
        out.backpressure.uid = h.uid;
        std::memcpy(&out.backpressure.payload, body,
                    sizeof(BackpressurePayload));
        return true;
    }
    }
    return false;  // unknown MsgType
}

}  // namespace semirdma::clear
