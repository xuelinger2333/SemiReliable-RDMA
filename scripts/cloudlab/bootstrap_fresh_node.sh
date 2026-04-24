#!/usr/bin/env bash
# One-shot bootstrap for a *fresh* CloudLab CX-5 amd-class node.
#
# Assumes:
#   - node is newly allocated under the chen123-302346.rdma-nic-perf-pg0
#     experiment profile (d7525/amd-class, ConnectX-5, 25 GbE experiment
#     LAN on 10.10.1.x, management LAN on 128.110.x)
#   - git is present, the repo has already been cloned to ~/SemiRDMA,
#     and this script is invoked from inside that clone
#
# Chains the existing per-concern scripts so each node needs just one
# command to go from empty → ready for Phase 4 P1 matrix:
#
#   day0_check.sh   hardware / NIC sanity    (read-only)
#   setup_env.sh    apt + venv + torch-cpu   (idempotent)
#   link_setup.sh   PFC off + MTU 9000       (per-reboot)
#   cmake + pybind  build C++ transport      (needs venv active)
#   smoke test      UCQPEngine instantiation (proves stack works)
#
# Idempotent — rerunning is cheap; existing venv / build dir are reused.
#
# Usage:
#   bash scripts/cloudlab/bootstrap_fresh_node.sh          # full
#   SKIP_BUILD=1 bash scripts/cloudlab/bootstrap_fresh_node.sh  # skip C++
#   SKIP_DAY0=1  bash scripts/cloudlab/bootstrap_fresh_node.sh  # skip sanity

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

hdr() { printf '\n========== %s ==========\n' "$1"; }

T0=$(date +%s)

if [ -z "${SKIP_DAY0:-}" ]; then
    hdr "Step 1/5 — day-0 hardware sanity"
    # Non-fatal: day0_check returns 1 if speed or NIC model looks wrong, but
    # we still want bootstrap to keep going so the user can see the later
    # build failures too (one pass, one log).
    bash scripts/cloudlab/day0_check.sh || \
        echo "WARN: day0_check.sh flagged issues — review output above before relying on Stage B assumptions"
fi

hdr "Step 2/5 — apt + venv + torch"
bash scripts/cloudlab/setup_env.sh

hdr "Step 3/5 — link setup (PFC off, MTU 9000)"
bash scripts/cloudlab/link_setup.sh

if [ -n "${SKIP_BUILD:-}" ]; then
    hdr "SKIP_BUILD=1 → bootstrap stops here"
    echo "  apt + venv + link ready; rerun without SKIP_BUILD to build C++."
    exit 0
fi

hdr "Step 4/5 — build C++ transport + pybind extension"
# shellcheck disable=SC1091
source .venv/bin/activate

PYBIND_DIR=$(python -c "import pybind11; print(pybind11.get_cmake_dir())")
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    -DSEMIRDMA_BUILD_BINDINGS=ON \
    -Dpybind11_DIR="${PYBIND_DIR}"
cmake --build build -j "$(nproc)"

# Stage the compiled extension next to the python sources so
# `PYTHONPATH=python python -c "import semirdma"` works.
cp build/python/semirdma/_semirdma_ext*.so python/semirdma/
echo "  pybind extension copied into python/semirdma/"

# Register the semirdma package in the venv (editable install).  Without
# this, torchrun-launched train scripts can't import semirdma (hooks,
# baselines, transport.py) because the repo root isn't on sys.path even
# when CWD is repo-root — torchrun puts ONLY the script's own dir on sys.path.
pip install -e . --no-build-isolation --quiet
echo "  semirdma package installed (editable)"

hdr "Step 5/5 — pybind smoke test"
DEV=$(bash scripts/cloudlab/detect_rdma_dev.sh)
echo "  RDMA device (experiment LAN): $DEV"
PYTHONPATH=python python - <<PY
from semirdma._semirdma_ext import UCQPEngine
e = UCQPEngine("${DEV}", 4*1024*1024, 16, 320)
info = e.local_qp_info()
print(f"  smoke OK   qpn={info.qpn}")
PY

T1=$(date +%s)
hdr "Bootstrap complete"
echo "  host:     $(hostname -f)"
echo "  exp LAN:  $(ip -br addr show enp65s0f0np0 2>/dev/null | awk '{print $3}')"
echo "  RDMA dev: ${DEV}"
echo "  elapsed:  $((T1 - T0))s"
echo ""
echo "Next: on receiver node — run scripts/cloudlab/run_p1_matrix.sh"
echo "      (see its header for MIDDLEBOX_HOST / DROP_RATES invocation)"
