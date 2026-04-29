# E0.1 — Slot-wrap correctness

**Status:** PASS (2026-04-29).

## Setup

- Host: `amd247.utah.cloudlab.us`, mlx5_2 (CX-5 Ex 100 GbE, GID idx 1)
- 2-rank in-process pair via `ClearHookState.for_in_process_pair`
- 5000 buckets × 1024 float32 (4 KiB / bucket = 1 chunk @ chunk_bytes=4096)
- `buckets_per_step=50` → ~100 step boundaries → ~100 lease-table drain cycles, ~20 slot-recycles per cycle on a 256-slot table
- Distinct random gradients per bucket so any cross-bucket attribution leak produces a numeric mismatch

## Result

| Metric | Value | Pass criterion | Verdict |
|---|---|---|---|
| `false_attribution_rate` | 0.0 | < 1e-4 | ✓ |
| `n_mismatches` | 0 / 5000 chunks | 0 | ✓ |
| `wall_time_s` | 6.97 s | — | — |
| `iter_ms p50` | 1.30 ms | — | — |
| `iter_ms p99` | 1.46 ms | — | — |
| throughput | 717 bkt/s | — | — |

Raw: [`e0_1_slot_wrap_20260429_035952.json`](../../../experiments/results/phase5/e0_1/) (CloudLab; rsync to archive).

## Interpretation

CLEAR's slot+gen indirection holds zero false attribution over 5000 buckets with periodic slot recycle. The 256-slot × 16-gen table cycles roughly every `256 / 50 ≈ 5` step boundaries, so the run exposed ~1000 slot-recycle events without a single alias.

## Next

- E0.2 prebegin_race (artificial BEGIN delay so UC writes arrive first).
- E0.6 long-run (12h, ~10⁶ buckets) deferred to W4 once E0.2–E0.5 sign off.
