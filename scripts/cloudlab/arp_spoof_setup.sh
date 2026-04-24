#!/usr/bin/env bash
# Phase 4 · ARP-spoofing helper for XDP middlebox "bump in the wire".
#
# Why
# ---
# amd186 (middlebox) lives on the same 10.10.1.0/24 L2 subnet as the two
# training nodes (amd196 + amd203).  Normal switching means amd196↔amd203
# packets bypass amd186 entirely.  To force them through the middlebox at L2
# without reprovisioning the CloudLab profile, we populate static ARP entries
# on the two peers such that each peer's ARP table claims the OTHER peer's
# IP lives at amd186's MAC.
#
# After setup:
#   amd196 ARP table:   10.10.1.1  → amd186's experiment-NIC MAC
#   amd203 ARP table:   10.10.1.3  → amd186's experiment-NIC MAC
#
# Packets from amd196 to 10.10.1.1 now physically land on amd186's NIC.
# The XDP program there (see middlebox_setup.sh + xdp_dropbox/) rewrites
# dst_mac to the real amd203 MAC and XDP_TX's it back out the same port.
# amd203 receives it as if it came from amd196 directly.  Reverse path
# symmetric.
#
# This script is driven from the receiver node (amd203) — the same node
# run_p1_matrix.sh uses — so the SSH identity chain is the same.
#
# Usage (from amd203):
#   MIDDLEBOX_MAC=0c:42:a1:e2:a6:a8 \
#     bash scripts/cloudlab/arp_spoof_setup.sh apply
#   bash scripts/cloudlab/arp_spoof_setup.sh status
#   bash scripts/cloudlab/arp_spoof_setup.sh restore
#
# The middlebox MAC is printed by middlebox_setup.sh bootstrap and also
# visible via `ip -br link show enp65s0f0np0` on amd186.

set -uo pipefail

MODE="${1:-status}"
shift || true

# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------
# The two training peers on the experiment LAN.
PEER_A_HOST="${PEER_A_HOST:-}"           # e.g. chen123@amd203 (receiver).  Empty = self.
PEER_A_IP="${PEER_A_IP:-10.10.1.1}"
PEER_B_HOST="${PEER_B_HOST:-chen123@10.10.1.3}"   # amd196 (sender)
PEER_B_IP="${PEER_B_IP:-10.10.1.3}"
PEER_IFACE="${PEER_IFACE:-enp65s0f0np0}" # experiment-NIC name on peers

# The middlebox (amd186) MAC that both peers will point to.
MIDDLEBOX_MAC="${MIDDLEBOX_MAC:-}"

info() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
err()  { printf '[%s] ERR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Run a command on a peer, or locally if PEER_*_HOST is empty.
peer_run() {
    local host="$1"; shift
    if [ -z "$host" ]; then
        bash -c "$*"
    else
        ssh "$host" "$*"
    fi
}

require_mac() {
    if [ -z "$MIDDLEBOX_MAC" ]; then
        err "MIDDLEBOX_MAC not set — run on amd186:  ip -br link show $PEER_IFACE"
        exit 2
    fi
}

# -------------------------------------------------------------------------
# apply — write static ARP entries on both peers
# -------------------------------------------------------------------------
do_apply() {
    require_mac
    info "applying ARP spoof: both peers → $MIDDLEBOX_MAC for the other peer's IP"

    # On PEER_A (the receiver, often local self): PEER_B_IP → MIDDLEBOX_MAC
    info "  $PEER_A_HOST (=${PEER_A_HOST:-self}): arp -s $PEER_B_IP $MIDDLEBOX_MAC"
    peer_run "$PEER_A_HOST" "sudo ip neigh replace $PEER_B_IP lladdr $MIDDLEBOX_MAC nud permanent dev $PEER_IFACE" \
        || { err "peer A apply failed"; exit 3; }

    # On PEER_B (the sender): PEER_A_IP → MIDDLEBOX_MAC
    info "  $PEER_B_HOST: arp -s $PEER_A_IP $MIDDLEBOX_MAC"
    peer_run "$PEER_B_HOST" "sudo ip neigh replace $PEER_A_IP lladdr $MIDDLEBOX_MAC nud permanent dev $PEER_IFACE" \
        || { err "peer B apply failed"; exit 4; }

    info "✓ ARP spoof in place — verify with 'bash $0 status'"
}

# -------------------------------------------------------------------------
# status — read both peers' current ARP entries for the relevant IPs
# -------------------------------------------------------------------------
do_status() {
    info "peer A (${PEER_A_HOST:-self}): ARP for $PEER_B_IP"
    peer_run "$PEER_A_HOST" "ip neigh show $PEER_B_IP" || true
    info "peer B ($PEER_B_HOST): ARP for $PEER_A_IP"
    peer_run "$PEER_B_HOST" "ip neigh show $PEER_A_IP" || true
}

# -------------------------------------------------------------------------
# restore — remove the static entries so normal ARP resolution takes over
# -------------------------------------------------------------------------
do_restore() {
    info "removing static ARP entries"
    peer_run "$PEER_A_HOST" "sudo ip neigh del $PEER_B_IP dev $PEER_IFACE" || true
    peer_run "$PEER_B_HOST" "sudo ip neigh del $PEER_A_IP dev $PEER_IFACE" || true
    info "✓ restored.  Normal ARP resolution will rediscover the real peer MACs."
}

# -------------------------------------------------------------------------
# dispatch
# -------------------------------------------------------------------------
case "$MODE" in
    apply)   do_apply   ;;
    status)  do_status  ;;
    restore) do_restore ;;
    *)
        cat >&2 <<USAGE
Usage: $0 {apply | status | restore}

  apply    write static ARP entries on both peers → middlebox MAC
           requires MIDDLEBOX_MAC env var (from amd186's ip -br link)
  status   show current ARP entries for the two peer IPs on both peers
  restore  remove the static entries, let normal ARP take over

Env overrides:
  PEER_A_HOST, PEER_A_IP (default: self, 10.10.1.1 — receiver)
  PEER_B_HOST, PEER_B_IP (default: chen123@10.10.1.3, 10.10.1.3 — sender)
  PEER_IFACE             (default: enp65s0f0np0)
  MIDDLEBOX_MAC          (required for apply)
USAGE
        exit 2
        ;;
esac
