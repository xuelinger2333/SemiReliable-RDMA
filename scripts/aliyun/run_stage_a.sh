#!/usr/bin/env bash
# Usage:
#   bash scripts/aliyun/run_stage_a.sh <transport> <loss_rate> [seed]
#
# Examples:
#   bash scripts/aliyun/run_stage_a.sh gloo     0.00 42    # RQ5-A1 reference
#   bash scripts/aliyun/run_stage_a.sh semirdma 0.00 42    # RQ5-A1 test cell
#   bash scripts/aliyun/run_stage_a.sh semirdma 0.01 42    # RQ5-A2 test cell

set -euo pipefail
TRANSPORT="${1:?transport must be gloo or semirdma}"
LOSS_RATE="${2:-0.0}"
SEED="${3:-42}"
STEPS="${STEPS:-500}"

# Normalize LOSS_RATE numerically so "0.00" vs "0.0" both take the A1 path.
# Without this, the string comparison below misses "0.00" and Stage A's
# loss=0 semirdma run would silently pick ratio=0.95 / timeout=20 — which
# zeroes the last ~5% of chunks every step and breaks Gloo equivalence.
LOSS_RATE_NORM="$(awk -v x="${LOSS_RATE}" 'BEGIN{printf "%g", x}')"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# shellcheck disable=SC1091
source .venv/bin/activate

# Free a contiguous TCP port range for {master_port, semirdma_port,
# semirdma_port+1}.  29500 + 29700/29701 rarely collides on aliyun.
MASTER_PORT="${MASTER_PORT:-29500}"
SEMIRDMA_PORT="${SEMIRDMA_PORT:-29700}"

# A1 wants bit-for-bit equivalence to Gloo, so wait for every chunk.  A
# 47 MiB ResNet-18 bucket is ~2900 chunks and can take seconds to fully
# land on SoftRoCE; 60 s is a generous headroom so GhostMask never kicks
# in for the loss=0 reference run.  A2 deliberately tolerates drop, so
# the 0.95 / 20 ms Phase-2 sweet spot applies.  Callers can override via
# RATIO=... / TIMEOUT_MS=... env.
if [ "${TRANSPORT}" = "semirdma" ] && [ "${LOSS_RATE_NORM}" = "0" ]; then
    RATIO_DEFAULT="1.0"
    TIMEOUT_MS_DEFAULT="60000"
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
