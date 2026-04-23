#!/usr/bin/env bash
# Stage B · B.5 · RC-Baseline + RC-Lossy real-NIC matrix on CX-6 Lx 25 GbE.
#
# 12-cell matrix matching the A2 SemiRDMA baseline for head-to-head paper
# comparison:
#   rc_baseline × loss=0.0  × seed ∈ {42,123,7}     →  3 cells (reliable control)
#   rc_lossy    × loss ∈ {0.01, 0.03, 0.05} × 3 seed → 9 cells (sim wire loss)
#
# Same step / warmup / chunk / network config as A2 (run_a2_real_nic.sh)
# so iter_time and final loss numbers are directly comparable.
#
# Usage (from node0):
#   NODE_PEER_HOST=chen123@10.10.1.2 \
#     bash scripts/cloudlab/run_b5_real_nic.sh
#
# Output:
#   /tmp/b5_matrix.log                — global progress
#   /tmp/{this,peer}_b5_cell{0..11}.log
#   ~/SemiRDMA/experiments/results/stage_b/<date>/<HH-MM-SS>_<transport>_loss<L>_seed<S>/

set -uo pipefail

# shellcheck source=_matrix_lib.sh
source "$(dirname "$0")/_matrix_lib.sh"

STEPS="${STEPS:-500}"
WARMUP="${WARMUP:-10}"
SEEDS="${SEEDS:-42 123 7}"
PORT_BASE="${PORT_BASE:-32100}"
THIS_NODE="${THIS_NODE:-0}"
NODE0_IP="${NODE0_IP:-10.10.1.1}"
NODE1_IP="${NODE1_IP:-10.10.1.2}"
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@10.10.1.2}"

REPO="${REPO:-$HOME/SemiRDMA}"
cd "$REPO"
source .venv/bin/activate

echo "this node: rank=$THIS_NODE"
echo "config: steps=$STEPS warmup=$WARMUP"
echo "seeds: [$SEEDS]"

if [ "$THIS_NODE" = 0 ]; then
    PEER_RANK=1; THIS_IP="$NODE0_IP"; PEER_IP="$NODE1_IP"
else
    PEER_RANK=0; THIS_IP="$NODE1_IP"; PEER_IP="$NODE0_IP"
fi

# Matrix: list of (transport, loss_rate) pairs
PAIRS=(
    "rc_baseline 0.0"
    "rc_lossy    0.01"
    "rc_lossy    0.03"
    "rc_lossy    0.05"
)

cell_idx=0
total_cells=$((${#PAIRS[@]} * $(echo "$SEEDS" | wc -w)))
t0=$(date +%s)

for pair in "${PAIRS[@]}"; do
    read transport loss <<< "$pair"
    for seed in $SEEDS; do
        master_port=$((PORT_BASE + cell_idx * 10))
        elapsed=$(( $(date +%s) - t0 ))
        echo
        echo "=== cell #$cell_idx/$total_cells: $transport loss=$loss seed=$seed (mport=$master_port, elapsed=${elapsed}s) ==="

        # Cell-level skip: don't redo a fully-completed cell on relaunch.
        if cell_already_done "$transport" "$loss" "$seed" "$STEPS"; then
            echo "  SKIP: prior complete result exists"
            cell_idx=$((cell_idx + 1))
            continue
        fi

        ssh "$NODE_PEER_HOST" "
cd $REPO
source .venv/bin/activate
torchrun --nnodes=2 --node_rank=$PEER_RANK --master_addr=$NODE0_IP --master_port=$master_port --nproc_per_node=1 \
  experiments/stage_a/train_cifar10.py \
  --config-name stage_b_cloudlab \
  transport=$transport loss_rate=$loss seed=$seed steps=$STEPS warmup_steps=$WARMUP \
  > /tmp/peer_b5_cell${cell_idx}.log 2>&1
echo \"peer cell $cell_idx exit \$?\"
" &
        PEER_PID=$!

        sleep 3

        torchrun --nnodes=2 --node_rank="$THIS_NODE" --master_addr="$NODE0_IP" --master_port="$master_port" --nproc_per_node=1 \
          experiments/stage_a/train_cifar10.py \
          --config-name stage_b_cloudlab \
          transport="$transport" loss_rate="$loss" seed="$seed" steps="$STEPS" warmup_steps="$WARMUP" \
          > "/tmp/this_b5_cell${cell_idx}.log" 2>&1
        local_rc=$?

        wait "$PEER_PID"
        peer_rc=$?
        echo "  this exit $local_rc, peer exit $peer_rc"

        cell_idx=$((cell_idx + 1))
    done
done

elapsed=$(( $(date +%s) - t0 ))
echo
echo "=== B.5 matrix done.  total elapsed: ${elapsed}s, cells: $total_cells ==="
find ~/SemiRDMA/experiments/results/stage_b -mmin -$((elapsed/60+5)) -name "loss_per_step.csv" \
    | grep -E "rc_baseline|rc_lossy" | sort
