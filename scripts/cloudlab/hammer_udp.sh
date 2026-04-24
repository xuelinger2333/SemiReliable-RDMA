#!/usr/bin/env bash
# Phase 4 — UDP-burst background traffic generator for lossy-wire validation.
#
# Why UDP, not TCP:
#   iperf3 TCP has AIMD congestion control → it backs off as soon as the
#   switch starts dropping frames, so utilization stabilizes at a low
#   fraction of link rate and never creates the persistent queue pressure
#   needed to induce RoCE drops.  UDP is open-loop: it keeps blasting at
#   the target bitrate regardless of losses → switch egress buffer stays
#   full → RoCE chunks traversing the same egress port get tail-dropped.
#
# Why not tc netem:
#   ConnectX-5/6 drives RDMA packets directly from QP doorbell to NIC TX
#   without passing through the Linux sk_buff path, so tc qdisc rules on
#   the netdev are a no-op for RoCE QP traffic.  See netem_inject.sh for
#   the empirical verification on CX-6 Lx.
#
# Topology assumed (three-node amd-class CloudLab experiment):
#   node0 = 10.10.1.1 (amd203, hammer TARGET + experiment RECEIVER)
#   node1 = 10.10.1.2 (amd186, hammer SOURCE)
#   node2 = 10.10.1.3 (amd196, experiment SENDER)
#
# During a lossy-wire cell:
#   - amd203 runs hammer server (receives amd186's UDP burst)
#   - amd186 runs hammer client (blasts to amd203 at target rate)
#   - amd196 ↔ amd203 runs the RDMA experiment over the congested link
#
# The shared choke point is the switch egress port to amd203, which also
# carries the RoCE traffic from amd196.  When (hammer + RDMA) exceeds
# 25 Gbps sustained, the egress queue overflows and both flows bleed.
#
# Rate targets (of 25 GbE link):
#   50% = 12.5G   (gentle — probably still 0 RoCE loss, useful as calibrator)
#   80% = 20G     (sweet spot — should push RoCE loss into 1–3% range)
#   95% = 23.75G  (aggressive — may cause experiment gloo rendezvous to time out)
#
# Usage on amd203 (hammer target; run once, leaves server up for reuse):
#   bash scripts/cloudlab/hammer_udp.sh server
#
# Usage on amd186 (hammer source; run per cell, blocks until done):
#   bash scripts/cloudlab/hammer_udp.sh client node0 20G 120
#   # target=amd203 via /etc/hosts alias `node0`; 20G/s for 120 sec
#
# Usage on any node (cleanup):
#   bash scripts/cloudlab/hammer_udp.sh stop
#   bash scripts/cloudlab/hammer_udp.sh status

set -uo pipefail

MODE="${1:-status}"

PIDFILE_SERVER="/tmp/hammer_udp_server.pid"
PIDFILE_CLIENT="/tmp/hammer_udp_client.pid"
LOGDIR="${LOGDIR:-/tmp/hammer_udp_logs}"
mkdir -p "$LOGDIR"

# Tunables (env override).
PKT_SIZE="${PKT_SIZE:-1470}"  # UDP payload bytes; stay under 1500 MTU to
                              # avoid kernel-side fragmentation pushing load
                              # onto softirqs rather than the switch path
PARALLEL="${PARALLEL:-4}"     # iperf3 UDP saturates ~5-7 Gbps per stream on
                              # a single CPU core; 4 parallel streams reach
                              # ≥20 Gbps on AMD EPYC with PCIe gen4 CX-5
PORT="${PORT:-5201}"

have_iperf3() {
    if ! command -v iperf3 >/dev/null 2>&1; then
        echo "ERR: iperf3 missing (install via setup_env.sh or 'sudo apt-get install -y iperf3')" >&2
        exit 127
    fi
}

case "$MODE" in
    server)
        have_iperf3
        if [ -f "$PIDFILE_SERVER" ] && kill -0 "$(cat "$PIDFILE_SERVER")" 2>/dev/null; then
            echo "hammer server already running (pid $(cat "$PIDFILE_SERVER"))"
            exit 0
        fi
        LOG="$LOGDIR/server_$(date +%Y%m%d_%H%M%S).log"
        # -s server, -p port, -1 exits after single session would prevent
        # long-lived reuse; omit -1 so server handles many client sessions.
        # --forceflush so tail -f shows progress immediately.
        nohup iperf3 -s -p "$PORT" --forceflush >"$LOG" 2>&1 &
        PID=$!
        echo "$PID" >"$PIDFILE_SERVER"
        sleep 0.5
        if kill -0 "$PID" 2>/dev/null; then
            echo "hammer server up  pid=$PID  port=$PORT  log=$LOG"
        else
            echo "ERR: server failed to start — see $LOG" >&2
            rm -f "$PIDFILE_SERVER"
            exit 1
        fi
        ;;

    client)
        have_iperf3
        TARGET="${2:-}"
        RATE="${3:-}"
        DUR="${4:-120}"
        if [ -z "$TARGET" ] || [ -z "$RATE" ]; then
            echo "Usage: $0 client <target_host_or_ip> <rate e.g. 20G> [duration_sec=120]" >&2
            exit 2
        fi
        # Reject if another client is already blasting — two overlapping
        # clients makes rate accounting impossible and usually crashes one.
        if [ -f "$PIDFILE_CLIENT" ] && kill -0 "$(cat "$PIDFILE_CLIENT")" 2>/dev/null; then
            echo "ERR: hammer client already running (pid $(cat "$PIDFILE_CLIENT")); stop first" >&2
            exit 3
        fi
        LOG="$LOGDIR/client_${TARGET}_${RATE}_$(date +%Y%m%d_%H%M%S).log"
        echo "hammer client → $TARGET  rate=$RATE  dur=${DUR}s  P=$PARALLEL  pkt=${PKT_SIZE}B"
        echo "  log: $LOG"
        # -u UDP; -b rate; -l payload; -P parallel streams; -t duration;
        # --json makes it easier to parse final summary programmatically
        # but we also keep a human-readable copy.
        iperf3 -c "$TARGET" -p "$PORT" -u -b "$RATE" -l "$PKT_SIZE" \
               -P "$PARALLEL" -t "$DUR" --forceflush \
               2>&1 | tee "$LOG" &
        PID=$!
        echo "$PID" >"$PIDFILE_CLIENT"
        wait "$PID"
        RC=$?
        rm -f "$PIDFILE_CLIENT"
        # iperf3 exits 0 on completion even if UDP loss is high; surface the
        # tail summary so the caller sees achieved bitrate + loss% at a glance.
        echo "--- iperf3 summary (last 8 lines) ---"
        tail -8 "$LOG"
        exit $RC
        ;;

    stop)
        STOPPED=0
        for PF in "$PIDFILE_CLIENT" "$PIDFILE_SERVER"; do
            if [ -f "$PF" ]; then
                PID=$(cat "$PF")
                if kill -0 "$PID" 2>/dev/null; then
                    kill "$PID" && STOPPED=$((STOPPED+1))
                    echo "killed pid=$PID ($(basename "$PF"))"
                fi
                rm -f "$PF"
            fi
        done
        # Best-effort sweep for orphans (e.g. pidfile deleted but process alive).
        pkill -f 'iperf3 -[sc]' 2>/dev/null && STOPPED=$((STOPPED+1)) || true
        echo "stop done  killed=$STOPPED"
        ;;

    status)
        echo "=== hammer_udp status on $(hostname -f) ==="
        for PF in "$PIDFILE_SERVER" "$PIDFILE_CLIENT"; do
            if [ -f "$PF" ]; then
                PID=$(cat "$PF")
                if kill -0 "$PID" 2>/dev/null; then
                    echo "  $(basename "$PF" .pid): pid=$PID ALIVE"
                else
                    echo "  $(basename "$PF" .pid): pid=$PID STALE (pidfile orphan)"
                fi
            else
                echo "  $(basename "$PF" .pid): (no pidfile)"
            fi
        done
        echo "--- recent logs ---"
        ls -lt "$LOGDIR" 2>/dev/null | head -6
        ;;

    *)
        echo "Usage: $0 {server | client <target> <rate> [dur] | stop | status}" >&2
        exit 2
        ;;
esac
