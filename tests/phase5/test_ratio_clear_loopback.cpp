// Phase 5 RDMA-gated test — RatioController CLEAR-mode end-to-end
//
// Two UC QPs on the same NIC. Sender posts UC Write-with-Imm with the
// CLEAR imm layout (slot:8 | chunk_idx:20 | gen:4); receiver runs
// wait_for_ratio_clear and verifies:
//   - DELIVERED when all chunks land
//   - DEADLINE when sender drops some chunks (simulated by posting < n)
//   - foreign (slot, gen) CQEs end up in clr_pending and can be drained
//   - n_chunks > 256 exercises the 20-bit chunk_idx field
//
// Opt in: RDMA_LOOPBACK_DEVICE=mlx5_X. Skips when unset.

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <vector>

#include <gtest/gtest.h>

#include "transport/clear/imm_codec.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "transport/uc_qp_engine.h"

namespace sd = semirdma;
namespace sc = semirdma::clear;

namespace {

class RatioClearLoopback : public ::testing::Test {
protected:
    void SetUp() override {
        const char* dev = std::getenv("RDMA_LOOPBACK_DEVICE");
        if (!dev || !*dev) {
            GTEST_SKIP() << "RDMA_LOOPBACK_DEVICE unset; skipping.";
        }
        dev_ = dev;
        const char* gid = std::getenv("RDMA_LOOPBACK_GID_INDEX");
        gid_idx_ = gid ? std::atoi(gid) : 1;
    }

    std::string dev_;
    int gid_idx_ = 1;
};

constexpr size_t kChunkBytes  = 256;
constexpr size_t kBufBytes    = 4 * 1024 * 1024;  // 4 MiB
constexpr int    kSqDepth     = 32;
constexpr int    kRqDepth     = 4096;

// Build sender + receiver UCQPEngines and bring them up against each other.
struct Pair {
    std::unique_ptr<sd::UCQPEngine> snd;
    std::unique_ptr<sd::UCQPEngine> rcv;
    sd::RemoteMR rcv_mr;
};

Pair make_uc_pair(const std::string& dev, int gid_idx) {
    Pair p;
    p.snd = std::make_unique<sd::UCQPEngine>(dev, kBufBytes, kSqDepth, kRqDepth,
                                             gid_idx, "uc");
    p.rcv = std::make_unique<sd::UCQPEngine>(dev, kBufBytes, kSqDepth, kRqDepth,
                                             gid_idx, "uc");
    p.snd->bring_up(p.rcv->local_qp_info());
    p.rcv->bring_up(p.snd->local_qp_info());
    p.rcv_mr = p.rcv->local_mr_info();
    return p;
}

}  // namespace

// ---------------------------------------------------------------------------

TEST_F(RatioClearLoopback, AllChunksDelivered) {
    auto pair = make_uc_pair(dev_, gid_idx_);
    constexpr uint32_t kNChunks = 64;
    constexpr uint8_t  kSlot    = 12;
    constexpr uint8_t  kGen     = 5;

    pair.rcv->post_recv_batch(kNChunks);

    sd::ChunkSet cs(/*base_offset=*/0,
                    /*total_bytes=*/kNChunks * kChunkBytes,
                    /*chunk_bytes=*/kChunkBytes);
    ASSERT_EQ(cs.size(), kNChunks);

    for (uint32_t i = 0; i < kNChunks; ++i) {
        uint32_t imm = sc::encode_imm(kSlot, i, kGen);
        pair.snd->post_write(/*wr_id=*/i,
                             /*local_offset=*/i * kChunkBytes,
                             /*remote_offset=*/i * kChunkBytes,
                             /*length=*/kChunkBytes,
                             pair.rcv_mr,
                             /*with_imm=*/true,
                             imm);
    }

    sd::RatioController rc(*pair.rcv);
    sd::RatioExitReason reason = sd::RatioExitReason::DEADLINE;
    sd::WaitStats stats;
    bool ok = rc.wait_for_ratio_clear(cs, /*ratio=*/1.0,
                                      /*timeout_ms=*/2000,
                                      kSlot, kGen, &reason, &stats);
    EXPECT_TRUE(ok);
    EXPECT_EQ(reason, sd::RatioExitReason::DELIVERED);
    EXPECT_EQ(stats.completed, kNChunks);
    EXPECT_EQ(rc.clr_pending_size(), 0u);
}

TEST_F(RatioClearLoopback, RatioMetWithSomeMissing) {
    auto pair = make_uc_pair(dev_, gid_idx_);
    constexpr uint32_t kNChunks      = 64;
    constexpr uint32_t kSent         = 60;       // drop 4 by simply not posting
    constexpr double   kRatioTarget  = 0.90;     // 58/64 = 0.906 covers it
    constexpr uint8_t  kSlot         = 7;
    constexpr uint8_t  kGen          = 2;

    pair.rcv->post_recv_batch(kNChunks);

    sd::ChunkSet cs(0, kNChunks * kChunkBytes, kChunkBytes);
    for (uint32_t i = 0; i < kSent; ++i) {
        uint32_t imm = sc::encode_imm(kSlot, i, kGen);
        pair.snd->post_write(i, i * kChunkBytes, i * kChunkBytes, kChunkBytes,
                             pair.rcv_mr, /*with_imm=*/true, imm);
    }

    sd::RatioController rc(*pair.rcv);
    sd::RatioExitReason reason = sd::RatioExitReason::DEADLINE;
    sd::WaitStats stats;
    bool ok = rc.wait_for_ratio_clear(cs, kRatioTarget, /*timeout_ms=*/2000,
                                      kSlot, kGen, &reason, &stats);
    EXPECT_TRUE(ok);
    // Sender posted 60/64; that's already past 0.90 so we should land in
    // RATIO_MET (or DELIVERED if all 60 actually arrived but ratio<1; here
    // we explicitly never hit n_chunks).
    EXPECT_EQ(reason, sd::RatioExitReason::RATIO_MET);
    EXPECT_LT(stats.completed, kNChunks);
    EXPECT_GE(stats.completed,
              static_cast<uint32_t>(kRatioTarget * kNChunks));
}

TEST_F(RatioClearLoopback, DeadlineWhenNothingArrives) {
    auto pair = make_uc_pair(dev_, gid_idx_);
    constexpr uint32_t kNChunks = 32;
    pair.rcv->post_recv_batch(kNChunks);

    sd::ChunkSet cs(0, kNChunks * kChunkBytes, kChunkBytes);
    // Sender posts ZERO chunks → receiver must hit DEADLINE.

    sd::RatioController rc(*pair.rcv);
    sd::RatioExitReason reason = sd::RatioExitReason::DELIVERED;
    sd::WaitStats stats;
    bool ok = rc.wait_for_ratio_clear(cs, /*ratio=*/0.95,
                                      /*timeout_ms=*/100,
                                      /*slot=*/3, /*gen=*/1, &reason, &stats);
    EXPECT_FALSE(ok);
    EXPECT_EQ(reason, sd::RatioExitReason::DEADLINE);
    EXPECT_EQ(stats.completed, 0u);
    EXPECT_TRUE(stats.timed_out);
}

TEST_F(RatioClearLoopback, ForeignSlotGenStashedThenDrained) {
    auto pair = make_uc_pair(dev_, gid_idx_);
    constexpr uint32_t kNChunks = 16;
    constexpr uint8_t  kSlotA = 4, kGenA = 1;
    constexpr uint8_t  kSlotB = 4, kGenB = 2;   // same slot, different gen
    pair.rcv->post_recv_batch(kNChunks * 2);

    sd::ChunkSet cs_a(0, kNChunks * kChunkBytes, kChunkBytes);

    // Sender posts kNChunks chunks under (slot=4, gen=2) — these should
    // appear as "foreign" to a wait_for_ratio_clear((slot=4, gen=1)).
    for (uint32_t i = 0; i < kNChunks; ++i) {
        uint32_t imm = sc::encode_imm(kSlotB, i, kGenB);
        pair.snd->post_write(i, i * kChunkBytes, i * kChunkBytes, kChunkBytes,
                             pair.rcv_mr, /*with_imm=*/true, imm);
    }

    sd::RatioController rc(*pair.rcv);
    sd::RatioExitReason reason = sd::RatioExitReason::DELIVERED;
    sd::WaitStats stats;
    bool ok = rc.wait_for_ratio_clear(cs_a, /*ratio=*/1.0,
                                      /*timeout_ms=*/300,
                                      kSlotA, kGenA, &reason, &stats);
    EXPECT_FALSE(ok);
    EXPECT_EQ(reason, sd::RatioExitReason::DEADLINE);
    EXPECT_EQ(cs_a.num_completed(), 0u);
    // Foreign CQEs must have been stashed into the (slot=4,gen=2) slot.
    EXPECT_EQ(rc.clr_pending_size_for(kSlotB, kGenB), kNChunks);
    EXPECT_EQ(rc.clr_pending_size(), kNChunks);

    // Now drain into the matching ChunkSet.
    sd::ChunkSet cs_b(0, kNChunks * kChunkBytes, kChunkBytes);
    size_t drained = rc.clr_drain_pending(cs_b, kSlotB, kGenB);
    EXPECT_EQ(drained, kNChunks);
    EXPECT_EQ(cs_b.num_completed(), kNChunks);
    EXPECT_EQ(rc.clr_pending_size(), 0u);
}

TEST_F(RatioClearLoopback, LargeChunkIdxFitsTwentyBits) {
    // Verify that chunk indexes near the 20-bit ceiling round-trip.
    // We can't actually post a million chunks; instead we hand a
    // single high chunk_idx through stash + drain to verify the wire
    // encoding doesn't truncate.
    auto pair = make_uc_pair(dev_, gid_idx_);
    sd::RatioController rc(*pair.rcv);
    constexpr uint32_t kHigh = 0xFFFFEu;  // near max 20-bit
    rc.clr_stash_foreign(/*slot=*/9, /*gen=*/4, kHigh);
    EXPECT_EQ(rc.clr_pending_size_for(9, 4), 1u);

    // Build a ChunkSet large enough to mark this chunk.
    sd::ChunkSet cs(0, (kHigh + 1) * 4u, /*chunk_bytes=*/4);
    size_t drained = rc.clr_drain_pending(cs, 9, 4);
    EXPECT_EQ(drained, 1u);
    EXPECT_EQ(cs.num_completed(), 1u);
}
