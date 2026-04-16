/*
 * ghost_mask.cpp — GhostMask implementation
 */

#include "transport/ghost_mask.h"

#include <cstring>

namespace semirdma {

void GhostMask::apply(uint8_t* buf, const ChunkSet& cs)
{
    for (size_t i = 0; i < cs.size(); i++) {
        if (!cs.state(i).has_cqe) {
            std::memset(buf + cs.chunk(i).local_offset, 0, cs.chunk(i).length);
        }
    }
}

} // namespace semirdma
