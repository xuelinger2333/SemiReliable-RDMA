#!/usr/bin/env bash
# aliyun Day-0 environment setup for Phase 3 Stage A.
#
# Idempotent: safe to re-run.  Assumes Ubuntu 22.04 + SoftRoCE available.

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "=== 1. System packages ==="
apt-get update -qq
apt-get install -qq -y \
    build-essential cmake pkg-config \
    libibverbs-dev rdma-core ibverbs-utils \
    libgtest-dev \
    python3 python3-dev python3-venv python3-pip

echo "=== 2. SoftRoCE device (rxe0) ==="
if ! ibv_devinfo -d rxe0 >/dev/null 2>&1; then
    NETDEV=$(ip -o -4 route show to default | awk '{print $5}')
    echo "Binding rxe0 to ${NETDEV}"
    rdma link add rxe0 type rxe netdev "${NETDEV}"
fi
ibv_devinfo -d rxe0 | head -5

echo "=== 3. uv (fast resolver) ==="
if ! command -v uv >/dev/null 2>&1; then
    pip install -q --index-url https://pypi.tuna.tsinghua.edu.cn/simple uv
fi
uv --version

echo "=== 4. Python 3.10 venv ==="
cd "${REPO_ROOT}"
if [ ! -d .venv ]; then
    uv venv --python 3.10 .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
    "setuptools>=61" wheel pybind11 numpy hydra-core omegaconf pytest pytest-timeout

echo "=== 5. Torch ==="
if ! python -c "import torch" >/dev/null 2>&1; then
    uv pip install --index-url http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
        --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        torch==2.4.1 torchvision==0.19.1 \
        --index-strategy unsafe-best-match
fi

echo "=== 6. C++ build (with bindings) ==="
rm -rf build
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DSEMIRDMA_BUILD_BINDINGS=ON \
    -Dpybind11_DIR="$(python -c 'import pybind11; print(pybind11.get_cmake_dir())')"
cmake --build build -j"$(nproc)"
# Copy the freshly-built .so next to python/semirdma/ so the editable
# install finds it.
cp build/python/semirdma/_semirdma_ext*.so python/semirdma/

echo "=== 7. Editable install ==="
uv pip install --no-deps --no-build-isolation -e .

echo "=== 8. Smoke ==="
python -c "from semirdma import SemiRDMATransport, TransportConfig, semirdma_allreduce_hook; print('OK')"
pytest tests/phase3 -q

echo "=== DONE ==="
