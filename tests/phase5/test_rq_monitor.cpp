// Phase 5 unit tests — CLEAR rq_monitor

#include <cstdint>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "transport/clear/rq_monitor.h"

namespace sc = semirdma::clear;

TEST(RQMonitor, InitialCreditsFromConfig) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 10;
    cfg.low_watermark = 2;
    cfg.refill_target = 8;
    sc::RQMonitor m(cfg);
    m.register_peer(/*peer_edge=*/1);
    EXPECT_EQ(m.credits(1), 10);
    EXPECT_FALSE(m.is_low(1));
}

TEST(RQMonitor, ConsumeReducesCreditsAndFiresLowWatermark) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 5;
    cfg.low_watermark = 2;
    cfg.refill_target = 5;
    sc::RQMonitor m(cfg);

    int low_calls = 0;
    int refill_calls = 0;
    int last_refill_k = 0;
    m.on_low_watermark([&](uint16_t, int32_t) { ++low_calls; });
    m.on_replenish_request([&](uint16_t, int32_t k) {
        ++refill_calls;
        last_refill_k = k;
    });

    m.register_peer(7);
    m.record_consumed(7, 2);   // credits 5→3 ; not yet at watermark
    EXPECT_EQ(low_calls, 0);

    m.record_consumed(7, 1);   // 3→2 ; HITS watermark (<=)
    EXPECT_EQ(low_calls, 1);
    EXPECT_EQ(refill_calls, 1);
    EXPECT_EQ(last_refill_k, 3);  // refill_target=5 - credits=2
}

TEST(RQMonitor, RecordPostedAddsCredits) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 0;
    sc::RQMonitor m(cfg);
    m.register_peer(1);
    m.record_posted(1, 8);
    EXPECT_EQ(m.credits(1), 8);
    m.record_posted(1, 4);
    EXPECT_EQ(m.credits(1), 12);
}

TEST(RQMonitor, NoLowWatermarkAfterReplenish) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 5;
    cfg.low_watermark = 2;
    cfg.refill_target = 5;
    sc::RQMonitor m(cfg);

    int low_calls = 0;
    m.on_low_watermark([&](uint16_t, int32_t) { ++low_calls; });

    m.register_peer(1);
    m.record_consumed(1, 4);   // 5→1, fires low_watermark
    EXPECT_EQ(low_calls, 1);

    // Caller posts 4 fresh recvs in response.
    m.record_posted(1, 4);
    EXPECT_EQ(m.credits(1), 5);
    EXPECT_FALSE(m.is_low(1));

    // Consume one more; should NOT cross the watermark again.
    m.record_consumed(1, 1);
    EXPECT_EQ(m.credits(1), 4);
    EXPECT_EQ(low_calls, 1);  // unchanged
}

TEST(RQMonitor, LowWatermarkRefiresAfterReturningAboveAndBelow) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 6;
    cfg.low_watermark = 2;
    cfg.refill_target = 6;
    sc::RQMonitor m(cfg);

    int low_calls = 0;
    m.on_low_watermark([&](uint16_t, int32_t) { ++low_calls; });
    m.register_peer(1);

    m.record_consumed(1, 5);  // 6→1, fires
    EXPECT_EQ(low_calls, 1);
    m.record_posted(1, 10);    // 1→11
    m.record_consumed(1, 9);   // 11→2, fires again
    EXPECT_EQ(low_calls, 2);
}

TEST(RQMonitor, MultiplePeersTrackedIndependently) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 4;
    cfg.low_watermark = 1;
    cfg.refill_target = 4;
    sc::RQMonitor m(cfg);

    std::vector<std::pair<uint16_t, int32_t>> low_events;
    m.on_low_watermark([&](uint16_t p, int32_t c) {
        low_events.push_back({p, c});
    });

    m.register_peer(10);
    m.register_peer(20);

    m.record_consumed(10, 3);  // 4→1 fires
    EXPECT_EQ(m.credits(10), 1);
    EXPECT_EQ(m.credits(20), 4);
    ASSERT_EQ(low_events.size(), 1u);
    EXPECT_EQ(low_events[0].first, 10u);

    m.record_consumed(20, 4);  // 4→0 fires
    EXPECT_EQ(m.credits(20), 0);
    ASSERT_EQ(low_events.size(), 2u);
    EXPECT_EQ(low_events[1].first, 20u);
}

TEST(RQMonitor, RecordConsumedZeroOrNegativeIsNoOp) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 5;
    sc::RQMonitor m(cfg);
    int low_calls = 0;
    m.on_low_watermark([&](uint16_t, int32_t) { ++low_calls; });
    m.register_peer(1);
    m.record_consumed(1, 0);
    m.record_consumed(1, -5);
    EXPECT_EQ(m.credits(1), 5);
    EXPECT_EQ(low_calls, 0);
}

TEST(RQMonitor, StatsCounters) {
    sc::RQMonitorConfig cfg;
    cfg.initial_credits = 4;
    cfg.low_watermark = 1;
    cfg.refill_target = 4;
    sc::RQMonitor m(cfg);
    m.on_low_watermark([](uint16_t, int32_t) {});
    m.on_replenish_request([](uint16_t, int32_t) {});
    m.register_peer(1);

    m.record_consumed(1, 2);   // 4→2
    m.record_consumed(1, 1);   // 2→1, fires
    m.record_posted(1, 3);     // 1→4

    EXPECT_EQ(m.stats().total_consumed, 3);
    EXPECT_EQ(m.stats().total_posted, 3);
    EXPECT_EQ(m.stats().low_watermark_events, 1u);
    EXPECT_EQ(m.stats().replenish_events, 1u);
}
