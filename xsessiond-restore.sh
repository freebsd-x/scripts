#!/bin/sh
# xsessiond-restore.sh — put xsessiond back on its normal rc.d service.
#
# Undoes a Stage-C / manual run: kills any hand-started xsessiond (e.g. one
# launched with XSESSIOND_LIBSEAT=1 by stagec-libseat.sh, which is NOT managed
# by rc.d and holds /var/run/xsessiond.sock), clears the stale socket, then
# starts xsessiond via rc.d in its normal default mode (plain OpenDevice broker,
# no libseat). This is the mode the broker-default compositor expects and the
# one that persists across reboots.
#
#   sudo sh scripts/xsessiond-restore.sh
#
# Safe to run even if nothing is wrong — it is idempotent.

set -u

SOCK=/var/run/xsessiond.sock

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (sudo sh $0)" >&2
    exit 1
fi

# A running compositor is using the broker; don't yank the daemon out from under it.
if pgrep -x freebsdx >/dev/null 2>&1; then
    echo "ERROR: freebsdx is running — stop the desktop first (it's using the broker)." >&2
    exit 1
fi

echo "=> stopping any running xsessiond (service + manual)"
service xsessiond stop 2>/dev/null || true
pkill -x xsessiond 2>/dev/null || true
sleep 1

echo "=> clearing stale socket"
rm -f "$SOCK"

echo "=> starting xsessiond via rc.d (default mode)"
if ! service xsessiond start; then
    echo "ERROR: 'service xsessiond start' failed." >&2
    echo "       Check that rc.d is installed (scripts/xsessiond-install.sh) and" >&2
    echo "       xsessiond_enable=YES in rc.conf (sysrc xsessiond_enable=YES)." >&2
    exit 1
fi

# Give it a moment to bind the socket, then report.
i=0
while [ ! -S "$SOCK" ] && [ $i -lt 20 ]; do sleep 0.25; i=$((i + 1)); done

echo
echo "=> pid:    $(pgrep -x xsessiond || echo '(none!)')"
echo "=> socket: $(ls -l "$SOCK" 2>/dev/null || echo '(missing!)')"
echo
service xsessiond status 2>/dev/null || true
echo

if pgrep -x xsessiond >/dev/null 2>&1 && [ -S "$SOCK" ]; then
    echo "RESULT: OK — xsessiond restored to the rc.d service (default broker mode)."
else
    echo "RESULT: FAIL — xsessiond is not running or the socket is missing; see output above." >&2
    exit 1
fi
