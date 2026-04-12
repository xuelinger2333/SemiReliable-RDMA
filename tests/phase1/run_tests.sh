#!/bin/bash
# SemiRDMA Phase 1: Run all UC QP validation tests
#
# Each test launches a server (background) and client (foreground)
# on localhost via SoftRoCE loopback.
#
# Usage: bash run_tests.sh

echo "======================================"
echo "  SemiRDMA Phase 1: UC QP Validation"
echo "======================================"
echo ""

PASS=0
FAIL=0

run_test() {
    local label="$1"
    local binary="$2"

    echo "--- $label ---"
    echo ""

    ./"$binary" server &
    local spid=$!
    sleep 1

    ./"$binary" client 127.0.0.1
    local crc=$?

    wait "$spid" 2>/dev/null
    local src=$?

    if [ $crc -eq 0 ] && [ $src -eq 0 ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        echo "[WARN] $label exit codes: server=$src client=$crc"
    fi
    echo ""
    echo "--------------------------------------------"
    echo ""
}

# ── Build ──
echo "[Build] Compiling ..."
make -j"$(nproc)" 2>&1
if [ $? -ne 0 ]; then
    echo "[ERROR] Compilation failed."
    exit 1
fi
echo "[Build] OK"
echo ""

# ── Tests ──
run_test "Test 1: UC Write-with-Immediate"  test_uc_write_imm
run_test "Test 2: Ghost Gradient"           test_ghost_gradient
run_test "Test 3: WQE Rate Benchmark"       test_wqe_rate

# ── Summary ──
echo "======================================"
echo "  Results:  PASS=$PASS  FAIL=$FAIL"
echo "======================================"
echo ""
echo "Next steps based on results:"
echo "  Test 1 PASS → CQE-driven ratio control is feasible."
echo "  Test 2 PASS → Ghost gradient exists; masked aggregation needed."
echo "  Test 3      → Check if small-chunk WQE rate is sufficient."
echo ""
echo "If all pass, proceed to Week 3-4: Core transport implementation."
