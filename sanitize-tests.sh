#!/bin/sh
# Build and run the xserver + xkit test binaries under AddressSanitizer and
# UndefinedBehaviorSanitizer, in their own build dir (out-asan/, separate from
# the out/ the desktop runs from). Exits non-zero on a failing test or any
# sanitizer report. GPU-only tools (e.g. xkit_dogfood_render) are skipped.
#
#   sh scripts/sanitize-tests.sh            # from the repo root
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/out-asan"
LOGDIR=$(mktemp -d /tmp/sanitize-tests.XXXXXX)
cd "$ROOT" || exit 1

if [ ! -d "$BUILD" ]; then
    meson setup "$BUILD" -Db_sanitize=address,undefined -Dbuildtype=debug -Db_lundef=false >"$LOGDIR/setup.log" 2>&1 \
        || { echo "meson setup failed (see $LOGDIR/setup.log)"; exit 1; }
fi

TARGETS=$(ninja -C "$BUILD" -t targets all 2>/dev/null |
          grep -oE '^(xserver_[a-z0-9_]+_test|sdk/xkit/xkit_[a-z0-9_]+)(:| )' | tr -d ': ' | sort -u |
          grep -v 'xkit_dogfood_render')
echo "$TARGETS" | xargs ninja -C "$BUILD" >"$LOGDIR/build.log" 2>&1 \
    || { echo "build failed (see $LOGDIR/build.log)"; exit 1; }

export ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=print_stacktrace=1
ran=0; bad=0
for t in $TARGETS; do
    [ -x "$BUILD/$t" ] || continue
    ran=$((ran + 1))
    log="$LOGDIR/$(echo "$t" | tr / _).log"
    timeout 300 "$BUILD/$t" >"$log" 2>&1
    rc=$?
    reports=$(grep -cE 'ERROR: AddressSanitizer|runtime error:' "$log")
    if [ "$rc" -ne 0 ] || [ "$reports" -ne 0 ]; then
        bad=$((bad + 1))
        echo "FAIL $t (exit $rc, $reports sanitizer report(s)) — $log"
        grep -m3 -E 'ERROR: AddressSanitizer|runtime error:' "$log"
    fi
done

echo "$ran test binaries under ASan+UBSan, $bad failing (logs: $LOGDIR)"
[ "$bad" -eq 0 ]
