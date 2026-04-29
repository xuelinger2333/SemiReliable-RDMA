// Phase 5 RDMA-gated integration test — CLEAR ControlPlane self-loopback
//
// Brings up two ControlPlane instances on the same NIC, hands each the
// other's QPN/GID, then exercises every msg type round-trip. Verifies:
//   - send → recv decode round-trip is byte-faithful
//   - callbacks fire with correctly-typed parsed payloads
//   - send-slot ring recycles after completions
//   - stats counters are coherent
//
// Opt in via env: RDMA_LOOPBACK_DEVICE=mlx5_X (e.g. mlx5_1 on amd247 cluster).
// Optional: RDMA_LOOPBACK_GID_INDEX (default 1 for RoCEv2 IPv4-mapped).
//
// Skipped automatically when RDMA_LOOPBACK_DEVICE is unset, so this test
// is safe in any CI environment that may not have a usable RDMA device.

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "transport/clear/control_plane.h"
#include "transport/clear/messages.h"
#include "transport/clear/witness_codec.h"

namespace sc = semirdma::clear;

namespace {

constexpr int kPollAttempts = 200;     // 200 × 10 ms = 2 s max wait
constexpr int kPollTimeoutMs = 10;

// Drive both endpoints' poll loops until predicate p() returns true or we
// give up. Returns true on success.
template <typename Pred>
bool poll_until(sc::ControlPlane& a, sc::ControlPlane& b, Pred p) {
    for (int i = 0; i < kPollAttempts; ++i) {
        a.poll_once(/*max=*/64, kPollTimeoutMs);
        b.poll_once(/*max=*/64, kPollTimeoutMs);
        if (p()) return true;
    }
    return false;
}

const char* env_or(const char* key, const char* fallback) {
    const char* v = std::getenv(key);
    return (v && *v) ? v : fallback;
}

class ControlPlaneLoopback : public ::testing::Test {
protected:
    void SetUp() override {
        const char* dev = std::getenv("RDMA_LOOPBACK_DEVICE");
        if (!dev || !*dev) {
            GTEST_SKIP() << "RDMA_LOOPBACK_DEVICE unset; skipping RDMA-gated test.";
        }
        dev_ = dev;
        gid_idx_ = std::atoi(env_or("RDMA_LOOPBACK_GID_INDEX", "1"));
    }

    sc::ControlPlaneConfig make_cfg() const {
        sc::ControlPlaneConfig c;
        c.dev_name   = dev_;
        c.gid_index  = gid_idx_;
        c.recv_slots = 32;
        c.send_slots = 8;
        return c;
    }

    std::string dev_;
    int gid_idx_ = 1;
};

}  // namespace

// ---------------------------------------------------------------------------

TEST_F(ControlPlaneLoopback, BringUpAndExchangeAllMsgTypes) {
    sc::ControlPlane cp_a(make_cfg());
    sc::ControlPlane cp_b(make_cfg());

    auto a_info = cp_a.local_qp_info();
    auto b_info = cp_b.local_qp_info();
    cp_a.bring_up(b_info);
    cp_b.bring_up(a_info);

    // Track received-on-B by type.
    int seen_begin = 0, seen_witness = 0, seen_repair = 0;
    int seen_finalize = 0, seen_retire = 0, seen_backpressure = 0;
    sc::ParsedBegin    last_begin{};
    sc::ParsedWitness  last_witness{};
    sc::ParsedRepairReq last_repair{};
    sc::ParsedFinalize last_finalize{};
    std::vector<uint8_t> last_witness_body;
    std::vector<uint8_t> last_finalize_body;

    cp_b.on_begin([&](const sc::ParsedBegin& b) { last_begin = b; ++seen_begin; });
    cp_b.on_witness([&](const sc::ParsedWitness& w) {
        last_witness = w;
        if (w.body && w.body_len) {
            last_witness_body.assign(w.body, w.body + w.body_len);
            last_witness.body = last_witness_body.data();
        }
        ++seen_witness;
    });
    cp_b.on_repair_req([&](const sc::ParsedRepairReq& r) { last_repair = r; ++seen_repair; });
    cp_b.on_finalize([&](const sc::ParsedFinalize& f) {
        last_finalize = f;
        if (f.mask_body && f.mask_body_len) {
            last_finalize_body.assign(f.mask_body, f.mask_body + f.mask_body_len);
            last_finalize.mask_body = last_finalize_body.data();
        }
        ++seen_finalize;
    });
    cp_b.on_retire([&](const sc::ParsedRetire&) { ++seen_retire; });
    cp_b.on_backpressure([&](const sc::ParsedBackpressure&) { ++seen_backpressure; });

    // ---- BEGIN ------------------------------------------------------------
    sc::BeginPayload bp{};
    bp.slot_id   = 7;  bp.gen = 3;  bp.policy = 1;
    bp.peer_edge = 0xABCD;
    bp.step_seq  = 100;  bp.bucket_seq = 42;
    bp.n_chunks  = 1024; bp.deadline_us = 200000;
    bp.chunk_bytes = 4096; bp.checksum_seed = 0x12345678;
    ASSERT_TRUE(cp_a.send_begin(0xAA00, bp));

    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return seen_begin == 1; }))
        << "BEGIN did not arrive";
    EXPECT_EQ(last_begin.uid, 0xAA00u);
    EXPECT_EQ(last_begin.payload.bucket_seq, 42u);
    EXPECT_EQ(last_begin.payload.n_chunks, 1024u);

    // ---- WITNESS (RANGE_MISSING body via witness_codec) -------------------
    constexpr uint32_t N = 1024;
    std::vector<uint8_t> bm((N + 7u) >> 3, 0xFF);
    bm[10] = 0;  // 8 missing chunks at offset 80
    auto enc = sc::encode_witness(bm.data(), bm.size(), N);
    ASSERT_EQ(enc.encoding, sc::WitnessEncoding::RANGE_MISSING);
    ASSERT_TRUE(cp_a.send_witness(0xAA01, enc.recv_count, enc.encoding,
                                  enc.body.data(), enc.body.size()));

    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return seen_witness == 1; }))
        << "WITNESS did not arrive";
    EXPECT_EQ(last_witness.uid, 0xAA01u);
    EXPECT_EQ(last_witness.recv_count, enc.recv_count);
    EXPECT_EQ(last_witness.encoding, sc::WitnessEncoding::RANGE_MISSING);
    EXPECT_EQ(last_witness.body_len, enc.body.size());
    ASSERT_EQ(last_witness_body.size(), enc.body.size());
    EXPECT_EQ(0, std::memcmp(last_witness_body.data(), enc.body.data(),
                             enc.body.size()));

    // ---- REPAIR_REQ -------------------------------------------------------
    sc::Range ranges[2] = {{10, 5}, {500, 20}};
    ASSERT_TRUE(cp_a.send_repair_req(0xAA02, ranges, 2));
    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return seen_repair == 1; }));
    EXPECT_EQ(last_repair.n_ranges, 2u);
    ASSERT_NE(last_repair.ranges, nullptr);
    EXPECT_EQ(last_repair.ranges[0].start, 10u);
    EXPECT_EQ(last_repair.ranges[1].length, 20u);

    // ---- FINALIZE ---------------------------------------------------------
    auto fin_enc = sc::encode_witness(bm.data(), bm.size(), N);
    ASSERT_TRUE(cp_a.send_finalize(0xAA03, sc::FinalizeDecision::MASKED,
                                   fin_enc.encoding, fin_enc.body.data(),
                                   fin_enc.body.size()));
    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return seen_finalize == 1; }));
    EXPECT_EQ(last_finalize.uid, 0xAA03u);
    EXPECT_EQ(last_finalize.decision, sc::FinalizeDecision::MASKED);
    EXPECT_EQ(last_finalize.mask_body_len, fin_enc.body.size());

    // ---- RETIRE -----------------------------------------------------------
    sc::RetirePayload rp{};
    rp.slot_id = 7; rp.gen = 3;
    ASSERT_TRUE(cp_a.send_retire(0xAA04, rp));
    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return seen_retire == 1; }));

    // ---- BACKPRESSURE -----------------------------------------------------
    sc::BackpressurePayload bpp{};
    bpp.peer_edge = 0xABCD; bpp.requested_credits = 32;
    ASSERT_TRUE(cp_a.send_backpressure(0xAA05, bpp));
    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return seen_backpressure == 1; }));

    // ---- Final stats sanity ----------------------------------------------
    const auto& sa = cp_a.stats();
    const auto& sb = cp_b.stats();
    EXPECT_EQ(sa.sent_total, 6u);
    EXPECT_EQ(sb.recv_total, 6u);
    EXPECT_EQ(sb.recv_decode_errors, 0u);
    EXPECT_EQ(sa.send_completion_errors, 0u);

    // Each msg type was sent once on A and received once on B.
    EXPECT_EQ(sa.sent_by_type[(int)sc::MsgType::BEGIN], 1u);
    EXPECT_EQ(sb.recv_by_type[(int)sc::MsgType::BEGIN], 1u);
    EXPECT_EQ(sb.recv_by_type[(int)sc::MsgType::WITNESS], 1u);
    EXPECT_EQ(sb.recv_by_type[(int)sc::MsgType::REPAIR_REQ], 1u);
    EXPECT_EQ(sb.recv_by_type[(int)sc::MsgType::FINALIZE], 1u);
    EXPECT_EQ(sb.recv_by_type[(int)sc::MsgType::RETIRE], 1u);
    EXPECT_EQ(sb.recv_by_type[(int)sc::MsgType::BACKPRESSURE], 1u);
}

// Stress: send more messages than the send-slot ring can hold concurrently.
// This forces the ring to recycle on completions; at the end every msg must
// have been delivered.
TEST_F(ControlPlaneLoopback, SendRingRecyclesUnderPressure) {
    sc::ControlPlane cp_a(make_cfg());
    sc::ControlPlane cp_b(make_cfg());
    cp_a.bring_up(cp_b.local_qp_info());
    cp_b.bring_up(cp_a.local_qp_info());

    int got = 0;
    cp_b.on_retire([&](const sc::ParsedRetire&) { ++got; });

    constexpr int kN = 200;
    sc::RetirePayload rp{};
    int sent = 0;
    for (int i = 0; i < kN; ++i) {
        rp.slot_id = static_cast<uint8_t>(i);
        rp.gen     = static_cast<uint8_t>(i & 0xF);
        // Spin briefly if the ring is full.
        for (int retry = 0; retry < 100; ++retry) {
            if (cp_a.send_retire(static_cast<uint64_t>(i), rp)) {
                ++sent;
                break;
            }
            cp_a.poll_once(/*max=*/16, /*timeout_ms=*/1);
            cp_b.poll_once(/*max=*/16, /*timeout_ms=*/0);
        }
    }
    ASSERT_EQ(sent, kN);
    ASSERT_TRUE(poll_until(cp_a, cp_b, [&]{ return got == kN; }))
        << "only " << got << " / " << kN << " RETIRE messages arrived";
}
