// Phase 5 unit tests — CLEAR lease_table (sender + receiver halves)
//
// Pure-logic; no RDMA fixture.

#include <cstdint>
#include <set>
#include <unordered_set>

#include <gtest/gtest.h>

#include "transport/clear/lease_table.h"

namespace sc = semirdma::clear;

// ============================================================================
// SenderLeaseTable
// ============================================================================

TEST(SenderLeaseTable, AcquireFirstSlotIsZero) {
    sc::SenderLeaseTable t;
    auto r = t.acquire(/*uid=*/100);
    ASSERT_TRUE(r.ok);
    EXPECT_EQ(r.slot_id, 0u);
    EXPECT_EQ(r.gen, 0u);  // never-used slot starts at gen 0

    auto p = t.pressure();
    EXPECT_EQ(p.in_use, 1u);
    EXPECT_EQ(p.total, sc::kSlotCount);
}

TEST(SenderLeaseTable, AcquireRejectsDuplicateUid) {
    sc::SenderLeaseTable t;
    ASSERT_TRUE(t.acquire(/*uid=*/42).ok);
    auto r = t.acquire(/*uid=*/42);
    EXPECT_FALSE(r.ok);
}

TEST(SenderLeaseTable, ReleaseClearsBinding) {
    sc::SenderLeaseTable t;
    auto r = t.acquire(/*uid=*/7);
    ASSERT_TRUE(r.ok);
    EXPECT_TRUE(t.release(7));
    EXPECT_FALSE(t.release(7));      // double-release is a no-op false
    EXPECT_FALSE(t.peek(7).has_value());
}

TEST(SenderLeaseTable, ReuseBumpsGen) {
    // Round-robin allocation otherwise picks the next free slot (which is
    // never-used → gen 0). To isolate the gen-bump-on-reuse behavior we
    // pin both acquires to the same slot via slot_pref.
    sc::SenderLeaseTable t(/*quarantine_ticks=*/0);
    auto r1 = t.acquire(/*uid=*/1, /*slot_pref=*/42);
    ASSERT_TRUE(r1.ok);
    EXPECT_EQ(r1.slot_id, 42u);
    EXPECT_EQ(r1.gen, 0u);
    EXPECT_TRUE(t.release(1));
    auto r2 = t.acquire(/*uid=*/2, /*slot_pref=*/42);
    ASSERT_TRUE(r2.ok);
    EXPECT_EQ(r2.slot_id, 42u);
    EXPECT_EQ(r2.gen, 1u);
}

TEST(SenderLeaseTable, QuarantineSkipsRecentSlot) {
    sc::SenderLeaseTable t(/*quarantine_ticks=*/2);
    auto r1 = t.acquire(/*uid=*/1);
    ASSERT_TRUE(r1.ok);
    EXPECT_EQ(r1.slot_id, 0u);
    EXPECT_TRUE(t.release(1));
    // tick has not advanced; slot 0 is quarantined → next acquire should pick slot 1
    auto r2 = t.acquire(/*uid=*/2);
    ASSERT_TRUE(r2.ok);
    EXPECT_EQ(r2.slot_id, 1u);
    EXPECT_EQ(r2.gen, 0u);  // never-used slot

    // Advance time past quarantine → slot 0 becomes eligible again
    t.tick(3);
    EXPECT_TRUE(t.release(2));
    auto r3 = t.acquire(/*uid=*/3);
    ASSERT_TRUE(r3.ok);
    // hint is now at slot 2 → next free non-quarantined ought to be slot 2
    // but slot 0 is also free + past quarantine. The first eligible slot from
    // the rolling hint is what matters; we just check the acquire succeeded.
    EXPECT_TRUE(r3.slot_id == 0u || r3.slot_id == 2u);
}

TEST(SenderLeaseTable, AcquireRespectsSlotPref) {
    sc::SenderLeaseTable t(/*quarantine_ticks=*/0);
    auto r = t.acquire(/*uid=*/5, /*slot_pref=*/200);
    ASSERT_TRUE(r.ok);
    EXPECT_EQ(r.slot_id, 200u);
}

TEST(SenderLeaseTable, FullTableRejectsNewUid) {
    sc::SenderLeaseTable t(/*quarantine_ticks=*/0);
    for (uint64_t uid = 0; uid < sc::kSlotCount; ++uid) {
        ASSERT_TRUE(t.acquire(uid).ok) << "uid=" << uid;
    }
    auto r = t.acquire(/*uid=*/999);
    EXPECT_FALSE(r.ok);
    EXPECT_EQ(t.pressure().in_use, sc::kSlotCount);
}

TEST(SenderLeaseTable, Allocates256DistinctSlotsBeforeReuse) {
    sc::SenderLeaseTable t(/*quarantine_ticks=*/0);
    std::set<uint8_t> seen;
    for (uint64_t uid = 0; uid < sc::kSlotCount; ++uid) {
        auto r = t.acquire(uid);
        ASSERT_TRUE(r.ok);
        seen.insert(r.slot_id);
    }
    EXPECT_EQ(seen.size(), sc::kSlotCount);
}

TEST(SenderLeaseTable, GenWrapsCleanly) {
    sc::SenderLeaseTable t(/*quarantine_ticks=*/0);
    // Force the same slot 17 times; gens should be 0, 1, 2, ..., 15, 0.
    std::vector<uint8_t> gens;
    for (uint64_t uid = 1; uid <= sc::kGenCount + 1; ++uid) {
        auto r = t.acquire(uid, /*slot_pref=*/42);
        ASSERT_TRUE(r.ok) << uid;
        EXPECT_EQ(r.slot_id, 42u);
        gens.push_back(r.gen);
        EXPECT_TRUE(t.release(uid));
    }
    for (size_t i = 0; i < gens.size(); ++i) {
        EXPECT_EQ(gens[i], static_cast<uint8_t>(i & sc::kGenMask)) << i;
    }
}

TEST(SenderLeaseTable, PeekReturnsBoundSlotAndGen) {
    sc::SenderLeaseTable t;
    auto r = t.acquire(/*uid=*/77);
    ASSERT_TRUE(r.ok);
    auto p = t.peek(77);
    ASSERT_TRUE(p.has_value());
    EXPECT_EQ(p->first, r.slot_id);
    EXPECT_EQ(p->second, r.gen);
}

// ============================================================================
// ReceiverLeaseTable
// ============================================================================

TEST(ReceiverLeaseTable, InstallAndLookup) {
    sc::ReceiverLeaseTable t;
    EXPECT_TRUE(t.install(/*uid=*/0xAA, /*slot=*/3, /*gen=*/5));
    auto r = t.lookup(/*slot=*/3, /*gen=*/5);
    EXPECT_EQ(r.outcome, sc::LookupOutcome::HIT);
    EXPECT_EQ(r.uid, 0xAAu);
}

TEST(ReceiverLeaseTable, LookupBeforeInstallIsPreBegin) {
    sc::ReceiverLeaseTable t;
    auto r = t.lookup(/*slot=*/3, /*gen=*/5);
    EXPECT_EQ(r.outcome, sc::LookupOutcome::PRE_BEGIN);
}

TEST(ReceiverLeaseTable, LookupWithWrongGenIsStale) {
    sc::ReceiverLeaseTable t;
    ASSERT_TRUE(t.install(/*uid=*/0xAB, /*slot=*/3, /*gen=*/5));
    auto r = t.lookup(/*slot=*/3, /*gen=*/4);
    EXPECT_EQ(r.outcome, sc::LookupOutcome::STALE);
}

TEST(ReceiverLeaseTable, RetireKeepsGenForStaleDetection) {
    sc::ReceiverLeaseTable t;
    ASSERT_TRUE(t.install(/*uid=*/0xCD, /*slot=*/9, /*gen=*/7));
    EXPECT_TRUE(t.retire(0xCD));
    // After retire, lookup for the retired (slot, gen) is PRE_BEGIN
    // (slot is inactive). The receiver must rely on enqueue_pending to stage
    // any post-RETIRE stragglers; STALE detection happens after a *new*
    // install with a different gen.
    auto r = t.lookup(/*slot=*/9, /*gen=*/7);
    EXPECT_EQ(r.outcome, sc::LookupOutcome::PRE_BEGIN);

    // Reinstall on the same slot with a fresh gen → old gen now reads STALE.
    ASSERT_TRUE(t.install(/*uid=*/0xCE, /*slot=*/9, /*gen=*/8));
    EXPECT_EQ(t.lookup(9, 7).outcome, sc::LookupOutcome::STALE);
    EXPECT_EQ(t.lookup(9, 8).outcome, sc::LookupOutcome::HIT);
}

TEST(ReceiverLeaseTable, InstallIdempotentForSamePair) {
    sc::ReceiverLeaseTable t;
    EXPECT_TRUE(t.install(/*uid=*/1, /*slot=*/0, /*gen=*/3));
    EXPECT_TRUE(t.install(/*uid=*/1, /*slot=*/0, /*gen=*/3));  // idempotent
}

TEST(ReceiverLeaseTable, InstallRejectsConflictingPair) {
    sc::ReceiverLeaseTable t;
    EXPECT_TRUE(t.install(/*uid=*/1, /*slot=*/0, /*gen=*/3));
    EXPECT_FALSE(t.install(/*uid=*/2, /*slot=*/0, /*gen=*/3));  // wrong uid
    EXPECT_FALSE(t.install(/*uid=*/1, /*slot=*/0, /*gen=*/4));  // wrong gen
}

TEST(ReceiverLeaseTable, GenIsMaskedToFourBits) {
    sc::ReceiverLeaseTable t;
    // Pass gen=0xF7 → low 4 bits = 7.
    ASSERT_TRUE(t.install(/*uid=*/1, /*slot=*/0, /*gen=*/0xF7));
    EXPECT_EQ(t.lookup(0, 7).outcome, sc::LookupOutcome::HIT);
    EXPECT_EQ(t.lookup(0, 0xA7).outcome, sc::LookupOutcome::HIT);  // also masked
    EXPECT_EQ(t.lookup(0, 6).outcome, sc::LookupOutcome::STALE);
}

// ============================================================================
// PREBEGIN_PENDING
// ============================================================================

TEST(ReceiverLeaseTable, EnqueueAndDrain) {
    sc::ReceiverLeaseTable t;
    t.enqueue_pending(/*slot=*/5, /*gen=*/2, /*chunk=*/100);
    t.enqueue_pending(/*slot=*/5, /*gen=*/2, /*chunk=*/101);
    t.enqueue_pending(/*slot=*/6, /*gen=*/2, /*chunk=*/200);
    EXPECT_EQ(t.pending_size(), 3u);

    auto drained = t.drain_pending_for(/*slot=*/5, /*gen=*/2);
    ASSERT_EQ(drained.size(), 2u);
    EXPECT_EQ(drained[0].chunk_idx, 100u);
    EXPECT_EQ(drained[1].chunk_idx, 101u);
    EXPECT_EQ(t.pending_size(), 1u);

    auto leftover = t.drain_pending_for(6, 2);
    ASSERT_EQ(leftover.size(), 1u);
    EXPECT_EQ(leftover[0].chunk_idx, 200u);
    EXPECT_EQ(t.pending_size(), 0u);
}

TEST(ReceiverLeaseTable, DrainIgnoresMismatchedGen) {
    sc::ReceiverLeaseTable t;
    t.enqueue_pending(/*slot=*/1, /*gen=*/3, /*chunk=*/0);
    auto d = t.drain_pending_for(/*slot=*/1, /*gen=*/4);
    EXPECT_TRUE(d.empty());
    EXPECT_EQ(t.pending_size(), 1u);
}

TEST(ReceiverLeaseTable, EnqueueOverflowDropsOldest) {
    sc::ReceiverLeaseTable t(/*pending_capacity=*/3);
    t.enqueue_pending(0, 0, 1);
    t.enqueue_pending(0, 0, 2);
    t.enqueue_pending(0, 0, 3);
    t.enqueue_pending(0, 0, 4);  // forces drop of chunk=1
    EXPECT_EQ(t.pending_size(), 3u);
    EXPECT_EQ(t.pending_dropped(), 1u);
    auto d = t.drain_pending_for(0, 0);
    ASSERT_EQ(d.size(), 3u);
    EXPECT_EQ(d[0].chunk_idx, 2u);
    EXPECT_EQ(d.back().chunk_idx, 4u);
}

TEST(ReceiverLeaseTable, ExpirePendingByAge) {
    sc::ReceiverLeaseTable t;
    t.enqueue_pending(/*slot=*/1, /*gen=*/2, /*chunk=*/10);
    t.tick(5);
    t.enqueue_pending(/*slot=*/1, /*gen=*/2, /*chunk=*/11);
    t.tick(5);
    // Now the first entry is age 10, the second age 5. Expire age > 6:
    size_t removed = t.expire_pending(/*max_age=*/6);
    EXPECT_EQ(removed, 1u);
    EXPECT_EQ(t.pending_size(), 1u);
    auto d = t.drain_pending_for(1, 2);
    ASSERT_EQ(d.size(), 1u);
    EXPECT_EQ(d[0].chunk_idx, 11u);
}

// ============================================================================
// Cross-half integration (one node owns one of each)
// ============================================================================

TEST(LeaseTable, SenderAcquireDrivesReceiverInstall) {
    sc::SenderLeaseTable snd(/*quarantine_ticks=*/0);
    sc::ReceiverLeaseTable rcv;

    // Simulate 1k buckets churning through the same node-pair
    constexpr int kIters = 1000;
    for (int i = 0; i < kIters; ++i) {
        uint64_t uid = 0x10000 + static_cast<uint64_t>(i);
        auto r = snd.acquire(uid);
        ASSERT_TRUE(r.ok) << "iter " << i;
        // Receiver installs lease as if BEGIN arrived.
        ASSERT_TRUE(rcv.install(uid, r.slot_id, r.gen));

        // Simulate a CQE for this bucket — must HIT.
        auto look = rcv.lookup(r.slot_id, r.gen);
        EXPECT_EQ(look.outcome, sc::LookupOutcome::HIT);
        EXPECT_EQ(look.uid, uid);

        // Retire on both sides.
        EXPECT_TRUE(rcv.retire(uid));
        EXPECT_TRUE(snd.release(uid));
    }
    EXPECT_EQ(snd.pressure().in_use, 0u);
    EXPECT_EQ(rcv.pressure().in_use, 0u);
}

TEST(LeaseTable, PreBeginRaceIsCorrectlyDrained) {
    // Sender acquires; a CQE leaks to the receiver before BEGIN.
    sc::SenderLeaseTable snd;
    sc::ReceiverLeaseTable rcv;
    auto r = snd.acquire(/*uid=*/0xBEEF);
    ASSERT_TRUE(r.ok);

    // Receiver sees CQE first → no lease yet, stage in pending.
    auto look = rcv.lookup(r.slot_id, r.gen);
    EXPECT_EQ(look.outcome, sc::LookupOutcome::PRE_BEGIN);
    rcv.enqueue_pending(r.slot_id, r.gen, /*chunk=*/42);
    rcv.enqueue_pending(r.slot_id, r.gen, /*chunk=*/43);

    // BEGIN finally arrives.
    ASSERT_TRUE(rcv.install(/*uid=*/0xBEEF, r.slot_id, r.gen));
    auto drained = rcv.drain_pending_for(r.slot_id, r.gen);
    EXPECT_EQ(drained.size(), 2u);

    // Subsequent CQEs go straight through.
    auto hit = rcv.lookup(r.slot_id, r.gen);
    EXPECT_EQ(hit.outcome, sc::LookupOutcome::HIT);
}

TEST(LeaseTable, SlotWrapStress) {
    // Cycle through slots ~20× to make sure neither side desyncs.
    // Total uids = 256 * 20 = 5120; deterministic.
    sc::SenderLeaseTable snd(/*quarantine_ticks=*/0);
    sc::ReceiverLeaseTable rcv;
    constexpr int kCycles = 20;
    std::unordered_set<uint64_t> live;
    for (int c = 0; c < kCycles; ++c) {
        for (int i = 0; i < sc::kSlotCount; ++i) {
            uint64_t uid = (static_cast<uint64_t>(c) << 32) | i;
            auto r = snd.acquire(uid);
            ASSERT_TRUE(r.ok) << c << "/" << i;
            ASSERT_TRUE(rcv.install(uid, r.slot_id, r.gen));
            live.insert(uid);
        }
        // Retire all of this cycle in random-ish order.
        for (uint64_t uid : live) {
            EXPECT_TRUE(rcv.retire(uid));
            EXPECT_TRUE(snd.release(uid));
        }
        live.clear();
        snd.tick();  // bump logical clock between cycles
        rcv.tick();
    }
}
