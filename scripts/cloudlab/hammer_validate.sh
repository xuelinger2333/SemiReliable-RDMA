#!/usr/bin/env bash
# Phase 4 — P0 saturation validation: does hammer_udp.sh actually induce
# enough switch-egress queue overflow to make RoCE traffic between the
# experiment pair suffer measurable degradation?
#
# Runs FROM amd196 (experiment client; also SSH-coordinates amd203 and
# amd186).  Does three things:
#
#   1. Baseline probe (no hammer):
#        ib_write_lat -s 65536 --iters 50000  amd196 → amd203
#        ib_write_bw  -s 65536 -D 10          amd196 → amd203
#
#   2. Start hammer at target rate (amd186 → amd203 UDP burst).
#
#   3. Loaded probe (hammer running):
#        same ib_write_lat + ib_write_bw while hammer blasts in background
#
#   4. Stop hammer.  Print latency p50/p99/p99.9 delta + bw delta.
#
# The "drop rate" is inferred from ib_write_bw degradation: if baseline
# BW is ~24 Gbps and hammer knocks it down to e.g. 12 Gbps at 80% load,
# that's ~50% packet drop for RoCE at this queue depth — plenty for the
# Phase 4 P1 matrix.  Latency tail expansion is the more precise signal:
# p99.9 going from ~3 µs baseline to >>100 µs indicates switch queue
# spikes are being felt by RoCE.
#
# Usage (from amd196, exp LAN IP 10.10.1.3):
#   bash scripts/cloudlab/hammer_validate.sh               # default 80% load, 60s
#   RATE=20G DUR=60 bash scripts/cloudlab/hammer_validate.sh
#   RATE=12.5G bash scripts/cloudlab/hammer_validate.sh    # 50% calibrator
#   RATE=23.75G bash scripts/cloudlab/hammer_validate.sh   # 95% aggressive
#
# Prereqs:
#   - bootstrap_fresh_node.sh has run on amd203, amd186, amd196
#   - link_setup.sh has been run on amd196 + amd203 (PFC off, MTU 9000)
#   - passwordless ssh from amd196 to amd203 and amd186 under user chen123

set -uo pipefail

# ----- topology (hardcoded per chen123-302346.rdma-nic-perf-pg0 profile) -----
TGT_HOST="${TGT_HOST:-amd203.utah.cloudlab.us}"    # experiment receiver + hammer target
HMR_HOST="${HMR_HOST:-amd186.utah.cloudlab.us}"    # hammer source (this profile's node1)
TGT_EXP_IP="${TGT_EXP_IP:-10.10.1.1}"              # amd203 experiment LAN
HMR_EXP_IP="${HMR_EXP_IP:-10.10.1.2}"              # amd186 experiment LAN
SSH="${SSH:-ssh -o StrictHostKeyChecking=no}"
SSH_USER="${SSH_USER:-chen123}"

# ----- hammer knobs -----
RATE="${RATE:-20G}"          # 80% of 25 GbE link
DUR="${DUR:-60}"             # hammer duration (sec); probe must finish inside
PKT="${PKT:-1470}"
PARALLEL="${PARALLEL:-4}"

# ----- probe knobs -----
PROBE_SIZE="${PROBE_SIZE:-65536}"
LAT_ITERS="${LAT_ITERS:-20000}"
BW_DURATION="${BW_DURATION:-10}"

REPO="${REPO:-$HOME/SemiRDMA}"
DEV=$(bash "$REPO/scripts/cloudlab/detect_rdma_dev.sh")
GID_IDX="${GID_IDX:-1}"
PORT_LAT="${PORT_LAT:-18522}"
PORT_BW="${PORT_BW:-18523}"

LOGDIR="${LOGDIR:-$REPO/experiments/results/phase4_hammer_validate}"
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "$LOGDIR"

echo "=== hammer_validate ==="
echo "  experiment: $(hostname -f) ($DEV) → $TGT_HOST ($TGT_EXP_IP)"
echo "  hammer:     $HMR_HOST ($HMR_EXP_IP) → $TGT_HOST ($TGT_EXP_IP) @ $RATE UDP for ${DUR}s"
echo "  log dir:    $LOGDIR"
echo "  timestamp:  $TS"
echo ""

# ---------- helpers ----------

ssh_tgt() { $SSH "$SSH_USER@$TGT_HOST" "$@"; }
ssh_hmr() { $SSH "$SSH_USER@$HMR_HOST" "$@"; }

# Start perftest server in background on the remote.  Uses ssh -f (fork the
# ssh connection itself) + </dev/null redirect — without this pair, `ssh
# "nohup X &"` fails to actually launch X because the remote shell exits
# before the fork completes and systemd cleans up the orphan.  Verified
# empirically on amd203/CX-5 during Phase 4 P0 setup.
start_lat_server() {
    ssh -f "$SSH_USER@$TGT_HOST" "nohup ib_write_lat -d $DEV -x $GID_IDX -p $PORT_LAT -s $PROBE_SIZE --iters=$LAT_ITERS -F </dev/null >/tmp/lat_server_$TS.log 2>&1"
    sleep 1.5
}

start_bw_server() {
    ssh -f "$SSH_USER@$TGT_HOST" "nohup ib_write_bw -d $DEV -x $GID_IDX -p $PORT_BW -s $PROBE_SIZE -D $BW_DURATION -q 1 --report_gbits -F </dev/null >/tmp/bw_server_$TS.log 2>&1"
    sleep 1.5
}

run_lat_client() {
    local tag="$1"
    echo "  [$tag] ib_write_lat probe..."
    ib_write_lat -d "$DEV" -x "$GID_IDX" -p "$PORT_LAT" -s "$PROBE_SIZE" \
        --iters="$LAT_ITERS" -F "$TGT_EXP_IP" \
        2>&1 | tee "$LOGDIR/lat_${tag}_${TS}.log" | tail -12
}

run_bw_client() {
    local tag="$1"
    echo "  [$tag] ib_write_bw probe..."
    ib_write_bw -d "$DEV" -x "$GID_IDX" -p "$PORT_BW" -s "$PROBE_SIZE" \
        -D "$BW_DURATION" -q 1 --report_gbits -F "$TGT_EXP_IP" \
        2>&1 | tee "$LOGDIR/bw_${tag}_${TS}.log" | tail -6
}

kill_remote_perftest() {
    ssh_tgt "pkill -f 'ib_write_(lat|bw)' 2>/dev/null; true" >/dev/null
}

# ---------- Phase A: baseline ----------
echo "--- Phase A: baseline (no hammer) ---"
kill_remote_perftest
start_lat_server
run_lat_client "baseline"
kill_remote_perftest

start_bw_server
run_bw_client "baseline"
kill_remote_perftest

# ---------- Phase B: start hammer ----------
echo ""
echo "--- Phase B: start hammer on $HMR_HOST → $TGT_HOST @ $RATE ---"
# Make sure target has hammer server up (hammer_udp.sh server self-backgrounds
# properly via its own nohup + pidfile, so regular ssh is fine here).
ssh_tgt "bash $REPO/scripts/cloudlab/hammer_udp.sh server" | sed 's/^/  tgt: /'
# Fire-and-forget client on amd186; use ssh -f so the client runs for $DUR
# seconds after our ssh returns.
ssh -f "$SSH_USER@$HMR_HOST" "cd $REPO && PARALLEL=$PARALLEL PKT_SIZE=$PKT nohup bash scripts/cloudlab/hammer_udp.sh client $TGT_EXP_IP $RATE $DUR </dev/null >/tmp/hammer_client_$TS.log 2>&1"
# Let the hammer ramp; iperf3 UDP reaches target rate in ~1-2 sec
sleep 3
echo "  hammer ramped (slept 3s)"

# ---------- Phase C: loaded probes ----------
echo ""
echo "--- Phase C: loaded probes (hammer running) ---"
kill_remote_perftest
start_lat_server
run_lat_client "loaded"
kill_remote_perftest

start_bw_server
run_bw_client "loaded"
kill_remote_perftest

# ---------- Phase D: stop hammer ----------
echo ""
echo "--- Phase D: cleanup ---"
ssh_hmr "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" | sed 's/^/  hmr: /'
ssh_tgt "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" | sed 's/^/  tgt: /'

# ---------- summary ----------
echo ""
echo "=== summary ==="
for tag in baseline loaded; do
    LAT_LOG="$LOGDIR/lat_${tag}_${TS}.log"
    BW_LOG="$LOGDIR/bw_${tag}_${TS}.log"
    if [ -s "$LAT_LOG" ]; then
        echo "  [$tag] ib_write_lat (µs):"
        grep -E '^ *[0-9]+ +[0-9]' "$LAT_LOG" | tail -2 | awk '{print "    "$0}'
    fi
    if [ -s "$BW_LOG" ]; then
        echo "  [$tag] ib_write_bw (Gbps, report_gbits):"
        grep -E '^ *[0-9]+ +[0-9]' "$BW_LOG" | tail -2 | awk '{print "    "$0}'
    fi
done
echo ""
echo "Full logs under: $LOGDIR"
echo ""
echo "Interpretation:"
echo "  - baseline p99.9 lat typically 2-5 µs on CX-5 25 GbE benign"
echo "  - loaded   p99.9 lat >>100 µs ⇒ hammer successfully overflows switch queue"
echo "  - baseline bw ~24 Gbps;  loaded bw drop >20% ⇒ significant RoCE drop rate"
