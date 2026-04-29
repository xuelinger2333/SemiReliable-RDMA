// Phase 5 unit tests — CLEAR imm_data layout (slot:8 | chunk_idx:20 | gen:4)
//
// Pure-logic; no RDMA. Verifies bit packing, masking, lease-key construction.

#include <cstdint>
#include <random>
#include <unordered_set>

#include <gtest/gtest.h>

#include "transport/clear/imm_codec.h"

namespace sc = semirdma::clear;

TEST(ImmCodec, RoundtripSimple) {
    uint32_t imm = sc::encode_imm(/*slot=*/123, /*chunk=*/0xABCDE, /*gen=*/9);
    EXPECT_EQ(sc::imm_slot(imm),  123u);
    EXPECT_EQ(sc::imm_chunk(imm), 0xABCDEu);
    EXPECT_EQ(sc::imm_gen(imm),   9u);
}

TEST(ImmCodec, BoundaryValues) {
    // Max in each field
    uint32_t imm = sc::encode_imm(/*slot=*/0xFF, sc::kImmMaxChunkIdx, /*gen=*/0x0F);
    EXPECT_EQ(sc::imm_slot(imm),  0xFFu);
    EXPECT_EQ(sc::imm_chunk(imm), sc::kImmMaxChunkIdx);
    EXPECT_EQ(sc::imm_gen(imm),   0x0Fu);

    // All zero
    EXPECT_EQ(sc::encode_imm(0, 0, 0), 0u);
    EXPECT_EQ(sc::imm_slot(0), 0u);
    EXPECT_EQ(sc::imm_chunk(0), 0u);
    EXPECT_EQ(sc::imm_gen(0), 0u);
}

TEST(ImmCodec, ChunkIdxOverflowMaskedSilently) {
    // Bit 21 of chunk_idx must not leak into slot_id field.
    uint32_t imm = sc::encode_imm(/*slot=*/5, /*chunk=*/0x00200000u, /*gen=*/3);
    // 0x00200000 has bit 21 set → masked to zero in chunk; should not bump
    // slot to 6 either.
    EXPECT_EQ(sc::imm_slot(imm), 5u);
    EXPECT_EQ(sc::imm_chunk(imm), 0u);
    EXPECT_EQ(sc::imm_gen(imm), 3u);
}

TEST(ImmCodec, GenOverflowMaskedSilently) {
    uint32_t imm = sc::encode_imm(/*slot=*/5, /*chunk=*/100, /*gen=*/0x37);
    // gen 0x37 → low 4 bits = 7; high bits must not leak into chunk_idx.
    EXPECT_EQ(sc::imm_slot(imm), 5u);
    EXPECT_EQ(sc::imm_chunk(imm), 100u);
    EXPECT_EQ(sc::imm_gen(imm), 7u);
}

TEST(ImmCodec, FieldsDoNotOverlap) {
    // Set just one field at a time and verify the others read back zero.
    EXPECT_EQ(sc::imm_slot(sc::encode_imm(0xFF, 0, 0)),    0xFFu);
    EXPECT_EQ(sc::imm_chunk(sc::encode_imm(0xFF, 0, 0)),   0u);
    EXPECT_EQ(sc::imm_gen(sc::encode_imm(0xFF, 0, 0)),     0u);

    EXPECT_EQ(sc::imm_slot(sc::encode_imm(0, sc::kImmMaxChunkIdx, 0)), 0u);
    EXPECT_EQ(sc::imm_chunk(sc::encode_imm(0, sc::kImmMaxChunkIdx, 0)),
              sc::kImmMaxChunkIdx);
    EXPECT_EQ(sc::imm_gen(sc::encode_imm(0, sc::kImmMaxChunkIdx, 0)), 0u);

    EXPECT_EQ(sc::imm_slot(sc::encode_imm(0, 0, 0x0F)), 0u);
    EXPECT_EQ(sc::imm_chunk(sc::encode_imm(0, 0, 0x0F)), 0u);
    EXPECT_EQ(sc::imm_gen(sc::encode_imm(0, 0, 0x0F)), 0x0Fu);
}

TEST(ImmCodec, RoundtripFuzz) {
    std::mt19937 rng(0xFEEDFACE);
    for (int i = 0; i < 5000; ++i) {
        uint8_t  slot  = static_cast<uint8_t>(rng() & 0xFFu);
        uint32_t chunk = rng() & sc::kImmChunkMask;
        uint8_t  gen   = static_cast<uint8_t>(rng() & 0x0Fu);
        uint32_t imm = sc::encode_imm(slot, chunk, gen);
        ASSERT_EQ(sc::imm_slot(imm),  slot)  << "iter " << i;
        ASSERT_EQ(sc::imm_chunk(imm), chunk) << "iter " << i;
        ASSERT_EQ(sc::imm_gen(imm),   gen)   << "iter " << i;
    }
}

TEST(ImmCodec, LeaseKeyUnique) {
    // Every (slot, gen) pair must map to a unique uint16_t.
    std::unordered_set<uint16_t> seen;
    for (uint16_t s = 0; s < 256; ++s) {
        for (uint8_t g = 0; g < 16; ++g) {
            uint16_t k = sc::lease_key(static_cast<uint8_t>(s), g);
            ASSERT_TRUE(seen.insert(k).second)
                << "collision at slot=" << s << " gen=" << +g;
        }
    }
    EXPECT_EQ(seen.size(), 256u * 16u);
}

TEST(ImmCodec, LeaseKeyMasksGen) {
    // High bits of gen must be ignored.
    EXPECT_EQ(sc::lease_key(/*slot=*/5, /*gen=*/0x07),
              sc::lease_key(/*slot=*/5, /*gen=*/0xF7));
}

TEST(ImmCodec, ConstantsAreSane) {
    EXPECT_EQ(sc::kImmGenMask, 0x0Fu);
    EXPECT_EQ(sc::kImmChunkMask, 0xFFFFFu);
    EXPECT_EQ(sc::kImmSlotShift, 24u);
    EXPECT_EQ(sc::kImmChunkShift, 4u);
    EXPECT_EQ(sc::kImmMaxChunkIdx, 1048575u);
}
