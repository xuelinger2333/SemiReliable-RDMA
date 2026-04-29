# CLEAR — Completion-Labeled Erasure Attribution for RoCE UC

> **Status:** Design draft (2026-04-29). Awaiting user sign-off before W1 code work starts.
> **Parent:** [PHASE5_PLAN.md](PHASE5_PLAN.md)
> **Source:** [deep-research-report.md](../../deep-research-report.md) §"最终建议方案 CLEAR"

---

## 1. Problem statement (one paragraph)

On RoCEv2 UC + RDMA Write-with-Immediate, packet loss is **silent**: the data is written into the receiver's MR buffer or simply isn't, and the only fast-path attribution channel is the 32-bit `imm_data`. Phase 4 PR-C encodes `(bucket_id mod 256, chunk_id)` directly in `imm_data`, but (a) `mod 256` aliases under bucket wrap, (b) attribution is *locally inferred* by ratio + ghost-mask — different ranks can disagree on whether a chunk was "delivered", (c) there is no protocol-level concept of "I declare this chunk lost", so finalize is one-sided. CLEAR fixes all three by **separating identity from delivery**: `imm_data` carries a short *slot lease*, the full transfer identity lives in an RC-installed lease table, and a witness pass over a small RC control plane turns silent loss into a cross-rank-consistent erasure decision.

## 2. Wire format

### 2.1 Data plane: `imm_data` (32 bits)

```
 31                  24 23                                  4 3        0
+---------------------+--------------------------------------+---------+
|   slot_id    (8)    |      chunk_idx        (20)           | gen (4) |
+---------------------+--------------------------------------+---------+
```

- **`slot_id` (8 bits, 256 values)** — short index into the receiver's lease table. Not a `bucket_id`. Reused across buckets after `RETIRE`.
- **`chunk_idx` (20 bits, 1 048 576 values)** — chunk index within the bucket. At 4 KiB chunks → 4 GiB max bucket. Far above any realistic DDP bucket.
- **`gen` (4 bits, 16 values)** — generation counter incremented each time `slot_id` is reused. Defends against stale-packet alias when a slot is recycled. `gen` near wrap → mandatory slot quarantine.

### 2.2 Control plane: 5 message types over RC

All messages travel on a separate RC QP per peer pair (sender↔receiver). Small, fixed-format. Sent as `IBV_WR_SEND` (not Write-with-Imm) so they self-deliver to a posted receive.

| Msg | Sender → | Fields | Purpose |
|---|---|---|---|
| `BEGIN` | rcv | `uid (u64), slot_id (u8), gen (u8), step_seq (u32), bucket_seq (u32), phase_id (u8), peer_edge (u16), n_chunks (u32), policy (u8), deadline_us (u32), chunk_bytes (u32), checksum_seed (u32)` | Install lease `(slot_id, gen) → uid`. Receiver allocates `recv_bitmap[uid]`. |
| `WITNESS` | rcv → finalizer | `uid (u64), recv_count (u32), encoding (u8 = RAW\|RLE\|RANGE\|FULL), payload_len (u16), payload[]` | Receiver-side declaration of which chunks were observed. Triggered by ratio-exit OR deadline. |
| `REPAIR_REQ` | finalizer → snd | `uid (u64), n_ranges (u16), ranges[(start, len)]` | Optional. Only issued for `policy=repair-first` buckets within budget. Repair is sent over RC (deterministic). |
| `FINALIZE` | finalizer → all ranks | `uid (u64), decision (u8 = DELIVERED\|REPAIRED\|MASKED\|STALE\|FALLBACK_RC), final_mask_encoding, final_mask[]` | Single canonical erasure decision. All ranks must apply the same mask before SGD. |
| `RETIRE` | finalizer → both | `uid (u64), slot_id (u8), gen (u8)` | Permits slot reuse. Until RETIRE, `(slot, gen)` is reserved. |

### 2.3 Identifier definitions

- `uid` — globally unique transfer id. **Not** `GradBucket.index()`; PyTorch rebuilds buckets after iter 0. Built post-warm-up as `uid = hash(rank_pair, step_seq, bucket_seq, phase_id, peer_edge)`. `bucket_seq` is a transport-local stable index assigned to each `GradBucket` *parameter manifest*, computed once per training run after DDP warm-up.
- `policy ∈ {repair-first, mask-first, stale-fill, estimator-scale}` — drives finalizer's choice when `recv_count < n_chunks`.
- `peer_edge` — directed sender→receiver pair within a ring/halving allreduce phase. Lets attribution survive multi-edge collectives.

## 3. State machines

### 3.1 Sender per `uid`

```
[IDLE] --acquire(slot,gen)--> [BEGIN_SENT] --post N UC writes--> [WRITES_POSTED]
   --ack from RC BEGIN-->     [WRITES_FLOWING]
   --REPAIR_REQ rx-->         [REPAIRING] --repair done--> [WRITES_FLOWING]
   --RETIRE rx-->             [DONE]    (free slot)
   --deadline_local_abort-->  [ABORTED] --best-effort RETIRE--> [DONE]
```

### 3.2 Receiver per `uid`

```
[no lease] --BEGIN--> [LEASE_INSTALLED, bitmap=zero, count=0]
   --CQE imm matches (slot,gen)--> [LEASE_INSTALLED, bitmap[idx]=1, count++]
   --CQE imm mismatches lease--> [PREBEGIN_PENDING queue]
   --(ratio || deadline)-->  [WITNESS_SENT]
   --REPAIR data CQE-->       merge into bitmap
   --FINALIZE rx-->           [FINAL]
   --RETIRE rx-->             [no lease] (slot free)
```

The **`PREBEGIN_PENDING`** queue is the critical correctness primitive. UC writes can land before RC `BEGIN` due to per-QP ordering. Drop nothing; on `BEGIN` arrival, drain pending CQEs whose `(slot,gen)` now matches.

### 3.3 Finalizer (one per receiver, or per-rank if symmetric)

```
[idle] --WITNESS rx-->
   if recv_count == n_chunks: emit FINALIZE(DELIVERED) → emit RETIRE.
   elif policy == repair-first AND missing_bytes <= repair_budget AND slack >= repair_eta:
       emit REPAIR_REQ; on completion → FINALIZE(REPAIRED) → RETIRE.
   elif policy in {mask-first, stale-fill, estimator-scale}:
       compute final_mask = NOT(recv_bitmap); emit FINALIZE(<decision>) → RETIRE.
   else: emit FINALIZE(FALLBACK_RC); resend whole bucket on RC; RETIRE.
```

`repair_budget_bytes` and `repair_deadline_slack_us` are config knobs; both are observable in metrics so we can tune per-workload.

## 4. Cross-rank consistency

The headline correctness property: **for every `uid`, all participating ranks apply the identical `final_mask` before the local SGD step.**

Two implementation options:

- **Option A — finalizer-broadcast.** One designated rank (e.g. lowest rank in the peer group, or root of the collective phase) runs the finalizer and broadcasts `FINALIZE` over RC to all peers. Simple; one extra RTT in the critical path. **Recommended for T1 scope.**
- **Option B — symmetric witness exchange.** Every rank exchanges its local `recv_bitmap`, computes `final_mask` deterministically (e.g. `OR` of received-from-this-rank views), no broadcaster. More complex but no single-point critical path. Defer to T3.

We will measure `semantic_mismatch_rate` = fraction of `uid`s where any two ranks disagree on `final_mask`. Target ≈ 0; non-zero is a protocol bug.

## 5. Repair budget and policy

| Policy | When |
|---|---|
| `repair-first` | Critical layers: BN, embedding, LN, output projection. Repair as long as budget + slack permit. |
| `mask-first` | Mid conv / MLP. Mask immediately on witness; do not consume repair budget. |
| `stale-fill` | Optimizer-state-like buffers where last-iter value is acceptable. |
| `estimator-scale` | For Distributed-Training-under-Packet-Loss-style unbiased rescaling: scale aggregated tensor by `n_chunks / recv_count` (equivalent in expectation under uniform loss). |

`repair_budget_bytes` is a per-step bucket budget shared across all repair-first buckets; mask-first buckets never consume from it. This bounds worst-case fallback-to-RC bytes, preserving CLEAR's bounded-time guarantee.

## 6. Receive-queue management

DOCA documentation: `Write-with-Imm` is **fatal** at the receiver if no Receive WR is posted. CLEAR therefore needs a **per-peer RQ low-watermark monitor**:

- Maintain `posted_recv_credits[peer]`. Pre-post enough zero-length RWRs to absorb `n_chunks + control_msgs` worth of imm_data.
- If `posted_recv_credits[peer] < threshold` mid-bucket, sender must stop posting Write-with-Imm and either (a) park the bucket, (b) downgrade the bucket to RC fallback, or (c) emit a `BACKPRESSURE` control message asking receiver to refill.
- E0 must include a stress test where receiver intentionally lags on RWR replenishment.

## 7. Failure handling and slot wrap

| Failure | Handling |
|---|---|
| `WITNESS` lost / late | Receiver retransmits over RC after `witness_timeout`. If still lost, fallback to RC for that uid. |
| `BEGIN` lost | Sender's UC writes pile in receiver's `PREBEGIN_PENDING`; sender RC-retransmits BEGIN. Receiver garbage-collects orphan pending after `prebegin_ttl`. |
| `RETIRE` lost | Slot stays reserved; sender RC-retransmits RETIRE. Slot table has `force_retire_after` long timeout to recover from peer crash. |
| Slot wrap (256 slots × 16 gens = 4096 unique transfers) | Sender + receiver track a global "slot pressure" counter. When >75 % occupancy, enforce slot-quarantine: skip recycling slots whose previous gen retired < `quarantine_us` ago. |
| Sender peer crash mid-bucket | Receiver's `force_retire_after` reclaims slot; uid finalizes as `FALLBACK_RC` (or aborted, depending on policy). |

## 8. Integration with PyTorch DDP

CLEAR sits **inside** `register_comm_hook`. Hook flow per bucket:

```
on_bucket_ready(bucket):
    bucket_seq = manifest.lookup(bucket)            # stable post-warm-up id
    if step < warmup_steps:
        return rc_baseline_hook(bucket)              # no UC, no CLEAR
    uid = hash(rank, step, bucket_seq, phase, peer)
    slot, gen = lease_table.acquire(uid)
    policy = registry.lookup(bucket_seq).policy
    deadline = wire_calibrator.deadline_us(bucket.size())
    control_plane.send_BEGIN(uid, slot, gen, ...)
    sender_engine.post_uc_writes(uid, bucket.buffer(), slot, gen)
    fut = finalizer.future_for(uid)
    fut.add_callback(apply_mask_then_finish)
    return fut
```

The shadow-RC oracle path is bolted on as a sampling layer: with probability `oracle_sample_rate`, *also* send the same bucket on RC and store the byte-exact reference. After FINALIZE, compute `false_attribution_rate` per sampled uid.

## 9. What CLEAR does NOT claim

- It does not make UC reliable. It makes UC **attributable**.
- It does not eliminate `repair_budget` byte cost; it bounds it.
- It does not cover multi-rail / multi-NIC; one peer pair, one UC QP + one RC QP.
- It does not depend on PFC; the entire point is correct behavior on lossy non-PFC RoCEv2.

## 10. Open design questions for review

1. Should `gen` be 4 bits (16 values, simpler) or 6 bits (64, less frequent quarantine)? Default 4 bits, revisit after E0 wrap stress.
2. Should `WITNESS` be unsolicited (receiver pushes on ratio-exit) or polled (finalizer requests after sender claims completion)? Default unsolicited — lower latency.
3. Where does `policy` live — registry static, or per-bucket adaptive based on observed loss? Default registry static for T1; adaptive in T3 ablation.
4. Single finalizer rank vs symmetric? Default single (Option A) for T1.
5. Is `FALLBACK_RC` the same QP as control-plane RC, or a third QP? Default same — control + repair + fallback all on one RC QP per peer pair, dispatched by message type.
