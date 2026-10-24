#!/bin/sh
# Stage C — step 1: switch xsessiond into libseat-client mode (a client of real
# seatd), so brokered DRM/evdev fds carry proper DRM master + VT control.
#
# Run as ROOT, ideally from an SSH session, and KEEP A SECOND SSH SESSION FREE for
# recovery (pkill -x freebsdx / xsessiond). It does NOT touch the display — that's
# step 2 (launch the compositor with XSESSIOND_USE_BROKER=1).
#
#   sudo sh scripts/stagec-libseat.sh
#
# Expected: "libseat-client mode active (seat brokered)". If instead it reports
# "seat never activated", something still holds the seat (a compositor must be
# stopped first) — nothing grabbed the VT, so it is safe to stop and retry.

set -u

BIN=/usr/local/bin/xsessiond
SOCK=/var/run/xsessiond.sock
LOG=/tmp/xsess-libseat.log

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (sudo sh $0)" >&2
    exit 1
fi
[ -x "$BIN" ] || { echo "ERROR: $BIN missing/not executable" >&2; exit 1; }

# A running compositor holds seatd's active session, which keeps xsessiond's libseat
# seat inactive ("seat never activated"). Bail early with a clear message.
if pgrep -x freebsdx >/dev/null 2>&1; then
    echo "ERROR: freebsdx is running — stop it first (it holds seatd's active session)." >&2
    exit 1
fi

echo "=> stopping default-mode xsessiond + clearing socket"
service xsessiond stop 2>/dev/null || true
pkill -x xsessiond 2>/dev/null || true
sleep 1
rm -f "$SOCK"

echo "=> starting xsessiond with XSESSIOND_LIBSEAT=1 (detached, log -> $LOG)"
: > "$LOG"; chmod 0644 "$LOG"
# nohup so closing the SSH session won't SIGHUP it (FreeBSD has no setsid). It is
# reparented to init and survives this script exiting.
nohup env XSESSIOND_LIBSEAT=1 "$BIN" >> "$LOG" 2>&1 &

# Wait for the activation verdict.
verdict=TIMEOUT
i=0
while [ $i -lt 20 ]; do
    if grep -q "libseat-client mode active" "$LOG" 2>/dev/null; then verdict=OK;   break; fi
    if grep -q "seat never activated"      "$LOG" 2>/dev/null; then verdict=FAIL; break; fi
    pgrep -x xsessiond >/dev/null 2>&1 || { verdict=DIED; break; }
    sleep 0.5; i=$((i + 1))
done

echo
echo "=> pid:    $(pgrep -x xsessiond || echo '(none!)')"
echo "=> socket: $(ls -l "$SOCK" 2>/dev/null || echo '(missing!)')"
echo "=> log tail:"; tail -6 "$LOG" 2>/dev/null | sed 's/^/     /'
echo
case "$verdict" in
  OK)      echo "RESULT: OK — libseat-client mode ACTIVE (seat brokered). Proceed to step 2.";;
  FAIL)    echo "RESULT: FAIL — 'seat never activated' (something holds the seat). Safe: VT not grabbed.";;
  DIED)    echo "RESULT: FAIL — xsessiond exited; see $LOG.";;
  *)       echo "RESULT: TIMEOUT — no verdict yet; inspect $LOG. Do NOT proceed until it says 'libseat-client mode active'.";;
esac
