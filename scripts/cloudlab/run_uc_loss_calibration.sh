#!/usr/bin/env bash
# Phase 4 — UC drop calibration harness.
#
# Drives uc_blaster.py (line-rate UC Write-with-immediate generator) with
# and without hammer, so we can map hammer-rate → real-wire UC drop rate
# without relying on tiny training workloads whose offered rate is way
# below link capacity.
#
# Topology (run FROM amd196 / node2):
#   server: amd203 (10.10.1.1, node0)    — blaster receiver + hammer target
#   client: amd196 (10.10.1.3, node2)    — blaster sender
#   hammer: amd186 (10.10.1.2, node1)    — optional iperf3 UDP burst
#
# Per-run output appended to CSV:
#   run_label, hammer_rate, sent, recv, missing, drop_pct, offered_gbps, recv_gbps
#
# Usage:
#   bash scripts/cloudlab/run_uc_loss_calibration.sh            # baseline + 1G + 5G + 10G
#   HAMMER_RATES="0 5G" bash scripts/cloudlab/run_uc_loss_calibration.sh
#   DURATION=60 HAMMER_RATES="10G" bash scripts/cloudlab/run_uc_loss_calibration.sh

set -uo pipefail

# ---- topology ------------------------------------------------------------
SRV_HOST="${SRV_HOST:-amd203}"
SRV_IP="${SRV_IP:-10.10.1.1}"
HMR_HOST="${HMR_HOST:-amd186}"
CLI_IP="${CLI_IP:-$(hostname -I | awk '{for(i=1;i<=NF;i++) if ($i ~ /^10\.10\.1\./) print $i}')}"

# ---- blaster tunables ----------------------------------------------------
DEV="${DEV:-$(bash "$(dirname "$0")/detect_rdma_dev.sh")}"
BLAST_PORT="${BLAST_PORT:-31111}"
CHUNK_BYTES="${CHUNK_BYTES:-16384}"
SQ_DEPTH="${SQ_DEPTH:-512}"
RQ_DEPTH="${RQ_DEPTH:-4096}"
DURATION="${DURATION:-30}"

# ---- hammer tunables -----------------------------------------------------
# Set to "0" to skip a hammer-off cell; anything else is passed to iperf3 -b.
# Use -P / packet size that our earlier calibration showed tops out ~24G
# aggregate: 16 streams × jumbo.  For 1 G target, P=1 pkt=1470 is enough.
HAMMER_RATES="${HAMMER_RATES:-0 5G 10G 20G}"
HAMMER_PARALLEL="${HAMMER_PARALLEL:-16}"
HAMMER_PKT="${HAMMER_PKT:-8900}"
HAMMER_DUR="${HAMMER_DUR:-$((DURATION + 15))}"

REPO="${REPO:-$HOME/SemiRDMA}"
TS=$(date +%Y%m%d_%H%M%S)
OUTDIR="${OUTDIR:-$REPO/experiments/results/phase4_uc_calibration}"
mkdir -p "$OUTDIR"
CSV="$OUTDIR/calibration_${TS}.csv"
LOG="$OUTDIR/calibration_${TS}.log"

echo "label,hammer_rate,sent,recv,missing,drop_pct,offered_gbps,recv_gbps" > "$CSV"

hdr() { printf '\n========== %s ==========\n' "$1" | tee -a "$LOG"; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

cleanup_servers() {
    ssh "$SRV_HOST" "pkill -f 'uc_blaster.py server' 2>/dev/null; true" 2>/dev/null
    # Local leftover from a crashed previous attempt.
    pkill -f 'uc_blaster.py client' 2>/dev/null || true
}

start_hammer() {
    local rate="$1"
    [ "$rate" = "0" ] && return 0
    ssh "$SRV_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh server" >/dev/null
    ssh -f "$HMR_HOST" "cd $REPO && PARALLEL=$HAMMER_PARALLEL PKT_SIZE=$HAMMER_PKT \
        nohup bash scripts/cloudlab/hammer_udp.sh client $SRV_IP $rate $HAMMER_DUR \
        </dev/null >/tmp/uc_cal_hammer_${TS}_${rate}.log 2>&1"
    sleep 3   # ramp
}

stop_hammer() {
    ssh "$HMR_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" >/dev/null 2>&1 || true
    ssh "$SRV_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" >/dev/null 2>&1 || true
}

run_one() {
    local label="$1"
    local hammer_rate="$2"

    hdr "$label  (hammer=$hammer_rate  dur=${DURATION}s)"
    cleanup_servers

    start_hammer "$hammer_rate"

    # Start blaster server in background on amd203 and let it accept()
    # when our client dials in.
    ssh -f "$SRV_HOST" "cd $REPO && source .venv/bin/activate && \
        nohup python scripts/cloudlab/uc_blaster.py server \
            --dev $DEV --port $BLAST_PORT --duration $DURATION \
            --chunk-bytes $CHUNK_BYTES --rq-depth $RQ_DEPTH \
            </dev/null >/tmp/uc_cal_srv_${TS}_${label}.log 2>&1"
    sleep 2   # let server enter accept()

    # Run the client locally; it prints both sides' summaries.
    local client_log="/tmp/uc_cal_cli_${TS}_${label}.log"
    source "$REPO/.venv/bin/activate"
    python "$REPO/scripts/cloudlab/uc_blaster.py" client \
        --peer "$SRV_IP" --dev "$DEV" --port "$BLAST_PORT" \
        --duration "$DURATION" --chunk-bytes "$CHUNK_BYTES" \
        --sq-depth "$SQ_DEPTH" 2>&1 | tee "$client_log"
    local rc=${PIPESTATUS[0]}

    stop_hammer

    if [ "$rc" -ne 0 ]; then
        log "  ❌ client exit rc=$rc  label=$label"
        echo "$label,$hammer_rate,,,,,,CLIENT_ERR" >> "$CSV"
        return 1
    fi

    # Parse the server-echoed summary line.
    local srv_line
    srv_line=$(grep -E "^\[client\] server:" "$client_log" | tail -n1 \
               | sed 's/^\[client\] server: //')
    local sent recv missing drop_pct recv_gbps
    sent=$(echo "$srv_line"    | sed -n 's/.*sent=\([0-9]*\).*/\1/p')
    recv=$(echo "$srv_line"    | sed -n 's/.*recv=\([0-9]*\).*/\1/p')
    missing=$(echo "$srv_line" | sed -n 's/.*missing=\([0-9]*\).*/\1/p')
    drop_pct=$(echo "$srv_line"| sed -n 's/.*drop=\([0-9.]*\)%.*/\1/p')
    recv_gbps=$(echo "$srv_line"| sed -n 's/.*gbps=\([0-9.]*\).*/\1/p')

    local offered_gbps
    offered_gbps=$(grep -E "^\[client\] local:" "$client_log" \
                   | sed -n 's/.*offered=\([0-9.]*\)Gbps.*/\1/p' | tail -n1)

    echo "$label,$hammer_rate,$sent,$recv,$missing,$drop_pct,$offered_gbps,$recv_gbps" >> "$CSV"
    log "  ✓ sent=$sent recv=$recv missing=$missing drop=${drop_pct}% offered=${offered_gbps}G recv=${recv_gbps}G"
}

# ---- main sweep ----------------------------------------------------------
hdr "calibration start  ts=$TS  dev=$DEV  duration=${DURATION}s  chunk=${CHUNK_BYTES}B"
log "hammer rates: $HAMMER_RATES"
log "output csv:   $CSV"

idx=0
for rate in $HAMMER_RATES; do
    label=$(printf "cell_%02d_hammer%s" "$idx" "$rate")
    run_one "$label" "$rate"
    idx=$((idx + 1))
done

hdr "summary"
column -t -s, "$CSV"
