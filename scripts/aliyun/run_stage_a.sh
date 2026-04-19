#!/usr/bin/env bash
# Usage:
#   bash scripts/aliyun/run_stage_a.sh <transport> <loss_rate> [seed]
#
# Examples:
#   bash scripts/aliyun/run_stage_a.sh gloo     0.00 42    # RQ5-A1 reference
#   bash scripts/aliyun/run_stage_a.sh semirdma 0.00 42    # RQ5-A1 test cell
#   bash scripts/aliyun/run_stage_a.sh semirdma 0.01 42    # RQ5-A2 test cell

set -euo pipefail
TRANSPORT="${1:?transport={gloo|semirdma}}"
LOSS_RATE="${2:-0.0}"
SEED="${3:-42}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck disable=SC1091
source .venv/bin/activate

# Free a contiguous TCP port range for {master_port, semirdma_port,
# semirdma_port+1}.  29500 + 29700/29701 rarely collides on aliyun.
MASTER_PORT="${MASTER_PORT:-29500}"
SEMIRDMA_PORT="${SEMIRDMA_PORT:-29700}"

echo ">>> stage_a: transport=${TRANSPORT} loss=${LOSS_RATE} seed=${SEED}"

torchrun --nproc_per_node=2 --master_port="${MASTER_PORT}" \
    experiments/stage_a/train_cifar10.py \
    transport="${TRANSPORT}" \
    loss_rate="${LOSS_RATE}" \
    seed="${SEED}" \
    dist.master_port="${MASTER_PORT}" \
    dist.semirdma_port="${SEMIRDMA_PORT}"
