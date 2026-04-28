# Cluster fact sheet — amd247 / amd245 / amd264 (CX-5 25 GbE, 2026-04-28+)

Single-page reference for the current Stage B / Phase 4 polishing cluster on
Utah CloudLab amd-class hardware. See [stage-b-hardware-notes.md](history/stage-b-hardware-notes.md)
§10 for the bring-up narrative; this file is the IP/MAC/dev cheatsheet.

## Topology

```
                     128.110.219.0/21  (public mgmt, ssh)
                     ──────────────────────────────────────
                                eno33np0 25 GbE
                                    │
       ┌────────────┬───────────────┴───────────────┬────────────┐
       │            │                               │            │
   amd247         amd245                         amd264       (amd259 spare,
   .219.158       .219.156                       .219.175      not used)
   eno34np1       eno34np1                       eno34np1
   25 GbE         25 GbE                         25 GbE
   10.10.1.1     10.10.1.2                      10.10.1.3
   (rank 0)      (rank 1)                       (XDP middlebox)
                     ──────────────────────────────────────
                     10.10.1.0/24      (experiment LAN, RoCEv2)
```

100 GbE ConnectX-5 Ex ports (enp65s0f0np0 / mlx5_2 / QSFP28) are physically
present on every node with a 100 G DAC plugged in but report
`No partner detected` — the CloudLab profile does **not** wire that LAN.
All experiments use the 25 GbE eno34np1 path.

## Per-node facts (verified 2026-04-28)

| | amd247 (rank 0 / receiver) | amd245 (rank 1 / sender) | amd264 (XDP middlebox) |
|---|---|---|---|
| Public mgmt | 128.110.219.158 | 128.110.219.156 | 128.110.219.175 |
| Mgmt iface | eno33np0 (mlx5_0) | eno33np0 (mlx5_0) | eno33np0 (mlx5_0) |
| **Experiment iface** | **eno34np1 (mlx5_1)** | **eno34np1 (mlx5_1)** | **eno34np1 (mlx5_1)** |
| Experiment IP | 10.10.1.1 | 10.10.1.2 | 10.10.1.3 |
| Experiment MAC | 04:3f:72:ac:ca:77 | 04:3f:72:ac:ca:57 | 04:3f:72:b2:c2:09 |
| 100 G port (unwired) | enp65s0f0np0 (mlx5_2) | enp65s0f0np0 (mlx5_2) | enp65s0f0np0 (mlx5_2) |
| OS / kernel | Ubuntu 22.04.2 / 5.15.0-168 | same | same |
| CPU | AMD EPYC 7402P 24-core (48 threads) | same | same |
| RAM | 125 GiB | same | same |

## NIC + firmware

- Mellanox ConnectX-5 (MT27800), firmware **16.28.4512** — bound to mlx5_0 (mgmt) + mlx5_1 (experiment, 25 GbE, RoCEv2)
- Mellanox ConnectX-5 Ex (MT28800), firmware **16.28.4512** — bound to mlx5_2 + mlx5_3 (100 G QSFP28, NOT wired by CloudLab profile)
- Driver: `mlx5_core` (kernel built-in)
- RoCEv2 GID index 1 (IPv6 link-local) for direct-cable RoCE
- RoCEv2 GID index 3 (IPv4-mapped, `::ffff:10.10.1.x`) when running through the XDP middlebox (required so dst-MAC resolution consults kernel ARP that we poison)

## Link configuration (after `link_setup.sh`)

- MTU 9000 (jumbo) on eno34np1 — verified end-to-end with `ping -M do -s 8972`
- Active path MTU = 4096 B (IBV_MTU_4096) — same as amd203/amd196
- PFC RX/TX off — lossy RoCE scenario, matches Stage B assumption
- Link partner: 25000 Mb/s, autoneg off after `ethtool -A` reset

## Day-0 baseline (`scripts/cloudlab/run_perftest.sh`)

| Test | Command | Result |
|------|---------|--------|
| ib_write_bw | `DEV=mlx5_1 run_perftest.sh server` ↔ `DEV=mlx5_1 run_perftest.sh client 10.10.1.2` | **24.39 Gbps** average @ 65536 B (97.6% of 25 GbE line rate) |
| ib_write_lat | `MODE=lat SIZE=8 DEV=mlx5_1 ...` | t_typical 2.13 µs / **p99 2.19 µs** / p99.9 3.17 µs @ 8 B |

Both numbers are within noise of the prior amd203/amd196 baselines documented
in [stage-b-hardware-notes.md](history/stage-b-hardware-notes.md) §9.2 — the hardware
is functionally identical (same NIC, same firmware, same line rate).

## Why the existing tunings carry over without re-sweep

amd247/amd245 and amd203/amd196 share:
- ConnectX-5 family + identical firmware 16.28.4512
- 25 GbE Ethernet wire rate
- Path MTU 4096
- RoCEv2 GID index 1
- AMD EPYC 7402P CPU, 48 threads, CPU-only torch
- ib_write_bw / ib_write_lat baselines (within < 1 % noise)

Therefore the post-fix Stage B values from amd203/amd196 hold without re-sweep:

| Parameter | Value | Source |
|-----------|-------|--------|
| `chunk_bytes` | 4096 (== path_mtu, 1 IB packet per chunk) | `experiments/configs/stage_b_cloudlab.yaml:97` |
| `sq_depth` | 8 (wave-throttle floor 32 KB inflight) | same |
| `rq_depth` | 16384 | same |
| `timeout_ms` | 200 (CPU jitter margin) | same |
| `ratio` | 0.95 | Phase 2 RQ4 sweet spot |
| `gid_index` | 1 (direct cable) / 3 (with XDP middlebox) | `run_p1_matrix.sh` auto-appends 3 when `MIDDLEBOX_HOST` is set |

If a future re-tune is needed (e.g. if a CloudLab profile change wires the
100 GbE LAN), follow the §10 "deferred recalibration punch list" in
[stage-b-hardware-notes.md](history/stage-b-hardware-notes.md).

## Operational quick-reference

```bash
# Per-node bring-up after a CloudLab reboot:
ssh chen123@amd<N>.utah.cloudlab.us 'cd ~/SemiRDMA && bash scripts/cloudlab/link_setup.sh'

# 2-node RDMA sanity:
ssh chen123@amd245.utah.cloudlab.us 'DEV=mlx5_1 bash ~/SemiRDMA/scripts/cloudlab/run_perftest.sh server' &
ssh chen123@amd247.utah.cloudlab.us 'DEV=mlx5_1 bash ~/SemiRDMA/scripts/cloudlab/run_perftest.sh client 10.10.1.2'

# XDP middlebox transparent (0% drop):
ssh chen123@amd264.utah.cloudlab.us 'sudo bash ~/SemiRDMA/scripts/cloudlab/middlebox_setup.sh start 0'

# ARP-spoof apply (from amd247):
ssh chen123@amd247.utah.cloudlab.us \
  'MIDDLEBOX_MAC=04:3f:72:b2:c2:09 PEER_B_HOST=chen123@10.10.1.2 \
   bash ~/SemiRDMA/scripts/cloudlab/arp_spoof_setup.sh apply'

# Phase 4 P1 matrix (from amd247):
ssh chen123@amd247.utah.cloudlab.us \
  'cd ~/SemiRDMA && DROP_RATES="0 0.01 0.05" TIMEOUTS_MS="50" \
   MIDDLEBOX_HOST=chen123@amd264.utah.cloudlab.us \
   bash scripts/cloudlab/run_p1_matrix.sh'
```
