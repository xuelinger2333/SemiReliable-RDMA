#!/usr/bin/env bash
# tc netem helper for the experiment NIC.
#
# ⚠️  IMPORTANT — TC NETEM DOES NOT AFFECT RoCE / RDMA TRAFFIC ON CX-6 Lx.
#
# Verified empirically on c240g5 + CX-6 Lx 25 GbE on 2026-04-23:
#   - With "tc qdisc add dev <iface> root netem loss 1%" active:
#       ping  -c 100  → 1% packet loss (kernel TCP/UDP path: drops as expected)
#       ib_write_bw -s 65536 → 24.39 Gbps (identical to no-netem baseline)
#
# Reason: ConnectX-6 Lx (and the entire mlx5 ASIC family) drives RDMA
# packets straight from the QP doorbell ring into the NIC TX engine.
# They never traverse the Linux netdev sk_buff path that tc qdiscs sit on,
# so any tc rule on the netdev is a no-op for RoCE QP traffic.
#
# Practical implication for Stage B's RQ6 (RC-Lossy / UD-Naive / SemiRDMA
# under 1%/3%/5% per-packet loss): we *cannot* use this script to make
# the wire lossy.  See docs/phase3/rq6-loss-injection-strategy.md for the
# application-layer loss injection that we use instead.
#
# This script is still useful for:
#   - sanity checking tc behaves on the kernel path (ping / TCP iperf)
#   - introducing latency / loss for any non-RDMA control-plane traffic
#     used by torchrun rendezvous (DDP backend = gloo over TCP)
#
# Usage:
#   bash scripts/cloudlab/netem_inject.sh on  1%       # apply 1% loss
#   bash scripts/cloudlab/netem_inject.sh on  3% 100ms # 3% loss, 100ms delay
#   bash scripts/cloudlab/netem_inject.sh off          # remove all qdisc
#   bash scripts/cloudlab/netem_inject.sh status       # show current qdisc

set -uo pipefail

ACTION="${1:-status}"

if [ -z "${IFACE:-}" ]; then
    IFACE=$(ip -br link show \
        | awk '$1 ~ /^enp[0-9]+s0f[0-9]+np[0-9]+/ && $2=="UP" {print $1; exit}')
fi
if [ -z "$IFACE" ]; then
    echo "ERR: cannot auto-detect Mellanox experiment netdev (set IFACE=...)" >&2
    exit 1
fi

case "$ACTION" in
    on)
        LOSS="${2:-1%}"
        DELAY="${3:-}"
        sudo tc qdisc del dev "$IFACE" root 2>/dev/null
        if [ -n "$DELAY" ]; then
            sudo tc qdisc add dev "$IFACE" root netem loss "$LOSS" delay "$DELAY"
        else
            sudo tc qdisc add dev "$IFACE" root netem loss "$LOSS"
        fi
        echo "applied netem loss=$LOSS ${DELAY:+delay=$DELAY} on $IFACE"
        echo "⚠️  reminder: this affects kernel-path traffic only, NOT RoCE QPs"
        ;;
    off)
        sudo tc qdisc del dev "$IFACE" root 2>/dev/null
        echo "cleared qdisc on $IFACE"
        ;;
    status)
        echo "qdisc on $IFACE:"
        sudo tc qdisc show dev "$IFACE" | head -3
        ;;
    *)
        echo "Usage: $0 {on <loss%> [delay] | off | status}" >&2
        exit 2
        ;;
esac
