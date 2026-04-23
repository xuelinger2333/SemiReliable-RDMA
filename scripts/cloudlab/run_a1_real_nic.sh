#!/usr/bin/env bash
# Stage A · A1 bit-for-bit equivalence on real CX-6 Lx 25 GbE.
#
# Runs the 6-cell matrix (transport ∈ {gloo, semirdma} × seed ∈ {42,123,7})
# at loss_rate=0.0, ratio=1.0, timeout_ms=60000, steps=100, on the
# 2-node c240g5 setup.  Each cell needs *both* ranks to launch in lockstep,
# so this script runs ONE rank locally and ssh-launches the matching peer
# rank on NODE_PEER for each cell.
#
# Usage (from node0):
#   bash scripts/cloudlab/run_a1_real_nic.sh
#
# Env knobs:
#   STEPS          (default 100)
#   WARMUP         (default 10)
#   SEEDS          (default "42 123 7")
#   TRANSPORTS     (default "gloo semirdma")
#   PORT_BASE      (default 30000;  master_port = PORT_BASE + N*10,
#                                   semirdma_port = PORT_BASE + N*10 + 5)
#   THIS_NODE      (default 0; rank of THIS host)
#   NODE0_IP       (default 10.10.1.1)  — control-plane / DDP rendezvous
#   NODE1_IP       (default 10.10.1.2)
#   NODE_PEER_HOST (default chen123@c240g5-110225.wisc.cloudlab.us)
#                  — used for SSH launching peer rank from THIS_NODE=0
#   DEV_THIS, DEV_PEER  RDMA dev names (auto-detected if unset)
#
# Output: each cell drops a Hydra run dir under
#   ~/SemiRDMA/experiments/results/stage_b/<date>/<HH-MM-SS>_<transport>_loss0.0_seed<S>/
# both nodes write their own dir; only rank 0's dir has loss_per_step.csv.

set -uo pipefail

STEPS="${STEPS:-100}"
WARMUP="${WARMUP:-10}"
SEEDS="${SEEDS:-42 123 7}"
TRANSPORTS="${TRANSPORTS:-gloo semirdma}"
PORT_BASE="${PORT_BASE:-30000}"
THIS_NODE="${THIS_NODE:-0}"
NODE0_IP="${NODE0_IP:-10.10.1.1}"
NODE1_IP="${NODE1_IP:-10.10.1.2}"
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@c240g5-110225.wisc.cloudlab.us}"

REPO="${REPO:-$HOME/SemiRDMA}"
cd "$REPO"
source .venv/bin/activate

# Auto-detect this-node DEV
if [ -z "${DEV_THIS:-}" ]; then
    DEV_THIS=$(bash scripts/cloudlab/detect_rdma_dev.sh)
fi
echo "this node: rank=$THIS_NODE, DEV=$DEV_THIS"

# Compute peer settings
if [ "$THIS_NODE" = 0 ]; then
    PEER_RANK=1
    THIS_IP="$NODE0_IP"
    PEER_IP="$NODE1_IP"
else
    PEER_RANK=0
    THIS_IP="$NODE1_IP"
    PEER_IP="$NODE0_IP"
fi

cell_idx=0
for transport in $TRANSPORTS; do
    for seed in $SEEDS; do
        master_port=$((PORT_BASE + cell_idx * 10))
        semi_port=$((PORT_BASE + cell_idx * 10 + 5))
        echo
        echo "=== cell #$cell_idx: transport=$transport seed=$seed (mport=$master_port, sport=$semi_port) ==="

        # SSH-launch peer rank on the other node, in background
        ssh "$NODE_PEER_HOST" "
cd $REPO
source .venv/bin/activate
DEV_PEER=\$(bash scripts/cloudlab/detect_rdma_dev.sh)
SEMIRDMA_PEER_HOST=$THIS_IP \
torchrun --nnodes=2 --node_rank=$PEER_RANK --master_addr=$NODE0_IP --master_port=$master_port --nproc_per_node=1 \
  experiments/stage_a/train_cifar10.py \
  --config-name stage_b_cloudlab \
  transport=$transport loss_rate=0.0 seed=$seed steps=$STEPS warmup_steps=$WARMUP \
  transport_cfg.dev_name=\$DEV_PEER transport_cfg.ratio=1.0 transport_cfg.timeout_ms=60000 \
  dist.semirdma_port=$semi_port \
  > /tmp/peer_cell${cell_idx}.log 2>&1
echo \"peer cell $cell_idx exit \$?\"
" &
        PEER_PID=$!

        sleep 3   # let peer torchrun bind first

        # Run THIS rank locally
        SEMIRDMA_PEER_HOST="$PEER_IP" \
        torchrun --nnodes=2 --node_rank="$THIS_NODE" --master_addr="$NODE0_IP" --master_port="$master_port" --nproc_per_node=1 \
          experiments/stage_a/train_cifar10.py \
          --config-name stage_b_cloudlab \
          transport="$transport" loss_rate=0.0 seed="$seed" steps="$STEPS" warmup_steps="$WARMUP" \
          transport_cfg.dev_name="$DEV_THIS" transport_cfg.ratio=1.0 transport_cfg.timeout_ms=60000 \
          dist.semirdma_port="$semi_port" \
          > "/tmp/this_cell${cell_idx}.log" 2>&1
        local_rc=$?

        wait "$PEER_PID"
        peer_rc=$?
        echo "  this exit $local_rc, peer exit $peer_rc"

        cell_idx=$((cell_idx + 1))
    done
done

echo
echo "=== matrix done.  Run dirs ==="
find ~/SemiRDMA/experiments/results/stage_b -mmin -60 -name "loss_per_step.csv" | sort
