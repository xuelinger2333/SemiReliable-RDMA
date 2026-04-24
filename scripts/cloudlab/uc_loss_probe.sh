#!/usr/bin/env bash
# Phase 4 — minimal UC-layer loss probe under hammer.
#
# Uses the existing test_chunk_sweep (tests/phase2/) as a real-wire UC
# traffic generator.  For each (chunk_bytes, loss_pct) cell the test
# reports `ghost_ratio` = total_ghost_chunks / total_chunks.  When the
# synthetic loss_pct row = 0.0, any ghost_ratio > 0 is 100% attributable
# to real switch/wire drops — which is exactly the UC-level drop rate
# SemiRDMA would see in production.
#
# Run FROM amd196 (experiment client).  Coordinates amd203 (experiment
# receiver + hammer target) and amd186 (hammer source) over the cluster
# SSH mesh set up earlier.
#
# Usage:
#   bash scripts/cloudlab/uc_loss_probe.sh              # baseline only
#   HAMMER_RATE=1G bash scripts/cloudlab/uc_loss_probe.sh   # with 1G hammer
#
# Minimal output: two CSVs under experiments/results/phase4_uc_loss/
#   chunk_sweep_baseline_<ts>.csv
#   chunk_sweep_hammer_<ts>.csv    (only if HAMMER_RATE set)
# Plus a one-line summary per cell comparing ghost_ratio at loss_pct=0%.

set -uo pipefail

REPO="${REPO:-$HOME/SemiRDMA}"
TGT_HOST="${TGT_HOST:-amd203}"
HMR_HOST="${HMR_HOST:-amd186}"
TGT_EXP_IP="${TGT_EXP_IP:-10.10.1.1}"
HAMMER_RATE="${HAMMER_RATE:-}"          # empty = baseline only
HAMMER_DUR="${HAMMER_DUR:-300}"         # long enough to cover sweep + probe
HAMMER_PARALLEL="${HAMMER_PARALLEL:-1}" # 1-stream → ~0.7G actual ≈ 4% RC drop
HAMMER_PKT="${HAMMER_PKT:-1470}"
ROUNDS="${ROUNDS:-200}"                 # rounds per (chunk, loss) cell

BIN="$REPO/build/tests/phase2/test_chunk_sweep"
if [ ! -x "$BIN" ]; then
    echo "ERR: $BIN missing — run bootstrap_fresh_node.sh first" >&2
    exit 1
fi

TS=$(date +%Y%m%d_%H%M%S)
OUTDIR="$REPO/experiments/results/phase4_uc_loss"
mkdir -p "$OUTDIR"

DEV="${DEV:-$(bash "$REPO/scripts/cloudlab/detect_rdma_dev.sh")}"

echo "=== uc_loss_probe ==="
echo "  client:        $(hostname -f) ($DEV)"
echo "  server:        $TGT_HOST ($TGT_EXP_IP)"
echo "  rounds/cell:   $ROUNDS"
echo "  hammer:        ${HAMMER_RATE:-OFF}"
echo "  output:        $OUTDIR"
echo ""

run_probe() {
    local tag="$1"
    local csv="$OUTDIR/chunk_sweep_${tag}_${TS}.csv"
    echo "--- probe $tag: starting server on $TGT_HOST ---"
    # Kill any leftover server, then launch fresh.  Note: test_chunk_sweep
    # server writes CSV to stdout and log chatter to stderr — redirect
    # stdout to the remote CSV file so we can scp it back afterwards.
    ssh "$TGT_HOST" "pkill -f test_chunk_sweep 2>/dev/null; sleep 0.5; true"
    ssh -f "$TGT_HOST" "cd $REPO && SEMIRDMA_DEV=$DEV nohup ./build/tests/phase2/test_chunk_sweep server $DEV $ROUNDS </dev/null >/tmp/chunksw_server_${tag}_${TS}.csv 2>/tmp/chunksw_server_${tag}_${TS}.err"
    sleep 2
    echo "--- probe $tag: running client ---"
    cd "$REPO" && SEMIRDMA_DEV=$DEV ./build/tests/phase2/test_chunk_sweep \
        client "$TGT_EXP_IP" "$DEV" "$ROUNDS" 2>"$OUTDIR/chunksw_client_${tag}_${TS}.err" \
        | tee "$OUTDIR/chunksw_client_${tag}_${TS}.csv" | tail -20
    # Retrieve server CSV
    scp "$TGT_HOST:/tmp/chunksw_server_${tag}_${TS}.csv" "$csv" 2>/dev/null
    echo "  server CSV: $csv"
    echo ""
}

# ---------- baseline (no hammer) ----------
run_probe baseline

# ---------- hammer on ----------
if [ -n "$HAMMER_RATE" ]; then
    echo "=== starting hammer: $HMR_HOST → $TGT_EXP_IP @ $HAMMER_RATE for ${HAMMER_DUR}s ==="
    ssh "$TGT_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh server" | sed 's/^/  tgt: /'
    ssh -f "$HMR_HOST" "cd $REPO && PARALLEL=$HAMMER_PARALLEL PKT_SIZE=$HAMMER_PKT nohup bash scripts/cloudlab/hammer_udp.sh client $TGT_EXP_IP $HAMMER_RATE $HAMMER_DUR </dev/null >/tmp/hammer_client_$TS.log 2>&1"
    sleep 3
    echo "  hammer ramped"
    run_probe hammer
    echo "--- cleanup hammer ---"
    ssh "$HMR_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" | sed 's/^/  hmr: /'
    ssh "$TGT_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" | sed 's/^/  tgt: /'
fi

# ---------- summary ----------
echo ""
echo "=== UC loss probe summary (ghost_ratio at synthetic loss_pct=0%) ==="
printf "  %-12s %-10s %-10s %-10s\n" "phase" "chunk_KB" "ghost_rate" "p99_ms"
for tag in baseline hammer; do
    CSV="$OUTDIR/chunk_sweep_${tag}_${TS}.csv"
    [ -f "$CSV" ] || continue
    # CSV columns: chunk_bytes,loss_pct,rounds,ghost_ratio,effective_goodput_MBs,wqe_throughput,p50_ms,p99_ms
    awk -F, -v tag="$tag" 'NR>1 && $2=="0.0" {printf "  %-12s %-10d %-10.6f %-10s\n", tag, $1/1024, $4, $8}' "$CSV"
done

echo ""
echo "Interpretation:"
echo "  - baseline ghost_rate should be ~0% (benign wire)"
echo "  - hammer ghost_rate is the actual UC loss rate SemiRDMA will see"
echo "  - If hammer ghost_rate ~1-5%, we have a good P1 operating point"
echo "  - If hammer ghost_rate >>10%, hammer rate too aggressive — reduce HAMMER_PARALLEL"
