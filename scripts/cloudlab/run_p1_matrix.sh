#!/usr/bin/env bash
# Phase 4 · P1 — Lossy-wire decision matrix: does hybrid buy us anything?
#
# Matrix:
#   transport ∈ {semirdma, semirdma_hybrid}
#   timeout_ms ∈ {5, 50, 500}
#   load ∈ {0, 1G}             # 0 = benign wire; 1G = UDP hammer from amd186
#   seed = 42 (single seed; Phase 3 already characterized seed variance)
#   steps = 500, loss_rate = 0  (no synthetic loss — only real wire drops)
#
# 12 cells total.  Ordering:
#   outer  = load    (0 first, then 1G — benign data protected against hammer bugs)
#   middle = transport
#   inner  = timeout (5/50/500 — tight-to-loose)
#
# Topology (driven FROM amd203, THIS_NODE=0):
#   amd203 (10.10.1.1) — rank 0 + experiment receiver + hammer target
#   amd196 (10.10.1.3) — rank 1
#   amd186 (10.10.1.2) — hammer source (iperf3 UDP, 1G, P=1, pkt=1470)
#
# Per-cell output:
#   $P1_ROOT/cell_NN_<transport>_t<to>_load<ld>/            (Hydra run dir)
#     loss_per_step.csv        rank 0 training loss
#     iter_time.csv            per-step wall time
#     grad_norm.csv            L2 norm of gradient tensor
#     completion.csv           (semirdma only) per-bucket n_expected/n_missing
#     .hydra/config.yaml       resolved config (reproducibility)
#
# Aggregate output:
#   $P1_ROOT/MATRIX_SUMMARY.csv    idx,transport,timeout_ms,load,rc,final_loss,mean_iter_ms
#   $P1_ROOT/MATRIX.log            chronological progress
#
# Usage (on amd203):
#   bash scripts/cloudlab/run_p1_matrix.sh                        # default 12 cells
#   STEPS=200 bash scripts/cloudlab/run_p1_matrix.sh              # quick smoke
#   LOADS=0 bash scripts/cloudlab/run_p1_matrix.sh                # benign only
#   TRANSPORTS=semirdma_hybrid TIMEOUTS_MS=5 LOADS=1G bash ...    # single cell
#
# Recover from failure:
#   A failed cell leaves $P1_ROOT/cell_NN_.../ either empty or partial.
#   Rerun the full script — cells that already have a full loss_per_step.csv
#   (STEPS+1 lines) are auto-skipped.

set -uo pipefail

# ================== knobs ==================
TRANSPORTS="${TRANSPORTS:-semirdma semirdma_hybrid}"
TIMEOUTS_MS="${TIMEOUTS_MS:-5 50 500}"
LOADS="${LOADS:-0 1G}"
SEED="${SEED:-42}"
STEPS="${STEPS:-500}"
WARMUP="${WARMUP:-10}"
RATIO="${RATIO:-0.95}"
PORT_BASE="${PORT_BASE:-32000}"

NODE0_IP="${NODE0_IP:-10.10.1.1}"       # amd203, rank 0, hammer target
NODE1_IP="${NODE1_IP:-10.10.1.3}"       # amd196, rank 1
HAMMER_IP="${HAMMER_IP:-10.10.1.2}"     # amd186, hammer source
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@$NODE1_IP}"
HAMMER_HOST="${HAMMER_HOST:-chen123@$HAMMER_IP}"

CELL_TIMEOUT="${CELL_TIMEOUT:-900}"     # hard ceiling per cell (15 min)
HAMMER_PARALLEL="${HAMMER_PARALLEL:-1}"
HAMMER_PKT="${HAMMER_PKT:-1470}"
HAMMER_DUR="${HAMMER_DUR:-$((CELL_TIMEOUT + 60))}"  # outlast the cell

REPO="${REPO:-$HOME/SemiRDMA}"
DEV_THIS="${DEV_THIS:-$(bash "$REPO/scripts/cloudlab/detect_rdma_dev.sh")}"

MATRIX_TS=$(date +%Y%m%d_%H%M%S)
P1_ROOT="${P1_ROOT:-$REPO/experiments/results/phase4_p1/${MATRIX_TS}}"
mkdir -p "$P1_ROOT"
SUMMARY_CSV="$P1_ROOT/MATRIX_SUMMARY.csv"
MATRIX_LOG="$P1_ROOT/MATRIX.log"
echo "idx,transport,timeout_ms,load,rc,final_loss,mean_iter_ms,cell_dir" > "$SUMMARY_CSV"

cd "$REPO"
source .venv/bin/activate

# ================== helpers ==================
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$MATRIX_LOG"; }

hammer_start() {
    local rate="$1"
    ssh "$NODE_PEER_HOST" "pkill -f 'iperf3' 2>/dev/null; true" 2>/dev/null
    ssh "$HAMMER_HOST"    "pkill -f 'iperf3' 2>/dev/null; true" 2>/dev/null
    # Server on the amd203 (target) — but we're on amd203 already.  Run locally.
    pkill -f 'iperf3 -s' 2>/dev/null || true
    bash "$REPO/scripts/cloudlab/hammer_udp.sh" server >/dev/null
    sleep 0.5
    # Client on amd186 (hammer source).
    ssh -f "$HAMMER_HOST" "cd $REPO && PARALLEL=$HAMMER_PARALLEL PKT_SIZE=$HAMMER_PKT \
        nohup bash scripts/cloudlab/hammer_udp.sh client $NODE0_IP $rate $HAMMER_DUR \
        </dev/null >/tmp/hammer_p1_${MATRIX_TS}_$(date +%H%M%S).log 2>&1"
    sleep 3   # hammer ramp
}

hammer_stop() {
    ssh "$HAMMER_HOST" "bash $REPO/scripts/cloudlab/hammer_udp.sh stop" >/dev/null 2>&1 || true
    bash "$REPO/scripts/cloudlab/hammer_udp.sh" stop >/dev/null 2>&1 || true
}

cell_done() {
    # $1 = cell_dir, $2 = expected_lines
    local csv="$1/loss_per_step.csv"
    [ -f "$csv" ] || return 1
    local n
    n=$(wc -l < "$csv" 2>/dev/null || echo 0)
    [ "$n" -eq "$2" ]
}

parse_cell() {
    local cell_dir="$1"
    local final_loss="?" mean_iter_ms="?"
    if [ -f "$cell_dir/loss_per_step.csv" ]; then
        final_loss=$(tail -n1 "$cell_dir/loss_per_step.csv" | cut -d, -f2)
    fi
    if [ -f "$cell_dir/iter_time.csv" ]; then
        # iter_time.csv schema:  step,fwd_ms,bwd_ms,opt_ms,total_ms  (col 5 = full iter time, already in ms).
        # Exclude warmup rows (first WARMUP+1 lines = header + warmup steps).
        mean_iter_ms=$(awk -F, -v w="$WARMUP" 'NR>1+w {sum+=$5; n++} END {if(n>0) printf "%.2f", sum/n; else print "?"}' "$cell_dir/iter_time.csv")
    fi
    echo "$final_loss $mean_iter_ms"
}

# ================== matrix loop ==================
cell_idx=0
total_cells=$(( $(echo "$LOADS" | wc -w) * $(echo "$TRANSPORTS" | wc -w) * $(echo "$TIMEOUTS_MS" | wc -w) ))
t0=$(date +%s)

log "=== P1 matrix start (ts=$MATRIX_TS  cells=$total_cells  steps=$STEPS) ==="
log "    transports=[$TRANSPORTS]  timeouts=[$TIMEOUTS_MS]  loads=[$LOADS]  seed=$SEED"
log "    root=$P1_ROOT"

for load in $LOADS; do
    log ""
    log "--- LOAD=$load block ---"
    for transport in $TRANSPORTS; do
        for timeout_ms in $TIMEOUTS_MS; do

            cell_tag=$(printf "cell_%02d_%s_t%s_load%s" \
                       "$cell_idx" "$transport" "$timeout_ms" "$load")
            cell_dir="$P1_ROOT/$cell_tag"
            master_port=$((PORT_BASE + cell_idx * 10))
            semi_port=$((PORT_BASE + cell_idx * 10 + 5))
            elapsed=$(( $(date +%s) - t0 ))
            log "=== cell #$cell_idx/$total_cells: $cell_tag (t+${elapsed}s) ==="

            if cell_done "$cell_dir" $((STEPS + 1)); then
                log "    SKIP: already complete"
                cell_idx=$((cell_idx + 1))
                continue
            fi

            # ---- start hammer if needed ----
            if [ "$load" != "0" ]; then
                log "    hammer: $HAMMER_HOST → $NODE0_IP @ $load"
                hammer_start "$load"
            fi

            # ---- start peer (amd196, rank 1) in background via ssh ----
            ssh "$NODE_PEER_HOST" "
cd $REPO
source .venv/bin/activate
DEV_PEER=\$(bash scripts/cloudlab/detect_rdma_dev.sh)
SEMIRDMA_PEER_HOST=$NODE0_IP \
torchrun --nnodes=2 --node_rank=1 --master_addr=$NODE0_IP --master_port=$master_port --nproc_per_node=1 \
  experiments/stage_a/train_cifar10.py \
  --config-name stage_b_cloudlab \
  transport=$transport loss_rate=0.0 seed=$SEED steps=$STEPS warmup_steps=$WARMUP \
  transport_cfg.dev_name=\$DEV_PEER transport_cfg.ratio=$RATIO transport_cfg.timeout_ms=$timeout_ms \
  dist.semirdma_port=$semi_port \
  hydra.run.dir=$cell_dir \
  > /tmp/p1_peer_${cell_tag}.log 2>&1
" &
            PEER_PID=$!

            sleep 3

            # ---- run rank 0 locally ----
            SEMIRDMA_PEER_HOST="$NODE1_IP" \
            timeout "$CELL_TIMEOUT" torchrun \
                --nnodes=2 --node_rank=0 --master_addr="$NODE0_IP" \
                --master_port="$master_port" --nproc_per_node=1 \
                experiments/stage_a/train_cifar10.py \
                --config-name stage_b_cloudlab \
                transport="$transport" loss_rate=0.0 seed="$SEED" \
                steps="$STEPS" warmup_steps="$WARMUP" \
                transport_cfg.dev_name="$DEV_THIS" transport_cfg.ratio="$RATIO" \
                transport_cfg.timeout_ms="$timeout_ms" \
                dist.semirdma_port="$semi_port" \
                hydra.run.dir="$cell_dir" \
                > "/tmp/p1_this_${cell_tag}.log" 2>&1
            local_rc=$?

            wait "$PEER_PID" 2>/dev/null
            peer_rc=$?

            # ---- stop hammer if needed ----
            if [ "$load" != "0" ]; then
                hammer_stop
            fi

            # ---- aggregate ----
            read final_loss mean_iter_ms < <(parse_cell "$cell_dir")
            log "    this=$local_rc peer=$peer_rc  final_loss=$final_loss  mean_iter_ms=$mean_iter_ms"
            echo "$cell_idx,$transport,$timeout_ms,$load,$local_rc,$final_loss,$mean_iter_ms,$cell_dir" >> "$SUMMARY_CSV"

            cell_idx=$((cell_idx + 1))
        done
    done
done

elapsed=$(( $(date +%s) - t0 ))
log ""
log "=== P1 matrix done.  total elapsed: ${elapsed}s  cells: $total_cells ==="
log ""
log "=== summary ==="
column -t -s, "$SUMMARY_CSV" | tee -a "$MATRIX_LOG"
log ""
log "=== output tree ==="
ls -d "$P1_ROOT"/cell_*/ 2>/dev/null | tee -a "$MATRIX_LOG"
