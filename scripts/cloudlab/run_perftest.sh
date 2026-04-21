#!/usr/bin/env bash
# CloudLab 2-node RDMA bandwidth / latency sanity check via ib_write_bw.
#
# Establishes a Stage B day-0 baseline on the real ConnectX-6 100 GbE link
# BEFORE we touch any SemiRDMA code.  If perftest does not reach ~90 Gbps
# here, no point running our RQ1 chunk sweep on this pair of nodes.
#
# Usage (from the repo root on EITHER node):
#   # Server side:
#   bash scripts/cloudlab/run_perftest.sh server
#
#   # Client side (run second, gives remote exp-LAN IP):
#   bash scripts/cloudlab/run_perftest.sh client 10.10.1.1
#
#   # Latency variant (message size 8 B, 1-thread):
#   MODE=lat bash scripts/cloudlab/run_perftest.sh server
#   MODE=lat bash scripts/cloudlab/run_perftest.sh client 10.10.1.1
#
#   # Custom size sweep (default: 1 × 65536 B, 10s, QPs=1):
#   SIZE=16384 DURATION=20 QPS=4 bash scripts/cloudlab/run_perftest.sh ...
#
# Expected results on d7525 CX-6 / 100 GbE / RoCEv2:
#   ib_write_bw  -s 65536 : ~92-96 Gbps
#   ib_write_lat -s 8     : ~1.5-2.0 µs

set -euo pipefail

ROLE="${1:-}"
PEER="${2:-}"
MODE="${MODE:-bw}"                  # bw | lat
DEV="${DEV:-mlx5_0}"
GID_IDX="${GID_IDX:-1}"              # RoCEv2 GID; check via day0_check.sh
PORT="${PORT:-18515}"                # perftest control TCP port
SIZE="${SIZE:-65536}"
DURATION="${DURATION:-10}"
QPS="${QPS:-1}"

usage() {
    echo "Usage: $0 {server|client [peer_ip]}"
    echo "  env: MODE=bw|lat DEV=mlx5_0 GID_IDX=1 PORT=18515 SIZE=65536 DURATION=10 QPS=1"
    exit 2
}

case "$MODE" in
    bw)  BIN="ib_write_bw"  ;;
    lat) BIN="ib_write_lat" ;;
    *) echo "MODE must be bw or lat, got $MODE"; exit 2 ;;
esac

if ! command -v "$BIN" >/dev/null 2>&1; then
    echo "[FAIL] $BIN not found.  Install: sudo apt-get install -y perftest"
    exit 1
fi

COMMON_ARGS=(-d "$DEV" -x "$GID_IDX" -p "$PORT" -s "$SIZE" -F)
# -F = don't fail on cpufreq governor; CloudLab nodes are 'performance' anyway.

# bw supports multi-QP and duration; lat uses --iters
if [ "$MODE" = "bw" ]; then
    COMMON_ARGS+=(-D "$DURATION" -q "$QPS" --report_gbits)
else
    COMMON_ARGS+=(--iters=10000)
fi

case "$ROLE" in
    server)
        echo "=== $BIN server on $DEV (GID idx $GID_IDX, port $PORT) ==="
        echo "  run on client:  bash $0 client <this-node-IP>"
        exec "$BIN" "${COMMON_ARGS[@]}"
        ;;
    client)
        [ -z "$PEER" ] && usage
        echo "=== $BIN client -> $PEER on $DEV (GID idx $GID_IDX, port $PORT) ==="
        exec "$BIN" "${COMMON_ARGS[@]}" "$PEER"
        ;;
    *)
        usage
        ;;
esac
