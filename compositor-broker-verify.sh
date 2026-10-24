#!/bin/sh
# compositor-broker-verify.sh — wait for startx, then drive AudioOutput
# .SetActiveSink directly at the compositor (no UI needed) and confirm the root
# broker actually moved hw.snd.default_unit. Restores the sink after. Launch in
# the background from the console (or by Claude); then just run `startx`.
set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT_DIR/scripts/compositor-broker-probe.c"
BIN="/tmp/comp-broker-probe.$$"
SOCK=/tmp/freebsdx.sock

cc -O2 -o "$BIN" "$SRC" || { echo "ERROR: compile failed" >&2; exit 1; }
trap 'rm -f "$BIN"' EXIT

echo "=> waiting for compositor ($SOCK) — run startx now (up to 180s)"
i=0; while [ ! -S "$SOCK" ]; do sleep 1; i=$((i+1)); [ $i -ge 180 ] && { echo "TIMEOUT: no startx"; exit 1; }; done
echo "=> compositor up — giving the shell 4s to register services"
sleep 4
"$BIN"
rc=$?
echo
[ $rc -eq 0 ] && echo "RESULT: OK — the full UI-independent chain works; you can exit the desktop." \
             || echo "RESULT: FAIL — the compositor did not apply the sink change via the broker."
exit $rc
