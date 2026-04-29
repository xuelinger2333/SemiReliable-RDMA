# E1 — Flat-path regression (smoke notes, 2026-04-29)

**Status:** Harness wired; smoke ran but surfaced two issues that block the
full 45-cell grid. Pause before launching matrix.

## What works

- `clear_t1` transport branch added to [`experiments/stage_a/train_cifar10.py`](../../experiments/stage_a/train_cifar10.py)
- [`experiments/configs/phase5_e1.yaml`](../../experiments/configs/phase5_e1.yaml) inherits `stage_a_baseline` and overrides `transport_cfg` with `ClearTransportConfig` field names
- Smoke run on amd247 (mlx5_2, single-node 2-rank torchrun) reaches `training done: 3 steps in 21.5s`
- Hook fixed for ResNet-18 single-bucket scale: clamp recv top-up at `rq_depth - outstanding - 8`; deepen `sq_depth=4096 / rq_depth=8192`; auto-advance `step_seq` after each bucket so consecutive hook calls don't reuse uids

## Issues blocking the grid

### 1. Teardown crash (cosmetic)

```
training done: 3 steps in 21.5s
terminate called without an active exception
```

Training writes all CSVs and exits its main loop, but the process dies with
SIGABRT during cleanup. Likely the bg poll thread (daemon) racing the C++
ControlPlane destructor. CSVs are valid; exit code is not. Fix: explicit
`state.shutdown()` before `dist.destroy_process_group()`. Tracked.

### 2. iter_ms ≈ 7 s/step (real concern)

```
step,fwd_ms,bwd_ms,opt_ms,total_ms
0,2456.4,4821.4,65.4,7343.2
1,2313.8,4606.3,17.5,6937.6
2,2215.7,4683.9,17.5,6917.0
```

ResNet-18 / batch=128 / single-bucket allreduce of 47 MB on 100 GbE should
land near Phase 4's iter_ms (~hundreds of ms, not 7 s). The 4.6 s `bwd_ms`
includes the full hook latency. NIC throughput at 47 MB / 4 s = 12 MB/s,
1000× under hardware capability — Python-side per-chunk overhead is the
prime suspect (2900 `post_write` trampoline calls per bucket).

This blocks "+5 % of phase4_flat" — current overhead is 100×, not 5 %.

### 3. Loss trajectory (diagnostic, not pass criterion)

```
step,loss
0,2.4233
1,2.8862
2,3.4674
```

Loss goes up over 3 steps. Could be transient warmup with high lr=0.1 on
CIFAR-10, but also possible the averaged gradient is incorrect under DDP.
Need a longer run + comparison against `phase4_flat` to disentangle.

## Recommended next steps

1. **Run `phase4_flat` baseline on the same harness** (50 steps) so we have
   apples-to-apples iter_ms + loss curve to compare against.
2. **Profile clear_allreduce_hook** to find the 4 s sink. Likely candidates:
   per-chunk Python `post_write` loop, the `bytes(flat.numpy().tobytes())`
   copy of 47 MB, or the bg poll thread starving CPU.
3. **Don't launch the 45-cell grid** until iter_ms is within an order of
   magnitude of `phase4_flat`. Otherwise we burn ~20 hours on cells that will
   all fail the +5 % criterion.

## Artifacts

- Smoke CSVs: `experiments/results/phase5/e1/2026-04-29/04-34-52_clear_t1_loss0.0_seed42/`
  - `iter_time.csv`, `loss_per_step.csv`, `grad_norm.csv`
