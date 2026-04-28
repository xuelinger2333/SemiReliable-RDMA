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
#
# Selection policy when ``want_netdev`` is unset:
#   1. prefer ACTIVE devices whose netdev has an RFC1918 (10.x / 192.168.x)
#      IPv4 — that's the experiment LAN on multi-NIC hosts. Works for both
#      amd203/amd196 (enp65s0f0np0 @ 10.10.1.x) and amd247/amd245/amd264
#      (eno34np1 @ 10.10.1.x).
#   2. otherwise, prefer ``enp<bus>s<slot>f<func>np<port>`` ACTIVE (legacy
#      single-NIC path, kept so c240g5/d7525-wisc still work without an IP).
#   3. otherwise, fall back to the first ACTIVE row.
# When ``want_netdev`` is set, match it exactly (no preference logic).

# Build a lookup of "iface -> private?" for the IP-preference rule.
declare -A IS_PRIVATE
while read -r ifname state ipv4 _; do
    [ "$state" = "UP" ] || continue
    case "$ipv4" in
        10.*|192.168.*) IS_PRIVATE["$ifname"]=1 ;;
    esac
done < <(ip -br addr show)

line=$(rdma link show 2>/dev/null \
    | awk -v want="$want_netdev" -v privates="${!IS_PRIVATE[*]}" '
        BEGIN { n=split(privates, arr, " "); for (i=1; i<=n; i++) priv[arr[i]]=1 }
        function iface_of(    i, v) {
            for (i=1; i<=NF; i++) if ($i=="netdev") { v=$(i+1); return v }
            return ""
        }
        $3=="state" && $4=="ACTIVE" {
            iface=iface_of()
            if (want != "") {
                if (iface == want) { print $0; found=1; exit }
                next
            }
            # Prefer private-IP iface (experiment LAN). Otherwise prefer
            # enp<bus>s<slot>f<func>np<port>. Otherwise remember as fallback.
            if (iface in priv) { print $0; found=1; exit }
            if (enp_pick == "" && iface ~ /^enp[0-9]+s[0-9]+f[0-9]+np[0-9]+$/) {
                enp_pick = $0
            }
            if (fallback == "") fallback = $0
        }
        END {
            if (!found) {
                if (enp_pick != "") print enp_pick
                else if (fallback != "") print fallback
            }
        }')

if [ -z "$line" ]; then
    echo "ERR: no ACTIVE RDMA device${want_netdev:+ on netdev $want_netdev}" >&2
    rdma link show >&2
    exit 1
fi

# $2 is "<dev>/<port>"; strip the /port suffix
echo "$line" | awk '{print $2}' | cut -d/ -f1
