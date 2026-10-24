#!/bin/sh
# startx-verify.sh — verify the COMPOSITOR-DRIVEN broker path + the UI dogfoods
# during a live startx session. Run it before and after making changes in
# Settings; it diffs world-readable system state (so no root needed) to prove
# the chain UI → compositor → privilege broker → root → effect actually fired,
# and grabs a screenshot of the dogfooded panels.
#
#   1. sh scripts/startx-verify.sh before
#   2. In the running desktop:
#        • Settings → Network/DNS: set the resolver (e.g. primary 1.1.1.1)
#        • Settings → Sound: switch the output sink
#        • (optional) collapse a Settings section (Expander), step "Sleep After" (Stepper)
#        • Calendar → New Event: the Date field (DatePicker)
#   3. sh scripts/startx-verify.sh after
#
# AFTER prints a before/after diff and a screenshot path. A changed resolv.conf
# (with the "com.freebsdx.DnsConfig" marker) and/or a changed hw.snd.default_unit
# proves the broker applied a real root effect from the UI.

set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CAP="$ROOT_DIR/out/freebsdx-headless-capture"
BEFORE=/tmp/fbxv-before.txt
AFTER=/tmp/fbxv-after.txt

snapshot() {
    echo "resolv.marker=$(awk '/com.freebsdx.DnsConfig/{n++} END{print n+0}' /etc/resolv.conf 2>/dev/null || echo 0)"
    echo "resolv.nameservers=$(awk '/^nameserver/{printf "%s ",$2}' /etc/resolv.conf 2>/dev/null)"
    echo "audio.default_unit=$(sysctl -n hw.snd.default_unit 2>/dev/null || echo '?')"
    echo "backlight=$(backlight 2>/dev/null | awk -F': ' '/brightness/{print $2}')"
    echo "kldstat.count=$(kldstat 2>/dev/null | awk '/0x/{n++} END{print n+0}')"
}

case "${1:-}" in
  before)
    snapshot > "$BEFORE"
    echo "=> baseline captured to $BEFORE:"
    sed 's/^/     /' "$BEFORE"
    echo
    echo "Now, in the running desktop, make changes in Settings (DNS resolver,"
    echo "audio output sink), exercise the dogfoods (collapse a section, Sleep"
    echo "stepper, Calendar date picker), then run:  sh $0 after"
    ;;
  after)
    [ -f "$BEFORE" ] || { echo "ERROR: run '$0 before' first." >&2; exit 1; }
    snapshot > "$AFTER"
    echo "=> before → after:"
    changed=0
    while IFS= read -r bline; do
        key=${bline%%=*}
        aline=$(grep "^$key=" "$AFTER")
        if [ "$bline" != "$aline" ]; then
            echo "  CHANGED  $bline  ->  ${aline#*=}"
            changed=1
        else
            echo "  same     $bline"
        fi
    done < "$BEFORE"
    echo
    if [ "$changed" = 1 ]; then
        echo "=> at least one real system effect changed — the compositor-driven"
        echo "   broker path works (a changed resolv.marker / audio.default_unit is"
        echo "   the strongest proof; backlight is a direct video-group write)."
    else
        echo "=> nothing changed. Either no Settings change was made, or the effect"
        echo "   didn't apply (check xsessiond is the new build: sh scripts/broker-verify.sh)."
    fi
    echo
    # Screenshot of whatever's on screen (open the dogfooded panel first).
    if [ -S /tmp/freebsdx.sock ] && [ -x "$CAP" ]; then
        TS=$(date +%H%M%S); PPM=/tmp/startx-shot-$TS.ppm; PNG=/tmp/startx-shot-$TS.png
        if "$CAP" "$PPM" 2>/dev/null && [ -s "$PPM" ] && pnmtopng "$PPM" > "$PNG" 2>/dev/null; then
            rm -f "$PPM"
            echo "=> screenshot: $PNG  (share it / tell Claude the path for a UI review)"
        else
            echo "=> (screenshot skipped: CaptureFrame unavailable on the live compositor)"
            rm -f "$PPM"
        fi
    else
        echo "=> (screenshot skipped: compositor socket or capture tool missing)"
    fi
    ;;
  *)
    echo "usage: $0 before | after" >&2
    exit 2
    ;;
esac
