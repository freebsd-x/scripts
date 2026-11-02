#!/usr/bin/env python3
"""Headless end-to-end checks for the FreeBSD-X desktop.

Drives a real `out/freebsdx --headless` compositor (plus the real shell and
apps where a scenario needs them) and checks what a user would see: files
open in the right app, the Screen Time card works, the trust gates hold, every
app starts cleanly, and nothing lays out off-screen.

    python3 scripts/headless-e2e.py              # every scenario
    python3 scripts/headless-e2e.py --list       # scenario names
    python3 scripts/headless-e2e.py file-open gates
    python3 scripts/headless-e2e.py --shots /tmp/shots   # + a capture of every app

Each scenario gets a fresh compositor and a throwaway $HOME, so nothing touches
your real configuration. It refuses to run while a desktop session is up: the
compositor must never be started without --headless on a live seat.
Exit status: 0 if every scenario passed.

Standard library only. Uses netpbm (pnmtopng, pnmcut, pnmscale, pnmcat) for
--shots only.
"""

import argparse
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")
SOCK = "/tmp/freebsdx.sock"

XBUS_MAGIC = 0x58425553
XBUS_FMT = "<II32sI256s"            # magic, transaction, service[32], opcode, payload[256]
INPUT_FMT = "<IiiIi"                # type, value_a, value_b, modifiers, char_value
RENDER_MSG_SIZE = 56

# Protocol constants (include/shared/protocol.h).
SURFACE_CREATED, SURFACE_CLOSED, SHELL_SURFACE_CREATED = 0, 2, 3
INPUT_MOUSE_MOVE, INPUT_MOUSE_BUTTON = 0, 1
BTN_LEFT = 272
HEADLESS_INJECT = 3
SHELL_LAUNCH_WITH_FILE = 5
NOTIFICATION_POST = 1

# Oversized-layout reports that are expected (an unbounded table header inside
# a horizontal scroller).
LAYOUT_ALLOW = {"taskmanager": 1}

# Visible content laid out into a zero-size box that is expected to be.
COLLAPSED_ALLOW = {}

# Low-contrast texts that are expected, by (app, theme).
CONTRAST_ALLOW = {}


class Failed(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Failed(msg)


TIME_SCALE = 1        # --asan: sanitized binaries start and run several times slower


def wait_for(pred, timeout, what):
    deadline = time.time() + timeout * TIME_SCALE
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    raise Failed(f"timed out waiting for {what}")


# ── Wire helpers ──────────────────────────────────────────────────────────────

def xbus_msg(service, opcode, payload=b"", tid=1):
    return struct.pack(XBUS_FMT, XBUS_MAGIC, tid, service.encode(), opcode, payload)


def connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.connect(SOCK)
    return s


def call(service, opcode, payload=b"", timeout=1.5):
    """Send one XBus request on a fresh connection; return the reply or None."""
    s = connect()
    tid = 4242
    s.send(xbus_msg(service, opcode, payload, tid))
    s.settimeout(timeout)
    try:
        while True:
            d = s.recv(4096)
            if len(d) >= 300 and struct.unpack("<I", d[4:8])[0] == tid:
                return d
    except socket.timeout:
        return None
    finally:
        s.close()


def send(service, opcode, payload=b""):
    s = connect()
    s.send(xbus_msg(service, opcode, payload))
    time.sleep(0.2)
    s.close()


def render_msg(surface_id, status, w=400, h=300):
    return struct.pack("<IIII", surface_id, status, w, h).ljust(RENDER_MSG_SIZE, b"\0")


def recv_render_reply(s, timeout=2.0):
    """The next 56-byte RenderMessage on s (skipping broadcasts), or None."""
    s.settimeout(timeout)
    try:
        while True:
            d = s.recv(4096)
            if len(d) == RENDER_MSG_SIZE:
                return struct.unpack("<IIII", d[:16])
    except socket.timeout:
        return None


class Pointer:
    """Injected mouse input on one connection (many short ones overflow the
    compositor's accept backlog). Motion is relative; it clamps at the edges."""

    def __init__(self):
        self.s = connect()

    def _inject(self, typ, a, b):
        payload = struct.pack(INPUT_FMT, typ, a, b, 0, 0)
        self.s.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, payload))
        time.sleep(0.1)

    def click(self, x, y):
        self._inject(INPUT_MOUSE_MOVE, -10000, -10000)
        self._inject(INPUT_MOUSE_MOVE, x, y)
        time.sleep(0.2)
        self._inject(INPUT_MOUSE_BUTTON, BTN_LEFT, 1)
        self._inject(INPUT_MOUSE_BUTTON, BTN_LEFT, 0)
        time.sleep(0.3)

    def close(self):
        self.s.close()


def launch_with_file(app_id, path):
    payload = app_id.encode().ljust(64, b"\0") + path.encode().ljust(188, b"\0")
    send("com.freebsdx.Shell", SHELL_LAUNCH_WITH_FILE, payload)


def write_png(path, w, h, rgb):
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


# ── Processes ─────────────────────────────────────────────────────────────────

SANITIZER_RE = re.compile(r"(ERROR: AddressSanitizer: .*|\S+: runtime error: .*)")


def sanitizer_reports(work, scanned):
    """Distinct sanitizer reports ("<log>: <first line>") in logs not yet scanned."""
    found = []
    for dirpath, _, files in os.walk(work):
        for fn in files:
            path = os.path.join(dirpath, fn)
            if not fn.endswith(".log") or path in scanned:
                continue
            scanned.add(path)
            seen = set()
            for line in open(path, "rb").read().decode("utf-8", "replace").splitlines():
                m = SANITIZER_RE.search(line)
                if m:
                    # Same site reported per-call: keep one per source location.
                    key = re.sub(r"0x[0-9a-f]+", "", m.group(1))
                    if key not in seen:
                        seen.add(key)
                        found.append(f"{os.path.relpath(path, work)}: {m.group(1)[:160]}")
    return found


def live_session():
    """Is a desktop session (or a stray compositor) running?"""
    if os.path.exists(SOCK):
        return "a compositor socket exists at " + SOCK
    r = subprocess.run(["pgrep", "-x", "freebsdx"], capture_output=True, text=True)   # any build dir
    if r.stdout.strip():
        return "a freebsdx process is running"
    return None


def built_apps():
    """(binary name, path) of every app bundle with a built binary."""
    apps = []
    apps_dir = os.path.join(ROOT, "apps")
    for bundle in sorted(os.listdir(apps_dir)):
        if not bundle.endswith(".xapp"):
            continue
        base = bundle[:-5].lower()
        try:
            m = re.search(r'"executable":\s*"([^"]*)"', open(os.path.join(apps_dir, bundle, "manifest.json")).read())
            if m:
                base = os.path.basename(m.group(1)).lower()
        except OSError:
            pass
        for cand in (base, base + "_app"):
            p = os.path.join(OUT, cand)
            if os.access(p, os.X_OK):
                apps.append((cand, p))
                break
    return apps


class Session:
    """A headless compositor with a throwaway $HOME, plus whatever a scenario
    starts in it. Everything is torn down on exit."""

    def __init__(self, workdir, name, strict=False, config=None):
        self.home = os.path.join(workdir, name, "home")
        self.logs = os.path.join(workdir, name)
        os.makedirs(os.path.join(self.home, ".config", "freebsd-x"), exist_ok=True)
        if config:
            with open(os.path.join(self.home, ".config", "freebsd-x", "config"), "w") as f:
                f.write(config)
        self.strict = strict
        self.procs = []

    def env(self, **extra):
        e = dict(os.environ, HOME=self.home)
        e.update(extra)
        return e

    def log(self, name):
        return os.path.join(self.logs, name + ".log")

    def read_log(self, name):
        try:
            return open(self.log(name), "rb").read().decode("utf-8", "replace")
        except OSError:
            return ""

    def start(self, name, argv, **env):
        f = open(self.log(name), "wb")
        p = subprocess.Popen(argv, cwd=ROOT, env=self.env(**env), stdout=f, stderr=subprocess.STDOUT,
                             start_new_session=True)   # own group: its children die with it
        self.procs.append(p)
        return p

    def __enter__(self):
        argv = [os.path.join(OUT, "freebsdx"), "--headless"] + (["--strict-shell-trust"] if self.strict else [])
        self.start("compositor", argv)
        try:
            wait_for(lambda: os.path.exists(SOCK), 15, "the compositor socket")
        except BaseException:
            self.__exit__()   # a failed start must not leave a compositor behind
            raise
        time.sleep(1.0)
        return self

    def shell(self):
        self.start("xshell", [os.path.join(OUT, "xshell")])
        wait_for(lambda: "Shell Surface CREATED (ID: 999)" in self.read_log("compositor"), 20, "the shell surface")
        time.sleep(2.0)

    def __exit__(self, *exc):
        for p in reversed(self.procs):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 5
        for p in self.procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
                # A killed compositor can't unlink its socket.
                if p is self.procs[0] and os.path.exists(SOCK):
                    os.unlink(SOCK)
        wait_for(lambda: not os.path.exists(SOCK), 10, "the compositor socket to go away")
        return False


def procs_running(binary_path):
    """How many processes run this binary (xshell launches apps by the
    relative path out/<name>, so match on that, not the absolute path)."""
    rel = "out/" + os.path.basename(binary_path)
    r = subprocess.run(["pgrep", "-f", "(^|/)" + re.escape(rel) + "( |$)"], capture_output=True, text=True)
    return len(r.stdout.split())


# ── Scenarios ─────────────────────────────────────────────────────────────────

def s_file_open(work):
    """Opening a file launches its app; a second file reuses the open window."""
    a, b = os.path.join(work, "first.png"), os.path.join(work, "second.png")
    write_png(a, 64, 64, (220, 40, 40))
    write_png(b, 64, 64, (40, 200, 40))
    viewer = os.path.join(OUT, "imageviewer")
    with Session(work, "file-open") as s:
        s.shell()
        launch_with_file("com.freebsdx.imageviewer", a)
        wait_for(lambda: "Image Viewer — first.png" in s.read_log("compositor"), 15, "the viewer to open first.png")
        check(procs_running(viewer) == 1, "expected one Image Viewer")
        launch_with_file("com.freebsdx.imageviewer", b)
        wait_for(lambda: "Image Viewer — second.png" in s.read_log("compositor"), 10, "the open viewer to show second.png")
        check(procs_running(viewer) == 1, f"a second file started another viewer ({procs_running(viewer)} running)")


def s_times_up(work):
    """An app at its daily limit gets the Time's-up card; Close app ends it."""
    a = os.path.join(work, "limit.png")
    write_png(a, 32, 32, (40, 40, 220))
    day = time.strftime("%Y-%m-%d")
    config = (f"applimit.limits=com.freebsdx.imageviewer=1\n"
              f"applimit.usage={day}|com.freebsdx.imageviewer=50\n")
    viewer = os.path.join(OUT, "imageviewer")
    with Session(work, "times-up", strict=True, config=config) as s:
        s.shell()
        launch_with_file("com.freebsdx.imageviewer", a)
        wait_for(lambda: procs_running(viewer) == 1, 10, "the viewer to start")
        wait_for(lambda: "reached its 1 min limit" in s.read_log("compositor"), 30, "the time limit")
        # The card is the 420x150 layer the shell reports as its input region.
        m = wait_for(lambda: re.search(r"420x150@(\d+),(\d+)", s.read_log("compositor")), 10, "the Time's-up card")
        x, y = int(m.group(1)), int(m.group(2))
        ptr = Pointer()
        ptr.click(x + 374, y + 89)          # "Close app" (right-aligned in the card's button row)
        ptr.close()
        wait_for(lambda: "closed 1 window(s) of com.freebsdx.imageviewer" in s.read_log("compositor"), 10, "Close app")
        wait_for(lambda: procs_running(viewer) == 0, 10, "the viewer process to exit")
        check("App crashed" not in s.read_log("compositor"), "closing the app was reported as a crash")


def s_shell_trust(work):
    """Only the shell binaries may register the trusted shell surface."""
    def claim(status):
        c = connect()
        c.send(render_msg(999, status))
        r = recv_render_reply(c)
        c.close()
        return r
    with Session(work, "shell-trust", strict=True) as s:
        r = claim(SHELL_SURFACE_CREATED)
        check(r and r[2] == 0 and r[3] == 0, f"an impostor's shell claim wasn't refused ({r})")
        r = claim(SURFACE_CREATED)
        check(r and r[2] == 0, f"a plain window took the shell's id 999 ({r})")
        s.shell()                            # the real shell is accepted
        r = claim(SHELL_SURFACE_CREATED)
        check(r and r[2] == 0, f"an impostor took over the running shell's surface ({r})")


def s_surface_ownership(work):
    """A client can't close or take over another client's window."""
    with Session(work, "surface-ownership", strict=True) as s:
        a = connect()
        a.send(render_msg(77, SURFACE_CREATED))
        check(recv_render_reply(a), "window 77 wasn't created")
        b = connect()
        b.send(render_msg(77, SURFACE_CLOSED))
        time.sleep(0.6)
        b.send(render_msg(77, SURFACE_CREATED))
        r = recv_render_reply(b)
        check(r and r[2] == 0, f"another client took over window 77 ({r})")
        time.sleep(0.3)
        check("Removing Window ID: 77" not in s.read_log("compositor"), "another client closed window 77")
        a.close()
        b.close()


def s_gates(work):
    """Untrusted calls to gated services are refused; reads stay open; a
    built-in app is recognized by its executable."""
    key = b"screenlock.enabled"
    with Session(work, "gates", strict=True) as s:
        refused = [("com.freebsdx.Firewall", 0x02, struct.pack("<I", 1)),
                   ("com.freebsdx.Keychain", 0x01, b""),
                   ("com.freebsdx.Configuration", 0x02, key),
                   ("com.freebsdx.Terminal", 0x03, b""),
                   ("com.freebsdx.Power", 0x04, b""),          # Reboot
                   ("com.freebsdx.Window", 0x02, struct.pack("<I", 999))]
        for svc, op, payload in refused:
            check(call(svc, op, payload) is None, f"{svc} op {op} answered an untrusted client")
        log = s.read_log("compositor")
        check(log.count("DENIED") >= len(refused), "denials weren't logged")
        check(call("com.freebsdx.Configuration", 0x01, key) is not None, "a config read was refused")
        check(call("com.freebsdx.Power", 0x01) is not None, "a power-state read was refused")
        s.start("terminal", [os.path.join(OUT, "terminal")])
        wait_for(lambda: "Created session" in s.read_log("compositor"), 10, "the Terminal app's session")


def s_consent(work):
    """An app's first camera request asks the user; the answer is saved and
    replayed, and a decided app isn't asked again."""
    def run_camera(s, click):
        before = s.read_log("compositor").count("Consent: asking")
        p = s.start("camera", [os.path.join(OUT, "camera")])
        if click is None:
            time.sleep(5)
            check(s.read_log("compositor").count("Consent: asking") == before,
                  "a decided app was asked again")
        else:
            wait_for(lambda: s.read_log("compositor").count("Consent: asking") > before, 15, "the consent prompt")
            # The card is the 460x150 layer the shell reports as its input region.
            m = wait_for(lambda: re.findall(r"460x150@(\d+),(\d+)", s.read_log("compositor")), 10, "the consent card")
            x, y = map(int, m[-1])
            ptr = Pointer()
            ptr.click(x + (425 if click == "allow" else 345), y + 89)   # Allow / Don't Allow
            ptr.close()
            wait_for(lambda: "replaying" in s.read_log("compositor").split("Consent: asking")[-1], 10,
                     "the held request to be replayed")
        os.killpg(p.pid, signal.SIGTERM)
        p.wait()

    perms = lambda s: open(os.path.join(s.home, ".freebsdx", "app-permissions")).read()
    with Session(work, "consent-allow", strict=True) as s:
        s.shell()
        run_camera(s, "allow")
        check(re.search(r"^com\.freebsdx\.camera - 1 ", perms(s), re.M), "Allow wasn't saved")
        run_camera(s, None)                      # decided: no second prompt
    with Session(work, "consent-deny", strict=True) as s:
        s.shell()
        run_camera(s, "deny")
        check(re.search(r"^com\.freebsdx\.camera - 0 ", perms(s), re.M), "Don't Allow wasn't saved")


class Keys:
    """Injected key presses on one connection (KeyPress = 2; Shift = 1)."""

    def __init__(self):
        self.s = connect()

    def press(self, code, shift=False):
        for state in (1, 0):
            payload = struct.pack(INPUT_FMT, 2, code, state, 1 if shift else 0, 0)
            self.s.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, payload))
            time.sleep(0.15 * TIME_SCALE)
        time.sleep(0.5 * TIME_SCALE)

    def close(self):
        self.s.close()


def s_screen_reader(work):
    """With the screen reader on, Tab moves focus through Settings and each
    control is spoken by name; Enter activates it (and Settings may save it)."""
    log = os.path.join(work, "screen-reader.spoken")
    # The speech log goes in the compositor's environment, so start it here
    # rather than with Session.__enter__.
    sess = Session(work, "screen-reader", config="a11y.screen_reader=1\n")
    sess.start("compositor", [os.path.join(OUT, "freebsdx"), "--headless"], FBX_READER_LOG=log)
    try:
        wait_for(lambda: os.path.exists(SOCK), 15, "the compositor socket")
        time.sleep(1.0)
        sess.start("settings", [os.path.join(OUT, "settings_app")])
        spoken = lambda: open(log).read().splitlines() if os.path.exists(log) else []
        wait_for(lambda: "Settings, window" in spoken(), 15, "the window announcement")
        time.sleep(0.5 * TIME_SCALE)   # the announcement can precede the first full layout
        keys = Keys()
        keys.press(15)            # Tab → the Appearance section
        keys.press(15)            # Tab → Dark Mode
        keys.press(28)            # Enter → toggles it
        keys.press(15, shift=True)
        keys.press(15)            # back onto Dark Mode: its new state is spoken
        keys.close()
        time.sleep(0.5)
        said = spoken()
        check("Dark Mode, switch, on" in said, f"the switch wasn't spoken by name: {said}")
        check("Dark Mode, switch, off" in said, f"Enter didn't toggle it (or it wasn't re-read): {said}")
        check(not any(set(l.strip()) <= set("▾▸✕ ") for l in said), f"a symbol was spoken as a name: {said}")
        theme = os.path.join(sess.home, ".freebsdx-theme")
        check(os.path.exists(theme) and open(theme).read().startswith("0"),
              "Settings couldn't save the theme (a sandbox gate refused it)")
    finally:
        sess.__exit__(None, None, None)


def s_shell_keyboard(work):
    """Super+Space search works from the keyboard over a focused app: typing
    goes into the search (not the app), Down + Enter opens a result, Esc
    closes it."""
    def procs(name):
        r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
        return len(r.stdout.split())

    def pause(t):
        time.sleep(t * TIME_SCALE)

    def ev(c, t, a=0, b=0, ch=0):
        c.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, struct.pack(INPUT_FMT, t, a, b, 0, ch)))
        pause(0.12)

    def key(c, code, ch=""):
        ev(c, 2, code, 1, ord(ch) if ch else 0)
        ev(c, 2, code, 0)
        pause(0.25)

    with Session(work, "shell-keyboard", strict=True) as s:
        s.shell()
        s.start("calculator", [os.path.join(OUT, "calculator")])
        wait_for(lambda: procs("calculator") == 1, 10, "the calculator to start")
        pause(2)
        c = connect()
        ev(c, 6)                                    # Super+Space (SearchToggle)
        pause(1.0)
        for code, ch in ((46, "c"), (30, "a"), (38, "l"), (46, "c")):
            key(c, code, ch)
        pause(1.5)
        key(c, 108)                                 # Down → the first result
        key(c, 28)                                  # Enter → open it
        wait_for(lambda: procs("calculator") == 2, 10, "the search result to open")
        check("calc" not in s.read_log("calculator"), "the typing went to the app")
        ev(c, 6)
        pause(1.0)
        key(c, 1)                                   # Esc closes search
        pause(1.0)
        c.close()
        regions = re.findall(r"\[Shell\] Input region:.*", s.read_log("compositor"))
        check(regions and regions[-1].endswith("(none)"), f"Esc didn't close search ({regions[-1:]})")


def s_apps_start(work):
    """The shell and every app start with no denied calls and no crash."""
    problems = []
    with Session(work, "apps-start", strict=True) as s:
        s.shell()
        for name, path in built_apps():
            before = s.read_log("compositor").count("DENIED")
            p = s.start(name, [path])
            time.sleep(4)
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            p.wait()
            app_log = s.read_log(name)
            if "FreeBSD-X crash" in app_log or "Terminating due to uncaught exception" in app_log:
                problems.append(f"{name} crashed")
            if s.read_log("compositor").count("DENIED") > before:
                problems.append(f"{name} was denied a call")
        if "DENIED" in s.read_log("xshell"):
            problems.append("the shell was denied a call")
    check(not problems, "; ".join(problems))


def fnv1a64(text):
    h = 1469598103934665603
    for b in text.encode():
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def count_rgb(ppm_path, rgb, step=4):
    """Pixels (sampling every `step`-th) of exactly colour `rgb` in a P6 capture."""
    data = open(ppm_path, "rb").read()
    parts = data.split(b"\n", 3)              # P6, "w h", maxval, pixels
    pix = memoryview(parts[3])
    want = bytes(rgb)
    return sum(1 for i in range(0, len(pix) - 2, 3 * step) if pix[i:i + 3] == want)


def s_fetch(work):
    """Sandboxed Maps gets its tiles through com.freebsdx.Fetch (xfetch's cache,
    offline here) and draws them; other callers and fake providers are refused."""
    FETCH, GET, REGISTER = "com.freebsdx.Fetch", 0x01, 0x10
    tile_url = "https://tile.openstreetmap.org/12/{}/{}.png"
    with Session(work, "fetch", strict=True) as s:
        wait_for(lambda: "[Fetch] xfetch registered" in s.read_log("compositor"), 15, "xfetch to register")

        # Not Maps (this script): refused, with a Denied reply (status 1).
        url = tile_url.format(2074, 1409).encode().ljust(252, b"\0")
        r = call(FETCH, GET, struct.pack("<I", 0) + url, timeout=3 * TIME_SCALE)
        check(r is not None and struct.unpack("<i", r[44:48])[0] == 1,   # FetchReply.status_ == Denied
              "a non-Maps caller wasn't refused")
        # Not xfetch: can't become the provider.
        send(FETCH, REGISTER)
        wait_for(lambda: "DENIED: provider registration" in s.read_log("compositor"), 5, "the fake provider refusal")

        # Seed xfetch's cache with solid-magenta tiles around Maps' start view
        # (Paris, zoom 12 → x≈2074, y≈1409) and let Maps draw them.
        cache = os.path.join(s.home, ".cache", "freebsdx", "fetch")
        os.makedirs(cache, exist_ok=True)
        seed = os.path.join(work, "magenta.png")
        write_png(seed, 256, 256, (255, 0, 255))
        tile = open(seed, "rb").read()
        for x in range(2064, 2085):
            for y in range(1400, 1420):
                with open(os.path.join(cache, fnv1a64(tile_url.format(x, y))), "wb") as f:
                    f.write(tile)
        s.start("maps", [os.path.join(OUT, "maps")])
        wait_for(lambda: "Capsicum sandbox active" in s.read_log("maps"), 15, "Maps to start sandboxed")
        ppm = os.path.join(s.logs, "maps.ppm")
        capture = os.path.join(OUT, "freebsdx-headless-capture")
        def drawn():
            subprocess.run([capture, ppm], capture_output=True, timeout=15 * TIME_SCALE)
            return os.path.exists(ppm) and count_rgb(ppm, (255, 0, 255)) > 20000
        wait_for(drawn, 20, "Maps to draw the fetched tiles")
        denied = [l for l in s.read_log("compositor").splitlines() if "[Fetch] DENIED: com.freebsdx.maps" in l]
        check(not denied, f"Maps was refused a fetch: {denied[:2]}")


def audit_count(text):
    m = re.search(r"\[layout-audit\] (\d+) oversized", text)
    return int(m.group(1)) if m else None


def collapsed_count(text):
    m = re.search(r"\[layout-audit\] (\d+) collapsed", text)
    return int(m.group(1)) if m else 0


def s_layout_audit(work):
    """No widget lays out absurdly large (the off-screen-content bug), and no
    visible content is squeezed into a zero-size box (the 0 px Maps map)."""
    problems = []
    with Session(work, "layout-audit") as s:
        s.start("xshell", [os.path.join(OUT, "xshell")], FBX_LAYOUT_AUDIT="1")
        wait_for(lambda: audit_count(s.read_log("xshell")) is not None, 20, "the shell's layout audit")
        n = audit_count(s.read_log("xshell"))
        if n:
            problems.append(f"shell: {n} oversized")
        c = collapsed_count(s.read_log("xshell"))
        if c > COLLAPSED_ALLOW.get("xshell", 0):
            detail = [l for l in s.read_log("xshell").splitlines() if "] collapsed " in l][:3]
            problems.append(f"shell: {c} collapsed ({'; '.join(detail)})")
        for name, path in built_apps():
            p = s.start(name, [path], FBX_LAYOUT_AUDIT="1")
            try:
                wait_for(lambda: audit_count(s.read_log(name)) is not None, 8, f"{name}'s layout audit")
            except Failed:
                pass   # an app that paints no frames (e.g. a GPU demo) has nothing to audit
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            p.wait()
            n = audit_count(s.read_log(name))
            if n and n > LAYOUT_ALLOW.get(name, 0):
                detail = [l for l in s.read_log(name).splitlines() if "[layout-audit]" in l][:3]
                problems.append(f"{name}: {n} oversized ({'; '.join(detail)})")
            c = collapsed_count(s.read_log(name))
            if c > COLLAPSED_ALLOW.get(name, 0):
                detail = [l for l in s.read_log(name).splitlines() if "] collapsed " in l][:3]
                problems.append(f"{name}: {c} collapsed ({'; '.join(detail)})")
    check(not problems, " | ".join(problems))


def low_contrast(text):
    m = re.search(r"\[layout-audit\] (\d+) low-contrast", text)
    return int(m.group(1)) if m else 0


THEME_SET = 0x02


def s_contrast(work):
    """Text is readable in both themes: the layout audit's contrast check
    (WCAG ratio >= 3:1 against what is painted behind it) passes for the
    shell and every app, dark and light."""
    problems = []
    for theme, dark in (("dark", 1), ("light", 0)):
        with Session(work, f"contrast-{theme}") as s:
            send("com.freebsdx.Theme", THEME_SET, struct.pack("<II", dark, 0))
            time.sleep(0.5)
            targets = [("xshell", os.path.join(OUT, "xshell"))] + built_apps()
            for name, path in targets:
                p = s.start(name, [path], FBX_LAYOUT_AUDIT="1")
                try:
                    wait_for(lambda: audit_count(s.read_log(name)) is not None, 20 if name == "xshell" else 8,
                             f"{name}'s layout audit")
                except Failed:
                    pass   # paints no frames (GPU demo): nothing to audit
                if name != "xshell":   # the shell stays up: apps' audits are of their own windows
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    p.wait()
                n = low_contrast(s.read_log(name))
                if n > CONTRAST_ALLOW.get((name, theme), 0):
                    detail = [l.split("] ", 1)[1] for l in s.read_log(name).splitlines() if "] low-contrast " in l][:2]
                    problems.append(f"{name} ({theme}): {n} ({'; '.join(detail)})")
    check(not problems, " | ".join(problems))


def s_smoke_tools(work):
    """Every freebsdx-*-smoke tool passes against a fresh compositor."""
    tools = sorted(f for f in os.listdir(OUT) if f.startswith("freebsdx-") and f.endswith("-smoke"))
    check(tools, "no smoke tools are built")
    failed = []
    for t in tools:
        with Session(work, "smoke-" + t) as s:
            r = subprocess.run([os.path.join(OUT, t)], cwd=ROOT, env=s.env(), capture_output=True, timeout=60)
            if r.returncode != 0:
                failed.append(t)
    check(not failed, "failed: " + ", ".join(failed))


SCENARIOS = {
    "file-open": s_file_open,
    "times-up": s_times_up,
    "shell-trust": s_shell_trust,
    "surface-ownership": s_surface_ownership,
    "gates": s_gates,
    "consent": s_consent,
    "screen-reader": s_screen_reader,
    "shell-keyboard": s_shell_keyboard,
    "fetch": s_fetch,
    "apps-start": s_apps_start,
    "layout-audit": s_layout_audit,
    "contrast": s_contrast,
    "smoke-tools": s_smoke_tools,
}


# ── Screenshots ───────────────────────────────────────────────────────────────

def take_shots(work, dest):
    """A capture of every app (its window region) plus a contact sheet."""
    for tool in ("pnmtopng", "pnmcut", "pnmscale", "pnmcat"):
        if not shutil.which(tool):
            raise Failed(f"--shots needs netpbm ({tool} not found)")
    os.makedirs(dest, exist_ok=True)
    capture = os.path.join(OUT, "freebsdx-headless-capture")
    thumbs = []
    with Session(work, "shots") as s:
        for name, path in built_apps():
            p = s.start(name, [path])
            time.sleep(4)
            ppm = os.path.join(s.logs, name + ".ppm")
            subprocess.run([capture, ppm], capture_output=True, timeout=15)
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            p.wait()
            if not os.path.exists(ppm):
                continue
            win = subprocess.run(["pnmcut", "360", "100", "1200", "880", ppm], capture_output=True).stdout
            with open(os.path.join(dest, name + ".png"), "wb") as f:
                f.write(subprocess.run(["pnmtopng"], input=win, capture_output=True).stdout)
            th = os.path.join(s.logs, name + ".th.ppm")
            with open(th, "wb") as f:
                f.write(subprocess.run(["pnmscale", "-xsize", "360"], input=win, capture_output=True).stdout)
            thumbs.append(th)
        rows = []
        for i in range(0, len(thumbs), 4):
            row = os.path.join(s.logs, f"row{i}.ppm")
            with open(row, "wb") as f:
                f.write(subprocess.run(["pnmcat", "-lr", "-jtop"] + thumbs[i:i + 4], capture_output=True).stdout)
            rows.append(row)
        if rows:
            sheet = subprocess.run(["pnmcat", "-tb", "-jleft"] + rows, capture_output=True).stdout
            with open(os.path.join(dest, "contact-sheet.png"), "wb") as f:
                f.write(subprocess.run(["pnmtopng"], input=sheet, capture_output=True).stdout)
    print(f"  shots: {len(thumbs)} apps → {dest} (contact-sheet.png)")


def main():
    ap = argparse.ArgumentParser(description="Headless end-to-end checks for FreeBSD-X.")
    ap.add_argument("scenarios", nargs="*", help="scenarios to run (default: all)")
    ap.add_argument("--list", action="store_true", help="list the scenarios")
    ap.add_argument("--shots", metavar="DIR", help="also capture every app into DIR")
    ap.add_argument("--keep", action="store_true", help="keep the logs and scratch homes")
    ap.add_argument("--asan", action="store_true",
                    help="run the out-asan/ (ASan+UBSan) binaries; any sanitizer report fails the scenario")
    args = ap.parse_args()
    global OUT, TIME_SCALE
    if args.asan:
        OUT = os.path.join(ROOT, "out-asan")
        TIME_SCALE = 3
        # Shaders are only built into out/. halt_on_error=0 so one report doesn't
        # hide the next; the scenario fails on any of them.
        os.environ.update(FREEBSDX_SHADER_DIR=os.path.join(ROOT, "out", "freebsdx.p"),
                          ASAN_OPTIONS="detect_leaks=0:halt_on_error=0",
                          UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=0")

    if args.list:
        for name, fn in SCENARIOS.items():
            print(f"{name:18} {fn.__doc__.splitlines()[0]}")
        return 0
    unknown = [n for n in args.scenarios if n not in SCENARIOS]
    if unknown:
        print("unknown scenario(s): " + ", ".join(unknown), file=sys.stderr)
        return 2
    busy = live_session()
    if busy:
        print(f"refusing to run: {busy} (stop the desktop session first)", file=sys.stderr)
        return 2
    if not os.access(os.path.join(OUT, "freebsdx"), os.X_OK):
        d = os.path.basename(OUT)
        print(f"{d}/freebsdx isn't built (ninja -C {d})", file=sys.stderr)
        return 2

    work = tempfile.mkdtemp(prefix="fbx-e2e.")
    scanned = set()
    failed = 0
    names = args.scenarios or list(SCENARIOS)
    for name in names:
        t0 = time.time()
        try:
            SCENARIOS[name](work)
            if args.asan:
                reports = sanitizer_reports(work, scanned)
                check(not reports, f"{len(reports)} sanitizer report(s): " + "; ".join(reports[:3]))
            print(f"  PASS  {name:18} {time.time() - t0:5.1f}s")
        except Failed as e:
            failed += 1
            print(f"  FAIL  {name:18} {time.time() - t0:5.1f}s  {e}")
        except Exception as e:   # a broken scenario shouldn't hide the others
            failed += 1
            print(f"  ERROR {name:18} {type(e).__name__}: {e}")
    if args.shots:
        try:
            take_shots(work, args.shots)
        except Failed as e:
            failed += 1
            print(f"  FAIL  shots: {e}")
    if args.keep or failed:
        print(f"  logs: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    print(f"{'PASS' if not failed else 'FAIL'}: {failed} failure(s) in {len(names)} scenario(s)"
          + (" + shots" if args.shots else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
