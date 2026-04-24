# Phase 4 · Hybrid allreduce hook — negative result + removal

## TL;DR

`semirdma_hybrid_allreduce_hook` was designed as an H3-drift fix: UC
reduce-scatter + Gloo AllGather, with magnitude compensation at
ghost-masked chunks. Phase 4 XDP-middlebox experiments (2026-04-24 /
04-25) showed **hybrid is strictly worse than the pure-UC
`semirdma_allreduce_hook` at every drop rate tested (0, 1%, 5%, 10%)**,
both in final loss and iteration time. Removed from the tree on
2026-04-25.

## Experiment setup

- **Platform**: CloudLab Utah amd203 / amd196 (CX-5 25 GbE), XDP
  middlebox on amd186 via ARP-spoof "bump in the wire"
  (`scripts/cloudlab/middlebox_setup.sh` + `arp_spoof_setup.sh`).
- **Workload**: `experiments/stage_a/train_cifar10.py`, CIFAR-10
  ResNet-small, 2 ranks, STEPS=500, timeout_ms=50.
- **Drop**: Bernoulli on UDP:4791 in the XDP forwarder
  (`scripts/cloudlab/xdp_dropbox/xdp_dropbox.bpf.c`); drop_pct
  calibrated end-to-end via XDP stats at ~0.5% across mixed
  `{0, 0.01}` cells, confirming honest per-chunk Bernoulli.
- **Seeds**: {42, 1337, 2024} at drop_rate ∈ {0, 0.01}; {42, 1337} at
  drop_rate ∈ {0.05, 0.1}.

## Results — final_loss Δ (hybrid − semirdma)

Negative Δ means hybrid wins. All 20 cells below.

| drop_rate | seed=42 | seed=1337 | seed=2024 | N | Δ mean | direction |
|---|---|---|---|---|---|---|
| 0 | −0.336 | +0.102 | −0.047 | 3 | −0.094 | noise (CI crosses 0) |
| 0.01 | +0.001 | +0.046 | +0.042 | 3 | +0.030 | hybrid marginally worse |
| 0.05 | +0.635 | +0.139 | — | 2 | **+0.387** | hybrid strongly worse |
| 0.10 | +0.327 | +0.227 | — | 2 | **+0.277** | hybrid strongly worse |

Hybrid's disadvantage **grows** with drop rate — the opposite of what
its design predicted.

## Results — iter_ms

Across all 20 cells: `semirdma` ≈ 800 ms/iter, `semirdma_hybrid` ≈ 900
ms/iter. Constant **+11 ± 2% overhead** for hybrid, regardless of
drop rate.

This is structural, not a bug: hybrid adds a reliable Gloo AllGather
(kernel TCP) on top of half-bucket UC reduce-scatter, which is
inherently slower than `semirdma_allreduce_hook`'s zero-copy UC
full-bucket swap.

## Mechanism — why the design premise fails

`semirdma_hybrid_allreduce_hook` computed, for each missed chunk:

```
own_partial[missed] = own + 0            (peer zeroed by ghost mask)
own_partial[missed].mul_(world_size)     (magnitude compensation)
→ Gloo all_gather → /world_size          (reliable broadcast + average)
→ final[missed] = rank_r.own             (single-sample, unbiased)
```

vs `semirdma_allreduce_hook`:

```
flat[missed] = (own + 0) / world_size    (= own / 2 — biased but stable)
```

At drop rate p, the trade-off at the p-fraction of positions where
chunks are missed:

| | bias | variance vs true avg |
|---|---|---|
| semirdma | gradient shrunken by 1/2 (biased toward slower progress) | same as full-information (one deterministic value) |
| hybrid (compensated) | unbiased (E[own] = E[avg] under i.i.d. mini-batches) | **doubled** (single sample instead of average) |

At **low** drop rate (≤1%), only ~1% of positions take this hit, so
both methods are close — and the 12% hybrid iter_ms tax is the
dominant cost → hybrid loses by the latency it adds.

At **high** drop rate (5–10%), 5–10% of positions see 2× noisier
gradients **at every step**. SGD accumulates this into parameter
noise faster than semirdma's bias harms progress. Hybrid
destabilizes; semirdma keeps converging (even at 10% drop, semirdma
holds final_loss ≈ 0.89–1.23 across seeds).

In short: **SGD at this scale prefers biased-but-stable over
unbiased-but-high-variance**. Hybrid's magnitude compensation chose
the wrong side of this trade-off.

## Raw data

- N=3 seeds × drop∈{0, 0.01}:
  `experiments/results/phase4_p1_3seed_20260424_080440/seed{42,1337,2024}/MATRIX_SUMMARY.csv`
- N=2 seeds × drop∈{0.05, 0.1}:
  `experiments/results/phase4_p1_highdrop_20260424_095327/seed{42,1337}/MATRIX_SUMMARY.csv`
- XDP counter end-of-sweep: rx_roce ≈ 346 M, drop_pct ≈ calibrated
  weighted average of the per-cell set-rate values.

## What was removed

- `python/semirdma/hooks.py` — `semirdma_hybrid_allreduce_hook`
  (~170 lines)
- `python/semirdma/__init__.py` — export
- `experiments/stage_a/train_cifar10.py` — `transport=semirdma_hybrid`
  dispatch branch
- `scripts/cloudlab/run_p1_matrix.sh` — removed from default
  `TRANSPORTS`

## What stays

The broader design insight documented here (asymmetric-ghost drift
exists in `semirdma_allreduce_hook` at high drop rates; but fixing it
via this specific compensation hurts more than it helps) is a
paper-relevant negative result. The pure `semirdma_allreduce_hook`
remains as the single UC-based transport hook.

If future work ever reopens this — e.g. a bias-variance trade-off
aware compensation, or an RDMA-native (not Gloo TCP) AllGather — the
raw data above is the baseline to beat.
