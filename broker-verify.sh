#!/bin/sh
# broker-verify.sh — prove the running root xsessiond's privilege broker works
# end-to-end, with ZERO system changes. No startx needed; just the daemon.
#
#   sh scripts/broker-verify.sh
#
# What it checks (via scripts/broker-probe.c, talking to /var/run/xsessiond.sock):
#   • the daemon advertises the Privileged capability (= the new build is running)
#   • an unknown verb is refused with -EINVAL (validate+refuse path)
#   • audio.default_unit set to the CURRENT unit returns 0 (root exec path, no change)
#
# If the daemon is the OLD build (no RunPrivilegedOp), the first check fails and
# you need: sudo sh scripts/xsessiond-install.sh && sudo service xsessiond restart

set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROBE_SRC="$ROOT_DIR/scripts/broker-probe.c"
PROBE_BIN="/tmp/broker-probe.$$"
SOCK="${XSESSIOND_SOCK:-/var/run/xsessiond.sock}"

[ -S "$SOCK" ] || { echo "ERROR: $SOCK not found — is xsessiond running? (service xsessiond status)" >&2; exit 1; }

echo "=> building probe"
if ! cc -O2 -o "$PROBE_BIN" "$PROBE_SRC"; then
    echo "ERROR: failed to compile $PROBE_SRC" >&2; exit 1
fi
trap 'rm -f "$PROBE_BIN"' EXIT

# Current default audio unit so the audio op is a genuine no-op.
UNIT="$(sysctl -n hw.snd.default_unit 2>/dev/null || echo 0)"
echo "=> current hw.snd.default_unit = $UNIT (audio op will set it to itself)"
echo

"$PROBE_BIN" "$UNIT"
rc=$?

echo
if [ $rc -eq 0 ]; then
    echo "RESULT: OK — the broker performs validated ops as root. To verify the"
    echo "        compositor-driven path too, startx and change the DNS resolver /"
    echo "        firewall / audio sink in Settings, then re-check the system state."
else
    echo "RESULT: FAIL — see the per-check lines above."
fi
exit $rc
