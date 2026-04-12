#!/bin/bash
# SemiRDMA: SoftRoCE (rxe) Environment Setup
# Usage: sudo bash scripts/setup_softroce.sh
#
# Idempotent — safe to run multiple times.

set -euo pipefail

echo "=== SemiRDMA SoftRoCE Setup ==="
echo ""

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] Please run as root: sudo bash $0"
    exit 1
fi

# ── Step 1: Install dependencies ──
echo "[1/4] Installing dependencies..."
apt-get update -qq
apt-get install -y -qq \
    libibverbs-dev \
    librdmacm-dev \
    rdma-core \
    ibverbs-utils \
    rdmacm-utils \
    perftest \
    build-essential \
    2>&1 | tail -1
echo "  Done."

# ── Step 2: Load kernel modules ──
echo "[2/4] Loading kernel modules..."
modules=(rdma_rxe ib_uverbs rdma_ucm)
for mod in "${modules[@]}"; do
    if modprobe "$mod" 2>/dev/null; then
        echo "  Loaded: $mod"
    else
        echo "  Skip:   $mod (not available, may be built-in)"
    fi
done

# ── Step 3: Configure SoftRoCE ──
echo "[3/4] Configuring SoftRoCE device..."

if rdma link show rxe0 >/dev/null 2>&1; then
    echo "  rxe0 already exists — skipping creation."
else
    # Find first non-loopback, UP interface
    IFACE=$(ip -o link show up | awk -F': ' '$2 !~ /^lo$/{print $2; exit}')
    if [ -z "$IFACE" ]; then
        echo "  [ERROR] No active network interface found."
        exit 1
    fi
    echo "  Creating rxe0 on interface: $IFACE"
    rdma link add rxe0 type rxe netdev "$IFACE"
    echo "  Created."
fi

# ── Step 4: Verify ──
echo "[4/4] Verifying..."
echo ""
echo "--- rdma link show ---"
rdma link show
echo ""
echo "--- ibv_devinfo (rxe0) ---"
ibv_devinfo -d rxe0 2>&1 || echo "  [WARN] ibv_devinfo failed — device may need a moment."
echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  cd tests/phase1"
echo "  make"
echo "  bash run_tests.sh"
