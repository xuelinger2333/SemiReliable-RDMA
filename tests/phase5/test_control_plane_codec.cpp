// Phase 5 unit tests — CLEAR control_plane_codec
//
// Pure-logic; verifies encode → decode roundtrip for all 6 message types
// plus malformed-input rejection. Pairs with witness_codec for the bitmap
// body content.

#include <cstdint>
#include <cstring>
#include <vector>

#include <gtest/gtest.h>

#include "transport/clear/control_plane_codec.h"
#include "transport/clear/messages.h"
#include "transport/clear/witness_codec.h"

namespace sc = semirdma::clear;

namespace {

constexpr uint64_t kTestUid = 0xCAFEBABE12345678ull;

sc::BeginPayload make_begin_payload() {
    sc::BeginPayload p{};
    p.slot_id        = 7;
    p.gen            = 3;
    p.phase_id       = 1;
    p.policy         = static_cast<uint8_t>(sc::Policy::REPAIR_FIRST);
    p.peer_edge      = 0xABCD;
    p.reserved       = 0;
    p.step_seq       = 0xDEADBEEFu;
    p.bucket_seq     = 42u;
    p.n_chunks       = 1024u;
    p.deadline_us    = 200'000u;
    p.chunk_bytes    = 4096u;
    p.checksum_seed  = 0x12345678u;
    p.reserved2      = 0;
    return p;
}

}  // namespace

// ---- BEGIN ------------------------------------------------------------------

TEST(ControlPlaneCodec, BeginRoundtrip) {
    auto p = make_begin_payload();
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_begin(kTestUid, p, buf, sizeof(buf));
    ASSERT_EQ(n, sizeof(sc::MsgHeader) + sizeof(sc::BeginPayload));

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.type, sc::MsgType::BEGIN);
    EXPECT_EQ(out.begin.uid, kTestUid);
    EXPECT_EQ(out.begin.payload.slot_id, p.slot_id);
    EXPECT_EQ(out.begin.payload.gen, p.gen);
    EXPECT_EQ(out.begin.payload.phase_id, p.phase_id);
    EXPECT_EQ(out.begin.payload.policy, p.policy);
    EXPECT_EQ(out.begin.payload.peer_edge, p.peer_edge);
    EXPECT_EQ(out.begin.payload.step_seq, p.step_seq);
    EXPECT_EQ(out.begin.payload.bucket_seq, p.bucket_seq);
    EXPECT_EQ(out.begin.payload.n_chunks, p.n_chunks);
    EXPECT_EQ(out.begin.payload.deadline_us, p.deadline_us);
    EXPECT_EQ(out.begin.payload.chunk_bytes, p.chunk_bytes);
    EXPECT_EQ(out.begin.payload.checksum_seed, p.checksum_seed);
}

TEST(ControlPlaneCodec, BeginRejectsShortBuf) {
    auto p = make_begin_payload();
    uint8_t buf[8] = {};
    EXPECT_EQ(sc::encode_begin(kTestUid, p, buf, sizeof(buf)), 0u);
}

// ---- WITNESS ----------------------------------------------------------------

TEST(ControlPlaneCodec, WitnessFullEncoding) {
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_witness(kTestUid, /*recv_count=*/1024,
                                  sc::WitnessEncoding::FULL_ALL_PRESENT,
                                  /*body=*/nullptr, /*body_len=*/0,
                                  buf, sizeof(buf));
    ASSERT_GT(n, 0u);
    EXPECT_EQ(n, sizeof(sc::MsgHeader) + sizeof(sc::WitnessPayloadHead));

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.type, sc::MsgType::WITNESS);
    EXPECT_EQ(out.witness.uid, kTestUid);
    EXPECT_EQ(out.witness.recv_count, 1024u);
    EXPECT_EQ(out.witness.encoding, sc::WitnessEncoding::FULL_ALL_PRESENT);
    EXPECT_EQ(out.witness.body_len, 0u);
    EXPECT_EQ(out.witness.body, nullptr);
}

TEST(ControlPlaneCodec, WitnessRangeBodyRoundtrip) {
    // Build a real witness body via witness_codec to ensure compatibility.
    constexpr uint32_t N = 1024;
    std::vector<uint8_t> bm((N + 7u) >> 3, 0xFF);
    bm[10] = 0;  // 8 missing chunks at offset 80
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    ASSERT_EQ(enc.encoding, sc::WitnessEncoding::RANGE_MISSING);

    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_witness(kTestUid, enc.recv_count, enc.encoding,
                                  enc.body.data(), enc.body.size(),
                                  buf, sizeof(buf));
    ASSERT_GT(n, 0u);

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.witness.uid, kTestUid);
    EXPECT_EQ(out.witness.recv_count, enc.recv_count);
    EXPECT_EQ(out.witness.encoding, sc::WitnessEncoding::RANGE_MISSING);
    EXPECT_EQ(out.witness.body_len, enc.body.size());
    ASSERT_NE(out.witness.body, nullptr);
    EXPECT_EQ(0, std::memcmp(out.witness.body, enc.body.data(),
                             enc.body.size()));

    // Decode the body using witness_codec — should match the original bitmap.
    std::vector<uint8_t> redecoded;
    uint32_t cnt = 0;
    ASSERT_TRUE(sc::decode_witness(out.witness.encoding, out.witness.body,
                                   out.witness.body_len, N, redecoded, cnt));
    for (uint32_t i = 0; i < N; ++i) {
        EXPECT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                  sc::bitmap_get(redecoded.data(), redecoded.size(), i));
    }
}

TEST(ControlPlaneCodec, WitnessRejectsInconsistentBodyLen) {
    // Hand-craft a malformed buffer: declared body_len doesn't match
    // payload_len.
    uint8_t buf[sc::kMaxMessageBytes] = {};
    sc::encode_header(sc::MsgType::WITNESS, kTestUid,
                      /*payload_len=*/sizeof(sc::WitnessPayloadHead) + 4,
                      buf, sizeof(buf));
    sc::WitnessPayloadHead head{};
    head.recv_count = 100;
    head.encoding   = static_cast<uint8_t>(sc::WitnessEncoding::RAW);
    head.body_len   = 8;  // mismatches payload_len (which says 4 body bytes)
    std::memcpy(buf + sizeof(sc::MsgHeader), &head, sizeof(head));

    sc::ParsedMessage out;
    EXPECT_FALSE(sc::decode(buf, sizeof(sc::MsgHeader) +
                                 sizeof(sc::WitnessPayloadHead) + 4, out));
}

// ---- REPAIR_REQ -------------------------------------------------------------

TEST(ControlPlaneCodec, RepairReqRoundtrip) {
    sc::Range ranges[3] = {{10, 5}, {100, 1}, {500, 20}};
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_repair_req(kTestUid, ranges, 3, buf, sizeof(buf));
    ASSERT_GT(n, 0u);

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.type, sc::MsgType::REPAIR_REQ);
    EXPECT_EQ(out.repair_req.uid, kTestUid);
    EXPECT_EQ(out.repair_req.n_ranges, 3u);
    ASSERT_NE(out.repair_req.ranges, nullptr);
    EXPECT_EQ(out.repair_req.ranges[0].start, 10u);
    EXPECT_EQ(out.repair_req.ranges[0].length, 5u);
    EXPECT_EQ(out.repair_req.ranges[2].start, 500u);
    EXPECT_EQ(out.repair_req.ranges[2].length, 20u);
}

TEST(ControlPlaneCodec, RepairReqEmpty) {
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_repair_req(kTestUid, /*ranges=*/nullptr,
                                     /*n_ranges=*/0, buf, sizeof(buf));
    ASSERT_GT(n, 0u);
    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.repair_req.n_ranges, 0u);
    EXPECT_EQ(out.repair_req.ranges, nullptr);
}

// ---- FINALIZE ---------------------------------------------------------------

TEST(ControlPlaneCodec, FinalizeRoundtrip) {
    constexpr uint32_t N = 256;
    std::vector<uint8_t> mask((N + 7u) >> 3, 0xFF);
    mask[5] &= 0x0F;  // some bits cleared
    auto enc = sc::encode_witness(mask.data(), mask.size(), N);

    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_finalize(kTestUid, sc::FinalizeDecision::MASKED,
                                   enc.encoding, enc.body.data(),
                                   enc.body.size(), buf, sizeof(buf));
    ASSERT_GT(n, 0u);

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.type, sc::MsgType::FINALIZE);
    EXPECT_EQ(out.finalize.uid, kTestUid);
    EXPECT_EQ(out.finalize.decision, sc::FinalizeDecision::MASKED);
    EXPECT_EQ(out.finalize.mask_encoding, enc.encoding);
    EXPECT_EQ(out.finalize.mask_body_len, enc.body.size());
}

TEST(ControlPlaneCodec, FinalizeDeliveredHasNoMask) {
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_finalize(kTestUid, sc::FinalizeDecision::DELIVERED,
                                   sc::WitnessEncoding::FULL_ALL_PRESENT,
                                   nullptr, 0, buf, sizeof(buf));
    ASSERT_GT(n, 0u);
    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.finalize.decision, sc::FinalizeDecision::DELIVERED);
    EXPECT_EQ(out.finalize.mask_body_len, 0u);
    EXPECT_EQ(out.finalize.mask_body, nullptr);
}

// ---- RETIRE -----------------------------------------------------------------

TEST(ControlPlaneCodec, RetireRoundtrip) {
    sc::RetirePayload p{};
    p.slot_id   = 13;
    p.gen       = 7;
    p.reserved  = 0;
    p.reserved2 = 0;
    uint8_t buf[64] = {};
    size_t n = sc::encode_retire(kTestUid, p, buf, sizeof(buf));
    ASSERT_EQ(n, sizeof(sc::MsgHeader) + sizeof(sc::RetirePayload));

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.type, sc::MsgType::RETIRE);
    EXPECT_EQ(out.retire.uid, kTestUid);
    EXPECT_EQ(out.retire.payload.slot_id, 13u);
    EXPECT_EQ(out.retire.payload.gen, 7u);
}

// ---- BACKPRESSURE -----------------------------------------------------------

TEST(ControlPlaneCodec, BackpressureRoundtrip) {
    sc::BackpressurePayload p{};
    p.peer_edge         = 0x1234;
    p.requested_credits = 64;
    uint8_t buf[64] = {};
    size_t n = sc::encode_backpressure(kTestUid, p, buf, sizeof(buf));
    ASSERT_EQ(n, sizeof(sc::MsgHeader) + sizeof(sc::BackpressurePayload));

    sc::ParsedMessage out;
    ASSERT_TRUE(sc::decode(buf, n, out));
    EXPECT_EQ(out.type, sc::MsgType::BACKPRESSURE);
    EXPECT_EQ(out.backpressure.uid, kTestUid);
    EXPECT_EQ(out.backpressure.payload.peer_edge, 0x1234u);
    EXPECT_EQ(out.backpressure.payload.requested_credits, 64u);
}

// ---- Decoder edge cases -----------------------------------------------------

TEST(ControlPlaneCodec, DecodeRejectsTruncatedBuffer) {
    auto p = make_begin_payload();
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_begin(kTestUid, p, buf, sizeof(buf));
    ASSERT_GT(n, 0u);
    sc::ParsedMessage out;
    EXPECT_FALSE(sc::decode(buf, n - 1, out));
}

TEST(ControlPlaneCodec, DecodeRejectsUnknownType) {
    uint8_t buf[sizeof(sc::MsgHeader)] = {};
    sc::encode_header(static_cast<sc::MsgType>(99), kTestUid,
                      /*payload_len=*/0, buf, sizeof(buf));
    sc::ParsedMessage out;
    EXPECT_FALSE(sc::decode(buf, sizeof(buf), out));
}

TEST(ControlPlaneCodec, DecodeRejectsWrongFixedPayloadSize) {
    // Encode RETIRE with a hand-set payload_len that doesn't match
    // sizeof(RetirePayload).
    uint8_t buf[64] = {};
    sc::encode_header(sc::MsgType::RETIRE, kTestUid, /*payload_len=*/16,
                      buf, sizeof(buf));
    // 16 bytes of zero payload follow — payload_len=16 != sizeof(RetirePayload)=8
    sc::ParsedMessage out;
    EXPECT_FALSE(sc::decode(buf, sizeof(sc::MsgHeader) + 16, out));
}

TEST(ControlPlaneCodec, DecodeRejectsRepairInconsistentRangeCount) {
    // Build a repair_req whose declared n_ranges doesn't match payload_len.
    uint8_t buf[sc::kMaxMessageBytes] = {};
    sc::encode_header(sc::MsgType::REPAIR_REQ, kTestUid,
                      /*payload_len=*/sizeof(sc::RepairReqPayloadHead) +
                                       2 * sizeof(sc::Range),
                      buf, sizeof(buf));
    sc::RepairReqPayloadHead head{};
    head.n_ranges = 5;  // claims 5 but payload only has 2
    std::memcpy(buf + sizeof(sc::MsgHeader), &head, sizeof(head));

    sc::ParsedMessage out;
    EXPECT_FALSE(sc::decode(buf,
                            sizeof(sc::MsgHeader) +
                            sizeof(sc::RepairReqPayloadHead) +
                            2 * sizeof(sc::Range), out));
}

// ---- Output-buffer overflow rejections --------------------------------------

TEST(ControlPlaneCodec, EncodeReturnsZeroOnSmallOutBuf) {
    auto p = make_begin_payload();
    uint8_t buf[1];
    EXPECT_EQ(sc::encode_begin(kTestUid, p, buf, sizeof(buf)), 0u);

    sc::RetirePayload rp{};
    EXPECT_EQ(sc::encode_retire(kTestUid, rp, buf, sizeof(buf)), 0u);

    sc::BackpressurePayload bp{};
    EXPECT_EQ(sc::encode_backpressure(kTestUid, bp, buf, sizeof(buf)), 0u);
}

TEST(ControlPlaneCodec, EncodeRejectsOversizedWitnessBody) {
    // Request a body_len so large it would exceed kMaxMessageBytes.
    std::vector<uint8_t> huge(sc::kMaxMessageBytes + 100, 0);
    uint8_t buf[sc::kMaxMessageBytes] = {};
    size_t n = sc::encode_witness(kTestUid, /*recv_count=*/0,
                                  sc::WitnessEncoding::RAW,
                                  huge.data(), huge.size(),
                                  buf, sizeof(buf));
    EXPECT_EQ(n, 0u);
}
