#!/bin/sh
# scripts/runtime_smoke_test.sh - Headless Runtime Verification for FreeBSD-X

LOG_DIR="$(pwd)/logs"
mkdir -p "$LOG_DIR"

echo "=> [1/4] Cleaning orphan processes and hardware locks..."
pkill -15 freebsdx 2>/dev/null
pkill -15 xshell 2>/dev/null
rm -f /tmp/freebsdx.sock /tmp/freebsd-x-input.sock
sleep 2

# Verify environment before starting
if [ ! -S "/var/run/seatd.sock" ]; then
    echo "ERROR: seatd is not running. Cannot perform hardware tests."
    exit 1
fi

echo "=> [2/4] Starting Master Compositor in Headless Test Mode..."
# --no-input ensures we don't capture the keyboard/CLI session
# LIBSEAT_BACKEND=builtin can be used if seatd is not available, but seatd is preferred
./out/freebsdx --headless --virtual > "$LOG_DIR/freebsdx-health-boot.log" 2>&1 &
SERVER_PID=$!

echo "=> [3/4] Waiting for OS to ignite (Max 20s)..."
count=0
while [ ! -S "/tmp/freebsdx.sock" ] && [ $count -lt 20 ]; do
    printf "."
    sleep 1
    count=$((count+1))
done
echo ""

if [ ! -S "/tmp/freebsdx.sock" ]; then
    echo "ERROR: Server failed to start the IPC socket."
    tail -n 20 "$LOG_DIR/freebsdx-health-boot.log"
    kill -9 $SERVER_PID 2>/dev/null
    exit 1
fi

echo "=> [4/5] Executing Health Diagnostic Suite..."
# Test IPC, telemetry, and GPU without taking console focus
./out/freebsdx-health
HEALTH_RESULT=$?

echo "=> [5/5] Executing Visual Regression Suite (GUI Auditor)..."
# Mathematically verify decorations, blur, and branding
./out/gui_auditor
AUDIT_RESULT=$?

echo "=> Finalizing session..."
# Request graceful shutdown if the health check didn't already
if kill -0 $SERVER_PID 2>/dev/null; then
    pkill -15 freebsdx
    sleep 2
    pkill -9 freebsdx 2>/dev/null
fi

if [ $HEALTH_RESULT -eq 0 ] && [ $AUDIT_RESULT -eq 0 ]; then
    echo "SUCCESS: FreeBSD-X verified working well (Behavioral & Visual)."
    RESULT=0
else
    echo "FAILURE: OS did not meet health or visual requirements."
    RESULT=1
fi

exit $RESULT
