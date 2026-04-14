#!/usr/bin/env bash
#
# run_netem_test.sh — Sweep packet-loss rates over SoftRoCE using tc netem,
#                     run test_netem_loss for each, and aggregate a CSV.
#
# Purpose (Phase 1 P0):
#   Validate packet-loss-induced ghost gradient on UC QP. Each loss rate
#   drives a full run of test_netem_loss (server + client on localhost),
#   and results are collected into a single CSV for the report.
#
# Requirements:
#   - SoftRoCE rxe0 configured and bound to a netdev (default: auto-detect
#     via `rdma link show`; override with NETDEV=<iface>).
#   - tc from iproute2 with netem (ships with most distros).
#   - sudo privileges (tc qdisc requires CAP_NET_ADMIN).
#   - test_netem_loss built (run `make -C tests/phase1`).
#
# Usage:
#   sudo ./scripts/run_netem_test.sh                     # default sweep
#   sudo NETDEV=lo ./scripts/run_netem_test.sh
#   sudo ROUNDS=200 LOSS_RATES="0 1 5" ./scripts/run_netem_test.sh
#
# Output:
#   experiments/results/phase1-netem/<timestamp>/
#     ├── summary.csv       (loss_pct, rounds, full, partial, none, ...)
#     ├── loss_0.00.log
#     ├── loss_0.10.log
#     └── ...

set -euo pipefail

# ---------------- Config ----------------
NETDEV="${NETDEV:-}"                     # empty = auto-detect
DEV="${DEV:-rxe0}"
ROUNDS="${ROUNDS:-500}"
LOSS_RATES="${LOSS_RATES:-0 0.1 0.5 1 2 5}"   # percent
SERVER_IP="${SERVER_IP:-127.0.0.1}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-1}"      # seconds between runs

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEST_BIN="${PROJECT_ROOT}/tests/phase1/test_netem_loss"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${PROJECT_ROOT}/experiments/results/phase1-netem/${TS}"
SUMMARY="${OUT_DIR}/summary.csv"

# ---------------- Preconditions ----------------
if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] This script modifies tc qdisc and must run as root (sudo)." >&2
    exit 1
fi

if [[ ! -x "${TEST_BIN}" ]]; then
    echo "[ERROR] ${TEST_BIN} not found. Build first:" >&2
    echo "        make -C ${PROJECT_ROOT}/tests/phase1" >&2
    exit 1
fi

# Auto-detect netdev backing rxe0 if not provided.
# `rdma link show` output example:
#   link rxe0/1 state ACTIVE physical_state LINK_UP netdev lo
if [[ -z "${NETDEV}" ]]; then
    NETDEV="$(rdma link show 2>/dev/null \
              | awk -v d="${DEV}" '$0 ~ d {for (i=1;i<=NF;i++) if ($i=="netdev") {print $(i+1); exit}}')"
    if [[ -z "${NETDEV}" ]]; then
        echo "[ERROR] Could not auto-detect netdev for ${DEV}. Set NETDEV=<iface>." >&2
        echo "        Hint: rdma link show" >&2
        exit 1
    fi
    echo "[INFO] Auto-detected ${DEV} -> netdev ${NETDEV}"
fi

mkdir -p "${OUT_DIR}"
echo "[INFO] Output dir: ${OUT_DIR}"
echo "[INFO] Sweeping loss rates: ${LOSS_RATES} (%)"
echo "[INFO] Rounds per run:      ${ROUNDS}"
echo "[INFO] netdev for netem:    ${NETDEV}"
echo

# ---------------- Cleanup hook ----------------
cleanup_tc() {
    tc qdisc del dev "${NETDEV}" root 2>/dev/null || true
}
trap 'cleanup_tc; kill $(jobs -p) 2>/dev/null || true' EXIT INT TERM

# Clear any stale qdisc before starting.
cleanup_tc

# ---------------- CSV header ----------------
echo "loss_pct,rounds,full,partial,none,corrupt,cqe_yes,avg_new_pct,avg_first_old_off" \
    > "${SUMMARY}"

# ---------------- Run sweep ----------------
for loss in ${LOSS_RATES}; do
    log_file="${OUT_DIR}/loss_$(printf '%.2f' "${loss}").log"
    echo "========================================================"
    echo "[RUN] loss = ${loss}%"
    echo "========================================================"

    # Apply netem (replace any existing qdisc).
    if [[ "${loss}" == "0" || "${loss}" == "0.0" || "${loss}" == "0.00" ]]; then
        # Still install an empty netem so behavior stays uniform.
        tc qdisc replace dev "${NETDEV}" root netem loss 0%
    else
        tc qdisc replace dev "${NETDEV}" root netem loss "${loss}%"
    fi
    tc -s qdisc show dev "${NETDEV}" | tee -a "${log_file}"
    echo >> "${log_file}"

    # Launch server (capture stdout = CSV line, stderr = human log).
    server_csv="${OUT_DIR}/.server_csv.$$"
    (
        "${TEST_BIN}" server "${DEV}" "${ROUNDS}" \
            > "${server_csv}" 2>> "${log_file}"
    ) &
    server_pid=$!

    # Small delay so server's TCP listen is ready.
    sleep 0.5

    # Launch client (stderr -> log, stdout ignored).
    "${TEST_BIN}" client "${SERVER_IP}" "${DEV}" "${ROUNDS}" \
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

    # Remove qdisc between runs for a clean state.
    cleanup_tc
    sleep "${SLEEP_BETWEEN}"
done

echo
echo "========================================================"
echo "Summary CSV: ${SUMMARY}"
echo "========================================================"
column -t -s, "${SUMMARY}" || cat "${SUMMARY}"
