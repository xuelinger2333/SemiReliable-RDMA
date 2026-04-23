#!/usr/bin/env bash
# Empirical probe: RC per-chunk tail latency on CX-5 under natural wire conditions.
#
# Purpose: validate (or refute) the hybrid-AllReduce design proposal
#   - reduce-scatter over UC QP (tail bounded by RatioController cutoff)
#   - all-gather   over RC QP (cross-rank bit-identical ⇒ H3 drift = 0)
# The hybrid's value depends on whether RC all-gather tail stays benign
# on a cloud lossy RoCE wire, OR whether RC's retransmission-timeout events
# dominate the tail and negate the savings vs full-RC AllReduce.
#
# Why not netem: on mlx5 (CX-5/CX-6), tc qdisc netem does NOT touch RoCE QP
# traffic (doorbell bypasses kernel netdev). So we can only measure the
# natural CX-5 wire drop rate here — which RQ1 chunk_sweep already showed
# is non-zero (p99 hit default 5s timeout on every UC cell). The question
# is whether that same natural drop shows up as retransmit tail in RC.
#
# Matrix:
#   transport ∈ {RC, UC}  (UC write-lat is degenerate but recorded for
#                           sanity — see §note below)
#   size      ∈ {4096, 16384, 65536, 262144, 1048576}   (per-chunk → bulk)
#   iters     = 20000 per cell
# Output: ~/SemiRDMA/experiments/results/stage_b/hybrid_tail_probe.csv
#
# Note: ib_write_lat under UC reports send-side CQE latency (no wire RTT),
# which is ~0.5-1 µs regardless of drop. The UC rows are kept only to
# confirm this degeneracy; the meaningful measurement is RC. UC ratio-wait
# phase tail should be read from the RQ1 chunk_sweep CSV, where the test
# harness uses app-level completion semantics (ChunkSet + ratio cutoff).
#
# Usage (from node0):
#   NODE_PEER_HOST=chen123@10.10.1.2  bash scripts/cloudlab/rq_hybrid_tail_probe.sh
#
# Knobs (env):
#   SIZES, ITERS, DEV_THIS, DEV_PEER, NODE1_IP

set -uo pipefail

SIZES="${SIZES:-4096 16384 65536 262144 1048576}"
ITERS="${ITERS:-20000}"
NODE1_IP="${NODE1_IP:-10.10.1.2}"
NODE_PEER_HOST="${NODE_PEER_HOST:-chen123@10.10.1.2}"
PORT="${PORT:-18520}"

DEV_THIS="${DEV_THIS:-$(bash "$(dirname "$0")/detect_rdma_dev.sh")}"
DEV_PEER="${DEV_PEER:-$DEV_THIS}"   # symmetric on amd203/amd196

CSV_OUT="${CSV_OUT:-$HOME/SemiRDMA/experiments/results/stage_b/hybrid_tail_probe.csv}"
mkdir -p "$(dirname "$CSV_OUT")"

# CSV schema matches `ib_write_lat` reporting:
#   min / max / typical(p50) / average / stdev / 99% / 99.9%   (all µs)
echo "transport,size_bytes,iters,min_us,p50_us,avg_us,stdev_us,p99_us,p999_us,max_us" > "$CSV_OUT"

parse_and_append() {
    # Pull the single data line from "*_lat" output.  Representative format:
    #   #bytes  #iterations  t_min[usec]  t_max[usec]  t_typical[usec]  t_avg[usec]  t_stdev[usec]  99%  99.9%
    local transport="$1"
    local size="$2"
    local log="$3"
    local row
    row=$(grep -E "^[[:space:]]*$size[[:space:]]+" "$log" | tail -1)
    if [ -z "$row" ]; then
        echo "  [WARN] no data row for transport=$transport size=$size in $log" >&2
        return
    fi
    # Fields (perftest >= 4.5):  $1=bytes  $2=iters  $3=tmin  $4=tmax  $5=tp50  $6=tavg  $7=tstdev  $8=p99  $9=p999
    local bytes iters tmin tmax tp50 tavg tstdev p99 p999
    read -r bytes iters tmin tmax tp50 tavg tstdev p99 p999 <<<"$row"
    echo "$transport,$bytes,$iters,$tmin,$tp50,$tavg,$tstdev,$p99,$p999,$tmax" >> "$CSV_OUT"
    printf '  [OK] %-3s  %7s B   p50=%7s  p99=%7s  p999=%7s  max=%7s us\n' \
        "$transport" "$bytes" "$tp50" "$p99" "$p999" "$tmax"
}

for TRANSPORT in RC UC; do
    for SIZE in $SIZES; do
        echo
        echo "=== transport=$TRANSPORT  size=${SIZE}B  iters=$ITERS ==="

        # Make sure no stale server listens on the port
        ssh "$NODE_PEER_HOST" "pkill -9 ib_write_lat 2>/dev/null; true" >/dev/null 2>&1 || true
        sleep 1

        # Launch server on peer
        ssh "$NODE_PEER_HOST" \
            "ib_write_lat -c $TRANSPORT -d $DEV_PEER -x 1 -s $SIZE -p $PORT --iters=$ITERS -F > /tmp/hybrid_probe_server.log 2>&1" &
        SERVER_PID=$!
        sleep 2

        # Client
        set +e
        ib_write_lat -c "$TRANSPORT" -d "$DEV_THIS" -x 1 -s "$SIZE" -p "$PORT" --iters="$ITERS" -F "$NODE1_IP" \
            > /tmp/hybrid_probe_client.log 2>&1
        CLIENT_RC=$?
        wait "$SERVER_PID"
        SERVER_RC=$?
        set -e

        if [ "$CLIENT_RC" -ne 0 ]; then
            echo "  [FAIL] client exit=$CLIENT_RC (transport=$TRANSPORT size=$SIZE)" >&2
            tail -20 /tmp/hybrid_probe_client.log | sed 's/^/    | /'
            continue
        fi

        parse_and_append "$TRANSPORT" "$SIZE" /tmp/hybrid_probe_client.log
    done
done

echo
echo "=== DONE.  CSV at $CSV_OUT ==="
column -t -s, "$CSV_OUT"
