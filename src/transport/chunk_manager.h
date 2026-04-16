/*
 * chunk_manager.h — Buffer chunking and per-chunk completion tracking
 *
 * Given a contiguous buffer region and a chunk size, generates N independent
 * ChunkDescriptors (each maps to one RDMA Write WR).  Tracks per-chunk
 * completion state for RatioController and GhostMask.
 *
 * Key design decision: chunk_id == array index → O(1) lookup via imm_data.
 */

#pragma once

#include <cstdint>
#include <cstddef>
#include <vector>

namespace semirdma {

struct ChunkDescriptor {
    uint32_t chunk_id;       // Used as wr_id and imm_data (== array index)
    size_t   local_offset;   // Byte offset within local MR buffer
    size_t   remote_offset;  // Byte offset within remote MR buffer
    size_t   length;         // Chunk byte count (last chunk may be shorter)
};

struct ChunkState {
    bool   has_cqe   = false;  // Updated by RatioController on CQE arrival
    size_t valid_len = 0;      // For GhostMask: == length when CQE received, 0 otherwise
};

class ChunkSet {
public:
    // Divide [base_offset, base_offset + total_bytes) into chunks of chunk_bytes.
    // The last chunk may be shorter than chunk_bytes.
    ChunkSet(size_t base_offset, size_t total_bytes, size_t chunk_bytes);

    size_t size() const { return chunks_.size(); }

    const ChunkDescriptor& chunk(size_t i) const { return chunks_[i]; }
    ChunkState&            state(size_t i)       { return states_[i]; }
    const ChunkState&      state(size_t i) const { return states_[i]; }

    // Mark chunk as successfully received.  chunk_id is the imm_data from CQE.
    // Returns false if chunk_id is out of range (defensive bounds check).
    bool mark_completed(uint32_t chunk_id);

    // Reset all chunk states to initial (for multi-round reuse).
    void reset_states();

    size_t num_completed() const;
    double completion_ratio() const;

    size_t chunk_bytes()  const { return chunk_bytes_; }
    size_t total_bytes()  const { return total_bytes_; }
    size_t base_offset()  const { return base_offset_; }

private:
    size_t base_offset_;
    size_t total_bytes_;
    size_t chunk_bytes_;
    std::vector<ChunkDescriptor> chunks_;
    std::vector<ChunkState>      states_;
};

} // namespace semirdma
