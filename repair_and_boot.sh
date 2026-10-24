#!/bin/sh
# scripts/repair_and_boot.sh - Automated Diagnostic & Repair tool for FreeBSD-X

echo "=> [1/4] Cleaning orphan processes..."
pkill -9 freebsdx 2>/dev/null || true
pkill -9 xshell 2>/dev/null || true
pkill -9 demoapp 2>/dev/null || true
pkill -9 terminal 2>/dev/null || true
pkill -9 calculator 2>/dev/null || true
pkill -9 settings_app 2>/dev/null || true
rm -f /tmp/freebsd-x.sock
rm -f /tmp/freebsd-x-input.sock

echo "=> [1.5/4] Refreshing Premium Branding Assets..."
./out/icon_gen 2>/dev/null || true

echo "=> [2/4] Verifying Seat Management..."
if [ ! -S "/var/run/seatd.sock" ]; then
    echo "   [!] seatd.sock missing. Attempting to start seatd..."
    service seatd onestart 2>/dev/null || echo "   [!] Could not start seatd as user. Please run 'sudo service seatd onestart'"
fi

# Check permissions
if [ ! -w "/var/run/seatd.sock" ]; then
    echo "   [!] Permission denied on seatd.sock. Checking groups..."
    id | grep -q "video" || echo "   [!] You are NOT in the 'video' group. Run: sudo pw groupmod video -m $(whoami)"
fi

echo "=> [3/4] Verifying GPU Drivers..."
kldstat | grep -q "kms" || echo "   [!] No KMS driver detected. Please load i915kms or amdgpu."

echo "=> [4/4] Final Boot Attempt..."
echo "   NOTE: If the screen turns BLUE but nothing happens, press ESCAPE to return."
./scripts/startx
