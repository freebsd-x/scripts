#!/bin/sh
# scripts/xsessiond-install.sh — install xsessiond as a persistent rc.d service.
#
# Copies the built daemon to /usr/local/bin, drops the rc.d script into the
# local startup dir, enables it in /etc/rc.conf, and starts it. Idempotent:
# re-running re-installs the current binary and restarts the service (handy
# after a rebuild). Run as root on the live seat:
#   sudo sh scripts/xsessiond-install.sh

set -eu

REPO="/home/wheel/freebsd-x"
BIN_SRC="${REPO}/out/xsessiond"
RCD_SRC="${REPO}/core/xserver/rc.d/xsessiond"
BIN_DST="/usr/local/bin/xsessiond"
RCD_DST="/usr/local/etc/rc.d/xsessiond"
SOCK="/var/run/xsessiond.sock"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (use: sudo sh $0)" >&2
    exit 1
fi
for f in "$BIN_SRC" "$RCD_SRC"; do
    [ -r "$f" ] || { echo "ERROR: missing $f — build first (ninja -C out xsessiond)" >&2; exit 1; }
done

# Migration: retire the old names this daemon has had (fbx-seatd → fbx-sessiond → xsessiond).
echo "=> retiring old fbx-seatd / fbx-sessiond (if present)"
for old in fbx-seatd fbx-sessiond; do
    service "$old" stop 2>/dev/null || true
    pkill -x "$old" 2>/dev/null || true
done
sysrc -x fbx_seatd_enable 2>/dev/null || true
sysrc -x fbx_sessiond_enable 2>/dev/null || true
rm -f /usr/local/bin/fbx-seatd /usr/local/bin/fbx-sessiond \
      /usr/local/etc/rc.d/fbx-seatd /usr/local/etc/rc.d/fbx-sessiond \
      /var/run/fbx-seatd.sock /var/run/fbx-sessiond.sock \
      /var/run/fbx-seatd.pid /var/run/fbx-sessiond.pid

echo "=> stopping any running instance + clearing stale socket"
service xsessiond stop 2>/dev/null || true
pkill -x xsessiond 2>/dev/null || true
rm -f "$SOCK"

echo "=> installing binary   -> $BIN_DST"
install -m 755 "$BIN_SRC" "$BIN_DST"
echo "=> installing rc.d     -> $RCD_DST"
install -m 755 "$RCD_SRC" "$RCD_DST"
echo "=> enabling in /etc/rc.conf (xsessiond_enable=YES)"
sysrc xsessiond_enable=YES >/dev/null

echo "=> starting service"
service xsessiond start

echo
echo "=> status:"
service xsessiond status || true
echo "=> pid:    $(pgrep -x xsessiond || echo '(none!)')"
echo "=> socket: $(ls -l "$SOCK" 2>/dev/null || echo '(missing!)')"
