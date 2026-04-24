#!/usr/bin/env bash
# Phase 4 · DPDK middlebox validation — two smoke stages.
#
# Stage A — transparency
#   At drop_rate=0, ib_write_bw from sender through the middlebox to receiver
#   should achieve ≥24 Gbps (≥96% of 25 GbE line).  If this fails, DPDK is
#   mangling RoCE headers (MAC rewrite, BTH corruption) and the forwarder
#   needs debugging before anything else matters.
#
# Stage B — calibration
#   At drop_rate=0.01 (1%), run uc_blaster for 60 s at a few Gbps offered
#   and verify missing% ∈ [0.9%, 1.1%].  Confirms Bernoulli is honest and
#   not biased by DPDK rx queue depth / rte_hash collisions.
#
# Runs end-to-end from the receiver node (NODE0) so the same ssh identity
# used by run_p1_matrix.sh is reused for middlebox control.
#
# Usage (from receiver node, e.g. amd203 or receiver in middlebox profile):
#   MIDDLEBOX_HOST=chen123@middlebox.utah.cloudlab.us \
#     bash scripts/cloudlab/middlebox_validate.sh
#
#   # Single stage:
#   STAGE=A MIDDLEBOX_HOST=... bash scripts/cloudlab/middlebox_validate.sh
#   STAGE=B MIDDLEBOX_HOST=... bash scripts/cloudlab/middlebox_validate.sh

set -uo pipefail

STAGE="${STAGE:-both}"   # A | B | both
MIDDLEBOX_HOST="${MIDDLEBOX_HOST:?set MIDDLEBOX_HOST=user@host}"

# Receiver = this node.  Sender = NODE1 (the training peer in line topology,
# which sends traffic through the middlebox toward us).
THIS_IP="${THIS_IP:-10.10.2.1}"              # receiver-side LAN_B
SENDER_HOST="${SENDER_HOST:-chen123@10.10.1.3}"  # sender-side LAN_A (management fqdn if needed)
SENDER_IP="${SENDER_IP:-10.10.1.3}"          # sender-side LAN_A IP

REPO="${REPO:-$HOME/SemiRDMA}"
MIDDLEBOX_REPO="${MIDDLEBOX_REPO:-\$HOME/SemiRDMA}"

DEV="${DEV:-$(bash "$REPO/scripts/cloudlab/detect_rdma_dev.sh" 2>/dev/null || echo mlx5_2)}"
GID_IDX="${GID_IDX:-1}"

TS=$(date +%Y%m%d_%H%M%S)
OUTDIR="${OUTDIR:-$REPO/experiments/results/phase4_middlebox_validate/$TS}"
mkdir -p "$OUTDIR"

hdr()  { printf '\n========== %s ==========\n' "$*"; }
info() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[%s] FAIL: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

set_middlebox_rate() {
    local rate="$1"
    info "middlebox set-rate $rate (via $MIDDLEBOX_HOST)"
    ssh "$MIDDLEBOX_HOST" "bash $MIDDLEBOX_REPO/scripts/cloudlab/middlebox_setup.sh set-rate $rate" \
        || fail "set-rate $rate failed on middlebox"
    sleep 2
}

# ------------------------------------------------------------------------
# Stage A — transparency: drop_rate=0, ib_write_bw sender → receiver
# ------------------------------------------------------------------------
stage_a() {
    hdr "Stage A: middlebox transparency at drop_rate=0"
    set_middlebox_rate 0

    # Start ib_write_bw server locally (receiver).
    info "receiver: starting ib_write_bw server on $DEV GID $GID_IDX port 18700"
    local srv_log="$OUTDIR/stageA_server.log"
    nohup ib_write_bw -d "$DEV" -x "$GID_IDX" -p 18700 \
        -s 65536 -D 30 -q 1 --report_gbits -F \
        </dev/null >"$srv_log" 2>&1 &
    local srv_pid=$!
    sleep 2

    # Start client remotely on sender.
    info "sender: starting ib_write_bw client → $THIS_IP (via middlebox)"
    local cli_log="$OUTDIR/stageA_client.log"
    ssh "$SENDER_HOST" "ib_write_bw -d $DEV -x $GID_IDX -p 18700 \
        -s 65536 -D 25 -q 1 --report_gbits -F $THIS_IP" \
        >"$cli_log" 2>&1 &
    local cli_pid=$!
    wait "$cli_pid" || true
    wait "$srv_pid" 2>/dev/null || true

    # Parse server log for BW average — last "BW average" column.
    local bw
    bw=$(awk '/#bytes/ {found=1; next} found && NF>=4 {print $4}' "$srv_log" | tail -n1)
    info "stage A result: BW average = ${bw:-?} Gbps"
    if [ -z "$bw" ]; then
        fail "no BW row in $srv_log — DPDK forwarder may be dropping handshake"
    fi
    # awk float comparison — require >= 24.0
    local pass
    pass=$(awk -v x="$bw" 'BEGIN { print (x >= 24.0) ? "yes" : "no" }')
    if [ "$pass" = "yes" ]; then
        info "✓ stage A PASS — middlebox is transparent at line rate"
    else
        fail "stage A FAIL — BW=${bw} < 24 Gbps.  Forwarder likely mangling RoCE headers."
    fi
}

# ------------------------------------------------------------------------
# Stage B — calibration: drop_rate=0.01, uc_blaster for 60 s
# ------------------------------------------------------------------------
stage_b() {
    hdr "Stage B: Bernoulli calibration at drop_rate=0.01"
    set_middlebox_rate 0.01

    info "running run_uc_loss_calibration.sh HAMMER_TYPE=none DURATION=60 (through middlebox)"
    # We don't want hammer overlay here — only the middlebox's software drop.
    local csv_before cal_rc
    csv_before=$(ls -t "$REPO/experiments/results/phase4_uc_calibration/calibration_*.csv" 2>/dev/null | head -1)

    HAMMER_RATES="0" DURATION=60 \
        bash "$REPO/scripts/cloudlab/run_uc_loss_calibration.sh" \
        >"$OUTDIR/stageB_calibration.log" 2>&1
    cal_rc=$?

    if [ "$cal_rc" -ne 0 ]; then
        fail "calibration harness exited $cal_rc — see $OUTDIR/stageB_calibration.log"
    fi

    local csv_after
    csv_after=$(ls -t "$REPO/experiments/results/phase4_uc_calibration/calibration_*.csv" | head -1)
    if [ "$csv_after" = "$csv_before" ]; then
        fail "no new calibration CSV produced"
    fi

    local drop_pct
    drop_pct=$(awk -F, 'NR==2 {print $6}' "$csv_after")
    info "stage B result: measured drop% = ${drop_pct}% (target 1.0% ± 0.1)"

    local pass
    pass=$(awk -v x="$drop_pct" 'BEGIN { print (x >= 0.9 && x <= 1.1) ? "yes" : "no" }')
    if [ "$pass" = "yes" ]; then
        info "✓ stage B PASS — Bernoulli drop is honest"
    else
        fail "stage B FAIL — drop_pct=${drop_pct}% outside [0.9, 1.1]%.  Check RNG / rate setter."
    fi

    # Reset to passthrough so the next experiment starts clean.
    set_middlebox_rate 0
}

# ------------------------------------------------------------------------
# dispatch
# ------------------------------------------------------------------------
case "$STAGE" in
    A)    stage_a                    ;;
    B)    stage_b                    ;;
    both) stage_a; stage_b           ;;
    *)    echo "ERR: STAGE=$STAGE (want A|B|both)" >&2; exit 2 ;;
esac

info "done.  artifacts under $OUTDIR"
