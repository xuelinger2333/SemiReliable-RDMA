#!/usr/bin/env bash
# Phase 4 · ARP-spoofing helper for XDP middlebox "bump in the wire".
#
# Why
# ---
# Middlebox (amd264 since 2026-04-28; amd186 prior) lives on the same
# 10.10.1.0/24 L2 subnet as the two training nodes (amd247 receiver +
# amd245 sender; amd203 + amd196 prior).  Normal switching means peer↔peer
# packets bypass the middlebox entirely.  To force them through the middlebox
# at L2 without reprovisioning the CloudLab profile, we populate static ARP
# entries on the two peers such that each peer's ARP table claims the OTHER
# peer's IP lives at the middlebox's MAC.
#
# After setup (amd247/amd245/amd264 cluster):
#   amd247 (receiver) ARP table:  10.10.1.2  → amd264's eno34np1 MAC
#   amd245 (sender)   ARP table:  10.10.1.1  → amd264's eno34np1 MAC
#
# Packets from amd245 to 10.10.1.1 now physically land on amd264's NIC.
# The XDP program there (see middlebox_setup.sh + xdp_dropbox/) rewrites
# dst_mac to the real amd247 MAC and XDP_TX's it back out the same port.
# amd247 receives it as if it came from amd245 directly.  Reverse path
# symmetric.
#
# This script is driven from the receiver node (amd247) — the same node
# run_p1_matrix.sh uses — so the SSH identity chain is the same.
#
# Usage (from amd247):
#   MIDDLEBOX_MAC=04:3f:72:b2:c2:09 \
#     bash scripts/cloudlab/arp_spoof_setup.sh apply
#   bash scripts/cloudlab/arp_spoof_setup.sh status
#   bash scripts/cloudlab/arp_spoof_setup.sh restore
#
# The middlebox MAC is printed by middlebox_setup.sh bootstrap and also
# visible via `ip -br link show eno34np1` on amd264 (amd-class CloudLab
# nodes use eno34np1 as the experiment-LAN port; older d7525/d6515 used
# enp65s0f0np0).
#
# IPv4 vs IPv6 note
# -----------------
# RoCE v2 GID index 1 uses the peer's IPv6 link-local address, and mlx5 HW
# derives the dst MAC from that GID *without* consulting the kernel neigh
# table — so IPv4 ARP spoof alone is not enough.  Two paths to handle this:
#
#   a. Force training code + ib_write_bw to use GID index 3 (IPv4-mapped
#      RoCE v2 — ::ffff:10.10.1.x).  That path DOES use kernel ARP → IPv4
#      ARP spoof works.  This is what run_p1_matrix.sh does; it passes
#      transport_cfg.gid_index=3 on the experiment and -x 3 on ib_write_bw.
#
#   b. Additionally spoof IPv6 link-local neighbor entries (apply-v6
#      subcommand).  Defensive — covers rdma_cm / older stacks that don't
#      honor the gid_index override.

set -uo pipefail

MODE="${1:-status}"
shift || true

# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------
# The two training peers on the experiment LAN.
PEER_A_HOST="${PEER_A_HOST:-}"                    # e.g. chen123@amd247 (receiver). Empty = self.
PEER_A_IP="${PEER_A_IP:-10.10.1.1}"               # amd247 (receiver)
PEER_B_HOST="${PEER_B_HOST:-chen123@10.10.1.2}"   # amd245 (sender)
PEER_B_IP="${PEER_B_IP:-10.10.1.2}"               # amd245 (sender)
PEER_IFACE="${PEER_IFACE:-eno34np1}"              # experiment-NIC name on amd-class nodes
                                                  # (override to enp65s0f0np0 for old d7525/d6515)

# The middlebox MAC (amd264 eno34np1: 04:3f:72:b2:c2:09) that both peers will point to.
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
# apply — write static IPv4 ARP entries on both peers
# -------------------------------------------------------------------------
do_apply() {
    require_mac
    info "applying IPv4 ARP spoof: both peers → $MIDDLEBOX_MAC for the other peer's IP"

    # On PEER_A (the receiver, often local self): PEER_B_IP → MIDDLEBOX_MAC
    info "  $PEER_A_HOST (=${PEER_A_HOST:-self}): arp -s $PEER_B_IP $MIDDLEBOX_MAC"
    peer_run "$PEER_A_HOST" "sudo ip neigh replace $PEER_B_IP lladdr $MIDDLEBOX_MAC nud permanent dev $PEER_IFACE" \
        || { err "peer A apply failed"; exit 3; }

    # On PEER_B (the sender): PEER_A_IP → MIDDLEBOX_MAC
    info "  $PEER_B_HOST: arp -s $PEER_A_IP $MIDDLEBOX_MAC"
    peer_run "$PEER_B_HOST" "sudo ip neigh replace $PEER_A_IP lladdr $MIDDLEBOX_MAC nud permanent dev $PEER_IFACE" \
        || { err "peer B apply failed"; exit 4; }

    info "✓ IPv4 ARP spoof in place — verify with 'bash $0 status'"
    info "  If RoCE still bypasses middlebox, also run: bash $0 apply-v6"
}

# -------------------------------------------------------------------------
# apply-v6 — additionally spoof IPv6 link-local neighbor entries
# -------------------------------------------------------------------------
do_apply_v6() {
    require_mac
    info "applying IPv6 neighbor spoof on link-local addresses"

    # Peer link-locals are EUI-64 derived from each peer's MAC.  Discover
    # them from /sys/class/net/<iface>/address on the respective peer
    # rather than hardcoding — avoids nested-quoting hell with ssh+awk.
    local peer_a_mac peer_b_mac peer_a_ll peer_b_ll
    peer_a_mac=$(peer_run "$PEER_A_HOST" "cat /sys/class/net/$PEER_IFACE/address")
    peer_b_mac=$(peer_run "$PEER_B_HOST" "cat /sys/class/net/$PEER_IFACE/address")
    peer_a_ll=$(mac_to_ll6 "$peer_a_mac")
    peer_b_ll=$(mac_to_ll6 "$peer_b_mac")
    info "  peer A LL = $peer_a_ll  (from mac $peer_a_mac)"
    info "  peer B LL = $peer_b_ll  (from mac $peer_b_mac)"

    # On A: B's link-local → middlebox MAC
    peer_run "$PEER_A_HOST" "sudo ip -6 neigh replace $peer_b_ll lladdr $MIDDLEBOX_MAC nud permanent dev $PEER_IFACE" \
        || { err "peer A v6 spoof failed"; exit 5; }
    # On B: A's link-local → middlebox MAC
    peer_run "$PEER_B_HOST" "sudo ip -6 neigh replace $peer_a_ll lladdr $MIDDLEBOX_MAC nud permanent dev $PEER_IFACE" \
        || { err "peer B v6 spoof failed"; exit 6; }
    info "✓ IPv6 link-local neighbors spoofed"
}

# Compute the IPv6 link-local (fe80::/10) address from a MAC via EUI-64.
mac_to_ll6() {
    local mac="$1"
    python3 -c "
mac = '$mac'.lower().split(':')
# Flip universal/local bit of first byte, then insert ff:fe between bytes 3 and 4.
b = [int(x, 16) for x in mac]
b[0] ^= 0x02
eui64 = [b[0], b[1], b[2], 0xff, 0xfe, b[3], b[4], b[5]]
# format as colon-separated 16-bit groups
groups = [(eui64[i] << 8) | eui64[i+1] for i in range(0, 8, 2)]
ll = 'fe80::' + ':'.join(f'{g:x}' for g in groups).lstrip(':')
# Drop leading zeros in each group — Linux canonicalizes that way.
print(ll)
"
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
    info "removing static IPv4 ARP entries"
    peer_run "$PEER_A_HOST" "sudo ip neigh del $PEER_B_IP dev $PEER_IFACE" 2>/dev/null || true
    peer_run "$PEER_B_HOST" "sudo ip neigh del $PEER_A_IP dev $PEER_IFACE" 2>/dev/null || true
    # Also clean v6 spoofs if any.
    local peer_a_mac peer_b_mac peer_a_ll peer_b_ll
    peer_a_mac=$(peer_run "$PEER_A_HOST" "cat /sys/class/net/$PEER_IFACE/address" 2>/dev/null)
    peer_b_mac=$(peer_run "$PEER_B_HOST" "cat /sys/class/net/$PEER_IFACE/address" 2>/dev/null)
    if [ -n "$peer_a_mac" ] && [ -n "$peer_b_mac" ]; then
        peer_a_ll=$(mac_to_ll6 "$peer_a_mac")
        peer_b_ll=$(mac_to_ll6 "$peer_b_mac")
        peer_run "$PEER_A_HOST" "sudo ip -6 neigh del $peer_b_ll dev $PEER_IFACE" 2>/dev/null || true
        peer_run "$PEER_B_HOST" "sudo ip -6 neigh del $peer_a_ll dev $PEER_IFACE" 2>/dev/null || true
    fi
    info "✓ restored.  Normal ARP/ND resolution will rediscover the real peer MACs."
}

# -------------------------------------------------------------------------
# dispatch
# -------------------------------------------------------------------------
case "$MODE" in
    apply)    do_apply    ;;
    apply-v6) do_apply_v6 ;;
    status)   do_status   ;;
    restore)  do_restore  ;;
    *)
        cat >&2 <<USAGE
Usage: $0 {apply | apply-v6 | status | restore}

  apply      write static IPv4 ARP entries on both peers → middlebox MAC
             requires MIDDLEBOX_MAC env var (from amd186's ip -br link)
  apply-v6   additionally spoof IPv6 link-local neighbor entries
             needed when RoCE v2 uses GID idx 1 (IPv6 link-local) —
             unnecessary if training is configured with gid_index=3
  status     show current ARP entries for the two peer IPs on both peers
  restore    remove the static entries, let normal ARP/ND take over

Env overrides:
  PEER_A_HOST, PEER_A_IP (default: self, 10.10.1.1 — amd247 receiver)
  PEER_B_HOST, PEER_B_IP (default: chen123@10.10.1.2, 10.10.1.2 — amd245 sender)
  PEER_IFACE             (default: eno34np1 — amd-class; override to enp65s0f0np0 for d7525)
  MIDDLEBOX_MAC          (required for apply and apply-v6)
USAGE
        exit 2
        ;;
esac
