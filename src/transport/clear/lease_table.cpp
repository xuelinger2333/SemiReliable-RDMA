/*
 * lease_table.cpp — see lease_table.h
 */

#include "lease_table.h"

namespace semirdma::clear {

namespace {

// Returns true if the slot is OK to (re)acquire under quarantine policy.
// Caller has already verified `!active`. We require either:
//   - slot was never used, OR
//   - enough ticks have elapsed since last retire AND the bumped gen does
//     not collide with a value an in-flight stale UC packet might still
//     carry. The simplest defense is to forbid recycling the same gen value
//     within `quarantine_ticks`; bumping (gen + 1) mod 16 is sufficient as
//     long as we aren't simultaneously wrapping.
bool slot_eligible(bool ever_used, uint64_t now, uint64_t last_retire,
                   uint64_t quarantine_ticks) {
    if (!ever_used) return true;
    return (now - last_retire) >= quarantine_ticks;
}

}  // namespace

// ---------- SenderLeaseTable -------------------------------------------------

SenderLeaseTable::SenderLeaseTable(uint64_t quarantine_ticks)
    : quarantine_ticks_(quarantine_ticks) {}

SenderLeaseTable::AcquireResult
SenderLeaseTable::acquire(uint64_t uid, std::optional<uint8_t> slot_pref) {
    // Reject duplicate uids; caller bug.
    if (uid_to_slot_.find(uid) != uid_to_slot_.end()) {
        return {false, 0, 0};
    }

    // Build candidate order: pref (if given) then linear scan from hint.
    uint16_t start = slot_pref.has_value() ? static_cast<uint16_t>(*slot_pref)
                                           : next_hint_;

    // Two passes: first only non-quarantined slots; second pass relaxes
    // quarantine if the first pass yielded nothing (graceful degradation
    // rather than failing the bucket — but we still report it via pressure).
    for (int relax = 0; relax < 2; ++relax) {
        for (uint16_t step = 0; step < kSlotCount; ++step) {
            uint16_t i = static_cast<uint16_t>((start + step) % kSlotCount);
            SlotState& s = slots_[i];
            if (s.active) continue;
            const bool ok = slot_eligible(s.ever_used, tick_, s.last_retire_tick,
                                          relax == 0 ? quarantine_ticks_ : 0);
            if (!ok) continue;

            const uint8_t new_gen = s.ever_used
                ? static_cast<uint8_t>((s.gen + 1u) & kGenMask)
                : 0u;

            s.active        = true;
            s.gen           = new_gen;
            s.uid           = uid;
            s.ever_used     = true;
            uid_to_slot_[uid] = static_cast<uint8_t>(i);
            next_hint_      = static_cast<uint16_t>((i + 1u) % kSlotCount);
            return {true, static_cast<uint8_t>(i), new_gen};
        }
    }
    return {false, 0, 0};
}

bool SenderLeaseTable::release(uint64_t uid) {
    auto it = uid_to_slot_.find(uid);
    if (it == uid_to_slot_.end()) return false;
    uint8_t slot = it->second;
    SlotState& s = slots_[slot];
    s.active           = false;
    s.last_retire_tick = tick_;
    // Note: we do NOT reset s.gen; the next acquire will bump from this value.
    uid_to_slot_.erase(it);
    return true;
}

SlotPressure SenderLeaseTable::pressure() const {
    SlotPressure p{0, 0, kSlotCount};
    for (const SlotState& s : slots_) {
        if (s.active) ++p.in_use;
        if (s.gen == kGenCount - 1) ++p.near_wrap;
    }
    return p;
}

std::optional<std::pair<uint8_t, uint8_t>>
SenderLeaseTable::peek(uint64_t uid) const {
    auto it = uid_to_slot_.find(uid);
    if (it == uid_to_slot_.end()) return std::nullopt;
    uint8_t slot = it->second;
    return std::make_pair(slot, slots_[slot].gen);
}

// ---------- ReceiverLeaseTable -----------------------------------------------

ReceiverLeaseTable::ReceiverLeaseTable(size_t pending_capacity)
    : pending_capacity_(pending_capacity) {}

bool ReceiverLeaseTable::install(uint64_t uid, uint8_t slot_id, uint8_t gen) {
    if (slot_id >= kSlotCount) return false;
    SlotState& s = slots_[slot_id];
    if (s.active) {
        // Two acceptable cases that we still allow:
        //   - install for the same (uid, gen) is idempotent.
        // Anything else is a protocol violation.
        if (s.uid == uid && s.gen == (gen & kGenMask)) return true;
        return false;
    }
    s.active = true;
    s.gen    = static_cast<uint8_t>(gen & kGenMask);
    s.uid    = uid;
    uid_to_slot_[uid] = slot_id;
    return true;
}

LookupResult ReceiverLeaseTable::lookup(uint8_t slot_id, uint8_t gen) const {
    if (slot_id >= kSlotCount) {
        return {LookupOutcome::PRE_BEGIN, 0};
    }
    const SlotState& s = slots_[slot_id];
    if (!s.active) {
        return {LookupOutcome::PRE_BEGIN, 0};
    }
    if (s.gen != (gen & kGenMask)) {
        return {LookupOutcome::STALE, 0};
    }
    return {LookupOutcome::HIT, s.uid};
}

bool ReceiverLeaseTable::retire(uint64_t uid) {
    auto it = uid_to_slot_.find(uid);
    if (it == uid_to_slot_.end()) return false;
    uint8_t slot = it->second;
    SlotState& s = slots_[slot];
    s.active = false;
    // gen is preserved so that a delayed CQE from this lease still resolves
    // to STALE (not a fresh PRE_BEGIN). Next install() may overwrite gen.
    uid_to_slot_.erase(it);
    return true;
}

void ReceiverLeaseTable::enqueue_pending(uint8_t slot_id, uint8_t gen,
                                        uint32_t chunk_idx) {
    if (pending_.size() >= pending_capacity_) {
        pending_.pop_front();
        ++dropped_count_;
    }
    pending_.push_back({slot_id, static_cast<uint8_t>(gen & kGenMask),
                        chunk_idx, tick_});
}

std::vector<PendingEntry>
ReceiverLeaseTable::drain_pending_for(uint8_t slot_id, uint8_t gen) {
    std::vector<PendingEntry> out;
    const uint8_t want_gen = static_cast<uint8_t>(gen & kGenMask);
    auto it = pending_.begin();
    while (it != pending_.end()) {
        if (it->slot_id == slot_id && it->gen == want_gen) {
            out.push_back(*it);
            it = pending_.erase(it);
        } else {
            ++it;
        }
    }
    return out;
}

size_t ReceiverLeaseTable::expire_pending(uint64_t max_age_ticks) {
    size_t removed = 0;
    auto it = pending_.begin();
    while (it != pending_.end()) {
        if (tick_ - it->enqueued_tick > max_age_ticks) {
            it = pending_.erase(it);
            ++removed;
        } else {
            ++it;
        }
    }
    return removed;
}

SlotPressure ReceiverLeaseTable::pressure() const {
    SlotPressure p{0, 0, kSlotCount};
    for (const SlotState& s : slots_) {
        if (s.active) ++p.in_use;
        if (s.gen == kGenCount - 1) ++p.near_wrap;
    }
    return p;
}

}  // namespace semirdma::clear
