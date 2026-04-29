/*
 * lease_table.h — CLEAR slot-lease state (sender + receiver halves)
 *
 * The slot lease is the indirection between fast-path imm_data and full
 * transfer identity. imm_data carries (slot_id:8, chunk_idx:20, gen:4) over
 * UC; the receiver maps (slot_id, gen) back to a uid via a lease previously
 * installed by an RC BEGIN message. See docs/phase5/clear-design.md §2 + §3.2.
 *
 * We expose two narrow classes rather than one fused class because the sender
 * and receiver halves have different ownership: the sender owns slot
 * allocation (acquire/release + quarantine), the receiver owns (slot, gen)
 * → uid resolution + the PREBEGIN_PENDING staging queue. A node that
 * communicates with multiple peers will instantiate one of each per peer pair.
 *
 * Generation field is 4 bits on the wire; we store it as uint8_t internally.
 * Quarantine policy: when a slot is retired, callers advance a logical tick
 * counter (per-step). The slot is not eligible for reuse until
 * `quarantine_ticks` ticks have passed *and* the new gen does not equal the
 * gen of the previous owner.
 *
 * Dependency-free (only <array>, <deque>, <unordered_map>, <vector>) so unit
 * tests run with no RDMA fixture.
 */

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <unordered_map>
#include <vector>

namespace semirdma::clear {

constexpr uint16_t kSlotCount      = 256;   // 8-bit slot_id
constexpr uint8_t  kGenMask        = 0x0F;  // 4-bit gen on the wire
constexpr uint8_t  kGenCount       = 16;
constexpr uint64_t kDefaultQuarantineTicks = 1;

struct PendingEntry {
    uint8_t  slot_id;
    uint8_t  gen;
    uint32_t chunk_idx;
    uint64_t enqueued_tick;  // for TTL cleanup
};

enum class LookupOutcome : uint8_t {
    HIT       = 0,  // (slot, gen) matches an active lease
    PRE_BEGIN = 1,  // slot empty — CQE arrived before BEGIN
    STALE     = 2,  // slot active but gen does not match (old packet)
};

struct LookupResult {
    LookupOutcome outcome;
    uint64_t      uid;  // valid iff outcome == HIT
};

struct SlotPressure {
    uint32_t in_use;        // slots currently holding an active lease
    uint32_t near_wrap;     // slots whose gen is at kGenCount - 1
    uint32_t total;         // == kSlotCount
};

// ---------- Sender side ------------------------------------------------------

class SenderLeaseTable {
public:
    struct AcquireResult {
        bool    ok;
        uint8_t slot_id;
        uint8_t gen;
    };

    explicit SenderLeaseTable(uint64_t quarantine_ticks = kDefaultQuarantineTicks);

    // Allocate a slot for `uid`. Returns ok=false when no slot is eligible
    // (all 256 in use, or all retired-but-quarantined).
    // The caller may pass `slot_pref` to bias selection (round-robin hint).
    AcquireResult acquire(uint64_t uid,
                          std::optional<uint8_t> slot_pref = std::nullopt);

    // Release the slot bound to `uid` (call on RETIRE rx). Returns true if
    // the uid was registered.
    bool release(uint64_t uid);

    // Advance the logical clock; controls quarantine eligibility.
    void tick(uint64_t delta = 1) { tick_ += delta; }
    uint64_t now() const { return tick_; }

    // Snapshot of slot occupancy.
    SlotPressure pressure() const;

    // Test/diag accessor: lookup the (slot_id, gen) currently bound to uid.
    std::optional<std::pair<uint8_t, uint8_t>> peek(uint64_t uid) const;

private:
    struct SlotState {
        bool     active        = false;
        uint8_t  gen           = 0;       // valid in [0, 15]
        uint64_t uid           = 0;
        uint64_t last_retire_tick = 0;
        bool     ever_used     = false;
    };

    std::array<SlotState, kSlotCount> slots_{};
    std::unordered_map<uint64_t, uint8_t> uid_to_slot_;
    uint64_t tick_              = 0;
    uint64_t quarantine_ticks_  = kDefaultQuarantineTicks;
    uint16_t next_hint_         = 0;
};

// ---------- Receiver side ----------------------------------------------------

class ReceiverLeaseTable {
public:
    explicit ReceiverLeaseTable(size_t pending_capacity = 4096);

    // Install a lease (call on BEGIN rx). Returns false if (slot_id, gen) is
    // already actively bound to a different uid (protocol violation).
    bool install(uint64_t uid, uint8_t slot_id, uint8_t gen);

    // Resolve an inbound UC CQE. Pure read; does not enqueue PREBEGIN.
    LookupResult lookup(uint8_t slot_id, uint8_t gen) const;

    // Retire a lease (call on RETIRE rx). Returns true if uid was installed.
    bool retire(uint64_t uid);

    // PREBEGIN_PENDING — for CQEs that arrived before their BEGIN.
    // enqueue_pending() never blocks; if the queue is at capacity, the oldest
    // entry is dropped (with `dropped_count_` incremented).
    void enqueue_pending(uint8_t slot_id, uint8_t gen, uint32_t chunk_idx);
    std::vector<PendingEntry> drain_pending_for(uint8_t slot_id, uint8_t gen);

    // Drop pending entries whose enqueued_tick is older than now() - max_age.
    // Returns the number of entries removed.
    size_t expire_pending(uint64_t max_age_ticks);

    void tick(uint64_t delta = 1) { tick_ += delta; }
    uint64_t now() const { return tick_; }

    size_t pending_size() const { return pending_.size(); }
    size_t pending_dropped() const { return dropped_count_; }

    SlotPressure pressure() const;

private:
    struct SlotState {
        bool     active = false;
        uint8_t  gen    = 0;
        uint64_t uid    = 0;
    };

    std::array<SlotState, kSlotCount> slots_{};
    std::unordered_map<uint64_t, uint8_t> uid_to_slot_;
    std::deque<PendingEntry> pending_;
    size_t pending_capacity_;
    size_t dropped_count_ = 0;
    uint64_t tick_        = 0;
};

}  // namespace semirdma::clear
