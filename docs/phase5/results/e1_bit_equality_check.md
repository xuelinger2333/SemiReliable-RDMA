# E1 — Bit-equality check resolves the suspected loss gap (2026-04-30)

**Verdict:** **No CLEAR averaging bug.** The loss "divergence" flagged in the
earlier 50-step compare was non-deterministic CPU floating-point ordering
between two independent torchrun invocations, not a CLEAR-vs-Phase-4
arithmetic difference.

## Setup

- amd247 / mlx5_2 (CX-5 Ex 100 GbE), single-node 2-rank
- ResNet-18 / CIFAR-10 batch=128, seed=42, loss_rate=0.0, 50 steps
- bucket_cap_mb=512 (1 bucket/step ≈ 47 MB), chunk_bytes=16384
- sha256 probes at step 0 only on both ranks: `in_sha256` (local gradient bytes),
  `peer_sha256` (received remote bytes), `out_sha256` (averaged bytes after
  the hook returns)

## Hashes captured

| Rank | Transport | in_sha256 (local) | out_sha256 (averaged) |
|---|---|---|---|
| 0 | phase4_flat | `9906376e…` | `683f9fac…` |
| 1 | phase4_flat | `6f783934…` | `683f9fac…` |
| 0 | clear_t1    | `9906376e…` | **`683f9fac…`** |
| 1 | clear_t1    | `6f783934…` | **`683f9fac…`** |

**`out_sha256` is bit-identical across both transports and both ranks**.
The averaging arithmetic is correct.

(`peer_sha256` differs slightly between hook implementations because each
hook hashes the peer-bytes view at a slightly different point in its own
flow; not relevant to correctness — what matters is the post-average bytes.)

## Loss curve

| step | phase4_flat | clear_t1 |
|---|---|---|
| 0 | 2.4233 | **2.4233** |
| 1 | 2.8862 | **2.8862** |
| 2 | 3.4674 | **3.4674** |
| 3 | 3.9984 | **3.9984** |
| 4 | 4.7964 | 4.1276 (diverges) |
| 5 | 4.8955 | 4.3997 |
| 49 | 2.4821 | 1.9657 |

Steps 0–3 are **bit-identical** between the two transports. From step 4 the
two diverge — but the cumulative direction is *not* systematic: in this run
clear_t1 ends *better* than phase4_flat (1.97 vs 2.48); in the prior 50-step
compare the order was inverted. Symptom of `torch.use_deterministic_algorithms(False)`
in `_set_seed` — CPU ops have non-deterministic intra-op reduction order, and
50 steps amplifies it.

## What this resolves

- **No fix needed in `_run_clear_bucket` averaging.** The numpy `(a32+b32)/world_size`
  path produces the same fp32 bytes as Phase 4's `flat.add_(remote_t).div_(world_size)`.
- **Both `set_result(flat)` (in-place) and `set_result(out_t)` (new tensor)
  hand DDP equivalent buffers** — DDP copies bytes back regardless.

## Outstanding (non-blockers for E1 grid)

1. The teardown `terminate called without an active exception` still fires
   after both transports complete; cosmetic, CSVs are valid.
2. The +6.2 % iter_ms of clear_t1 over phase4_flat reflects the per-chunk
   Python `post_write` loop (2729 calls/bucket). Optimization candidate
   for Phase 6, not E1.
3. With CPU torch non-determinism, individual 50-step loss snapshots cannot
   distinguish transports. E1 cells should average over 3 seeds and report
   last-50-step mean with confidence intervals, not single-seed final loss.

## Decision

Diag injection removed (commit `<this commit>`). Ready to proceed to E1 full
grid once user signs off on grid scoping (45-cell × 7-s/step ≈ 45 h on
amd247 CPU; need to either trim cells, drop step count to 200, or run two
nodes in parallel — amd245 + amd247).
