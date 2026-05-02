#!/usr/bin/env bash
# E1 grid completion — runs missing cells in-process (no ssh self-loop).
#
# Usage on the node itself:
#   nohup bash scripts/phase5/e1_run_remaining.sh amd247 \
#       > experiments/results/phase5/e1/logs/amd247.runner2.log 2>&1 &
#
# Each cell takes ~22 min; 7-8 cells per node ≈ 3 hours.
set -uo pipefail

NODE=${1:?"node arg required: amd247 or amd245"}
REPO=${REPO:-/users/chen123/SemiRDMA}
DATA=${DATA:-/users/chen123/data/cifar10}
LOGDIR=${LOGDIR:-$REPO/experiments/results/phase5/e1/logs}
DEV=${DEV:-mlx5_2}
GID=${GID:-1}
STEPS=${STEPS:-200}
CELL_TIMEOUT=${CELL_TIMEOUT:-3000}  # 50 min; clear_t1 with loss>0 needs ~40 min

# Cell descriptors: id transport drop seed master_port semi_port
case "$NODE" in
  amd247)
    CELLS=(
      "22 phase4_prc 0.01 42 29511 29744"
      "24 phase4_prc 0.05 41 29512 29748"
      "26 phase4_prc 0.05 43 29513 29752"
      "28 clear_t1   0    42 29514 29756"
      "30 clear_t1   0.01 41 29515 29760"
      "32 clear_t1   0.01 43 29516 29764"
      "34 clear_t1   0.05 42 29517 29768"
    )
    ;;
  amd247_retry)
    # Re-run cells that hit the 30 min timeout under the original
    # CELL_TIMEOUT=1800; new default is 3000 s.
    CELLS=(
      "30 clear_t1 0.01 41 29515 29760"
      "32 clear_t1 0.01 43 29516 29764"
      "34 clear_t1 0.05 42 29517 29768"
    )
    ;;
  amd245)
    CELLS=(
      "21 phase4_prc 0.01 41 29610 29840"
      "23 phase4_prc 0.01 43 29611 29844"
      "25 phase4_prc 0.05 42 29612 29848"
      "27 clear_t1   0    41 29613 29852"
      "29 clear_t1   0    43 29614 29856"
      "31 clear_t1   0.01 42 29615 29860"
      "33 clear_t1   0.05 41 29616 29864"
      "35 clear_t1   0.05 43 29617 29868"
    )
    ;;
  amd245_retry)
    CELLS=(
      "31 clear_t1 0.01 42 29615 29860"
      "33 clear_t1 0.05 41 29616 29864"
      "35 clear_t1 0.05 43 29617 29868"
    )
    ;;
  amd247_clear_redo)
    # Re-run after ratio-margin fix (commit 211259c). Only clear_t1
    # cells were affected by the timeout bug; phase4 / rc_baseline
    # data from the original grid is still valid.
    CELLS=(
      "28 clear_t1 0    42 29514 29756"
      "30 clear_t1 0.01 41 29515 29760"
      "32 clear_t1 0.01 43 29516 29764"
      "34 clear_t1 0.05 42 29517 29768"
    )
    ;;
  amd245_clear_redo)
    CELLS=(
      "27 clear_t1 0    41 29613 29852"
      "29 clear_t1 0    43 29614 29856"
      "31 clear_t1 0.01 42 29615 29860"
      "33 clear_t1 0.05 41 29616 29864"
      "35 clear_t1 0.05 43 29617 29868"
    )
    ;;
  *) echo "unknown node: $NODE"; exit 1;;
esac

cd "$REPO"
source .venv/bin/activate
export RDMA_LOOPBACK_DEVICE="$DEV"
export RDMA_LOOPBACK_GID_INDEX="$GID"
export SEMIRDMA_PEER_HOST=127.0.0.1
export HYDRA_FULL_ERROR=1

mkdir -p "$LOGDIR"

ts() { date '+[%H:%M:%S]'; }

for desc in "${CELLS[@]}"; do
  read -r CID TRANSPORT DROP SEED MASTER_PORT SEMI_PORT <<<"$desc"

  d_tag=$(printf "%g" "$DROP")
  case "$DROP" in 0|0.0|0.000) d_tag=0;; esac
  TAG="c${CID}_${TRANSPORT}_loss${d_tag}_seed${SEED}"
  LOG="$LOGDIR/${TAG}.log"

  # Skip if already DONE with full step count.
  if [[ -f "$LOG" ]]; then
    n=$(grep -oE 'training done: [0-9]+' "$LOG" 2>/dev/null | grep -oE '[0-9]+' | sort -n | tail -1)
    if [[ -n "$n" && "$n" -ge "$STEPS" ]]; then
      echo "$(ts) === $TAG already DONE (n=$n)"
      continue
    fi
    mv "$LOG" "${LOG}.prev_$(date +%s)" 2>/dev/null || true
  fi

  # Map phase4_* labels to actual code transport.
  case "$TRANSPORT" in
    phase4_flat|phase4_prc) CODE_T=semirdma;;
    *)                       CODE_T=$TRANSPORT;;
  esac

  if [[ "$TRANSPORT" == "clear_t1" ]]; then
    CFG_ARGS="--config-name phase5_e1"
    EXTRAS=""
  else
    CFG_ARGS=""
    EXTRAS="transport_cfg.dev_name=$DEV +transport_cfg.gid_index=$GID transport_cfg.chunk_bytes=16384 transport_cfg.sq_depth=4096 transport_cfg.rq_depth=8192 +bucket_cap_mb=512"
  fi

  echo "$(ts) >>> $TAG master=$MASTER_PORT semi=$SEMI_PORT"
  PYTHONPATH="$REPO/python" timeout "$CELL_TIMEOUT" torchrun \
    --nproc_per_node=2 --master_port="$MASTER_PORT" \
    "$REPO/experiments/stage_a/train_cifar10.py" \
    $CFG_ARGS \
    transport="$CODE_T" loss_rate="$DROP" seed="$SEED" steps="$STEPS" \
    data.root="$DATA" data.download=false \
    dist.semirdma_port="$SEMI_PORT" \
    $EXTRAS \
    > "$LOG" 2>&1
  RC=$?

  n=$(grep -oE 'training done: [0-9]+' "$LOG" 2>/dev/null | grep -oE '[0-9]+' | sort -n | tail -1)
  if [[ -n "$n" && "$n" -ge "$STEPS" ]]; then
    echo "$(ts) <<< $TAG DONE n=$n rc=$RC"
  else
    echo "$(ts) !!! $TAG FAILED rc=$RC n=${n:-NONE}"
  fi

  # 60 s cooldown for TIME_WAIT on master_port.
  sleep 60
done

echo "$(ts) ALL CELLS PROCESSED"
