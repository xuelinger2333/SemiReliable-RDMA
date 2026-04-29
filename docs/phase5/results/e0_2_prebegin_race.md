# E0.2 — Pre-BEGIN race correctness

**Status:** PASS (2026-04-29).

## Setup

- Host: `amd247.utah.cloudlab.us`, mlx5_2 (CX-5 Ex 100 GbE, GID idx 1)
- 2-rank in-process pair via `ClearHookState.for_in_process_pair`
- 500 buckets × 1024 float32 (1 chunk/bucket @ chunk_bytes=4096)
- `buckets_per_step=20` → 25 step boundaries
- **Send order inverted:** sender drains UC writes from SQ, sleeps a uniform 1–10 ms, then sends BEGIN. Both ranks do this independently, so UC chunks land in the peer's rx CQ before the lease is installed via `on_begin_rx`.

## Result

| Metric | Value | Pass criterion | Verdict |
|---|---|---|---|
| `false_attribution_rate` | 0.0 | < 1e-4 | ✓ |
| `n_mismatches` | 0 / 500 chunks | 0 | ✓ |
| every uid finalized | yes | yes | ✓ |
| `wall_time_s` | 4.50 s | — | — |
| `iter_ms p50` | 9.17 ms | — | — |
| `iter_ms p99` | 12.11 ms | — | — |
| throughput | 111 bkt/s | — | — |

`iter_ms p50 ≈ 9 ms` reflects the dominant artificial 1–10 ms BEGIN delay. The CLEAR protocol overhead on top is sub-ms, consistent with E0.1's 1.30 ms p50.

Raw: [`e0_2_prebegin_race_20260429_040648.json`](../../../experiments/results/phase5/e0_2/).

## Interpretation

UC writes carrying slot+gen in `imm_data` are buffered in the receiver's rx CQ during the BEGIN delay window. Once BEGIN arrives, `on_begin_rx` installs the receiver lease, the foreground recv_thread wakes, and `wait_for_ratio_clear` polls the (already arrived) UC completions. The lease-table lookup correctly attributes each chunk to the now-known uid. No PREBEGIN chunks were dropped, no aliases occurred.

This validates that CLEAR's separation of concerns — UC carries the data, RC carries the metadata — survives the natural cross-plane reorder where the data plane outruns the control plane.

## Next

- E0.3 ucrc_reorder (RC ±5 ms jitter)
- E0.4 rq_starvation (lag RWR replenishment to trigger BACKPRESSURE)
- E0.5 witness_loss (5% WITNESS drop on RC plane)
