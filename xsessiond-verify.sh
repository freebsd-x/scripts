#!/bin/sh
# scripts/xsessiond-verify.sh — live-seat sanity check for xsessiond.
#
# Since the daemon went single-process (phase-2b groundwork: accept-fork → one
# poll() loop), the session-module pam_setcred/pam_open_session calls now run in
# the long-lived root daemon instead of a throwaway forked child. On the stock
# PAM stack those are no-ops (session = pam_permit, setcred via pam_unix), so
# nothing should change — this script confirms that empirically and guards
# against a future /etc/pam.d/freebsdx that adds an active session module.
#
# It checks two things across a real GUI login+logout:
#   1. the daemon's own credentials (uid/gid/group list) stay IDENTICAL, and
#   2. utmpx accounting appears (who/w) and clears on logout (last).
#
# Run as root on the live seat, with xsessiond already running:
#   doas sh scripts/xsessiond-verify.sh        # or: sudo / su
# Optionally pin the pid:  scripts/xsessiond-verify.sh --pid 1234

set -u

PIDFILE="/var/run/xsessiond.pid"
PID=""

while [ $# -gt 0 ]; do
    case "$1" in
        --pid) PID="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "WARNING: not root — procstat cred on a root daemon may be incomplete." >&2
fi

# Locate the daemon: explicit --pid, then the pidfile, then pgrep.
if [ -z "$PID" ]; then
    if [ -r "$PIDFILE" ]; then
        PID="$(cat "$PIDFILE")"
    else
        PID="$(pgrep -x xsessiond 2>/dev/null | head -1)"
    fi
fi

if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
    echo "ERROR: xsessiond not found/running (pid='${PID:-}')." >&2
    echo "       Start it first, or pass --pid <n>." >&2
    exit 1
fi
echo "=> xsessiond pid: $PID"

# Snapshot the daemon's credentials. We compare the whole procstat cred row
# (euid/ruid/svuid, egid/rgid/svgid, and the supplementary GROUPS list); any
# drift there is exactly the failure mode we care about.
snap_cred() { procstat cred "$1" 2>/dev/null | sed -n '2,$p'; }

TMP="$(mktemp -t fbxseatd)"
BEFORE="${TMP}.before"
AFTER="${TMP}.after"
trap 'rm -f "$BEFORE" "$AFTER" "$TMP"' EXIT INT TERM

snap_cred "$PID" > "$BEFORE"
if [ ! -s "$BEFORE" ]; then
    echo "ERROR: 'procstat cred $PID' returned nothing — can't read the daemon's" >&2
    echo "       credentials, so the comparison would be a meaningless empty==empty." >&2
    echo "       Re-run as root." >&2
    exit 1
fi
echo "=> daemon credentials BEFORE:"
sed 's/^/     /' "$BEFORE"

echo
echo "=> Baseline sessions (who):"
who | sed 's/^/     /'

cat <<EOF

------------------------------------------------------------------
Now, on the GUI:  LOG IN, then LOG OUT (or lock + unlock).
This drives a real xsessiond StartSession -> pam_open_session +
utmpx login, then EndSession/disconnect -> pam_close_session.
When you are back, press Enter here.
------------------------------------------------------------------
EOF
# shellcheck disable=SC2034
read -r _dummy

if ! kill -0 "$PID" 2>/dev/null; then
    echo "FAIL: xsessiond (pid $PID) is no longer running — it died during the login." >&2
    exit 1
fi

snap_cred "$PID" > "$AFTER"
echo "=> daemon credentials AFTER:"
sed 's/^/     /' "$AFTER"

echo
echo "=> Sessions now (who) — an xsess0 entry should have appeared+cleared:"
who | sed 's/^/     /'
echo "=> Recent login/logout (last):"
last | head -5 | sed 's/^/     /'

echo
if cmp -s "$BEFORE" "$AFTER"; then
    echo "PASS: daemon credentials unchanged across login/logout."
    echo "      Single-process pam_setcred/pam_open_session did not mutate the daemon."
    RC=0
else
    echo "FAIL: daemon credentials CHANGED across login/logout — diff:"
    diff "$BEFORE" "$AFTER" | sed 's/^/     /'
    echo "      A session/setcred module is mutating the daemon process. Move it"
    echo "      into the fork+exec'd session child (phase 1c), or drop it from the"
    echo "      freebsdx PAM stack. See core/xserver/docs/xsessiond-design.md."
    RC=1
fi

echo
echo "Also confirm above that 'who' showed your GUI login while active and that"
echo "'last' lists the login/logout pair — that proves the PAM session + utmpx"
echo "path ran. (Credential stability is the automated check; accounting is by eye.)"
exit $RC
