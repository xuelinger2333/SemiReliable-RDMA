// Phase 5 unit tests — CLEAR finalizer (decide_finalize + Finalizer)
//
// Pure logic. Drives the state machine via mocked send_*/apply_mask
// callbacks; verifies decision selection across all four policies, repair
// budget accounting, and FINALIZE/RETIRE emission.

#include <cstdint>
#include <cstring>
#include <vector>

#include <gtest/gtest.h>

#include "transport/clear/finalizer.h"
#include "transport/clear/messages.h"
#include "transport/clear/witness_codec.h"

namespace sc = semirdma::clear;

namespace {

std::vector<uint8_t> make_bitmap(uint32_t n_chunks,
                                 const std::vector<uint32_t>& present) {
    std::vector<uint8_t> bm((n_chunks + 7u) >> 3, 0);
    for (auto i : present) sc::bitmap_set(bm.data(), i);
    return bm;
}

std::vector<uint8_t> all_present(uint32_t n_chunks) {
    std::vector<uint8_t> bm((n_chunks + 7u) >> 3, 0);
    for (uint32_t i = 0; i < n_chunks; ++i) sc::bitmap_set(bm.data(), i);
    return bm;
}

}  // namespace

// ============================================================================
// decide_finalize — pure kernel
// ============================================================================

TEST(DecideFinalize, AllPresentIsDelivered) {
    constexpr uint32_t N = 64;
    auto bm = all_present(N);
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), /*chunk_bytes=*/4096,
                                 sc::Policy::REPAIR_FIRST,
                                 /*budget=*/1ull << 30, /*max_per_uid=*/0);
    EXPECT_EQ(r.decision, sc::FinalizeDecision::DELIVERED);
    EXPECT_EQ(r.missing_count, 0u);
    EXPECT_EQ(r.missing_bytes, 0u);
    EXPECT_EQ(r.budget_consumed_bytes, 0u);
    EXPECT_TRUE(r.repair_ranges.empty());
}

TEST(DecideFinalize, RepairFirstWithBudgetEmitsRanges) {
    constexpr uint32_t N = 64;
    constexpr uint32_t CB = 4096;
    auto bm = all_present(N);
    bm[2 >> 3] &= ~uint8_t(1 << (2 & 7));   // chunk 2 missing
    bm[40 >> 3] &= ~uint8_t(1 << (40 & 7));  // chunk 40 missing
    bm[41 >> 3] &= ~uint8_t(1 << (41 & 7));  // chunk 41 missing
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), CB,
                                 sc::Policy::REPAIR_FIRST,
                                 /*budget=*/1ull << 20, /*max_per_uid=*/0);
    EXPECT_EQ(r.decision, sc::FinalizeDecision::REPAIRED);  // pending
    EXPECT_EQ(r.missing_count, 3u);
    EXPECT_EQ(r.missing_bytes, 3ull * CB);
    EXPECT_EQ(r.budget_consumed_bytes, 3ull * CB);
    ASSERT_EQ(r.repair_ranges.size(), 2u);  // {2,1} and {40,2}
    EXPECT_EQ(r.repair_ranges[0].start, 2u);
    EXPECT_EQ(r.repair_ranges[0].length, 1u);
    EXPECT_EQ(r.repair_ranges[1].start, 40u);
    EXPECT_EQ(r.repair_ranges[1].length, 2u);
}

TEST(DecideFinalize, RepairFirstBudgetExhaustedFallsToMasked) {
    constexpr uint32_t N = 64;
    constexpr uint32_t CB = 4096;
    auto bm = make_bitmap(N, {});  // all missing
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), CB,
                                 sc::Policy::REPAIR_FIRST,
                                 /*budget=*/CB,  // only one chunk's worth
                                 /*max_per_uid=*/0);
    EXPECT_EQ(r.decision, sc::FinalizeDecision::MASKED);
    EXPECT_EQ(r.missing_count, N);
    EXPECT_EQ(r.budget_consumed_bytes, 0u);
    EXPECT_TRUE(r.repair_ranges.empty());
}

TEST(DecideFinalize, RepairFirstRespectsMaxPerUidCap) {
    constexpr uint32_t N = 64;
    constexpr uint32_t CB = 4096;
    auto bm = all_present(N);
    bm[10 >> 3] &= ~uint8_t(1 << (10 & 7));
    bm[20 >> 3] &= ~uint8_t(1 << (20 & 7));  // 2 missing → 2*4096 = 8 KiB
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), CB,
                                 sc::Policy::REPAIR_FIRST,
                                 /*budget=*/1ull << 30,
                                 /*max_per_uid=*/4096);  // 1-chunk cap
    // 8 KiB > 4 KiB cap → MASKED
    EXPECT_EQ(r.decision, sc::FinalizeDecision::MASKED);
}

TEST(DecideFinalize, MaskFirstAlwaysMasksOnMissing) {
    constexpr uint32_t N = 32;
    auto bm = make_bitmap(N, {0, 1, 2});  // 3 present, 29 missing
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), 4096,
                                 sc::Policy::MASK_FIRST,
                                 1ull << 30, 0);
    EXPECT_EQ(r.decision, sc::FinalizeDecision::MASKED);
    EXPECT_EQ(r.missing_count, 29u);
    EXPECT_EQ(r.budget_consumed_bytes, 0u);  // mask-first never spends budget
}

TEST(DecideFinalize, StaleFillReturnsStale) {
    constexpr uint32_t N = 32;
    auto bm = make_bitmap(N, {0, 1, 2});
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), 4096,
                                 sc::Policy::STALE_FILL,
                                 1ull << 30, 0);
    EXPECT_EQ(r.decision, sc::FinalizeDecision::STALE);
}

TEST(DecideFinalize, EstimatorScaleReturnsMasked) {
    constexpr uint32_t N = 32;
    auto bm = make_bitmap(N, {0, 1, 2});
    auto r = sc::decide_finalize(N, bm.data(), bm.size(), 4096,
                                 sc::Policy::ESTIMATOR_SCALE,
                                 1ull << 30, 0);
    EXPECT_EQ(r.decision, sc::FinalizeDecision::MASKED);
}

// ============================================================================
// Finalizer state machine
// ============================================================================

struct CallbackProbe {
    int                                   repair_calls = 0;
    int                                   finalize_calls = 0;
    int                                   retire_calls = 0;
    int                                   apply_mask_calls = 0;
    uint64_t                              last_repair_uid = 0;
    std::vector<sc::Range>                last_repair_ranges;
    uint64_t                              last_finalize_uid = 0;
    sc::FinalizeDecision                  last_finalize_decision =
        sc::FinalizeDecision::DELIVERED;
    sc::WitnessEncoding                   last_finalize_mask_encoding =
        sc::WitnessEncoding::FULL_ALL_PRESENT;
    std::vector<uint8_t>                  last_finalize_mask_body;
    uint64_t                              last_retire_uid = 0;
    std::vector<uint8_t>                  last_apply_mask;
};

void wire(sc::Finalizer& f, CallbackProbe& p) {
    f.on_send_repair_req([&](uint64_t uid, const sc::Range* ranges,
                             uint16_t n) {
        ++p.repair_calls;
        p.last_repair_uid = uid;
        p.last_repair_ranges.assign(ranges, ranges + n);
    });
    f.on_send_finalize([&](uint64_t uid, sc::FinalizeDecision d,
                           sc::WitnessEncoding enc, const uint8_t* body,
                           size_t len) {
        ++p.finalize_calls;
        p.last_finalize_uid = uid;
        p.last_finalize_decision = d;
        p.last_finalize_mask_encoding = enc;
        p.last_finalize_mask_body.assign(body, body + len);
    });
    f.on_send_retire([&](uint64_t uid, uint8_t, uint8_t) {
        ++p.retire_calls;
        p.last_retire_uid = uid;
    });
    f.on_apply_mask([&](uint64_t, sc::FinalizeDecision, const uint8_t* mask,
                        size_t len, uint32_t /*n_chunks*/) {
        ++p.apply_mask_calls;
        p.last_apply_mask.assign(mask, mask + len);
    });
}

TEST(Finalizer, TrackThenWitnessAllPresentEmitsDelivered) {
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);

    constexpr uint32_t N = 16;
    ASSERT_TRUE(f.track(/*uid=*/1, /*slot=*/3, /*gen=*/2,
                        N, /*chunk_bytes=*/4096, sc::Policy::REPAIR_FIRST));

    auto bm = all_present(N);
    EXPECT_EQ(f.on_witness(1, bm.data(), bm.size()),
              sc::FinalizeDecision::DELIVERED);
    EXPECT_EQ(p.repair_calls, 0);
    EXPECT_EQ(p.finalize_calls, 1);
    EXPECT_EQ(p.retire_calls, 1);
    EXPECT_EQ(p.apply_mask_calls, 1);
    EXPECT_EQ(p.last_finalize_decision, sc::FinalizeDecision::DELIVERED);
    EXPECT_FALSE(f.is_tracked(1));
    EXPECT_EQ(f.stats().n_finalized, 1u);
    EXPECT_EQ(f.stats().n_decisions[(int)sc::FinalizeDecision::DELIVERED], 1u);
}

TEST(Finalizer, RepairFirstFlowEmitsRepairThenFinalize) {
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);

    constexpr uint32_t N  = 16;
    constexpr uint32_t CB = 4096;
    ASSERT_TRUE(f.track(7, 1, 1, N, CB, sc::Policy::REPAIR_FIRST));

    auto bm = all_present(N);
    bm[5 >> 3] &= ~uint8_t(1 << (5 & 7));  // chunk 5 missing
    auto initial_budget = f.repair_budget_remaining_bytes();

    EXPECT_EQ(f.on_witness(7, bm.data(), bm.size()),
              sc::FinalizeDecision::REPAIRED);
    EXPECT_EQ(p.repair_calls, 1);
    EXPECT_EQ(p.finalize_calls, 0);          // pending — not yet final
    EXPECT_EQ(p.retire_calls, 0);
    ASSERT_EQ(p.last_repair_ranges.size(), 1u);
    EXPECT_EQ(p.last_repair_ranges[0].start, 5u);
    EXPECT_EQ(p.last_repair_ranges[0].length, 1u);
    EXPECT_EQ(initial_budget - f.repair_budget_remaining_bytes(), CB);
    EXPECT_TRUE(f.is_tracked(7));

    // Now repair completes.
    EXPECT_TRUE(f.on_repair_complete(7));
    EXPECT_EQ(p.finalize_calls, 1);
    EXPECT_EQ(p.retire_calls, 1);
    EXPECT_EQ(p.last_finalize_decision, sc::FinalizeDecision::REPAIRED);
    EXPECT_FALSE(f.is_tracked(7));
}

TEST(Finalizer, RepairBudgetUnderrunFallsToMaskedAndCounts) {
    sc::FinalizerConfig cfg;
    cfg.repair_budget_bytes_per_step = 0;  // no budget at all
    sc::Finalizer f(cfg);
    CallbackProbe p;
    wire(f, p);

    constexpr uint32_t N = 16;
    ASSERT_TRUE(f.track(11, 1, 1, N, 4096, sc::Policy::REPAIR_FIRST));
    auto bm = all_present(N);
    bm[3 >> 3] &= ~uint8_t(1 << (3 & 7));

    EXPECT_EQ(f.on_witness(11, bm.data(), bm.size()),
              sc::FinalizeDecision::MASKED);
    EXPECT_EQ(p.repair_calls, 0);
    EXPECT_EQ(p.finalize_calls, 1);
    EXPECT_EQ(p.last_finalize_decision, sc::FinalizeDecision::MASKED);
    EXPECT_EQ(f.stats().budget_underruns, 1u);
}

TEST(Finalizer, MaskFirstSkipsRepair) {
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);
    constexpr uint32_t N = 8;
    ASSERT_TRUE(f.track(2, 1, 1, N, 256, sc::Policy::MASK_FIRST));
    auto bm = make_bitmap(N, {0, 1});
    EXPECT_EQ(f.on_witness(2, bm.data(), bm.size()),
              sc::FinalizeDecision::MASKED);
    EXPECT_EQ(p.repair_calls, 0);
    EXPECT_EQ(p.finalize_calls, 1);
    EXPECT_EQ(p.retire_calls, 1);
    EXPECT_EQ(f.stats().budget_underruns, 0u);  // mask-first never spends
}

TEST(Finalizer, StaleFillEmitsStaleDecision) {
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);
    ASSERT_TRUE(f.track(3, 0, 0, 8, 256, sc::Policy::STALE_FILL));
    auto bm = make_bitmap(8, {0});
    EXPECT_EQ(f.on_witness(3, bm.data(), bm.size()),
              sc::FinalizeDecision::STALE);
    EXPECT_EQ(p.last_finalize_decision, sc::FinalizeDecision::STALE);
}

TEST(Finalizer, StepBoundaryRefillsBudget) {
    sc::FinalizerConfig cfg;
    cfg.repair_budget_bytes_per_step = 8192;
    sc::Finalizer f(cfg);
    EXPECT_EQ(f.repair_budget_remaining_bytes(), 8192u);

    // Spend it: repair 8 chunks @ 1024.
    CallbackProbe p;
    wire(f, p);
    ASSERT_TRUE(f.track(99, 1, 1, /*n_chunks=*/16, /*chunk_bytes=*/1024,
                        sc::Policy::REPAIR_FIRST));
    auto bm = all_present(16);
    for (uint32_t i = 0; i < 8; ++i) bm[i >> 3] &= ~uint8_t(1 << (i & 7));
    EXPECT_EQ(f.on_witness(99, bm.data(), bm.size()),
              sc::FinalizeDecision::REPAIRED);
    EXPECT_EQ(f.repair_budget_remaining_bytes(), 0u);
    f.on_step_boundary();
    EXPECT_EQ(f.repair_budget_remaining_bytes(), 8192u);
    EXPECT_EQ(f.stats().budget_refills, 1u);
}

TEST(Finalizer, DuplicateUidTrackRejected) {
    sc::Finalizer f;
    EXPECT_TRUE(f.track(42, 0, 0, 8, 256, sc::Policy::MASK_FIRST));
    EXPECT_FALSE(f.track(42, 0, 0, 8, 256, sc::Policy::MASK_FIRST));
}

TEST(Finalizer, OnRepairCompleteUntrackedReturnsFalse) {
    sc::Finalizer f;
    EXPECT_FALSE(f.on_repair_complete(/*uid=*/777));
}

TEST(Finalizer, RepairCompleteWithoutPendingRepairFails) {
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);
    ASSERT_TRUE(f.track(5, 0, 0, 8, 256, sc::Policy::MASK_FIRST));
    auto bm = all_present(8);
    f.on_witness(5, bm.data(), bm.size());  // terminates as DELIVERED
    EXPECT_FALSE(f.on_repair_complete(5));
}

TEST(Finalizer, FinalMaskBodyReadable) {
    // Mask body is whatever encoding witness_codec picks as smallest wire
    // size. Pick N=512 with 2 sparse missing → RANGE (8+16=24 B) clearly
    // beats RAW (64 B), so we can also assert encoding choice here.
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);

    constexpr uint32_t N = 512;
    ASSERT_TRUE(f.track(1, 0, 0, N, 4096, sc::Policy::MASK_FIRST));
    auto bm = all_present(N);
    bm[5 >> 3] &= ~uint8_t(1 << (5 & 7));
    bm[400 >> 3] &= ~uint8_t(1 << (400 & 7));
    f.on_witness(1, bm.data(), bm.size());

    EXPECT_EQ(p.last_finalize_decision, sc::FinalizeDecision::MASKED);
    EXPECT_EQ(p.last_finalize_mask_encoding, sc::WitnessEncoding::RANGE_MISSING);

    std::vector<uint8_t> reconstructed;
    uint32_t cnt = 0;
    ASSERT_TRUE(sc::decode_witness(p.last_finalize_mask_encoding,
                                   p.last_finalize_mask_body.data(),
                                   p.last_finalize_mask_body.size(),
                                   N, reconstructed, cnt));
    for (uint32_t i = 0; i < N; ++i) {
        EXPECT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                  sc::bitmap_get(reconstructed.data(), reconstructed.size(), i))
            << "bit " << i;
    }
}

TEST(Finalizer, FinalMaskBodyReadableSmallNUsesRaw) {
    // Companion to the test above: at small N, RAW beats RANGE because the
    // bitmap itself is tiny. We accept whichever encoding the codec picks
    // and only require that the body roundtrips.
    sc::Finalizer f;
    CallbackProbe p;
    wire(f, p);

    constexpr uint32_t N = 64;
    ASSERT_TRUE(f.track(2, 0, 0, N, 4096, sc::Policy::MASK_FIRST));
    auto bm = all_present(N);
    bm[5 >> 3] &= ~uint8_t(1 << (5 & 7));
    bm[40 >> 3] &= ~uint8_t(1 << (40 & 7));
    f.on_witness(2, bm.data(), bm.size());

    // For 2 missing in 64 chunks: RAW is 8 B, RANGE is 24 B → encoder
    // picks RAW.
    EXPECT_EQ(p.last_finalize_mask_encoding, sc::WitnessEncoding::RAW);

    std::vector<uint8_t> reconstructed;
    uint32_t cnt = 0;
    ASSERT_TRUE(sc::decode_witness(p.last_finalize_mask_encoding,
                                   p.last_finalize_mask_body.data(),
                                   p.last_finalize_mask_body.size(),
                                   N, reconstructed, cnt));
    for (uint32_t i = 0; i < N; ++i) {
        EXPECT_EQ(sc::bitmap_get(bm.data(), bm.size(), i),
                  sc::bitmap_get(reconstructed.data(), reconstructed.size(), i));
    }
}
