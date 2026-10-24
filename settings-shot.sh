#!/bin/sh
# settings-shot.sh — grab a screenshot of the running FreeBSD-X desktop so the
# dogfooded Settings widgets (collapsible sections + Sleep stepper) can be
# reviewed. Run this WITH SETTINGS OPEN on screen, in whatever state you want
# captured (e.g. a section collapsed, the stepper on a given value):
#
#   sh scripts/settings-shot.sh
#
# It asks the compositor for its current frame (Headless/CaptureFrame over the
# master socket), writes a PNG, and prints the path. Needs no root and does not
# disturb the display.

set -u

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CAP="$ROOT_DIR/out/freebsdx-headless-capture"
SOCK=/tmp/freebsdx.sock
TS="$(date +%H%M%S)"
PPM="/tmp/settings-shot-$TS.ppm"
PNG="/tmp/settings-shot-$TS.png"

[ -S "$SOCK" ] || { echo "ERROR: compositor socket $SOCK not found — is the desktop running (startx)?" >&2; exit 1; }
[ -x "$CAP" ] || { echo "ERROR: capture tool missing: $CAP (ninja -C out freebsdx-headless-capture)" >&2; exit 1; }

echo "=> requesting a frame from the compositor…"
if ! "$CAP" "$PPM" 2>&1; then
    echo "ERROR: CaptureFrame failed. The Headless capture service may not be wired" >&2
    echo "       on the live DRM compositor (it works in --headless mode). If so," >&2
    echo "       we'll fall back to an eyeball check." >&2
    exit 2
fi

if [ ! -s "$PPM" ]; then
    echo "ERROR: capture produced an empty file ($PPM)." >&2
    exit 2
fi

echo "=> converting PPM → PNG"
if ! pnmtopng "$PPM" > "$PNG" 2>/dev/null; then
    echo "ERROR: pnmtopng conversion failed; raw PPM left at $PPM" >&2
    exit 3
fi
rm -f "$PPM"

echo
echo "RESULT: screenshot saved -> $PNG"
ls -l "$PNG"
echo "(Tell Claude the path, or it will pick up $PNG.)"
