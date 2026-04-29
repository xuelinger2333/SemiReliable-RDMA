# E0.3 — UC/RC reorder correctness

**Status:** PASS (2026-04-29).

## Setup

- Host: `amd247.utah.cloudlab.us`, mlx5_2 (CX-5 Ex 100 GbE, GID idx 1)
- 2-rank in-process pair
- 500 buckets × 1024 float32 (1 chunk/bucket @ chunk_bytes=4096)
- `buckets_per_step=20`
- **Jitter injection:** every RC receive callback (`on_begin`, `on_finalize`, `on_retire`) sleeps a uniform `[0, 10] ms` (jitter ±5 ms) before doing its bookkeeping. Bg poll thread is sequential, so this delays subsequent callbacks too — conservative test for cross-message timing variance.

## Result

| Metric | Value | Pass criterion | Verdict |
|---|---|---|---|
| `false_attribution_rate` | 0.0 | < 1e-4 | ✓ |
| `n_mismatches` | 0 / 500 | 0 | ✓ |
| `n_finalize_collisions` | 0 | 0 | ✓ |
| `wall_time_s` | 9.36 s | — | — |
| `iter_ms p50` | 18.59 ms | — | — |
| `iter_ms p99` | 27.29 ms | — | — |
| throughput | 53 bkt/s | — | — |

`iter_ms p50 ≈ 18.6 ms` reflects the dominant ~5 ms × 3 callbacks of average jitter per bucket.

Raw: [`e0_3_ucrc_reorder_20260429_041142.json`](../../../experiments/results/phase5/e0_3/).

## Interpretation

The protocol is robust to RC plane delivery jitter:
- BEGIN late: receiver simply waits longer on `begin_event`; UC chunks land in rx CQ in the meantime (same regime as E0.2).
- FINALIZE late: sender's `send_thread` waits on `finalize_event`; lease stays acquired but no protocol violation.
- RETIRE late: sender's slot stays in the lease table longer; under MASK_FIRST policy the next bucket gets a different slot from round-robin acquisition; quarantine_ticks=1 absorbs gen-bit pressure.

Zero finalize collisions across 500 buckets confirms the per-uid sync object model is exclusive: only the receiver's Finalizer can drive FINALIZE for a given uid in this scope (no peer can spuriously double-publish).

## Next

E0.4 (rq_starvation) deferred — requires wiring the BACKPRESSURE callback path through the hook layer, which is currently scaffolded in C++ (`RQMonitor`) but not invoked by `_run_clear_bucket`. Tracked as W4 follow-up.
