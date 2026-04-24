#!/usr/bin/env bash
# Phase 4 · P1 — Lossy-wire decision matrix: does hybrid buy us anything?
#
# Matrix:
#   transport ∈ {semirdma, semirdma_hybrid}
#   timeout_ms ∈ {5, 50, 500}
#   drop_rate ∈ $DROP_RATES     # outermost: wire-level Bernoulli drop via DPDK middlebox
#   load ∈ $LOADS               # 0 = benign wire; "line" = optional RDMA hammer overlay
#   seed = 42 (single seed; Phase 3 already characterized seed variance)
#   steps = 500, loss_rate = 0  (app-level synthetic drop off — wire drops come from middlebox)
#
# Total cells = |DROP_RATES| × |TRANSPORTS| × |TIMEOUTS_MS| × |LOADS|.
# Default DROP_RATES="0" + LOADS="0" → 6 cells (today's "benign + no middlebox" behavior).
# Default for the paper main matrix: DROP_RATES="0 0.001 0.005 0.01 0.02 0.05" + LOADS="0"
#   → 36 cells (~9 h on CX-5 25 GbE).
#
# Ordering:
#   outermost = drop_rate (set middlebox once per rate, then inner matrix)
#   outer     = load
#   middle    = transport
#   inner     = timeout (5/50/500 — tight-to-loose)
#
# Topology (driven FROM the receiver node, THIS_NODE=0):
#   Baseline (star, no middlebox):
#     NODE0 (rank 0 + receiver)  ←─ switch ─→  NODE1 (rank 1)
#                                  ↑
#                            amd186 (hammer source, optional overlay)
#   With XDP middlebox (ARP-spoof "bump in the wire"):
#     NODE1 (sender) ─→ MIDDLEBOX (XDP/eBPF on amd186) ─→ NODE0 (receiver)
#     MIDDLEBOX_HOST env var points to middlebox's management address; drop rate is
#     set via ssh + scripts/cloudlab/middlebox_setup.sh set-rate <rate> at the top
#     of each outer iteration.
#
#     CRITICAL: when MIDDLEBOX_HOST is set, training MUST use
#       transport_cfg.gid_index=3
#     because GID idx 1 (IPv6 link-local) has its dst MAC derived by mlx5 HW
#     directly from the GID (no kernel ARP lookup), so the ARP spoof doesn't
#     steer RoCE traffic through amd186.  Idx 3 is RoCE v2 IPv4-mapped and
#     DOES consult kernel ARP.  The matrix auto-appends gid_index=3 to the
#     torchrun args when MIDDLEBOX_HOST is non-empty.
#
# HAMMER_MODE switch (default off):
#   off   — no hammer.  Normal operation.  Used by the DPDK-middlebox main matrix.
#   rdma  — ib_write_bw RC line-rate hammer from $HAMMER_HOST to NODE0.  Used for the
#           "NIC contention as extra stressor" sidecar experiment.  load=line activates it.
#
# Why the hammer is NOT the primary loss source in P1 (recorded here for future-me):
#   UDP hammer at 1 Gbps on CPU-only nodes only triggers Python RQ-refill starvation.
#   ib_write_bw RC line-rate hammer saturates the switch egress but training's offered
#   AllReduce rate (~0.3 Gbps) is too low to force overflow drops on a 25 GbE switch.
#   The DPDK middlebox gives controlled, reproducible wire-level Bernoulli drop at any
#   configured rate — independent of training load, no contention artifacts.
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
#   $P1_ROOT/MATRIX_SUMMARY.csv    idx,drop_rate,transport,timeout_ms,load,rc,final_loss,mean_iter_ms
#   $P1_ROOT/MATRIX.log            chronological progress
#
# Usage (on receiver node, which defaults to amd203's 10.10.1.1):
#   # Benign wire (no middlebox, no hammer) — today's default, 6 cells:
#   bash scripts/cloudlab/run_p1_matrix.sh
#
#   # Full paper main matrix with DPDK middlebox (36 cells, ~9 h):
#   DROP_RATES="0 0.001 0.005 0.01 0.02 0.05" \
#     MIDDLEBOX_HOST=chen123@middlebox-mgmt \
#     bash scripts/cloudlab/run_p1_matrix.sh
#
#   # Legacy: RDMA hammer sidecar (no middlebox, load=0/line):
#   HAMMER_MODE=rdma LOADS="0 line" bash scripts/cloudlab/run_p1_matrix.sh
#
#   # Quick smoke:
#   STEPS=200 TIMEOUTS_MS=50 TRANSPORTS=semirdma bash scripts/cloudlab/run_p1_matrix.sh
#
# Recover from failure:
#   A failed cell leaves $P1_ROOT/cell_NN_.../ either empty or partial.
#   Rerun the full script — cells that already have a full loss_per_step.csv
#   (STEPS+1 lines) are auto-skipped.

set -uo pipefail

# ================== knobs ==================
TRANSPORTS="${TRANSPORTS:-semirdma semirdma_hybrid}"
TIMEOUTS_MS="${TIMEOUTS_MS:-5 50 500}"
LOADS="${LOADS:-0}"                     # "0" = benign; "line" requires HAMMER_MODE=rdma
DROP_RATES="${DROP_RATES:-0}"           # Bernoulli wire-level drop via DPDK middlebox; "0" = middlebox pass-through
SEED="${SEED:-42}"
STEPS="${STEPS:-500}"
WARMUP="${WARMUP:-10}"
RATIO="${RATIO:-0.95}"
PORT_BASE="${PORT_BASE:-32000}"

NODE0_IP="${NODE0_IP:-10.10.1.1}"       # rank 0 + experiment receiver (amd203 in baseline topology)
NODE1_IP="${NODE1_IP:-10.10.1.3}"       # rank 1 + experiment sender (amd196)
HAMMER_IP="${HAMMER_IP:-10.10.1.2}"     # amd186, hammer source (only used when HAMMER_MODE=rdma)
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@$NODE1_IP}"
HAMMER_HOST="${HAMMER_HOST:-chen123@$HAMMER_IP}"

# Middlebox control — empty MIDDLEBOX_HOST disables the hook entirely so the script
# still runs on the star topology (today's behavior).  Non-empty means we ssh into the
# middlebox at the top of each DROP_RATES iteration and call middlebox_setup.sh set-rate.
MIDDLEBOX_HOST="${MIDDLEBOX_HOST:-}"    # e.g. chen123@middlebox.utah.cloudlab.us
MIDDLEBOX_REPO="${MIDDLEBOX_REPO:-\$HOME/SemiRDMA}"

# HAMMER_MODE — off (default) | rdma
# When rdma, LOADS="line" cells activate ib_write_bw RC hammer overlay on top of any
# middlebox-injected drop.  Useful for studying NIC contention on top of wire loss.
HAMMER_MODE="${HAMMER_MODE:-off}"
case "$HAMMER_MODE" in off|rdma) ;; *) echo "ERR: HAMMER_MODE=$HAMMER_MODE (want off|rdma)" >&2; exit 2 ;; esac

CELL_TIMEOUT="${CELL_TIMEOUT:-900}"     # hard ceiling per cell (15 min)
HAMMER_DUR="${HAMMER_DUR:-$((CELL_TIMEOUT + 60))}"  # outlast the cell; passed to ib_write_bw -D

REPO="${REPO:-$HOME/SemiRDMA}"
DEV_THIS="${DEV_THIS:-$(bash "$REPO/scripts/cloudlab/detect_rdma_dev.sh")}"

MATRIX_TS=$(date +%Y%m%d_%H%M%S)
P1_ROOT="${P1_ROOT:-$REPO/experiments/results/phase4_p1/${MATRIX_TS}}"
mkdir -p "$P1_ROOT"
SUMMARY_CSV="$P1_ROOT/MATRIX_SUMMARY.csv"
MATRIX_LOG="$P1_ROOT/MATRIX.log"
echo "idx,drop_rate,transport,timeout_ms,load,rc,final_loss,mean_iter_ms,cell_dir" > "$SUMMARY_CSV"

cd "$REPO"
source .venv/bin/activate

# ================== helpers ==================
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$MATRIX_LOG"; }

middlebox_set_rate() {
    # $1 = drop_rate as decimal (e.g. 0.01 for 1%)
    # No-op when MIDDLEBOX_HOST is empty (baseline star topology, no DPDK forwarder).
    [ -z "$MIDDLEBOX_HOST" ] && return 0
    local rate="$1"
    ssh "$MIDDLEBOX_HOST" "bash $MIDDLEBOX_REPO/scripts/cloudlab/middlebox_setup.sh set-rate $rate" \
        >/dev/null 2>&1 || {
            echo "WARN: middlebox_set_rate $rate on $MIDDLEBOX_HOST failed — continuing" >&2
            return 1
        }
    sleep 2   # let forwarder reseed RNG + reset counters
}

hammer_start() {
    # $1 = rate label (e.g. "line"); ib_write_bw always blasts at wire line-rate,
    # so rate is informational only — passed through for log readability.
    local rate="$1"
    # Wipe any stale hammer process (UDP or RDMA) on all three nodes so the
    # RC QP server port + uverbs resources are clean before we start.
    ssh "$NODE_PEER_HOST" "pkill -f 'iperf3|ib_write_bw' 2>/dev/null; true" 2>/dev/null
    ssh "$HAMMER_HOST"    "pkill -f 'iperf3|ib_write_bw' 2>/dev/null; true" 2>/dev/null
    pkill -f 'iperf3|ib_write_bw' 2>/dev/null || true
    # Server on amd203 (target) — we're already on amd203, so run locally.
    bash "$REPO/scripts/cloudlab/hammer_rdma.sh" server >/dev/null
    sleep 1   # ib_write_bw -D server needs a beat to listen
    # Client on amd186 (hammer source).  Use the SAME simple-ssh pattern that
    # run_uc_loss_calibration.sh uses — hammer_rdma.sh already does nohup + &
    # internally, so wrapping this in `ssh -f ... nohup bash ... &` would kill
    # ib_write_bw via the nested detach (observed: client log 0 bytes, no BW
    # rows in server log).  Plain `ssh "$HAMMER_HOST" "bash ..."` is correct.
    ssh "$HAMMER_HOST" "bash $REPO/scripts/cloudlab/hammer_rdma.sh client $NODE0_IP $rate $HAMMER_DUR" >/dev/null
    sleep 3   # hammer ramp — RC QP reaches line rate within a second
}

hammer_stop() {
    ssh "$HAMMER_HOST" "bash $REPO/scripts/cloudlab/hammer_rdma.sh stop" >/dev/null 2>&1 || true
    bash "$REPO/scripts/cloudlab/hammer_rdma.sh" stop >/dev/null 2>&1 || true
    # Belt + suspenders: kill any orphan ib_write_bw that escaped the stop subcommand.
    ssh "$HAMMER_HOST" "pkill -f ib_write_bw 2>/dev/null; true" 2>/dev/null || true
    pkill -f ib_write_bw 2>/dev/null || true
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
total_cells=$(( $(echo "$DROP_RATES" | wc -w) * $(echo "$LOADS" | wc -w) * $(echo "$TRANSPORTS" | wc -w) * $(echo "$TIMEOUTS_MS" | wc -w) ))
t0=$(date +%s)

log "=== P1 matrix start (ts=$MATRIX_TS  cells=$total_cells  steps=$STEPS) ==="
log "    transports=[$TRANSPORTS]  timeouts=[$TIMEOUTS_MS]  loads=[$LOADS]  drop_rates=[$DROP_RATES]  seed=$SEED"
if [ -n "$MIDDLEBOX_HOST" ]; then
    log "    middlebox=$MIDDLEBOX_HOST  (DPDK dropbox — wire-level Bernoulli drop on UDP:4791)"
else
    log "    middlebox=(none — star topology, no wire-level drop injection)"
fi
case "$HAMMER_MODE" in
    off)  log "    hammer_mode=off  (no ib_write_bw overlay)" ;;
    rdma) log "    hammer_mode=rdma ($HAMMER_HOST → $NODE0_IP line-rate, duration=${HAMMER_DUR}s; activates on load!=0)" ;;
esac
log "    root=$P1_ROOT"

for drop_rate in $DROP_RATES; do
  log ""
  log "########## DROP_RATE=$drop_rate block ##########"
  middlebox_set_rate "$drop_rate"

  for load in $LOADS; do
    log ""
    log "--- LOAD=$load block ---"
    for transport in $TRANSPORTS; do
        for timeout_ms in $TIMEOUTS_MS; do

            cell_tag=$(printf "cell_%02d_drop%s_%s_t%s_load%s" \
                       "$cell_idx" "$drop_rate" "$transport" "$timeout_ms" "$load")
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

            # ---- start hammer if HAMMER_MODE=rdma and load!=0 ----
            hammer_on=0
            if [ "$HAMMER_MODE" = "rdma" ] && [ "$load" != "0" ]; then
                log "    hammer: $HAMMER_HOST → $NODE0_IP @ $load"
                hammer_start "$load"
                hammer_on=1
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

            # ---- stop hammer if it was started for this cell ----
            if [ "$hammer_on" = "1" ]; then
                hammer_stop
            fi

            # ---- aggregate ----
            read final_loss mean_iter_ms < <(parse_cell "$cell_dir")
            log "    this=$local_rc peer=$peer_rc  final_loss=$final_loss  mean_iter_ms=$mean_iter_ms"
            echo "$cell_idx,$drop_rate,$transport,$timeout_ms,$load,$local_rc,$final_loss,$mean_iter_ms,$cell_dir" >> "$SUMMARY_CSV"

            cell_idx=$((cell_idx + 1))
        done
    done
  done
done

# Reset middlebox to pass-through when matrix is done so future reservations aren't
# confused.  No-op when MIDDLEBOX_HOST is empty.
middlebox_set_rate 0

elapsed=$(( $(date +%s) - t0 ))
log ""
log "=== P1 matrix done.  total elapsed: ${elapsed}s  cells: $total_cells ==="
log ""
log "=== summary ==="
column -t -s, "$SUMMARY_CSV" | tee -a "$MATRIX_LOG"
log ""
log "=== output tree ==="
ls -d "$P1_ROOT"/cell_*/ 2>/dev/null | tee -a "$MATRIX_LOG"
