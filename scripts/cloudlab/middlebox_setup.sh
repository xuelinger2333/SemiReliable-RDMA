#!/usr/bin/env bash
# Phase 4 · XDP loss-injection middlebox — bootstrap + lifecycle wrapper.
#
# Why XDP instead of DPDK
# -----------------------
# amd186 has a single experiment-LAN CX-5 port (enp65s0f0np0).  A full DPDK
# forwarder on this node would need hugepages + VFIO bind + ~300 lines of C,
# with no measurable benefit for training's ~30 Kpps AllReduce rate or even
# uc_blaster's ~1.5 Mpps calibration.  XDP in DRV mode on mlx5 gives us
# ≥20 Mpps per core with ~100 lines of eBPF C, no hugepages, no VFIO, no
# library install beyond clang + libbpf-dev + bpftool (all in Ubuntu 22.04).
#
# Design: ARP-spoof-based "bump in the wire"
# ------------------------------------------
# 1. amd196 and amd203 are both on 10.10.1.0/24 via a shared CloudLab switch,
#    so traffic between them normally bypasses amd186.
# 2. We populate static ARP entries on amd196 and amd203 (see
#    arp_spoof_setup.sh) that map each peer's IP → amd186's MAC.  Now every
#    amd196↔amd203 packet lands on amd186's NIC at L2.
# 3. XDP program on amd186 inspects each packet; drops UDP:4791 (RoCE v2)
#    with configured Bernoulli probability, rewrites dst_mac to the real
#    peer MAC, XDP_TX back out the same port.  Non-RoCE (ARP, ICMP, non-UDP,
#    UDP on other ports) returns XDP_PASS — kernel keeps handling.
# 4. src_mac is preserved end-to-end so receiving kernel/NIC/QP see packets
#    as if from the original sender.  RoCE identifies endpoints by QPN, not
#    MAC, so the BTH header is untouched.
#
# Control plane
# -------------
#   bootstrap          once per node — installs toolchain, builds .bpf.o
#   start <rate>       attach XDP to $IFACE with initial drop rate
#   set-rate <rate>    live tweak via bpftool map update (no restart)
#   stop               detach XDP, unpin maps
#   status             RUNNING/stopped + rate + counter sums
#   stats              dump per-map counters
#   logs               kernel dmesg related to bpf (rare)
#
# Usage:
#   # Once per fresh middlebox node:
#   IFACE=enp65s0f0np0 PEER_A_IP=10.10.1.1 PEER_A_MAC=... \
#     PEER_B_IP=10.10.1.3 PEER_B_MAC=... \
#     sudo bash scripts/cloudlab/middlebox_setup.sh bootstrap
#
#   # Start forwarder with 0% drop (transparent):
#   sudo bash scripts/cloudlab/middlebox_setup.sh start 0
#
#   # Live-set 1% drop (matrix loop does this between cells):
#   sudo bash scripts/cloudlab/middlebox_setup.sh set-rate 0.01
#
#   # Detach:
#   sudo bash scripts/cloudlab/middlebox_setup.sh stop

set -uo pipefail

MODE="${1:-status}"
shift || true

# -------------------------------------------------------------------------
# Config (env-overridable)
# -------------------------------------------------------------------------
REPO="${REPO:-$HOME/SemiRDMA}"
XDP_DIR="${XDP_DIR:-$REPO/scripts/cloudlab/xdp_dropbox}"
BPF_OBJ="${BPF_OBJ:-$XDP_DIR/xdp_dropbox.bpf.o}"

# The interface we attach XDP to.  On amd186 this is enp65s0f0np0.
IFACE="${IFACE:-enp65s0f0np0}"

# XDP attach mode.  drv = NIC driver (fast path on mlx5); skb = generic
# (kernel receive path, slower but works on any driver).  Default drv;
# caller can set XDP_MODE=skb if drv fails.
XDP_MODE="${XDP_MODE:-drv}"

# Where pinned BPF maps + prog live.  Any bpftool invocation uses these.
PIN_ROOT="${PIN_ROOT:-/sys/fs/bpf/xdp_dropbox}"

# Peer table: IP → MAC.  Used by `start` to populate peer_macs map.
# Typically amd196 + amd203, overridable at bootstrap or start time.
PEER_A_IP="${PEER_A_IP:-10.10.1.1}"   # amd203
PEER_A_MAC="${PEER_A_MAC:-}"          # must be set by caller
PEER_B_IP="${PEER_B_IP:-10.10.1.3}"   # amd196
PEER_B_MAC="${PEER_B_MAC:-}"          # must be set by caller

LOGDIR="${LOGDIR:-/tmp/xdp_dropbox_logs}"
mkdir -p "$LOGDIR"

hdr()  { printf '\n========== %s ==========\n' "$*"; }
info() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
err()  { printf '[%s] ERR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "$MODE needs root — rerun with sudo"
        exit 2
    fi
}

require_peer_macs() {
    if [ -z "$PEER_A_MAC" ] || [ -z "$PEER_B_MAC" ]; then
        err "PEER_A_MAC and PEER_B_MAC must be set (see 'ip -br link' on the training nodes)"
        err "  Example: PEER_A_IP=10.10.1.1 PEER_A_MAC=0c:42:a1:b3:c4:d5 \\"
        err "           PEER_B_IP=10.10.1.3 PEER_B_MAC=0c:42:a1:b3:c4:e6 \\"
        err "           sudo bash $0 start <rate>"
        exit 3
    fi
}

# Convert a decimal drop probability (0.01) to ppm (10000).
# Accepts either decimal or plain integer ppm (if ≥1 we assume user already
# passed ppm, if <1 we convert).  Handles "0" cleanly.
rate_to_ppm() {
    local raw="$1"
    python3 -c "
r = float('$raw')
if r < 0 or r > 1:
    print('ERR: drop rate must be in [0.0, 1.0]', flush=True)
    import sys; sys.exit(1)
print(int(round(r * 1_000_000)))
"
}

# BPF value syntax for a __u32: 4 little-endian bytes.
u32_le_bytes() {
    local n="$1"
    python3 -c "
n = int('$n')
b = n.to_bytes(4, 'little')
print(' '.join('0x%02x' % x for x in b))
"
}

# BPF value for 6-byte MAC (as-is, high-to-low).
mac_bytes() {
    local mac="$1"
    echo "$mac" | tr ':' ' '
}

# IP address as 4 bytes network-byte-order (= big-endian, but bpftool expects
# literal bytes).  For a key-by-value lookup BPF stores __be32 — on little-
# endian host we write bytes in NETWORK order (high byte first in the value
# area), but bpftool map keys are given in the order they sit in memory,
# which for a __be32 key on LE host means LAST byte first in the TAG.
# Simpler: convert IP to __be32 representation the same way kernel sees it.
ip_key_bytes() {
    local ip="$1"
    # bpftool wants the key bytes in host-memory order.  __be32 stored in
    # a BPF map means the key is literally the wire bytes (network order),
    # because maps don't byte-swap.  We pass them high-to-low.
    echo "$ip" | awk -F. '{printf "0x%02x 0x%02x 0x%02x 0x%02x\n", $1, $2, $3, $4}'
}

# -------------------------------------------------------------------------
# bootstrap — install toolchain, build BPF object, prepare pin directory
# -------------------------------------------------------------------------
do_bootstrap() {
    require_root
    hdr "bootstrap step 1/3 — apt packages (clang, libbpf-dev, bpftool)"
    apt-get update -qq
    apt-get install -y -qq clang llvm libbpf-dev linux-tools-common \
        linux-tools-$(uname -r) || true   # bpftool often comes via linux-tools
    # Ubuntu 22.04 also ships a standalone bpftool package on some releases.
    if ! command -v bpftool >/dev/null 2>&1; then
        apt-get install -y -qq bpftool || {
            err "bpftool not found after apt install — check your Ubuntu release"
            exit 1
        }
    fi
    info "toolchain: clang=$(clang --version | head -1 | awk '{print $NF}'), bpftool=$(bpftool --version | head -1)"

    hdr "bootstrap step 2/3 — build BPF object"
    [ -d "$XDP_DIR" ] || { err "$XDP_DIR missing — did you scp the xdp_dropbox/ tree?"; exit 2; }
    ( cd "$XDP_DIR" && make clean && make ) || { err "make failed"; exit 3; }
    info "built $BPF_OBJ"

    hdr "bootstrap step 3/3 — mount bpffs if needed"
    mountpoint -q /sys/fs/bpf || mount -t bpf bpffs /sys/fs/bpf
    info "bootstrap done.  Next:  sudo $0 start 0"
}

# -------------------------------------------------------------------------
# start — load + attach XDP, populate peer_macs, set initial drop rate
# -------------------------------------------------------------------------
do_start() {
    require_root
    require_peer_macs
    local rate="${1:-0}"

    if [ -d "$PIN_ROOT" ] && [ "$(ls -A "$PIN_ROOT" 2>/dev/null)" ]; then
        err "$PIN_ROOT already populated — run 'stop' first"
        exit 4
    fi
    [ -e "$BPF_OBJ" ] || { err "$BPF_OBJ missing — run bootstrap"; exit 5; }

    hdr "start — loading + pinning maps"
    mkdir -p "$PIN_ROOT"
    bpftool prog loadall "$BPF_OBJ" "$PIN_ROOT" type xdp \
        || { err "bpftool prog loadall failed"; exit 6; }

    hdr "start — populating peer_macs map"
    # key format: 4 bytes IP in network order; value: 6 bytes MAC
    local key_a val_a key_b val_b
    key_a=$(ip_key_bytes "$PEER_A_IP")
    val_a=$(mac_bytes "$PEER_A_MAC")
    key_b=$(ip_key_bytes "$PEER_B_IP")
    val_b=$(mac_bytes "$PEER_B_MAC")
    bpftool map update pinned "$PIN_ROOT/peer_macs" key hex $key_a value hex $val_a
    bpftool map update pinned "$PIN_ROOT/peer_macs" key hex $key_b value hex $val_b
    info "peer_macs: $PEER_A_IP → $PEER_A_MAC   $PEER_B_IP → $PEER_B_MAC"

    hdr "start — setting initial drop rate"
    do_set_rate "$rate"

    hdr "start — attaching XDP to $IFACE (mode=$XDP_MODE)"
    bpftool net attach xdp"${XDP_MODE:+_$XDP_MODE}" pinned "$PIN_ROOT/xdp_dropbox" dev "$IFACE" \
        || { err "bpftool net attach failed (try XDP_MODE=skb for generic-mode fallback)"; exit 7; }
    info "XDP attached.  Check: ip -d link show dev $IFACE | grep xdp"
}

# -------------------------------------------------------------------------
# set-rate — live tweak drop_rate_map[0]
# -------------------------------------------------------------------------
do_set_rate() {
    require_root
    local raw="${1:-0}"
    local ppm
    ppm=$(rate_to_ppm "$raw") || { err "bad rate: $raw"; exit 8; }

    if [ ! -e "$PIN_ROOT/drop_rate_map" ]; then
        err "XDP not loaded — run start first"
        exit 9
    fi

    local val
    val=$(u32_le_bytes "$ppm")
    # key is __u32 = 0 (4 LE bytes)
    bpftool map update pinned "$PIN_ROOT/drop_rate_map" \
        key hex 0x00 0x00 0x00 0x00 value hex $val
    info "set-rate $raw → $ppm ppm"
}

# -------------------------------------------------------------------------
# stop — detach XDP + remove pinned maps/prog
# -------------------------------------------------------------------------
do_stop() {
    require_root
    if [ ! -d "$PIN_ROOT" ]; then
        info "no pin root — nothing to stop"
        return 0
    fi
    # Detach XDP from iface (ignore errors — may already be detached)
    bpftool net detach xdp"${XDP_MODE:+_$XDP_MODE}" dev "$IFACE" 2>/dev/null || true
    # Fallback kernel-level detach
    ip link set dev "$IFACE" xdp off 2>/dev/null || true
    # Remove all pinned objects
    rm -rf "$PIN_ROOT"
    info "stopped.  XDP detached from $IFACE, maps unpinned."
}

# -------------------------------------------------------------------------
# status — one-line state + current rate + counters
# -------------------------------------------------------------------------
do_status() {
    echo "=== xdp_dropbox status on $(hostname -s) ==="
    if [ -d "$PIN_ROOT" ] && [ -e "$PIN_ROOT/xdp_dropbox" ]; then
        echo "  state : RUNNING"
        echo "  iface : $IFACE  (mode=$XDP_MODE)"
    else
        echo "  state : stopped"
        return 0
    fi
    if [ -e "$PIN_ROOT/drop_rate_map" ]; then
        local ppm
        ppm=$(bpftool map dump pinned "$PIN_ROOT/drop_rate_map" 2>/dev/null \
              | python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    # BPF dump format: list of {key: bytes, value: bytes}
    v = data[0]['value']
    n = int.from_bytes(bytes(int(b, 16) if isinstance(b, str) else b for b in v), 'little')
    print(n)
except Exception as e:
    print('?')
" 2>/dev/null || echo '?')
        echo "  rate  : ${ppm} ppm  ($(python3 -c "print(float('$ppm')/10000)" 2>/dev/null || echo '?')%)"
    fi
    do_stats_brief
}

do_stats_brief() {
    [ -e "$PIN_ROOT/stats_map" ] || return 0
    python3 <<'PYEOF' 2>/dev/null || echo "  (stats: failed to read)"
import json, subprocess, sys
try:
    raw = subprocess.check_output(['bpftool', '-j', 'map', 'dump', 'pinned', '/sys/fs/bpf/xdp_dropbox/stats_map'])
    data = json.loads(raw)
except Exception as e:
    print(f"  (stats: {e})")
    sys.exit(0)

sums = [0, 0, 0, 0]
names = ['rx_total', 'rx_roce', 'dropped', 'tx_ok']
for entry in data:
    key_bytes = bytes(int(b, 16) if isinstance(b, str) else b for b in entry['key'])
    k = int.from_bytes(key_bytes, 'little')
    if k >= len(sums):
        continue
    # percpu value: list of per-cpu dicts {cpu, value}
    for percpu in entry.get('values', []):
        v_bytes = bytes(int(b, 16) if isinstance(b, str) else b for b in percpu['value'])
        sums[k] += int.from_bytes(v_bytes, 'little')

for k, name in enumerate(names):
    print(f"  {name:10s}: {sums[k]}")

if sums[1] > 0:
    pct = 100.0 * sums[2] / sums[1]
    print(f"  drop_pct  : {pct:.4f}%  (dropped/rx_roce)")
PYEOF
}

do_stats() {
    do_stats_brief
}

do_logs() {
    # XDP prints verifier log + drops/errors to dmesg when loading.
    dmesg | grep -iE "bpf|xdp" | tail -20
}

# -------------------------------------------------------------------------
# dispatch
# -------------------------------------------------------------------------
case "$MODE" in
    bootstrap)  do_bootstrap          ;;
    start)      do_start    "$@"      ;;
    set-rate)   do_set_rate "$@"      ;;
    stop)       do_stop               ;;
    status)     do_status             ;;
    stats)      do_stats              ;;
    logs)       do_logs               ;;
    *)
        cat >&2 <<USAGE
Usage: sudo $0 {bootstrap | start <rate> | set-rate <rate> | stop | status | stats | logs}

  bootstrap   one-shot setup (apt clang + libbpf-dev + bpftool; build BPF obj)
  start       load + attach XDP + populate peer_macs + set initial rate
              needs PEER_A_IP/MAC and PEER_B_IP/MAC env vars
  set-rate    live tweak — writes drop_rate_map via bpftool (no restart)
  stop        detach XDP, unpin maps/prog
  status      one-line state + current rate + counter sums
  stats       full per-CPU counter dump
  logs        tail of dmesg entries touching bpf/xdp
USAGE
        exit 2
        ;;
esac
