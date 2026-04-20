#!/usr/bin/env bash
# CloudLab Day-0 read-only sanity check.
#
# Run on EACH node right after the experiment reaches Ready state.
# Verifies hardware, RDMA stack, and link speed match Stage B's
# assumptions (2 x d7615, 100 Gbps, ConnectX-5, Ubuntu 22.04).
#
# Does NOT modify system state.  Safe to rerun.
#
# Usage:
#   bash scripts/cloudlab/day0_check.sh                # auto-detect iface
#   IFACE=ens1f1np1 bash scripts/cloudlab/day0_check.sh
#   PEER_IP=10.10.1.2 bash scripts/cloudlab/day0_check.sh  # also pings peer
#
# Exit code 0 = all critical checks pass.  Non-zero = something is off
# and Stage B baseline re-calibration cannot start yet.

set -uo pipefail

FAIL=0
pass() { printf '  [PASS] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n=== %s ===\n' "$1"; }

hdr "Host identity"
echo "  hostname:   $(hostname -f 2>/dev/null || hostname)"
echo "  kernel:     $(uname -r)"
echo "  os:         $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep -m1 PRETTY_NAME)"
echo "  cpu:        $(lscpu | awk -F: '/Model name/ {gsub(/^ +/,"",$2); print $2; exit}')"
echo "  cores:      $(nproc)"
echo "  memory:     $(free -h | awk '/^Mem:/ {print $2}')"
if dmidecode -s system-product-name 2>/dev/null | grep -qi .; then
    echo "  product:    $(sudo -n dmidecode -s system-product-name 2>/dev/null || echo 'need sudo')"
fi

hdr "RDMA devices"
if ! command -v ibv_devices >/dev/null 2>&1; then
    fail "ibv_devices missing — libibverbs-utils not installed (apt-get install -y libibverbs-utils rdma-core)"
else
    ibv_devices
    N_DEV=$(ibv_devices | tail -n +3 | awk 'NF' | wc -l)
    if [ "$N_DEV" -ge 1 ]; then
        pass "$N_DEV RDMA device(s) present"
    else
        fail "no RDMA device — CX-5 driver not loaded?"
    fi
fi

hdr "RDMA device details"
for dev in $(ibv_devices 2>/dev/null | awk 'NR>2 {print $1}'); do
    [ -z "$dev" ] && continue
    echo "--- $dev ---"
    ibv_devinfo -d "$dev" 2>&1 | grep -E \
        'hw_ver|fw_ver|node_guid|state|phys_state|link_layer|active_mtu|active_width|active_speed' \
        | sed 's/^/    /'
done

hdr "Kernel modules"
lsmod | grep -E '^(mlx5_core|mlx5_ib|ib_core|ib_uverbs|rdma_ucm|ib_umad|rdma_cm)' \
    | awk '{print "  " $1}' || true
if lsmod | grep -q '^mlx5_core'; then
    pass "mlx5_core loaded (Mellanox driver)"
else
    warn "mlx5_core not loaded — NIC may not be Mellanox, double-check Day-0 product"
fi

hdr "Network interfaces"
ip -br link show | awk '$1 !~ /^(lo|docker|virbr)/ {print "  " $0}'

# Auto-detect experiment link iface if not given.
if [ -z "${IFACE:-}" ]; then
    # CloudLab usually labels eth1/ens1f1/etc for the experiment lan.
    IFACE=$(ip -br link show \
        | awk '$1 !~ /^(lo|eth0|docker|virbr|eno1)/ && $2 == "UP" {print $1; exit}')
fi

if [ -z "${IFACE:-}" ]; then
    warn "could not auto-detect experiment interface; set IFACE=... and rerun"
else
    hdr "Experiment link: $IFACE"
    SPEED=$(sudo -n ethtool "$IFACE" 2>/dev/null | awk '/Speed:/ {print $2}')
    DRIVER=$(sudo -n ethtool -i "$IFACE" 2>/dev/null | awk '/^driver:/ {print $2}')
    MTU=$(ip -br link show "$IFACE" | awk '{print $NF}')
    IP4=$(ip -br addr show "$IFACE" | awk '{print $3}')
    echo "  speed:   ${SPEED:-?}"
    echo "  driver:  ${DRIVER:-?}"
    echo "  mtu:     ${MTU:-?}"
    echo "  ipv4:    ${IP4:-none}"
    case "${SPEED:-}" in
        100000Mb/s) pass "link is 100 Gbps" ;;
        ""|Unknown!) warn "could not read link speed (need sudo?)" ;;
        *) fail "link speed $SPEED is not 100 Gbps" ;;
    esac
    case "${DRIVER:-}" in
        mlx5_core) pass "driver is mlx5_core (Mellanox CX-5/6)" ;;
        "") warn "driver unknown" ;;
        *) warn "driver $DRIVER — not Mellanox, verify NIC model" ;;
    esac
    # PFC / pause state — Stage B runs 'lossy RoCE' scenario, so PFC=off is fine.
    sudo -n ethtool -a "$IFACE" 2>/dev/null \
        | awk '/^(RX|TX|Autonegotiate)/ {print "  pause " $0}' || true

    if [ -n "${PEER_IP:-}" ]; then
        hdr "Peer reachability ($PEER_IP)"
        if ping -c 3 -W 2 "$PEER_IP" >/dev/null 2>&1; then
            pass "peer $PEER_IP ping OK"
            # Jumbo frame check — PMTUD at MTU 9000
            if ping -c 2 -W 2 -M do -s 8972 "$PEER_IP" >/dev/null 2>&1; then
                pass "jumbo 9000 B MTU end-to-end"
            else
                warn "jumbo 9000 B blocked (PMTUD); MTU may be 1500 on switch path"
            fi
        else
            fail "peer $PEER_IP unreachable"
        fi
    fi
fi

hdr "Build toolchain"
for tool in cmake gcc g++ python3 pip3; do
    if command -v "$tool" >/dev/null 2>&1; then
        echo "  $tool: $($tool --version 2>&1 | head -1)"
    else
        warn "$tool missing"
    fi
done

hdr "RDMA dev-headers (for pybind rebuild)"
for pkg in libibverbs-dev librdmacm-dev rdma-core; do
    if dpkg -l "$pkg" 2>/dev/null | grep -q '^ii'; then
        pass "$pkg installed"
    else
        warn "$pkg missing — apt-get install -y $pkg (needed if rebuilding C++)"
    fi
done

hdr "Perftest availability"
for bin in ib_write_bw ib_write_lat; do
    if command -v "$bin" >/dev/null 2>&1; then
        pass "$bin present"
    else
        warn "$bin missing — apt-get install -y perftest"
    fi
done

hdr "Summary"
if [ "$FAIL" -eq 0 ]; then
    echo "  all critical checks PASS"
    exit 0
else
    echo "  $FAIL critical check(s) FAILED — resolve before Stage B re-calibration"
    exit 1
fi
