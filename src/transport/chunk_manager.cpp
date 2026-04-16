/*
 * chunk_manager.cpp — ChunkSet implementation
 */

#include "transport/chunk_manager.h"
#include "utils/logging.h"

#include <algorithm>

namespace semirdma {

ChunkSet::ChunkSet(size_t base_offset, size_t total_bytes, size_t chunk_bytes)
    : base_offset_(base_offset)
    , total_bytes_(total_bytes)
    , chunk_bytes_(chunk_bytes)
{
    if (chunk_bytes == 0 || total_bytes == 0) {
        SEMIRDMA_LOG_ERR("ChunkSet: invalid parameters (total=%zu, chunk=%zu)",
                         total_bytes, chunk_bytes);
        return;
    }

    size_t num_chunks = (total_bytes + chunk_bytes - 1) / chunk_bytes;
    chunks_.reserve(num_chunks);
    states_.resize(num_chunks);

    for (size_t i = 0; i < num_chunks; i++) {
        size_t offset = i * chunk_bytes;
        size_t len    = std::min(chunk_bytes, total_bytes - offset);

        ChunkDescriptor cd;
        cd.chunk_id      = static_cast<uint32_t>(i);
        cd.local_offset  = base_offset + offset;
        cd.remote_offset = base_offset + offset;
        cd.length        = len;
        chunks_.push_back(cd);
    }
}

bool ChunkSet::mark_completed(uint32_t chunk_id)
{
    if (chunk_id >= static_cast<uint32_t>(chunks_.size())) {
        SEMIRDMA_LOG_WARN("mark_completed: chunk_id %u out of range (size=%zu)",
                          chunk_id, chunks_.size());
        return false;
    }
    states_[chunk_id].has_cqe   = true;
    states_[chunk_id].valid_len = chunks_[chunk_id].length;
    return true;
}

void ChunkSet::reset_states()
{
    for (auto& s : states_) {
        s.has_cqe   = false;
        s.valid_len = 0;
    }
}

size_t ChunkSet::num_completed() const
{
    size_t count = 0;
    for (const auto& s : states_) {
        if (s.has_cqe) count++;
    }
    return count;
}

double ChunkSet::completion_ratio() const
{
    if (chunks_.empty()) return 0.0;
    return static_cast<double>(num_completed()) / static_cast<double>(chunks_.size());
}

} // namespace semirdma
