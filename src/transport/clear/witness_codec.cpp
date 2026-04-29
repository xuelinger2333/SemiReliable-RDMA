/*
 * witness_codec.cpp — see witness_codec.h
 */

#include "witness_codec.h"

#include <cstring>

namespace semirdma::clear {

namespace {

size_t bitmap_byte_count(uint32_t n_bits) {
    return (static_cast<size_t>(n_bits) + 7u) >> 3;
}

// Walk the bitmap and emit a list of (start, length) runs of *absent* chunks.
// Used both for size estimation and for actual RANGE_MISSING encoding.
std::vector<Range> compute_missing_ranges(const uint8_t* bitmap,
                                          size_t bitmap_bytes,
                                          uint32_t n_chunks) {
    std::vector<Range> ranges;
    uint32_t i = 0;
    while (i < n_chunks) {
        if (bitmap_get(bitmap, bitmap_bytes, i)) {
            ++i;
            continue;
        }
        uint32_t start = i;
        while (i < n_chunks && !bitmap_get(bitmap, bitmap_bytes, i)) {
            ++i;
        }
        ranges.push_back(Range{start, i - start});
    }
    return ranges;
}

}  // namespace

uint32_t bitmap_popcount(const uint8_t* bitmap, uint32_t n_bits) {
    uint32_t count = 0;
    uint32_t full_bytes = n_bits >> 3;
    for (uint32_t b = 0; b < full_bytes; ++b) {
        count += static_cast<uint32_t>(__builtin_popcount(bitmap[b]));
    }
    uint32_t tail_bits = n_bits & 7u;
    if (tail_bits) {
        uint8_t mask = static_cast<uint8_t>((1u << tail_bits) - 1u);
        count += static_cast<uint32_t>(__builtin_popcount(bitmap[full_bytes] & mask));
    }
    return count;
}

WitnessEncodeResult encode_witness(const uint8_t* bitmap,
                                   size_t bitmap_bytes,
                                   uint32_t n_chunks) {
    WitnessEncodeResult out;
    out.recv_count = bitmap_popcount(bitmap,
                                     static_cast<uint32_t>(
                                         std::min<size_t>(bitmap_bytes * 8u,
                                                          n_chunks)));

    if (out.recv_count == n_chunks) {
        out.encoding = WitnessEncoding::FULL_ALL_PRESENT;
        return out;
    }
    if (out.recv_count == 0) {
        out.encoding = WitnessEncoding::FULL_ALL_ABSENT;
        return out;
    }

    const size_t raw_bytes = bitmap_byte_count(n_chunks);
    auto ranges = compute_missing_ranges(bitmap, bitmap_bytes, n_chunks);

    // RANGE_MISSING wire size = sizeof(u16 n_ranges) padding + n_ranges * 8.
    // We carry n_ranges in RepairReqPayloadHead-style (u16 n_ranges + u16 pad
    // + u32 reserved = 8 bytes), so the encoded body size is 8 + n*8.
    const size_t range_bytes = 8u + ranges.size() * sizeof(Range);

    if (range_bytes < raw_bytes && range_bytes <= 0xFFFFu) {
        out.encoding = WitnessEncoding::RANGE_MISSING;
        out.body.resize(range_bytes);
        uint16_t n_ranges_u16 = static_cast<uint16_t>(ranges.size());
        std::memcpy(out.body.data(), &n_ranges_u16, sizeof(uint16_t));
        // bytes [2..8) are zero-padding (reserved fields).
        std::memset(out.body.data() + 2, 0, 6);
        if (!ranges.empty()) {
            std::memcpy(out.body.data() + 8, ranges.data(),
                        ranges.size() * sizeof(Range));
        }
        return out;
    }

    // Default: RAW.
    out.encoding = WitnessEncoding::RAW;
    out.body.assign(bitmap, bitmap + raw_bytes);
    // Zero any bits past n_chunks in the last byte to keep the wire form
    // canonical (helps test equality checks).
    uint32_t tail_bits = n_chunks & 7u;
    if (tail_bits && raw_bytes > 0) {
        uint8_t mask = static_cast<uint8_t>((1u << tail_bits) - 1u);
        out.body.back() &= mask;
    }
    return out;
}

bool decode_witness(WitnessEncoding encoding,
                    const uint8_t* body,
                    size_t body_len,
                    uint32_t n_chunks,
                    std::vector<uint8_t>& out_bitmap,
                    uint32_t& out_recv_count) {
    const size_t raw_bytes = bitmap_byte_count(n_chunks);
    out_bitmap.assign(raw_bytes, 0);

    switch (encoding) {
    case WitnessEncoding::FULL_ALL_PRESENT: {
        if (body_len != 0) return false;
        for (uint32_t i = 0; i < n_chunks; ++i) bitmap_set(out_bitmap.data(), i);
        out_recv_count = n_chunks;
        return true;
    }
    case WitnessEncoding::FULL_ALL_ABSENT: {
        if (body_len != 0) return false;
        out_recv_count = 0;
        return true;
    }
    case WitnessEncoding::RAW: {
        if (body_len != raw_bytes) return false;
        std::memcpy(out_bitmap.data(), body, raw_bytes);
        // Validate trailing bits past n_chunks are zero.
        uint32_t tail_bits = n_chunks & 7u;
        if (tail_bits && raw_bytes > 0) {
            uint8_t mask = static_cast<uint8_t>((1u << tail_bits) - 1u);
            if (out_bitmap.back() & ~mask) return false;
        }
        out_recv_count = bitmap_popcount(out_bitmap.data(), n_chunks);
        return true;
    }
    case WitnessEncoding::RANGE_MISSING: {
        if (body_len < 8u) return false;
        uint16_t n_ranges = 0;
        std::memcpy(&n_ranges, body, sizeof(uint16_t));
        const size_t expected = 8u + static_cast<size_t>(n_ranges) * sizeof(Range);
        if (body_len != expected) return false;

        // Start with all-present, then clear the missing ranges.
        for (uint32_t i = 0; i < n_chunks; ++i) bitmap_set(out_bitmap.data(), i);

        const Range* ranges = reinterpret_cast<const Range*>(body + 8);
        uint32_t prev_end = 0;
        for (uint16_t r = 0; r < n_ranges; ++r) {
            const Range& rg = ranges[r];
            // Validate: ranges must be sorted, non-overlapping, in-bounds.
            if (rg.length == 0) return false;
            if (rg.start < prev_end) return false;
            if (static_cast<uint64_t>(rg.start) + rg.length >
                static_cast<uint64_t>(n_chunks)) {
                return false;
            }
            for (uint32_t i = rg.start; i < rg.start + rg.length; ++i) {
                out_bitmap[i >> 3] &= static_cast<uint8_t>(~(1u << (i & 7)));
            }
            prev_end = rg.start + rg.length;
        }
        out_recv_count = bitmap_popcount(out_bitmap.data(), n_chunks);
        return true;
    }
    }
    return false;
}

}  // namespace semirdma::clear
