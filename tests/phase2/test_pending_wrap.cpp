/*
 * test_pending_wrap.cpp — P2-① regression: 8-bit bucket_id wrap aliasing
 *
 * Background
 * ----------
 * RatioController::pending_cqes_ is keyed by uint8_t bucket_id. When the
 * caller cycles through more than 256 logical buckets, bucket_id values
 * are reused on the wire. A CQE stashed under bucket_id=K during cycle N
 * must NOT be claimed by a wait_for_ratio(K) belonging to cycle N+1.
 *
 * The protection: each pending entry carries a deposit_seq tag. drain_pending
 * evicts entries whose age (wait_seq_ - deposit_seq) has reached
 * kPendingMaxAgeWaits (256). The ``>=`` boundary is the one verified here:
 * an entry deposited at seq T must be evicted at the wait that brings
 * wait_seq_ to T+256, not at T+257.
 *
 * Uses ``advance_wait_seq_for_tests`` to skip the engine — pure logic.
 */

#include <gtest/gtest.h>

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"

#include <cstdlib>

namespace {

inline const char* const DEV = [](){
    const char* d = std::getenv("SEMIRDMA_DEV");
    return (d && *d) ? d : "rxe0";
}();

constexpr size_t BUF_SIZE    = 1 * 1024 * 1024;
constexpr size_t BUCKET_LEN  = 16 * 1024;
constexpr size_t CHUNK_BYTES = 4 * 1024;

}  // namespace

TEST(PendingWrap, EntryEvictedExactlyAt256Waits)
{
    using namespace semirdma;

    UCQPEngine engine(DEV, BUF_SIZE, /*sq_depth=*/4, /*rq_depth=*/4);
    RatioController rc(engine);

    // Stash a foreign CQE under bucket_id=42 at deposit_seq=0.
    rc.stash_foreign(/*bucket_id=*/42, /*chunk_id=*/3);
    EXPECT_EQ(rc.pending_size_for(42), 1u);

    // Advance wait_seq_ to 255 (one wait short of full cycle). The entry
    // must still be claimable.
    rc.advance_wait_seq_for_tests(255);
    {
        ChunkSet cs(0, BUCKET_LEN, CHUNK_BYTES);
        size_t drained = rc.drain_pending(cs, 42);
        EXPECT_EQ(drained, 1u);
        EXPECT_EQ(cs.num_completed(), 1u);
    }

    // Restash and advance to exactly 256. The entry is now at the alias
    // boundary and MUST be evicted (otherwise the next wait_for_ratio(42)
    // for a fresh ChunkSet would inherit a stale chunk_id).
    rc.stash_foreign(/*bucket_id=*/42, /*chunk_id=*/3);
    rc.advance_wait_seq_for_tests(256);
    {
        ChunkSet cs(0, BUCKET_LEN, CHUNK_BYTES);
        size_t drained = rc.drain_pending(cs, 42);
        EXPECT_EQ(drained, 0u);
        EXPECT_EQ(cs.num_completed(), 0u);
    }
}

TEST(PendingWrap, EntryEvictedWhenWellPastBoundary)
{
    using namespace semirdma;

    UCQPEngine engine(DEV, BUF_SIZE, 4, 4);
    RatioController rc(engine);

    rc.stash_foreign(/*bucket_id=*/7, /*chunk_id=*/1);
    rc.advance_wait_seq_for_tests(1000);  // many cycles past

    ChunkSet cs(0, BUCKET_LEN, CHUNK_BYTES);
    EXPECT_EQ(rc.drain_pending(cs, 7), 0u);
    EXPECT_EQ(cs.num_completed(), 0u);
}
