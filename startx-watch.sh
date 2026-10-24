#!/bin/sh
# startx-watch.sh — auto-capture the desktop across a whole startx session, for
# when you can't run a shell inside the session. Launched in the background from
# the console (or by Claude); it waits for the compositor, grabs a frame every
# few seconds while it's up (keeping the most recent handful), and on exit prints
# the final system-state diff vs the startx-verify baseline.
set -u
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CAP="$ROOT_DIR/out/freebsdx-headless-capture"
OUT=/tmp/fbxwatch
BEFORE=/tmp/fbxv-before.txt
SOCK=/tmp/freebsdx.sock
mkdir -p "$OUT"; rm -f "$OUT"/frame-*.png

snapshot() {
    echo "resolv.marker=$(awk '/com.freebsdx.DnsConfig/{n++} END{print n+0}' /etc/resolv.conf 2>/dev/null || echo 0)"
    echo "resolv.nameservers=$(awk '/^nameserver/{printf "%s ",$2}' /etc/resolv.conf 2>/dev/null)"
    echo "audio.default_unit=$(sysctl -n hw.snd.default_unit 2>/dev/null || echo '?')"
    echo "backlight=$(backlight 2>/dev/null | awk -F': ' '/brightness/{print $2}')"
}

echo "=> waiting for compositor (run startx now; up to 180s)"
i=0; while [ ! -S "$SOCK" ]; do sleep 1; i=$((i+1)); [ $i -ge 180 ] && { echo "TIMEOUT: no startx"; exit 1; }; done
echo "=> compositor up — capturing a frame every 8s while it runs"
[ -f "$BEFORE" ] || snapshot > "$BEFORE"

seq=0; got=0
while [ -S "$SOCK" ]; do
    ppm="$OUT/f.ppm"
    if "$CAP" "$ppm" 2>/dev/null && [ -s "$ppm" ]; then
        pnmtopng "$ppm" > "$OUT/frame-$(printf '%03d' "$seq").png" 2>/dev/null && got=$((got+1))
        seq=$((seq+1))
        # keep only the most recent 12 frames
        ls -1t "$OUT"/frame-*.png 2>/dev/null | tail -n +13 | while read -r f; do rm -f "$f"; done
    fi
    rm -f "$ppm"
    sleep 8
done

echo "=> compositor exited. frames captured: $got (in $OUT)"
if [ "$got" = 0 ]; then
    echo "   (CaptureFrame produced nothing on the live DRM compositor — likely"
    echo "    headless-only; UI must be eyeballed. The state diff below still holds.)"
fi
snapshot > /tmp/fbxv-after.txt
echo "=> state diff (before → after), changes persist past exit:"
while IFS= read -r b; do
    k=${b%%=*}; a=$(grep "^$k=" /tmp/fbxv-after.txt)
    [ "$b" != "$a" ] && echo "  CHANGED $b -> ${a#*=}" || echo "  same    $b"
done < "$BEFORE"
