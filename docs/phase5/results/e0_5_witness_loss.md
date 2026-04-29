# E0.5 — WITNESS-loss correctness

**Status:** PASS (2026-04-29).

## Setup

- Host: `amd247.utah.cloudlab.us`, mlx5_2
- 2-rank in-process pair
- 500 buckets × 1024 float32
- `buckets_per_step=20`
- **Witness drop:** per recv-side `on_witness` call, with prob 5% replace `recv_bitmap` with all-zeros before invoking the Finalizer. The receiver acts as if no chunks arrived; under MASK_FIRST policy this drives a MASKED finalize → peer slice is zeroed → averaged output = `local / 2`, which differs from `(G_a + G_b) / 2` deterministically.

The point of this test is **the bucket still completes** — no hang, no exception — and the count of MASKED outputs matches the witness-drop count.

## Result

| Metric | Value | Pass criterion | Verdict |
|---|---|---|---|
| `n_witness_dropped` | 41 / ~1000 calls (4.1%) | ≈ 5% | ✓ |
| `masked_buckets` | 41 | == n_witness_dropped | ✓ |
| every uid finalized | yes | yes | ✓ |
| `wall_time_s` | 0.69 s | — | — |
| `iter_ms p50` | 1.26 ms | — | — |
| `iter_ms p99` | 1.44 ms | — | — |
| throughput | 730 bkt/s | — | — |

Raw: [`e0_5_witness_loss_20260429_041541.json`](../../../experiments/results/phase5/e0_5/).

## Interpretation

The Finalizer's bulk-decision path (zero bitmap → MASKED under MASK_FIRST → `apply_finalize` zeros peer chunks → averaging proceeds with the surviving local data) is sound: every uid completes, no protocol hang. The 1:1 ratio between `n_witness_dropped` and `masked_buckets` confirms a correct per-uid attribution under loss — no cross-bucket contamination, no double-mask.

This is the foundational correctness for CLEAR's fail-soft path: when WITNESS evidence is missing, the protocol degrades to "treat all peer chunks as missing" rather than wedging the step.

## Next

- E0.6 long-run wrap (12 h, deferred to W4).
- E1 flat-path regression on ResNet-18 / CIFAR-10.
