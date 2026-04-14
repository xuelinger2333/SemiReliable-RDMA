#!/usr/bin/env bash
#
# run_netem_test.sh — Sweep simulated packet-loss rates over SoftRoCE and
#                     aggregate a CSV of ghost-gradient delivery stats.
#
# NOTE on "netem":
#   The original plan was to use tc netem on the netdev backing rxe0.
#   That does not work on loopback because SoftRoCE (rxe) short-circuits
#   same-host traffic inside the driver and never hands packets to the
#   kernel IP stack — tc / iptables / tcpdump all see zero RDMA packets.
#   We therefore simulate loss at the client side: test_netem_loss draws
#   a geometric "first-lost-packet" index per round and truncates the
#   Write to that length (without IMM).  From the receiver's perspective
#   this is indistinguishable from true PSN-loss ghost gradient.
#
#   When we move to two real hosts (Phase 2 CloudLab ConnectX-5), we can
#   re-enable a real-netem variant; until then this software model is
#   the source of truth for the Phase 1 P0 experiment.
#
# Requirements:
#   - SoftRoCE rxe0 configured and working (verify: rdma link show).
#   - test_netem_loss built (make -C tests/phase1).
#   - NO sudo needed.
#
# Usage:
#   ./scripts/run_netem_test.sh                           # default sweep
#   ROUNDS=200 LOSS_RATES="0 1 5" ./scripts/run_netem_test.sh
#   SEED=7 ./scripts/run_netem_test.sh
#
# Output:
#   experiments/results/phase1-netem/<timestamp>/
#     ├── summary.csv       (loss_pct, rounds, full, partial, none, ...)
#     ├── loss_0.00.log
#     ├── loss_0.10.log
#     └── ...

set -euo pipefail

# ---------------- Config ----------------
DEV="${DEV:-rxe0}"
ROUNDS="${ROUNDS:-500}"
LOSS_RATES="${LOSS_RATES:-0 0.1 0.5 1 2 5}"   # percent, per-packet
SERVER_IP="${SERVER_IP:-127.0.0.1}"
SEED="${SEED:-42}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-1}"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_BIN="${PROJECT_ROOT}/tests/phase1/test_netem_loss"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${PROJECT_ROOT}/experiments/results/phase1-netem/${TS}"
SUMMARY="${OUT_DIR}/summary.csv"

# ---------------- Preconditions ----------------
if [[ ! -x "${TEST_BIN}" ]]; then
    echo "[ERROR] ${TEST_BIN} not found. Build first:" >&2
    echo "        make -C ${PROJECT_ROOT}/tests/phase1" >&2
    exit 1
fi

mkdir -p "${OUT_DIR}"
echo "[INFO] Output dir: ${OUT_DIR}"
echo "[INFO] Sweeping simulated loss rates: ${LOSS_RATES} (% per packet)"
echo "[INFO] Rounds per run: ${ROUNDS}"
echo "[INFO] RNG seed:       ${SEED}"
echo "[INFO] RDMA device:    ${DEV}"
echo

trap 'kill $(jobs -p) 2>/dev/null || true' EXIT INT TERM

# ---------------- CSV header ----------------
echo "loss_pct,rounds,full,partial,none,corrupt,cqe_yes,avg_new_pct,avg_first_old_off" \
    > "${SUMMARY}"

# ---------------- Run sweep ----------------
for loss in ${LOSS_RATES}; do
    log_file="${OUT_DIR}/loss_$(printf '%.2f' "${loss}").log"
    echo "========================================================"
    echo "[RUN] loss = ${loss}% (per-packet, software injection)"
    echo "========================================================"

    server_csv="${OUT_DIR}/.server_csv.$$"
    (
        "${TEST_BIN}" server "${DEV}" "${ROUNDS}" \
            > "${server_csv}" 2>> "${log_file}"
    ) &
    server_pid=$!

    sleep 0.5

    "${TEST_BIN}" client "${SERVER_IP}" "${DEV}" "${ROUNDS}" "${loss}" "${SEED}" \
        >> "${log_file}" 2>> "${log_file}" || true

    wait "${server_pid}" || true

    if [[ -s "${server_csv}" ]]; then
        csv_line="$(tr -d '\r\n' < "${server_csv}")"
        echo "${loss},${csv_line}" >> "${SUMMARY}"
        echo "[OK]  loss=${loss}%  -> ${csv_line}"
    else
        echo "${loss},ERROR" >> "${SUMMARY}"
        echo "[FAIL] loss=${loss}%  (see ${log_file})"
    fi
    rm -f "${server_csv}"

    sleep "${SLEEP_BETWEEN}"
done

echo
echo "========================================================"
echo "Summary CSV: ${SUMMARY}"
echo "========================================================"
column -t -s, "${SUMMARY}" || cat "${SUMMARY}"
