#!/usr/bin/env bash
# One-shot CloudLab environment setup.
#
# Run on EACH node after day0_check.sh passes the hardware section.
# Idempotent: rerunning skips anything already installed.
#
# Installs:
#   - apt: libibverbs-dev + ibverbs-utils + perftest + rdma-core + build-essential
#   - apt: cmake + python3-{pip,venv,dev} + pkg-config + libgtest-dev
#   - venv at ~/SemiRDMA/.venv with torch (cpu), torchvision, pybind11,
#     hydra-core, omegaconf, numpy
#
# Does NOT:
#   - build the C++ library (that's `cmake --build build -j`)
#   - install the Python package (that's `pip install -e .` after cmake)
#
# Usage:
#   bash scripts/cloudlab/setup_env.sh             # all steps
#   SKIP_APT=1 bash scripts/cloudlab/setup_env.sh  # only python venv
#   SKIP_PY=1  bash scripts/cloudlab/setup_env.sh  # only apt

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

hdr() { printf '\n=== %s ===\n' "$1"; }

if [ -z "${SKIP_APT:-}" ]; then
    hdr "APT packages"
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        build-essential cmake pkg-config python3 python3-pip python3-venv python3-dev \
        libibverbs-dev librdmacm-dev ibverbs-utils rdmacm-utils perftest rdma-core \
        libgtest-dev ethtool pciutils iperf3
    echo "  apt deps installed"
fi

if [ -z "${SKIP_PY:-}" ]; then
    hdr "Python venv at .venv"
    if [ ! -d .venv ]; then
        python3 -m venv .venv
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install --quiet -U pip setuptools wheel
    # Torch CPU wheels live on the PyTorch index; others are on PyPI.
    pip install --quiet --index-url https://download.pytorch.org/whl/cpu torch torchvision
    pip install --quiet pybind11 numpy hydra-core omegaconf
    echo "  venv ready: $(python --version), torch=$(python -c 'import torch; print(torch.__version__)')"
fi

hdr "Next steps"
cat <<EOF
  # Configure + build C++ and pybind extension:
  source .venv/bin/activate
  cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \\
      -DSEMIRDMA_BUILD_BINDINGS=ON \\
      -Dpybind11_DIR=\$(python -c "import pybind11; print(pybind11.get_cmake_dir())")
  cmake --build build -j \$(nproc)

  # Pybind extension needs to sit alongside the Python sources:
  cp build/python/semirdma/_semirdma_ext*.so python/semirdma/

  # Smoke-test:
  PYTHONPATH=python python -c "from semirdma._semirdma_ext import UCQPEngine; \\
      e = UCQPEngine('mlx5_0', 4*1024*1024, 16, 320); \\
      print('qpn=', e.local_qp_info().qpn)"
EOF
