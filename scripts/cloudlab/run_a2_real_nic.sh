#!/usr/bin/env bash
# Stage A · A2 + wall-clock convergence on real CX-6 Lx 25 GbE.
#
# Drives the matrix:  transport=semirdma × loss ∈ {0, 0.01, 0.03, 0.05} × seed ∈ {42,123,7}
# at steps=500, warmup=10, ratio=0.95, timeout_ms=5  (the c240g5-tuned
# operating point from docs/phase3/stage-b-phase2-resweep.md §4).
#
# 12 cells × ~8 min/cell ≈ 100 min total wall-clock.
#
# Captures per-cell:  loss_per_step.csv (rank 0 only), iter_time.csv,
# grad_norm.csv, completion.csv (semirdma) — all under
# ~/SemiRDMA/experiments/results/stage_b/<date>/<HH-MM-SS>_semirdma_loss<L>_seed<S>/
#
# Usage (from node0):
#   NODE_PEER_HOST=chen123@10.10.1.2 bash scripts/cloudlab/run_a2_real_nic.sh
#
# Output: tail-watch /tmp/a2_matrix.log for global progress, or per-cell:
#   /tmp/this_a2_cell{N}.log  + /tmp/peer_a2_cell{N}.log
#
# Knobs (env):
#   STEPS / WARMUP / SEEDS / LOSS_RATES / RATIO / TIMEOUT_MS / PORT_BASE

set -uo pipefail

# shellcheck source=_matrix_lib.sh
source "$(dirname "$0")/_matrix_lib.sh"

STEPS="${STEPS:-500}"
WARMUP="${WARMUP:-10}"
SEEDS="${SEEDS:-42 123 7}"
LOSS_RATES="${LOSS_RATES:-0.0 0.01 0.03 0.05}"
RATIO="${RATIO:-0.95}"
TIMEOUT_MS="${TIMEOUT_MS:-5}"
PORT_BASE="${PORT_BASE:-31000}"
THIS_NODE="${THIS_NODE:-0}"
NODE0_IP="${NODE0_IP:-10.10.1.1}"
NODE1_IP="${NODE1_IP:-10.10.1.2}"
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@10.10.1.2}"

REPO="${REPO:-$HOME/SemiRDMA}"
cd "$REPO"
source .venv/bin/activate

if [ -z "${DEV_THIS:-}" ]; then
    DEV_THIS=$(bash scripts/cloudlab/detect_rdma_dev.sh)
fi
echo "this node: rank=$THIS_NODE, DEV=$DEV_THIS"
echo "config: steps=$STEPS warmup=$WARMUP ratio=$RATIO timeout_ms=$TIMEOUT_MS"
echo "loss_rates: [$LOSS_RATES]   seeds: [$SEEDS]"

if [ "$THIS_NODE" = 0 ]; then
    PEER_RANK=1
    THIS_IP="$NODE0_IP"; PEER_IP="$NODE1_IP"
else
    PEER_RANK=0
    THIS_IP="$NODE1_IP"; PEER_IP="$NODE0_IP"
fi

cell_idx=0
total_cells=$(($(echo "$LOSS_RATES" | wc -w) * $(echo "$SEEDS" | wc -w)))
t0=$(date +%s)

for loss in $LOSS_RATES; do
    for seed in $SEEDS; do
        master_port=$((PORT_BASE + cell_idx * 10))
        semi_port=$((PORT_BASE + cell_idx * 10 + 5))
        elapsed=$(( $(date +%s) - t0 ))
        echo
        echo "=== cell #$cell_idx/$total_cells: semirdma loss=$loss seed=$seed (mport=$master_port, sport=$semi_port, elapsed=${elapsed}s) ==="

        # Cell-level skip: don't redo a fully-completed cell on relaunch.
        if cell_already_done "semirdma" "$loss" "$seed" "$STEPS"; then
            echo "  SKIP: prior complete result exists"
            cell_idx=$((cell_idx + 1))
            continue
        fi

        ssh "$NODE_PEER_HOST" "
cd $REPO
source .venv/bin/activate
DEV_PEER=\$(bash scripts/cloudlab/detect_rdma_dev.sh)
SEMIRDMA_PEER_HOST=$THIS_IP \
torchrun --nnodes=2 --node_rank=$PEER_RANK --master_addr=$NODE0_IP --master_port=$master_port --nproc_per_node=1 \
  experiments/stage_a/train_cifar10.py \
  --config-name stage_b_cloudlab \
  transport=semirdma loss_rate=$loss seed=$seed steps=$STEPS warmup_steps=$WARMUP \
  transport_cfg.dev_name=\$DEV_PEER transport_cfg.ratio=$RATIO transport_cfg.timeout_ms=$TIMEOUT_MS \
  dist.semirdma_port=$semi_port \
  > /tmp/peer_a2_cell${cell_idx}.log 2>&1
echo \"peer cell $cell_idx exit \$?\"
" &
        PEER_PID=$!

        sleep 3

        SEMIRDMA_PEER_HOST="$PEER_IP" \
        torchrun --nnodes=2 --node_rank="$THIS_NODE" --master_addr="$NODE0_IP" --master_port="$master_port" --nproc_per_node=1 \
          experiments/stage_a/train_cifar10.py \
          --config-name stage_b_cloudlab \
          transport=semirdma loss_rate="$loss" seed="$seed" steps="$STEPS" warmup_steps="$WARMUP" \
          transport_cfg.dev_name="$DEV_THIS" transport_cfg.ratio="$RATIO" transport_cfg.timeout_ms="$TIMEOUT_MS" \
          dist.semirdma_port="$semi_port" \
          > "/tmp/this_a2_cell${cell_idx}.log" 2>&1
        local_rc=$?

        wait "$PEER_PID"
        peer_rc=$?
        echo "  this exit $local_rc, peer exit $peer_rc"

        cell_idx=$((cell_idx + 1))
    done
done

elapsed=$(( $(date +%s) - t0 ))
echo
echo "=== A2 matrix done.  total elapsed: ${elapsed}s, cells: $total_cells ==="
find ~/SemiRDMA/experiments/results/stage_b -mmin -$((elapsed/60+5)) -name "loss_per_step.csv" \
    | grep semirdma | sort
