#!/usr/bin/env bash
# Phase 4 · DPDK loss-injection middlebox — bootstrap + lifecycle wrapper.
#
# Why this exists
# ---------------
# RoCE bypasses the kernel on the endpoints (mlx5 NIC DMAs straight to/from
# the application MR), so tc/netem on the training nodes is a no-op — see
# docs/phase3/history/rq6-loss-injection-strategy.md.  The only realistic way
# to inject *wire-level* packet loss is to route the RoCE path through a
# third machine that drops packets on the wire before they reach the
# destination NIC.  DPDK poll-mode drivers let that third machine forward at
# 25 GbE line rate while a single Bernoulli check decides whether each
# packet is dropped or passed.
#
# This script is the control plane for the forwarder.  It does two things:
#
#   bootstrap    install DPDK + hugepages + PMD + build the dpdk_dropbox
#                binary once per node
#   lifecycle    start / set-rate / stop / status / logs the forwarder
#
# The build step (bootstrap) is idempotent.  The lifecycle subcommands are
# what the matrix runner uses via ssh between drop-rate sweeps:
#
#   ssh "$MIDDLEBOX_HOST" "bash ... middlebox_setup.sh set-rate 0.01"
#
# set-rate is a live tweak: it writes /tmp/dropbox_rate and sends SIGUSR1 to
# the running forwarder, which re-reads the rate without restarting (so rx /
# tx counters stay continuous across a matrix sweep).
#
# Usage:
#   bash scripts/cloudlab/middlebox_setup.sh bootstrap         # once per node
#   bash scripts/cloudlab/middlebox_setup.sh start <rate>      # start forwarder
#   bash scripts/cloudlab/middlebox_setup.sh set-rate <rate>   # live tweak
#   bash scripts/cloudlab/middlebox_setup.sh stop
#   bash scripts/cloudlab/middlebox_setup.sh status
#   bash scripts/cloudlab/middlebox_setup.sh logs [-n 40]
#
# <rate> is a decimal drop probability — e.g. 0.01 = 1% Bernoulli drop.  Only
# UDP:4791 (RoCE v2) packets are candidates for drop; everything else (ARP,
# SSH to mgmt NIC, ICMP, etc.) is forwarded untouched.

set -uo pipefail

MODE="${1:-status}"
shift || true

REPO="${REPO:-$HOME/SemiRDMA}"
DPDK_DIR="${DPDK_DIR:-$REPO/scripts/cloudlab/dpdk_dropbox}"
DPDK_BIN="${DPDK_BIN:-$DPDK_DIR/dpdk_dropbox}"
PIDFILE="${PIDFILE:-/tmp/dpdk_dropbox.pid}"
RATEFILE="${RATEFILE:-/tmp/dropbox_rate}"
LOGDIR="${LOGDIR:-/tmp/dpdk_dropbox_logs}"
mkdir -p "$LOGDIR"

# Hugepages: 1 GB × 16 = 16 GB.  DPDK mbuf pool + rings fit easily in 2-4 GB,
# the extra is headroom for large bursts and future 100 GbE.  1 GB pages are
# mandatory on x86_64 to avoid TLB pressure at line rate.
HUGEPAGES="${HUGEPAGES:-16}"

# The middlebox has two experiment-LAN ports, typically mlx5_2 (sender side)
# and mlx5_3 (receiver side).  DPDK rebinds both to vfio-pci.  Override with
# env vars if the CloudLab profile hands out different PCI addresses.
PCI_A="${PCI_A:-}"   # e.g. 0000:41:00.0  (sender-side, LAN_A)
PCI_B="${PCI_B:-}"   # e.g. 0000:41:00.1  (receiver-side, LAN_B)

hdr()  { printf '\n========== %s ==========\n' "$*"; }
info() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
err()  { printf '[%s] ERR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        err "$MODE needs root — rerun with sudo"
        exit 2
    fi
}

# -------------------------------------------------------------------------
# bootstrap — one-shot node setup.  Safe to re-run; skips completed steps.
# -------------------------------------------------------------------------
do_bootstrap() {
    require_root
    hdr "bootstrap step 1/4 — apt packages"
    apt-get update -qq
    apt-get install -y -qq build-essential pkg-config \
        libnuma-dev python3-pyelftools \
        dpdk dpdk-dev dpdk-kmods-dkms \
        linux-modules-extra-$(uname -r) \
        || { err "apt-get install failed"; exit 1; }

    hdr "bootstrap step 2/4 — hugepages ($HUGEPAGES × 1GB)"
    if ! grep -q "hugepagesz=1G" /proc/cmdline; then
        err "kernel cmdline lacks hugepagesz=1G — edit /etc/default/grub:"
        err "  GRUB_CMDLINE_LINUX_DEFAULT=\"... default_hugepagesz=1G hugepagesz=1G hugepages=$HUGEPAGES\""
        err "  then: update-grub && reboot"
        # Non-fatal on 2MB pages — warn but continue.
    fi
    mkdir -p /mnt/huge
    mountpoint -q /mnt/huge || mount -t hugetlbfs nodev /mnt/huge
    echo "$HUGEPAGES" > /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages \
        || echo "$((HUGEPAGES * 512))" > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages
    info "allocated $(cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null || echo 0) × 1GB + \
$(cat /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages 2>/dev/null || echo 0) × 2MB hugepages"

    hdr "bootstrap step 3/4 — VFIO + NIC binding"
    modprobe vfio-pci
    if [ -z "$PCI_A" ] || [ -z "$PCI_B" ]; then
        err "PCI_A / PCI_B env vars not set — run 'dpdk-devbind.py --status' to find the"
        err "two experiment-LAN NIC PCI addresses, then export them and rerun bootstrap."
        exit 3
    fi
    dpdk-devbind.py --bind=vfio-pci "$PCI_A" "$PCI_B" || true
    info "bound $PCI_A, $PCI_B to vfio-pci"
    dpdk-devbind.py --status-dev net | grep -E "(drv=vfio|$PCI_A|$PCI_B)" || true

    hdr "bootstrap step 4/4 — build dpdk_dropbox"
    if [ ! -d "$DPDK_DIR" ]; then
        err "missing $DPDK_DIR — is the repo up to date?  (W2 — dpdk_dropbox.c — not yet committed?)"
        exit 4
    fi
    ( cd "$DPDK_DIR" && make clean && make -j"$(nproc)" ) \
        || { err "make failed in $DPDK_DIR"; exit 5; }
    info "build OK → $DPDK_BIN"

    info "bootstrap done.  Start the forwarder with:  sudo $0 start 0"
}

# -------------------------------------------------------------------------
# start — launch forwarder in background; reads initial rate from argv[1]
# -------------------------------------------------------------------------
do_start() {
    require_root
    local rate="${1:-0}"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        info "dpdk_dropbox already running (pid $(cat "$PIDFILE"))"
        return 0
    fi
    [ -x "$DPDK_BIN" ] || { err "binary not found: $DPDK_BIN — run bootstrap first"; exit 6; }

    echo "$rate" > "$RATEFILE"
    LOG="$LOGDIR/dropbox_$(date +%Y%m%d_%H%M%S).log"
    # EAL args: -l 0-1 = two lcores (main + one forwarding), -n 4 memory
    # channels, --huge-dir /mnt/huge, --file-prefix keeps multiple DPDK apps
    # from stepping on each other's shared-mem files.
    nohup "$DPDK_BIN" \
        -l 0-1 -n 4 --huge-dir /mnt/huge --file-prefix=dropbox \
        -- --rate-file "$RATEFILE" --rng-seed 49658 \
        </dev/null >"$LOG" 2>&1 &
    echo "$!" > "$PIDFILE"
    sleep 1
    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        info "dpdk_dropbox up  pid=$(cat "$PIDFILE")  rate=$rate  log=$LOG"
    else
        err "dpdk_dropbox failed to start — see $LOG"
        rm -f "$PIDFILE"
        exit 7
    fi
}

# -------------------------------------------------------------------------
# set-rate — live tweak; writes rate file + sends SIGUSR1 to forwarder
# -------------------------------------------------------------------------
do_set_rate() {
    local rate="${1:-0}"
    echo "$rate" > "$RATEFILE"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill -USR1 "$(cat "$PIDFILE")" 2>/dev/null
        info "set-rate $rate → sent SIGUSR1 to pid $(cat "$PIDFILE")"
    else
        info "set-rate $rate → stashed in $RATEFILE (forwarder not running; will pick up on next start)"
    fi
}

# -------------------------------------------------------------------------
# stop / status / logs — standard lifecycle
# -------------------------------------------------------------------------
do_stop() {
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        sleep 0.5
        kill -9 "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
        info "stopped"
    else
        info "no pidfile — nothing to stop"
    fi
    # Belt + suspenders — DPDK apps can leave hugepage-backed shm behind.
    pkill -f "$DPDK_BIN" 2>/dev/null || true
}

do_status() {
    echo "=== dpdk_dropbox status on $(hostname -f) ==="
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "  state   : RUNNING  pid=$(cat "$PIDFILE")"
    elif [ -f "$PIDFILE" ]; then
        echo "  state   : STALE pidfile (process gone)"
    else
        echo "  state   : stopped"
    fi
    [ -f "$RATEFILE" ] && echo "  rate    : $(cat "$RATEFILE")" || echo "  rate    : (not set)"
    echo "  binary  : $DPDK_BIN"
    ls -lt "$LOGDIR" 2>/dev/null | head -4
}

do_logs() {
    latest=$(ls -t "$LOGDIR"/dropbox_*.log 2>/dev/null | head -1 || true)
    [ -z "$latest" ] && { info "no logs yet"; return 0; }
    tail "$@" "$latest"
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
    logs)       do_logs    "$@"       ;;
    *)
        cat >&2 <<USAGE
Usage: $0 {bootstrap | start <rate> | set-rate <rate> | stop | status | logs [-n N]}

  bootstrap   one-shot setup (apt, hugepages, VFIO bind, build dpdk_dropbox)
              requires root + PCI_A / PCI_B env vars for the two NIC addresses
  start       launch forwarder in background with initial <rate>
  set-rate    live tweak — writes rate file + SIGUSR1 to running forwarder
  stop        kill forwarder, clean pidfile
  status      one-line state + current rate + recent logs
  logs        tail the latest log (default tail-10, pass -n N to override)
USAGE
        exit 2
        ;;
esac
