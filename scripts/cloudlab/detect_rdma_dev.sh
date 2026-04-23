#!/usr/bin/env bash
# Detect the ACTIVE Mellanox RDMA device on this node.
#
# Why: CloudLab Ubuntu 22.04 with rdma-core ≥ 35 renames RDMA devices via
# /usr/lib/udev/rules.d/60-rdma-persistent-naming.rules — kernel-default
# names like 'mlx5_0/_1/_2/_3' get rewritten to PCI-stable names like
# 'rocep94s0f0' across reboots, and which port is ACTIVE depends on which
# DAC cable went into which physical port (asymmetric across nodes).
#
# Hard-coding the device name in scripts therefore breaks after any reboot
# or hardware swap.  Source this helper instead:
#
#   DEV=$(bash scripts/cloudlab/detect_rdma_dev.sh)
#
# Usage:
#   bash scripts/cloudlab/detect_rdma_dev.sh                   # any ACTIVE
#   bash scripts/cloudlab/detect_rdma_dev.sh enp94s0f0np0      # match netdev
#
# Exit 0 + prints device name on stdout, exit 1 + stderr message otherwise.

set -uo pipefail

want_netdev="${1:-}"

if ! command -v rdma >/dev/null 2>&1; then
    echo "ERR: rdma command missing (apt install -y rdma-core)" >&2
    exit 1
fi

# rdma link show output:
#   link <dev>/<port> state ACTIVE physical_state LINK_UP netdev <iface>
# Pick the first ACTIVE row, optionally filtered by netdev.
line=$(rdma link show 2>/dev/null \
    | awk -v want="$want_netdev" '
        $3=="state" && $4=="ACTIVE" {
            for (i=1; i<=NF; i++) if ($i=="netdev") { iface=$(i+1); break }
            if (want=="" || iface==want) { print $0; exit }
        }')

if [ -z "$line" ]; then
    echo "ERR: no ACTIVE RDMA device${want_netdev:+ on netdev $want_netdev}" >&2
    rdma link show >&2
    exit 1
fi

# $2 is "<dev>/<port>"; strip the /port suffix
echo "$line" | awk '{print $2}' | cut -d/ -f1
