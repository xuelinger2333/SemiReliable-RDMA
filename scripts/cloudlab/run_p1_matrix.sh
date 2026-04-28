#!/usr/bin/env bash
# Phase 4 · P1/P2 — Lossy-wire decision matrix.
#
# Transport roster (post-2026-04-25 RC-RDMA baseline):
#
#   gloo        Pure PyTorch Gloo-TCP default hook.  "Baseline of
#               baselines" — fully reliable, unaffected by XDP middlebox
#               (talks TCP:29500, not UDP:4791).  Gives the pure-SGD
#               reference curve with no wire-drop noise.
#   rc_lossy    Gloo-TCP all_reduce + shared-seed per-chunk Bernoulli
#               zeroing (drift-free).  Isolates "accuracy degradation
#               from lossy information" from "UC per-rank drift".
#               loss_rate is taken from $drop_rate (app-level
#               simulation; NOT wire drop).
#   rc_rdma     HW-reliable RC QP over the same UCQPEngine as SemiRDMA.
#               Wire loss comes from the XDP middlebox; NIC HW handles
#               ACK/retx; retry-exhausted → IBV_WC_RETRY_EXC_ERR.
#               Exposes real "RC崩" at training layer.
#   semirdma    Your method: UC QP + semi-reliable ghost mask.  Wire
#               loss from the middlebox.
#   semirdma_layer_aware
#               Layer-aware variant: each model layer registers p_L via
#               the YAML ``loss_tolerance:`` block; the dispatcher routes
#               each bucket to RC (if p_bucket < eps_ema + safety_margin)
#               or SemiRDMA UC (with ratio = 1 - p_bucket and T_max
#               derived from continuous wire calibration).  See
#               python/semirdma/layer_aware/.  loss_rate must be 0
#               (wire drop comes from the middlebox).
#
#   timeout_ms ∈ {5, 50, 500}     # meaningful for UC only; RC has its
#                                 # own internal 30s deadline + rc_timeout
#   drop_rate ∈ $DROP_RATES       # wire-level Bernoulli drop via XDP middlebox
#   seed = 42 (single seed; Phase 3 already characterized seed variance)
#   steps = 500
#
# Per-transport loss_rate wiring (per $drop_rate iteration):
#   gloo / rc_rdma / semirdma / semirdma_layer_aware:  loss_rate=0
#   rc_lossy:                                          loss_rate=$drop_rate
#
# Total cells = |DROP_RATES| × |TRANSPORTS| × |TIMEOUTS_MS|.
# Default DROP_RATES="0" → sanity cells only (no middlebox hook).
# Paper P2 matrix: DROP_RATES="0 0.001 0.005 0.01 0.05" TIMEOUTS_MS=50
#                  TRANSPORTS="gloo rc_lossy rc_rdma semirdma" SEED={42,1337}
#                  → 40 cells (~3.5 h on CX-5 25 GbE).
#
# RC timeout sweep (run matrix twice):
#   RC_TIMEOUT=14 RC_RETRY_CNT=7 ...   # default: ~67 ms × 7 ≈ 500 ms/chunk
#   RC_TIMEOUT=18 RC_RETRY_CNT=7 ...   # extreme: ~1 s × 7 ≈ 7 s/chunk
# Only forwarded to transport_cfg when transport=rc_rdma; other
# transports ignore them.
#
# Ordering:
#   outer  = drop_rate (set middlebox once per rate, then inner matrix)
#   middle = transport
#   inner  = timeout (5/50/500 — tight-to-loose)
#
# Topology (driven FROM the receiver node, THIS_NODE=0):
#   Baseline (star, no middlebox):
#     NODE0 (rank 0 + receiver)  ←─ switch ─→  NODE1 (rank 1)
#
#   With XDP middlebox (ARP-spoof "bump in the wire"):
#     NODE1 (sender) ─→ MIDDLEBOX (XDP/eBPF on amd264; was amd186) ─→ NODE0 (receiver)
#     MIDDLEBOX_HOST env var points to middlebox's management address; drop rate is
#     set via ssh + scripts/cloudlab/middlebox_setup.sh set-rate <rate> at the top
#     of each outer iteration.
#
#     CRITICAL: when MIDDLEBOX_HOST is set, training MUST use
#       transport_cfg.gid_index=3
#     because GID idx 1 (IPv6 link-local) has its dst MAC derived by mlx5 HW
#     directly from the GID (no kernel ARP lookup), so the ARP spoof doesn't
#     steer RoCE traffic through the middlebox.  Idx 3 is RoCE v2 IPv4-mapped
#     and DOES consult kernel ARP.  The matrix auto-appends gid_index=3 to the
#     torchrun args when MIDDLEBOX_HOST is non-empty.
#
# Per-cell output:
#   $P1_ROOT/cell_NN_drop<rate>_<transport>_t<to>/          (Hydra run dir)
#     loss_per_step.csv        rank 0 training loss
#     iter_time.csv            per-step wall time
#     grad_norm.csv            L2 norm of gradient tensor
#     completion.csv           (semirdma only) per-bucket n_expected/n_missing
#     .hydra/config.yaml       resolved config (reproducibility)
#
# Aggregate output:
#   $P1_ROOT/MATRIX_SUMMARY.csv    idx,drop_rate,transport,timeout_ms,rc,final_loss,mean_iter_ms
#   $P1_ROOT/MATRIX.log            chronological progress
#
# Usage (on receiver node, e.g. amd247):
#   # Benign wire (no middlebox) — 6 cells, sanity check only:
#   bash scripts/cloudlab/run_p1_matrix.sh
#
#   # Paper main matrix with XDP middlebox (36 cells, ~9 h):
#   DROP_RATES="0 0.001 0.005 0.01 0.02 0.05" \
#     MIDDLEBOX_HOST=chen123@amd264.utah.cloudlab.us \
#     bash scripts/cloudlab/run_p1_matrix.sh
#
#   # Quick smoke (2 drop × 2 transports × 1 timeout = 4 cells, ~6 min):
#   DROP_RATES="0 0.01" TIMEOUTS_MS=50 STEPS=100 \
#     MIDDLEBOX_HOST=chen123@amd264.utah.cloudlab.us \
#     bash scripts/cloudlab/run_p1_matrix.sh
#
# Recover from failure:
#   A failed cell leaves $P1_ROOT/cell_NN_.../ either empty or partial.
#   Rerun the full script — cells that already have a full loss_per_step.csv
#   (STEPS+1 lines) are auto-skipped.

set -uo pipefail

# ================== knobs ==================
TRANSPORTS="${TRANSPORTS:-semirdma}"
TIMEOUTS_MS="${TIMEOUTS_MS:-5 50 500}"
DROP_RATES="${DROP_RATES:-0}"           # Bernoulli wire-level drop via XDP middlebox; "0" = pass-through
SEED="${SEED:-42}"
STEPS="${STEPS:-500}"
WARMUP="${WARMUP:-10}"
RATIO="${RATIO:-0.95}"
PORT_BASE="${PORT_BASE:-32000}"

# RC QP state-transition params (only forwarded when transport=rc_rdma).
#   RC_TIMEOUT is log2(4.096 µs) — 14 ≈ 67 ms per retry; 18 ≈ 1 s
#   RC_RETRY_CNT is total retransmits before IBV_WC_RETRY_EXC_ERR; 7 = max
RC_TIMEOUT="${RC_TIMEOUT:-14}"
RC_RETRY_CNT="${RC_RETRY_CNT:-7}"

# RQ_DEPTH override (semirdma + rc_rdma).  Default is the YAML value
# (16384).  Set RQ_DEPTH to test whether the receive-side WR pool is the
# bottleneck under UC line-rate burst.  CX-5 max_qp_wr is 32768.
RQ_DEPTH="${RQ_DEPTH:-}"

# SQ_DEPTH override.  Default is the YAML value (now 8 — see
# stage_b_cloudlab.yaml header for the 99.55%-vs-67% delivery story).
# Larger SQ depth lets the sender post hundreds of WRs back-to-back; CX-5
# UC silently drops a fraction of those back-to-back IB packets even on
# a benign direct cable.  Tight wave-throttle (SQ=8) keeps NIC TX bursts
# small enough to avoid that loss.
SQ_DEPTH="${SQ_DEPTH:-}"

# CHUNK_BYTES override.  Default is the YAML value (now 4096 == path_mtu)
# so each chunk rides the wire as exactly one IB packet, eliminating UC
# multi-packet PSN-gap loss entirely.  Larger chunks (16K, 64K, ...)
# spread one chunk over multiple IB packets; in UC any single packet
# loss kills the whole chunk.
CHUNK_BYTES="${CHUNK_BYTES:-}"

# LOSS_TOLERANCE_DEFAULT override.  Only consumed by transport=
# semirdma_layer_aware (other transports ignore the field).  Sets the
# global p_L for every layer not explicitly registered via a
# loss_tolerance:<name>=p Hydra override.  Use 0.10 for PR-B uniform-
# budget validation cells (keeps every drop rate on the SemiRDMA route
# given safety_margin=0.005 and drop=0.05).  Default empty = use YAML
# value (0.0 → all layers route to RC; useful for sanity bit-equal
# tests vs rc_rdma).
LOSS_TOLERANCE_DEFAULT="${LOSS_TOLERANCE_DEFAULT:-}"

NODE0_IP="${NODE0_IP:-10.10.1.1}"       # rank 0 + experiment receiver (amd247; was amd203)
NODE1_IP="${NODE1_IP:-10.10.1.2}"       # rank 1 + experiment sender   (amd245; was amd196 @ 10.10.1.3)
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@$NODE1_IP}"

# Middlebox control — empty MIDDLEBOX_HOST disables the hook entirely so the script
# still runs on the star topology for benign-wire sanity checks.  Non-empty means
# we ssh into the middlebox at the top of each DROP_RATES iteration and call
# middlebox_setup.sh set-rate.
MIDDLEBOX_HOST="${MIDDLEBOX_HOST:-}"    # e.g. chen123@amd186.utah.cloudlab.us
MIDDLEBOX_REPO="${MIDDLEBOX_REPO:-\$HOME/SemiRDMA}"

# GID index.  When MIDDLEBOX_HOST is set we MUST force GID idx 3 (IPv4-mapped
# RoCE v2, ::ffff:10.10.1.x) so kernel ARP is consulted for dst MAC lookup
# and the ARP spoof actually steers RoCE through the middlebox.  Idx 1
# (IPv6 link-local) has its MAC derived from the GID by HW and bypasses
# the spoof.  Override with GID_INDEX=N if you know what you're doing.
if [ -n "$MIDDLEBOX_HOST" ]; then
    GID_INDEX="${GID_INDEX:-3}"
else
    GID_INDEX="${GID_INDEX:-1}"
fi

CELL_TIMEOUT="${CELL_TIMEOUT:-900}"     # hard ceiling per cell (15 min)

REPO="${REPO:-$HOME/SemiRDMA}"
DEV_THIS="${DEV_THIS:-$(bash "$REPO/scripts/cloudlab/detect_rdma_dev.sh")}"

MATRIX_TS=$(date +%Y%m%d_%H%M%S)
P1_ROOT="${P1_ROOT:-$REPO/experiments/results/phase4_p1/${MATRIX_TS}}"
mkdir -p "$P1_ROOT"
SUMMARY_CSV="$P1_ROOT/MATRIX_SUMMARY.csv"
MATRIX_LOG="$P1_ROOT/MATRIX.log"
echo "idx,drop_rate,transport,timeout_ms,rc,final_loss,mean_iter_ms,cell_dir" > "$SUMMARY_CSV"

cd "$REPO"
source .venv/bin/activate

# ================== helpers ==================
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$MATRIX_LOG"; }

middlebox_preflight_once=0
middlebox_set_rate() {
    # $1 = drop_rate as decimal (e.g. 0.01 for 1%)
    # No-op when MIDDLEBOX_HOST is empty (baseline star topology, no middlebox).
    [ -z "$MIDDLEBOX_HOST" ] && return 0
    local rate="$1"

    # Preflight once: passwordless sudo is required on the middlebox because
    # set-rate writes a BPF map (CAP_BPF).  Without this check a missing
    # passwordless-sudo config would hang the first ssh indefinitely waiting
    # on a password prompt — we'd rather fail fast with a clear message.
    if [ "$middlebox_preflight_once" = "0" ]; then
        if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$MIDDLEBOX_HOST" "sudo -n true" 2>/dev/null; then
            log "ERR: middlebox $MIDDLEBOX_HOST lacks passwordless sudo — aborting matrix"
            log "     fix on middlebox:  echo \"\$USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/\$USER"
            exit 11
        fi
        middlebox_preflight_once=1
    fi

    # Non-silent on failure so misconfigurations surface in the matrix log —
    # a silent fail would mean cells run at the wrong drop rate and we'd
    # chase phantom "no drops observed" bugs (like earlier attempts did).
    if ! ssh "$MIDDLEBOX_HOST" "sudo bash $MIDDLEBOX_REPO/scripts/cloudlab/middlebox_setup.sh set-rate $rate" 2>&1 | tee -a "$MATRIX_LOG"; then
        log "ERR: middlebox_set_rate $rate on $MIDDLEBOX_HOST failed — aborting matrix"
        exit 10
    fi
    sleep 2   # let forwarder reseed RNG + reset counters
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
        # tr -d '\r' because Python's csv writer on CRLF-aware platforms emits
        # \r\n line endings; without stripping, final_loss ends up as "1.29\r"
        # and contaminates the downstream MATRIX_SUMMARY.csv with an embedded
        # CR that breaks DictReader parsers on Windows/mixed environments.
        final_loss=$(tail -n1 "$cell_dir/loss_per_step.csv" | cut -d, -f2 | tr -d '\r')
    fi
    if [ -f "$cell_dir/iter_time.csv" ]; then
        # iter_time.csv schema:  step,fwd_ms,bwd_ms,opt_ms,total_ms  (col 5 = full iter time, already in ms).
        # Exclude warmup rows (first WARMUP+1 lines = header + warmup steps).
        mean_iter_ms=$(awk -F, -v w="$WARMUP" 'NR>1+w {sum+=$5; n++} END {if(n>0) printf "%.2f", sum/n; else print "?"}' "$cell_dir/iter_time.csv" | tr -d '\r')
    fi
    echo "$final_loss $mean_iter_ms"
}

# ================== matrix loop ==================
cell_idx=0
total_cells=$(( $(echo "$DROP_RATES" | wc -w) * $(echo "$TRANSPORTS" | wc -w) * $(echo "$TIMEOUTS_MS" | wc -w) ))
t0=$(date +%s)

log "=== P1 matrix start (ts=$MATRIX_TS  cells=$total_cells  steps=$STEPS) ==="
log "    transports=[$TRANSPORTS]  timeouts=[$TIMEOUTS_MS]  drop_rates=[$DROP_RATES]  seed=$SEED"
if echo "$TRANSPORTS" | grep -qw rc_rdma; then
    log "    rc_rdma params: rc_timeout=$RC_TIMEOUT (~$(awk -v t=$RC_TIMEOUT 'BEGIN{printf "%.1f", 0.004096 * (2^t)}') ms/retry)  rc_retry_cnt=$RC_RETRY_CNT"
fi
if [ -n "$MIDDLEBOX_HOST" ]; then
    log "    middlebox=$MIDDLEBOX_HOST  (XDP dropbox — wire-level Bernoulli drop on UDP:4791)"
    log "    gid_index=$GID_INDEX (IPv4-mapped RoCE v2 — required for ARP-spoof steering)"
else
    log "    middlebox=(none — star topology, no wire-level drop injection)"
    log "    gid_index=$GID_INDEX"
fi
log "    root=$P1_ROOT"

for drop_rate in $DROP_RATES; do
  log ""
  log "########## DROP_RATE=$drop_rate block ##########"
  middlebox_set_rate "$drop_rate"

  for transport in $TRANSPORTS; do
    for timeout_ms in $TIMEOUTS_MS; do

        cell_tag=$(printf "cell_%02d_drop%s_%s_t%s" \
                   "$cell_idx" "$drop_rate" "$transport" "$timeout_ms")
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

        # Per-transport loss_rate wiring:
        #   rc_lossy uses cfg.loss_rate for its shared-seed app-level
        #     chunk mask (no RDMA involvement, no middlebox effect on it).
        #   gloo / rc_rdma / semirdma / semirdma_layer_aware leave loss_rate=0
        #     because wire drops arrive via the XDP middlebox (or, for gloo,
        #     aren't injected at all — that's exactly gloo's baseline-of-
        #     baselines role).
        case "$transport" in
          rc_lossy) cell_loss_rate="$drop_rate" ;;
          *)        cell_loss_rate=0.0 ;;
        esac

        # RC-specific Hydra overrides.  Use Hydra's `+` prefix because
        # rc_timeout / rc_retry_cnt aren't declared in the YAML (the
        # Python side reads them via transport_cfg.get(..., default)).
        rc_args=""
        if [ "$transport" = "rc_rdma" ]; then
            rc_args="+transport_cfg.rc_timeout=$RC_TIMEOUT +transport_cfg.rc_retry_cnt=$RC_RETRY_CNT"
        fi

        # RQ_DEPTH / SQ_DEPTH / CHUNK_BYTES overrides — only emitted
        # when explicitly requested.  All fields ARE in the YAML so we
        # use plain (no `+`).
        rq_args=""
        if [ -n "$RQ_DEPTH" ]; then
            rq_args="transport_cfg.rq_depth=$RQ_DEPTH"
        fi
        if [ -n "$SQ_DEPTH" ]; then
            rq_args="$rq_args transport_cfg.sq_depth=$SQ_DEPTH"
        fi
        if [ -n "$CHUNK_BYTES" ]; then
            rq_args="$rq_args transport_cfg.chunk_bytes=$CHUNK_BYTES"
        fi
        # Layer-aware uniform default p_L.  Only meaningful for
        # transport=semirdma_layer_aware; emitting it for other
        # transports is harmless (Hydra accepts the override on
        # the top-level YAML key).
        if [ -n "$LOSS_TOLERANCE_DEFAULT" ]; then
            rq_args="$rq_args loss_tolerance_default=$LOSS_TOLERANCE_DEFAULT"
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
  transport=$transport loss_rate=$cell_loss_rate seed=$SEED steps=$STEPS warmup_steps=$WARMUP \
  transport_cfg.dev_name=\$DEV_PEER transport_cfg.ratio=$RATIO transport_cfg.timeout_ms=$timeout_ms \
  transport_cfg.gid_index=$GID_INDEX \
  $rc_args $rq_args \
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
            transport="$transport" loss_rate="$cell_loss_rate" seed="$SEED" \
            steps="$STEPS" warmup_steps="$WARMUP" \
            transport_cfg.dev_name="$DEV_THIS" transport_cfg.ratio="$RATIO" \
            transport_cfg.timeout_ms="$timeout_ms" \
            transport_cfg.gid_index="$GID_INDEX" \
            $rc_args $rq_args \
            dist.semirdma_port="$semi_port" \
            hydra.run.dir="$cell_dir" \
            > "/tmp/p1_this_${cell_tag}.log" 2>&1
        local_rc=$?

        wait "$PEER_PID" 2>/dev/null
        peer_rc=$?

        # ---- aggregate ----
        read final_loss mean_iter_ms < <(parse_cell "$cell_dir")
        log "    this=$local_rc peer=$peer_rc  final_loss=$final_loss  mean_iter_ms=$mean_iter_ms"
        echo "$cell_idx,$drop_rate,$transport,$timeout_ms,$local_rc,$final_loss,$mean_iter_ms,$cell_dir" >> "$SUMMARY_CSV"

        cell_idx=$((cell_idx + 1))
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
