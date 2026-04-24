#!/usr/bin/env bash
# Phase 4 — RDMA ib_write_bw hammer.  Sibling of hammer_udp.sh but
# replaces the iperf3 UDP blast with RC RDMA Write traffic.
#
# Why this exists
# ---------------
# The UDP hammer exercises the amd203 kernel softirq path, which then
# preempts the user-space Python uc_blaster receiver and causes a
# refill-side cliff at ~15 Gbps UC offered rate.  That cliff is an
# implementation artifact, not switch-egress overflow, and is useless
# as a lossy-wire model.
#
# RDMA hammer bypasses the kernel entirely — ib_write_bw uses NIC DMA
# straight to the target MR (Write, not Send, so no RQ WRs consumed
# either) and competes with the uc_blaster only for switch egress
# bandwidth.  That's the lossy wire we actually want.
#
# Same CLI surface as hammer_udp.sh so run_uc_loss_calibration.sh can
# switch between them via HAMMER_TYPE={udp,rdma}.
#
# Usage:
#   bash scripts/cloudlab/hammer_rdma.sh server               # run on target
#   bash scripts/cloudlab/hammer_rdma.sh client <peer> <rate> [dur]
#   bash scripts/cloudlab/hammer_rdma.sh stop
#   bash scripts/cloudlab/hammer_rdma.sh status
#
# <rate> is informational only: ib_write_bw always blasts at RC line rate.
# Pass something like "24G" for logging consistency with hammer_udp.sh.

set -uo pipefail

MODE="${1:-status}"

PIDFILE_SERVER="/tmp/hammer_rdma_server.pid"
PIDFILE_CLIENT="/tmp/hammer_rdma_client.pid"
LOGDIR="${LOGDIR:-/tmp/hammer_rdma_logs}"
mkdir -p "$LOGDIR"

DEV="${DEV:-$(bash "$(dirname "$0")/detect_rdma_dev.sh" 2>/dev/null || echo mlx5_2)}"
GID_IDX="${GID_IDX:-1}"
PORT="${PORT:-18600}"
MSG_SIZE="${MSG_SIZE:-65536}"
QPS="${QPS:-1}"

have_ibwb() {
    if ! command -v ib_write_bw >/dev/null 2>&1; then
        echo "ERR: ib_write_bw missing (apt-get install -y perftest)" >&2
        exit 127
    fi
}

case "$MODE" in
    server)
        have_ibwb
        if [ -f "$PIDFILE_SERVER" ] && kill -0 "$(cat "$PIDFILE_SERVER")" 2>/dev/null; then
            echo "rdma hammer server already running (pid $(cat "$PIDFILE_SERVER"))"
            exit 0
        fi
        LOG="$LOGDIR/server_$(date +%Y%m%d_%H%M%S).log"
        # -D <sec> makes the server exit after N sec; set the duration
        # very high here (2 hr) so a server survives many back-to-back
        # client connections.  The calibration harness calls `stop`
        # between cells anyway.
        nohup ib_write_bw -d "$DEV" -x "$GID_IDX" -p "$PORT" \
            -s "$MSG_SIZE" -D 7200 -q "$QPS" --report_gbits -F \
            </dev/null >"$LOG" 2>&1 &
        PID=$!
        echo "$PID" >"$PIDFILE_SERVER"
        sleep 0.5
        if kill -0 "$PID" 2>/dev/null; then
            echo "rdma hammer server up  pid=$PID  port=$PORT  log=$LOG"
        else
            echo "ERR: server failed to start — see $LOG" >&2
            rm -f "$PIDFILE_SERVER"
            exit 1
        fi
        ;;

    client)
        have_ibwb
        TARGET="${2:-}"
        RATE="${3:-line}"   # informational only
        DUR="${4:-120}"
        if [ -z "$TARGET" ]; then
            echo "Usage: $0 client <target_ip> <rate_label> [duration_sec=120]" >&2
            exit 2
        fi
        if [ -f "$PIDFILE_CLIENT" ] && kill -0 "$(cat "$PIDFILE_CLIENT")" 2>/dev/null; then
            echo "ERR: rdma hammer client already running (pid $(cat "$PIDFILE_CLIENT"))" >&2
            exit 3
        fi
        LOG="$LOGDIR/client_${TARGET}_${RATE}_$(date +%Y%m%d_%H%M%S).log"
        echo "rdma hammer client → $TARGET  rate=$RATE(line)  dur=${DUR}s  qps=$QPS  msg=${MSG_SIZE}B"
        echo "  log: $LOG"
        nohup ib_write_bw -d "$DEV" -x "$GID_IDX" -p "$PORT" \
            -s "$MSG_SIZE" -D "$DUR" -q "$QPS" --report_gbits -F \
            "$TARGET" </dev/null >"$LOG" 2>&1 &
        PID=$!
        echo "$PID" >"$PIDFILE_CLIENT"
        # Don't wait — client is expected to be reaped by the matrix driver
        # via `stop`.  Return immediately so the caller can proceed.
        echo "client pid=$PID (running async)"
        ;;

    stop)
        for PF in "$PIDFILE_CLIENT" "$PIDFILE_SERVER"; do
            if [ -f "$PF" ]; then
                PID=$(cat "$PF")
                kill "$PID" 2>/dev/null && echo "killed pid=$PID ($(basename "$PF"))"
                rm -f "$PF"
            fi
        done
        pkill -f 'ib_write_bw' 2>/dev/null || true
        echo "stop done"
        ;;

    status)
        echo "=== hammer_rdma status on $(hostname -f) ==="
        for PF in "$PIDFILE_SERVER" "$PIDFILE_CLIENT"; do
            if [ -f "$PF" ]; then
                PID=$(cat "$PF")
                if kill -0 "$PID" 2>/dev/null; then
                    echo "  $(basename "$PF" .pid): pid=$PID ALIVE"
                else
                    echo "  $(basename "$PF" .pid): pid=$PID STALE"
                fi
            else
                echo "  $(basename "$PF" .pid): (no pidfile)"
            fi
        done
        ls -lt "$LOGDIR" 2>/dev/null | head -6
        ;;

    *)
        echo "Usage: $0 {server | client <target> <rate_label> [dur] | stop | status}" >&2
        exit 2
        ;;
esac
