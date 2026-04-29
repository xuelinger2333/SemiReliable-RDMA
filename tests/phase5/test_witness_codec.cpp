// Phase 5 unit tests — CLEAR witness_codec + messages
//
// Pure-logic; no RDMA fixture. Runs as part of the standard ctest suite.

#include <cstdint>
#include <random>
#include <vector>

#include <gtest/gtest.h>

#include "transport/clear/messages.h"
#include "transport/clear/witness_codec.h"

namespace sc = semirdma::clear;

namespace {

std::vector<uint8_t> make_bitmap(uint32_t n_chunks,
                                 const std::vector<uint32_t>& present_idx) {
    std::vector<uint8_t> bm((n_chunks + 7u) >> 3, 0);
    for (uint32_t idx : present_idx) sc::bitmap_set(bm.data(), idx);
    return bm;
}

void roundtrip_and_compare(const std::vector<uint8_t>& bm, uint32_t n_chunks,
                           sc::WitnessEncoding expected_encoding) {
    auto enc = sc::encode_witness(bm.data(), bm.size(), n_chunks);
    EXPECT_EQ(enc.encoding, expected_encoding);

    std::vector<uint8_t> decoded;
    uint32_t decoded_count = 0;
    ASSERT_TRUE(sc::decode_witness(enc.encoding, enc.body.data(),
                                   enc.body.size(), n_chunks, decoded,
                                   decoded_count));
    EXPECT_EQ(decoded_count, enc.recv_count);

    // Compare bit-by-bit (input may have trailing bits; only first n_chunks
    // matter).
    for (uint32_t i = 0; i < n_chunks; ++i) {
        EXPECT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                  sc::bitmap_get(decoded.data(), decoded.size(), i))
            << "bit " << i << " mismatch";
    }
}

}  // namespace

// ---- Encoding selection -----------------------------------------------------

TEST(WitnessCodec, AllPresentIsFull) {
    constexpr uint32_t N = 256;
    std::vector<uint8_t> bm((N + 7u) >> 3, 0xFF);
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    EXPECT_EQ(enc.encoding, sc::WitnessEncoding::FULL_ALL_PRESENT);
    EXPECT_EQ(enc.recv_count, N);
    EXPECT_TRUE(enc.body.empty());
}

TEST(WitnessCodec, AllAbsentIsFull) {
    constexpr uint32_t N = 256;
    std::vector<uint8_t> bm((N + 7u) >> 3, 0);
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    EXPECT_EQ(enc.encoding, sc::WitnessEncoding::FULL_ALL_ABSENT);
    EXPECT_EQ(enc.recv_count, 0u);
    EXPECT_TRUE(enc.body.empty());
}

TEST(WitnessCodec, SparseMissingPicksRange) {
    // 1024 chunks with 3 missing → RANGE encoding (8 + 3*8 = 32 bytes)
    // is much smaller than RAW (128 bytes).
    constexpr uint32_t N = 1024;
    std::vector<uint8_t> bm((N + 7u) >> 3, 0xFF);
    // Drop bits 100, 500, 700.
    bm[100 >> 3] &= ~static_cast<uint8_t>(1u << (100 & 7));
    bm[500 >> 3] &= ~static_cast<uint8_t>(1u << (500 & 7));
    bm[700 >> 3] &= ~static_cast<uint8_t>(1u << (700 & 7));

    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    EXPECT_EQ(enc.encoding, sc::WitnessEncoding::RANGE_MISSING);
    EXPECT_EQ(enc.recv_count, N - 3);
    // 3 single-bit ranges → 8 + 3*8 = 32 bytes.
    EXPECT_EQ(enc.body.size(), 32u);
}

TEST(WitnessCodec, DenseMissingPicksRaw) {
    // 256 chunks with 128 missing → many runs → RAW (32 bytes) wins.
    constexpr uint32_t N = 256;
    std::vector<uint8_t> bm = make_bitmap(N, {});
    // Set even-indexed bits only.
    for (uint32_t i = 0; i < N; i += 2) sc::bitmap_set(bm.data(), i);
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    EXPECT_EQ(enc.encoding, sc::WitnessEncoding::RAW);
    EXPECT_EQ(enc.recv_count, N / 2u);
    EXPECT_EQ(enc.body.size(), 32u);
}

// ---- Roundtrip --------------------------------------------------------------

TEST(WitnessCodec, RoundtripSparse) {
    constexpr uint32_t N = 2048;
    auto bm = make_bitmap(N, {});
    for (uint32_t i = 0; i < N; ++i) sc::bitmap_set(bm.data(), i);
    bm[42 >> 3] &= ~static_cast<uint8_t>(1u << (42 & 7));
    bm[1234 >> 3] &= ~static_cast<uint8_t>(1u << (1234 & 7));
    roundtrip_and_compare(bm, N, sc::WitnessEncoding::RANGE_MISSING);
}

TEST(WitnessCodec, RoundtripDense) {
    constexpr uint32_t N = 200;
    std::mt19937 rng(0xC0FFEE);
    auto bm = make_bitmap(N, {});
    for (uint32_t i = 0; i < N; ++i) {
        if (rng() & 1u) sc::bitmap_set(bm.data(), i);
    }
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    // Could be RAW or RANGE depending on RNG; just verify roundtrip.
    std::vector<uint8_t> decoded;
    uint32_t decoded_count = 0;
    ASSERT_TRUE(sc::decode_witness(enc.encoding, enc.body.data(),
                                   enc.body.size(), N, decoded, decoded_count));
    EXPECT_EQ(decoded_count, enc.recv_count);
    for (uint32_t i = 0; i < N; ++i) {
        EXPECT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                  sc::bitmap_get(decoded.data(), decoded.size(), i));
    }
}

TEST(WitnessCodec, RoundtripNonByteAligned) {
    // n_chunks not a multiple of 8 — test trailing-bit canonicalization.
    constexpr uint32_t N = 11;
    auto bm = make_bitmap(N, {0, 1, 4, 9, 10});
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    std::vector<uint8_t> decoded;
    uint32_t cnt = 0;
    ASSERT_TRUE(sc::decode_witness(enc.encoding, enc.body.data(),
                                   enc.body.size(), N, decoded, cnt));
    EXPECT_EQ(cnt, 5u);
    for (uint32_t i = 0; i < N; ++i) {
        EXPECT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                  sc::bitmap_get(decoded.data(), decoded.size(), i));
    }
}

// ---- Stress -----------------------------------------------------------------

TEST(WitnessCodec, RoundtripFuzz) {
    std::mt19937 rng(0xDEADBEEFu);
    for (int trial = 0; trial < 200; ++trial) {
        uint32_t N = 1u + (rng() % 4096u);
        auto bm = make_bitmap(N, {});
        // Random density.
        double p = (rng() & 0xFFFF) / 65535.0;
        for (uint32_t i = 0; i < N; ++i) {
            if ((rng() & 0xFFFF) < p * 65536.0) sc::bitmap_set(bm.data(), i);
        }
        auto enc = sc::encode_witness(bm.data(), bm.size(), N);
        std::vector<uint8_t> decoded;
        uint32_t cnt = 0;
        ASSERT_TRUE(sc::decode_witness(enc.encoding, enc.body.data(),
                                       enc.body.size(), N, decoded, cnt))
            << "trial " << trial << " N=" << N;
        EXPECT_EQ(cnt, enc.recv_count);
        for (uint32_t i = 0; i < N; ++i) {
            ASSERT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                      sc::bitmap_get(decoded.data(), decoded.size(), i))
                << "trial " << trial << " bit " << i;
        }
    }
}

// ---- Decode rejects malformed input -----------------------------------------

TEST(WitnessCodec, RejectsWrongRawSize) {
    constexpr uint32_t N = 100;
    std::vector<uint8_t> bad(7);  // not ceil(100/8) = 13
    std::vector<uint8_t> out;
    uint32_t cnt = 0;
    EXPECT_FALSE(sc::decode_witness(sc::WitnessEncoding::RAW, bad.data(),
                                    bad.size(), N, out, cnt));
}

TEST(WitnessCodec, RejectsRawTrailingBitsSet) {
    constexpr uint32_t N = 11;  // last byte has 3 valid bits + 5 trailing
    std::vector<uint8_t> body((N + 7u) >> 3, 0);
    body.back() = 0xFF;  // sets bits past N → must be rejected
    std::vector<uint8_t> out;
    uint32_t cnt = 0;
    EXPECT_FALSE(sc::decode_witness(sc::WitnessEncoding::RAW, body.data(),
                                    body.size(), N, out, cnt));
}

TEST(WitnessCodec, RejectsRangeOutOfBounds) {
    constexpr uint32_t N = 100;
    std::vector<uint8_t> body(8 + 8, 0);
    uint16_t n = 1;
    std::memcpy(body.data(), &n, sizeof(n));
    sc::Range r{95, 10};  // 95 + 10 = 105 > 100
    std::memcpy(body.data() + 8, &r, sizeof(r));
    std::vector<uint8_t> out;
    uint32_t cnt = 0;
    EXPECT_FALSE(sc::decode_witness(sc::WitnessEncoding::RANGE_MISSING,
                                    body.data(), body.size(), N, out, cnt));
}

TEST(WitnessCodec, RejectsRangeOverlap) {
    constexpr uint32_t N = 100;
    std::vector<uint8_t> body(8 + 16, 0);
    uint16_t n = 2;
    std::memcpy(body.data(), &n, sizeof(n));
    sc::Range r1{10, 5};    // covers 10..14
    sc::Range r2{12, 3};    // overlaps
    std::memcpy(body.data() + 8, &r1, sizeof(r1));
    std::memcpy(body.data() + 8 + sizeof(r1), &r2, sizeof(r2));
    std::vector<uint8_t> out;
    uint32_t cnt = 0;
    EXPECT_FALSE(sc::decode_witness(sc::WitnessEncoding::RANGE_MISSING,
                                    body.data(), body.size(), N, out, cnt));
}

TEST(WitnessCodec, RejectsZeroLengthRange) {
    constexpr uint32_t N = 100;
    std::vector<uint8_t> body(8 + 8, 0);
    uint16_t n = 1;
    std::memcpy(body.data(), &n, sizeof(n));
    sc::Range r{10, 0};
    std::memcpy(body.data() + 8, &r, sizeof(r));
    std::vector<uint8_t> out;
    uint32_t cnt = 0;
    EXPECT_FALSE(sc::decode_witness(sc::WitnessEncoding::RANGE_MISSING,
                                    body.data(), body.size(), N, out, cnt));
}

// ---- messages.h header roundtrip -------------------------------------------

TEST(Messages, HeaderRoundtrip) {
    uint8_t buf[64] = {};
    size_t n = sc::encode_header(sc::MsgType::BEGIN, /*uid=*/0xDEADBEEFCAFEBABEull,
                                 /*payload_len=*/40, buf, sizeof(buf));
    ASSERT_EQ(n, sizeof(sc::MsgHeader));

    sc::MsgHeader h{};
    ASSERT_TRUE(sc::decode_header(buf, sizeof(buf), h));
    EXPECT_EQ(h.type, static_cast<uint8_t>(sc::MsgType::BEGIN));
    EXPECT_EQ(h.version, sc::kProtocolVersion);
    EXPECT_EQ(h.payload_len, 40u);
    EXPECT_EQ(h.uid, 0xDEADBEEFCAFEBABEull);
    EXPECT_EQ(h.reserved, 0u);
}

TEST(Messages, DecodeHeaderRejectsShortBuf) {
    uint8_t buf[8] = {};
    sc::MsgHeader h{};
    EXPECT_FALSE(sc::decode_header(buf, sizeof(buf), h));
}

TEST(Messages, DecodeHeaderRejectsTruncatedPayload) {
    uint8_t buf[sizeof(sc::MsgHeader)] = {};
    sc::encode_header(sc::MsgType::WITNESS, /*uid=*/0x1, /*payload_len=*/100,
                      buf, sizeof(buf));
    sc::MsgHeader h{};
    // Buf only contains the header; declared payload_len=100 must fail.
    EXPECT_FALSE(sc::decode_header(buf, sizeof(buf), h));
}

TEST(Messages, DecodeHeaderRejectsWrongVersion) {
    uint8_t buf[sizeof(sc::MsgHeader)] = {};
    sc::encode_header(sc::MsgType::BEGIN, /*uid=*/0x1, /*payload_len=*/0,
                      buf, sizeof(buf));
    buf[1] = 99;  // bump version
    sc::MsgHeader h{};
    EXPECT_FALSE(sc::decode_header(buf, sizeof(buf), h));
}

TEST(Messages, PayloadStructSizesAreFixed) {
    EXPECT_EQ(sizeof(sc::MsgHeader), 16u);
    EXPECT_EQ(sizeof(sc::BeginPayload), 40u);
    EXPECT_EQ(sizeof(sc::WitnessPayloadHead), 8u);
    EXPECT_EQ(sizeof(sc::RepairReqPayloadHead), 8u);
    EXPECT_EQ(sizeof(sc::FinalizePayloadHead), 8u);
    EXPECT_EQ(sizeof(sc::RetirePayload), 8u);
    EXPECT_EQ(sizeof(sc::BackpressurePayload), 8u);
    EXPECT_EQ(sizeof(sc::Range), 8u);
}
