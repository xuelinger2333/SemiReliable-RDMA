/*
 * imm_codec.h — CLEAR `imm_data` wire layout (32 bits total)
 *
 *  31                  24 23                              4 3        0
 * +---------------------+--------------------------------+----------+
 * |   slot_id    (8)    |     chunk_idx       (20)       | gen (4)  |
 * +---------------------+--------------------------------+----------+
 *
 * Slot identifies a short lease (see lease_table.h); chunk_idx is the chunk
 * within the bucket (4 KiB chunk × 1 048 576 = 4 GiB max bucket); gen is the
 * 4-bit generation counter that defends against stale-packet alias when a
 * slot is recycled.
 *
 * Header-only; no dependencies beyond <cstdint>. Both the sender (when
 * issuing UC Write-with-Imm via UCQPEngine::post_write) and the receiver
 * (RatioController CLEAR-mode poll loop) include this file so the encoding
 * has a single source of truth.
 *
 * See docs/phase5/clear-design.md §2.1.
 */

#pragma once

#include <cstdint>

namespace semirdma::clear {

constexpr uint32_t kImmGenBits   = 4;
constexpr uint32_t kImmGenMask   = (1u << kImmGenBits) - 1u;       // 0x0F
constexpr uint32_t kImmChunkBits = 20;
constexpr uint32_t kImmChunkMask = (1u << kImmChunkBits) - 1u;     // 0x0F'FFFF
constexpr uint32_t kImmSlotShift = kImmGenBits + kImmChunkBits;    // 24
constexpr uint32_t kImmChunkShift = kImmGenBits;                   // 4

constexpr uint32_t kImmMaxChunkIdx = kImmChunkMask;                // 1 048 575

inline uint32_t encode_imm(uint8_t slot_id, uint32_t chunk_idx, uint8_t gen) {
    return (static_cast<uint32_t>(slot_id) << kImmSlotShift) |
           ((chunk_idx & kImmChunkMask) << kImmChunkShift) |
           (static_cast<uint32_t>(gen) & kImmGenMask);
}

inline uint8_t imm_slot(uint32_t imm) {
    return static_cast<uint8_t>((imm >> kImmSlotShift) & 0xFFu);
}

inline uint32_t imm_chunk(uint32_t imm) {
    return (imm >> kImmChunkShift) & kImmChunkMask;
}

inline uint8_t imm_gen(uint32_t imm) {
    return static_cast<uint8_t>(imm & kImmGenMask);
}

// Pack (slot, gen) into a 16-bit key for hashing in pending-CQE maps.
inline uint16_t lease_key(uint8_t slot_id, uint8_t gen) {
    return static_cast<uint16_t>(
        (static_cast<uint16_t>(slot_id) << 8) |
        static_cast<uint16_t>(gen & kImmGenMask));
}

}  // namespace semirdma::clear
