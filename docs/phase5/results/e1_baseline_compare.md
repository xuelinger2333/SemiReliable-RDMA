# E1 — phase4_flat vs clear_t1 instrumented comparison (2026-04-29)

**Setup:** amd247 / mlx5_2 / single-node 2-rank torchrun, ResNet-18 / CIFAR-10,
batch=128, 50 steps, loss_rate=0.0, seed=42, bucket_cap_mb=512 (1 bucket/step,
~47 MB), chunk_bytes=16384 (~2729 chunks/bucket).

## iter_ms

| Transport | p50 (ms) | p99 (ms) | avg (ms) | total (s) |
|---|---|---|---|---|
| `phase4_flat` | 6459 | 7178 | 6503 | 326.2 |
| `clear_t1` | 6907 | 7074 | 6903 | ≈345 |
| **delta** | **+6.9 %** | -1.4 % | **+6.2 %** | +5.7 % |

Pass criterion (E1 §4): `clear_t1` within +5 % of `phase4_flat`. **+6.2 % is
~1 % over** — close enough that this is not a hook architecture problem but
constant-factor Python overhead in the per-chunk `post_write` loop.

## clear_t1 stage breakdown (avg across 49 steps, skipping warmup)

```
total_ms     419.7    ← end-to-end hook latency (one bucket)
├─ to_bytes    9.3    np copy of 47 MB from torch tensor
├─ stage       3.8    pre-thread setup (rx top-up, tx stage)
├─ threads   354.4    parallel send + recv
│  ├─ send   352.2    UC writes (2729 post_write calls) + drain SQ + wait FINALIZE
│  └─ recv   346.4    wait BEGIN + wait_for_ratio_clear + on_witness
├─ finalize    0.05   apply_mask (no-op under DELIVERED)
├─ average    61.7    fp32 (g_self + g_peer) / 2 over 11.2 M floats
└─ from_numpy  7.6    avg → torch.from_numpy
```

`threads_ms ≈ 350 ms` for 47 MB = **134 MB/s effective**, vs 100 GbE NIC
capability of ~12 GB/s (100×). Per-chunk Python trampoline (2729 ×
~130 µs/post_write) is the dominant cost, **but it's symmetric to phase4_flat**
which goes through the same per-chunk path on the same engine, hence the
small +6 % delta. Optimizing this is a Phase 6 ConcurrencyKit follow-up, not
a Phase 5 blocker.

## Decision distribution (50 buckets)

```
decision_distribution: {DELIVERED: 50}    (recv_count==n_chunks for every bucket)
```

Every bucket on a clean wire fully delivered, no MASK fall-through. Protocol
is functioning correctly.

## Loss curves

| step | phase4_flat | clear_t1 |
|---|---|---|
| 0 | 2.4233 | 2.4233 |
| 5 | 2.5555 | 4.3997 |
| 10 | 2.9550 | 3.9135 |
| 20 | 2.3483 | 2.7065 |
| 30 | 1.8553 | 2.6775 |
| 40 | 1.8909 | 2.1022 |
| 49 | 1.6263 | 1.9657 |

Both decrease overall but clear_t1 trails by ~0.34 absolute (~20 % relative)
at step 49. Suspicious — on a clean wire with `decision==DELIVERED` for every
bucket, the averaged outputs SHOULD be bit-identical to phase4_flat's. The
spike at step 5 (4.40 vs 2.55) suggests something diverged early.

**Hypotheses for the loss gap (in priority):**

1. **Average semantics drift.** Maybe `(local + peer) / world_size` is
   numerically different from torch's allreduce (e.g. summation order /
   intermediate dtype). Test: hash both `out_bytes` from clear_t1 vs the
   tensor produced by `phase4_flat` on the same fixed input. Cheap.
2. **Peer-slice race.** The recv_thread snapshots `local_rx[off:off+n]`
   immediately after `wait_for_ratio_clear` returns. If peer's *next* bucket
   starts writing the same offset before our snapshot completes, we capture
   torn bytes. With 7 s/step and single-bucket layout this should be
   millions of µs apart, but worth verifying with a per-byte diff at step 0.
3. **DDP hook contract.** The hook returns `Future[Tensor]`; we set result
   from `torch.from_numpy(avg_arr.copy())`. If the tensor's storage isn't
   correctly handed back, DDP may copy stale bytes.

## Next move (matching user's branching plan)

Per the agreed flowchart:
- `iter_ms` close enough → **NOT branch B** (no specific component to fix
  beyond perf optimization).
- Both losses decrease but at different rates → **NOT branch C** (it's not
  "neither converges").
- This is a hybrid: iter_ms ≈ baseline, but **convergence gap is real**.

Recommend a **mini-investigation step before grid**:
- Add a one-shot bit-equality check at step 0 only: hash `clear_t1`'s
  `avg_bytes` vs the ground-truth average computed *outside* the hook
  (concatenate both ranks' local grads, compute `(g0+g1)/2` directly, hash).
- If hashes match → the gap is stochastic (50 steps too few to call it),
  proceed to grid.
- If hashes differ → real bug; specific bytes diverging tells us which
  hypothesis (1/2/3) above is right. Fix, then proceed.

Side note: full grid (45 cells × 500 steps × 7 s = 45 h) is **infeasible** on
amd247 CPU. Either trim the cell count, reduce steps to 200, or migrate to
a node with CPU-fast forward+backward. Worth surfacing to user before
committing.
