#!/bin/sh
# FreeBSD-X automated test runner.
# Stops any running compositor, runs all test suites, reports results.
# Usage: sh run_tests.sh

set -e
cd "$(dirname "$0")/builddir"

PASS=0
FAIL=0
ERRORS=""

run_suite() {
    name="$1"
    binary="$2"
    result=$(timeout 30 ./"$binary" 2>&1)
    rc=$?
    passed=$(echo "$result" | grep -o '\[  PASSED  \] [0-9]* test' | head -1)
    failed=$(echo "$result" | grep -o '\[  FAILED  \] [0-9]* test' | head -1)
    if [ $rc -eq 0 ] && [ -n "$passed" ]; then
        echo "  ✓  $name — $passed"
        PASS=$((PASS + 1))
    elif [ $rc -eq 124 ]; then
        echo "  ✗  $name — TIMEOUT"
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS\n  $name: timeout"
    else
        echo "  ✗  $name — ${failed:-exit $rc}"
        FAIL=$((FAIL + 1))
        ERRORS="$ERRORS\n  $name: ${failed:-exit $rc}"
    fi
}

echo ""
echo "FreeBSD-X Test Suite"
echo "===================="

# Stop the compositor if running (it holds DRM exclusive access).
if pgrep -qf './freebsdx'; then
    echo "[*] Stopping compositor..."
    pkill -f './freebsdx' 2>/dev/null
    sleep 2
fi

echo ""
echo "Unit Tests:"
run_suite "protocol"       xserver_protocol_test
run_suite "message_pool"   xserver_message_pool_test
run_suite "window_stack"   xserver_window_stack_test
run_suite "shell_util"     xserver_shell_util_test
run_suite "motion_engine"  xserver_motion_engine_test
run_suite "seat_manager"   xserver_seat_manager_test
run_suite "display_server" xserver_display_server_test
run_suite "mime_service"   xserver_mime_service_test
run_suite "ipc_pipeline"   xserver_ipc_pipeline_test
run_suite "xshell_helpers" xshell_helpers_test

echo ""
echo "GPU / Render Tests:"
run_suite "offscreen_render" xserver_offscreen_render_test

echo ""
echo "────────────────────────────────"
TOTAL=$((PASS + FAIL))
echo "  Suites passed: $PASS / $TOTAL"
if [ $FAIL -gt 0 ]; then
    printf "  Failed:%b\n" "$ERRORS"
    echo ""
    exit 1
fi
echo ""
