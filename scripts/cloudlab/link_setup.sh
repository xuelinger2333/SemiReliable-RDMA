#!/usr/bin/env bash
# Re-apply lossy-RoCE link configuration on a CloudLab CX-6 Lx node.
#
# Each c240g5 reboot resets the experiment NIC to default (PFC RX/TX on,
# MTU 1500), which contradicts the Stage B "lossy RoCE, no PFC, jumbo"
# assumption.  Run this script once after every reboot, on each node, to
# bring the link back to the baseline used in
#   docs/phase3/stage-b-hardware-notes.md §8.1
#
# Auto-detects the experiment netdev as the only UP non-control interface
# matching enp*np* (Mellanox kernel naming).  Override via IFACE=...
#
# Usage:
#   bash scripts/cloudlab/link_setup.sh           # auto-detect IFACE
#   IFACE=enp94s0f0np0 bash scripts/cloudlab/link_setup.sh
#   MTU=1500 bash scripts/cloudlab/link_setup.sh  # opt back to non-jumbo

set -uo pipefail

MTU="${MTU:-9000}"

if [ -z "${IFACE:-}" ]; then
    IFACE=$(ip -br link show \
        | awk '$1 ~ /^enp[0-9]+s0f[0-9]+np[0-9]+/ && $2=="UP" {print $1; exit}')
fi
if [ -z "$IFACE" ]; then
    echo "ERR: could not auto-detect Mellanox experiment netdev (no enpXsYfZnpW UP)" >&2
    ip -br link show >&2
    exit 1
fi

echo "=== link_setup on IFACE=$IFACE (MTU=$MTU, PFC=off) ==="

sudo ethtool -A "$IFACE" rx off tx off
sudo ip link set "$IFACE" mtu "$MTU"

cur_mtu=$(ip -o link show "$IFACE" | grep -oP 'mtu \K[0-9]+')
cur_pfc=$(sudo ethtool -a "$IFACE" | awk '/^(RX|TX):/ {printf "%s=%s ", $1, $2}')
echo "  $IFACE  mtu=$cur_mtu  pfc=$cur_pfc"

if [ "$cur_mtu" != "$MTU" ]; then
    echo "WARN: MTU set to $MTU but readback shows $cur_mtu" >&2
    exit 2
fi

# Print the auto-detected RDMA device for downstream callers
if command -v rdma >/dev/null 2>&1; then
    DEV=$(bash "$(dirname "$0")/detect_rdma_dev.sh" "$IFACE" 2>/dev/null || echo unknown)
    echo "  RDMA dev for $IFACE: $DEV"
fi
