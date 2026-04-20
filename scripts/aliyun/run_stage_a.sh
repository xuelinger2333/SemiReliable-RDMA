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
STEPS="${STEPS:-500}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck disable=SC1091
source .venv/bin/activate

# Free a contiguous TCP port range for {master_port, semirdma_port,
# semirdma_port+1}.  29500 + 29700/29701 rarely collides on aliyun.
MASTER_PORT="${MASTER_PORT:-29500}"
SEMIRDMA_PORT="${SEMIRDMA_PORT:-29700}"

# A1 wants bit-for-bit equivalence to Gloo, so wait for every chunk.
# A2 deliberately tolerates drop, so the 0.95/20ms Phase-2 sweet spot
# applies.  Callers can force a specific ratio via RATIO=... env.
if [ "${TRANSPORT}" = "semirdma" ] && [ "${LOSS_RATE}" = "0.0" ]; then
    RATIO_DEFAULT="1.0"
    TIMEOUT_MS_DEFAULT="500"
else
    RATIO_DEFAULT="0.95"
    TIMEOUT_MS_DEFAULT="20"
fi
RATIO="${RATIO:-${RATIO_DEFAULT}}"
TIMEOUT_MS="${TIMEOUT_MS:-${TIMEOUT_MS_DEFAULT}}"

echo ">>> stage_a: transport=${TRANSPORT} loss=${LOSS_RATE} seed=${SEED} steps=${STEPS} ratio=${RATIO} timeout_ms=${TIMEOUT_MS}"

torchrun --nproc_per_node=2 --master_port="${MASTER_PORT}" \
    experiments/stage_a/train_cifar10.py \
    transport="${TRANSPORT}" \
    loss_rate="${LOSS_RATE}" \
    seed="${SEED}" \
    steps="${STEPS}" \
    dist.master_port="${MASTER_PORT}" \
    dist.semirdma_port="${SEMIRDMA_PORT}" \
    transport_cfg.ratio="${RATIO}" \
    transport_cfg.timeout_ms="${TIMEOUT_MS}"
