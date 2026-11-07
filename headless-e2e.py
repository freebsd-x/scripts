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
import base64
import wave
import ssl
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
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
INPUT_MOUSE_MOVE, INPUT_MOUSE_BUTTON, INPUT_MOUSE_WHEEL = 0, 1, 3
BTN_LEFT = 272
HEADLESS_INJECT = 3
SHELL_LAUNCH_WITH_FILE = 5
NOTIFICATION_POST = 1

# Oversized-layout reports that are expected (an unbounded table header inside
# a horizontal scroller).
LAYOUT_ALLOW = {"taskmanager": 1}

# Visible content laid out into a zero-size box that is expected to be.
COLLAPSED_ALLOW = {}

# What --asan runs by default. Sanitized processes run 3-5x slower, so the full
# suite took ~14 min; these keep what a sanitizer adds: every app sanitized
# (apps-start) and the compositor paths each scenario drives. Left out: the
# layout/contrast audits (they launch the same apps again; their findings are
# about layout, which the normal run checks) and smoke-tools (~3.5 min).
# --asan --all still runs everything.
ASAN_DEFAULT = ["file-open", "persistence", "shell-trust", "gates", "consent", "shell-keyboard", "fetch", "apps-start", "app-tasks"]

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
    end = time.time() + timeout
    try:
        while True:
            # A deadline, not just a socket timeout: broadcasts arriving would
            # restart that one for as long as they keep coming.
            left = end - time.time()
            if left <= 0:
                return None
            s.settimeout(left)
            d = s.recv(4096)
            if not d:          # the compositor is gone; never spin on a closed socket
                return None
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
    end = time.time() + timeout
    try:
        while True:
            left = end - time.time()
            if left <= 0:
                return None
            s.settimeout(left)
            d = s.recv(4096)
            if not d:
                return None
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

    def __init__(self, workdir, name, strict=False, config=None, browser_engine="/nonexistent"):
        self.browser_engine = browser_engine
        self.home = os.path.join(workdir, name, "home")
        self.logs = os.path.join(workdir, name)
        self.runtime = os.path.join(workdir, name, "runtime")   # where Wayland's socket goes
        os.makedirs(self.runtime, mode=0o700, exist_ok=True)
        os.makedirs(os.path.join(self.home, ".config", "freebsd-x"), exist_ok=True)
        if config:
            with open(os.path.join(self.home, ".config", "freebsd-x", "config"), "w") as f:
                f.write(config)
        self.strict = strict
        self.procs = []

    def env(self, **extra):
        # FBX_NO_AUDIO: nothing here should play through the real sound card.
        e = dict(os.environ, HOME=self.home, FBX_NO_AUDIO="1", XDG_RUNTIME_DIR=self.runtime,
                 # Starting Chromium costs seconds in every scenario that opens each
                 # app; they get the Browser's own window instead.
                 FBX_BROWSER_ENGINE=self.browser_engine,
                 FBX_BROWSER_ARGS="--disable-background-networking")
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

def on_screen(s, rgb, tag):
    """How many (sampled) pixels of the screen are this colour, give or take a little."""
    ppm = os.path.join(s.logs, tag + ".ppm")
    subprocess.run([os.path.join(OUT, "freebsdx-headless-capture"), ppm], capture_output=True, timeout=15 * TIME_SCALE)
    if not os.path.exists(ppm):
        return 0
    parts = open(ppm, "rb").read().split(b"\n", 3)
    pix, near = parts[3], 0
    for i in range(0, len(pix) - 2, 3 * 4):
        if abs(pix[i] - rgb[0]) < 12 and abs(pix[i + 1] - rgb[1]) < 12 and abs(pix[i + 2] - rgb[2]) < 12:
            near += 1
    return near


def s_file_open(work):
    """Opening a file launches its app and shows it; a second file gets a window of its own."""
    a, b = os.path.join(work, "first.png"), os.path.join(work, "second.png")
    write_png(a, 64, 64, (220, 40, 40))
    write_png(b, 64, 64, (40, 200, 40))
    viewer = os.path.join(OUT, "imageviewer")
    with Session(work, "file-open") as s:
        s.shell()
        launch_with_file("com.freebsdx.imageviewer", a)
        wait_for(lambda: "Image Viewer — first.png" in s.read_log("compositor"), 15, "the viewer to open first.png")
        check(procs_running(viewer) == 1, "expected one Image Viewer")
        wait_for(lambda: on_screen(s, (220, 40, 40), "first") > 5000, 10, "first.png's pixels on screen")
        # A running, sandboxed viewer can't open a path it's told: its own window.
        launch_with_file("com.freebsdx.imageviewer", b)
        wait_for(lambda: "Image Viewer — second.png" in s.read_log("compositor"), 15, "second.png to open")
        wait_for(lambda: on_screen(s, (40, 200, 40), "second") > 5000, 10, "second.png's pixels on screen")


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


def s_window_chrome(work):
    """Window chrome is drawn in its own colours (the solid shader once read each
    colour 8 bytes late, painting a bright green band across every title bar)."""
    with Session(work, "window-chrome") as s:
        calc = AppDriver(s, "calculator")
        ppm = os.path.join(s.logs, "chrome.ppm")
        subprocess.run([os.path.join(OUT, "freebsdx-headless-capture"), ppm], capture_output=True, timeout=15 * TIME_SCALE)
        with open(ppm, "rb") as f:
            _, dims, _, rgb = f.read().split(b"\n", 3)
        w, h = map(int, dims.split())
        green = 0
        for i in range(0, w * h * 3, 3):
            if rgb[i + 1] > 180 and rgb[i] < 110 and rgb[i + 2] < 110:
                green += 1
        calc.close()
        # The maximize button is green too, but only about 110 pixels of it.
        check(green < 2000, f"{green} bright green pixels on screen")


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


class AppDriver:
    """Drives one app the way a user would — keys on the keyboard — and reads
    back what it shows: FBX_UI_DUMP=1 makes an xkit app print its visible text
    (labels and fields, with positions) when sent SIGUSR1."""
    KEYCODE = {**{str(d): (11 if d == 0 else d + 1) for d in range(10)},
               "+": 13, "=": 13, "-": 12, "*": 9, "/": 53, ".": 52, " ": 57}

    def __init__(self, session, name, *args, env=None):
        self.s, self.name = session, name
        self.p = session.start(name, [os.path.join(OUT, name), *args], FBX_UI_DUMP="1", **(env or {}))
        wait_for(lambda: "Painting frame" in session.read_log(name), 15, f"{name} to paint")
        time.sleep(1.0 * TIME_SCALE)
        self.c = connect()

    def key(self, code, ch="", shift=False, ctrl=False):
        mods = (1 if shift else 0) | (2 if ctrl else 0)
        for state in (1, 0):
            payload = struct.pack(INPUT_FMT, 2, code, state, mods, ord(ch) if ch else 0)
            self.c.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, payload))
            time.sleep(0.06 * TIME_SCALE)

    def type(self, text):
        for ch in text:
            self.key(self.KEYCODE.get(ch.lower(), 30 if ch.isalpha() else 57), ch, shift=ch in "+*")
        time.sleep(0.3 * TIME_SCALE)

    def tab(self, shift=False): self.key(15, shift=shift)
    def enter(self): self.key(28); time.sleep(0.3 * TIME_SCALE)

    def screen(self):
        """The app's visible texts as [(x, y, text)]."""
        before = self.s.read_log(self.name).count("[ui-dump] end")
        os.kill(self.p.pid, signal.SIGUSR1)
        wait_for(lambda: self.s.read_log(self.name).count("[ui-dump] end") > before, 5, f"{self.name}'s screen")
        block = self.s.read_log(self.name).rsplit("[ui-dump] begin", 1)[1].split("[ui-dump] end")[0]
        out = []
        for line in block.splitlines():
            m = re.match(r"\[ui-dump\] (-?\d+),(-?\d+) (.*)", line)
            if m:
                out.append((int(m.group(1)), int(m.group(2)), m.group(3)))
        return out

    def texts(self):
        return [t for _, _, t in self.screen()]

    def close(self):
        self.c.close()
        try:
            os.killpg(self.p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        self.p.wait()


def s_app_tasks(work):
    """Apps do their job, driven from the keyboard and checked on screen:
    Calculator computes (typing previews, Enter commits once and chains), Text
    Editor types into the document, Notes' "+ New Note" and Contacts' "+ Add
    Contact" let you type straight away, Files opens folders, Terminal runs
    commands, gets Tab (Shift+Tab leaves it) and renames sessions, Weather's
    field holds what's typed, Calendar's grid gets the window."""
    problems = []
    # A plain shell: the user's zsh would stop at its first-run wizard in the
    # scenario's empty $HOME.
    saved_shell = os.environ.get("SHELL")
    os.environ["SHELL"] = "/bin/sh"
    with Session(work, "app-tasks") as s:
        calc = AppDriver(s, "calculator")
        calc.type("12+30")
        t = calc.texts()
        if "42" not in t:
            problems.append(f"calculator: no live result for 12+30 ({t[:6]})")
        if any("=" in x and x != "=" for x in t):
            problems.append("calculator: typing added history entries")
        calc.enter()
        t = calc.texts()
        if t.count("12+30 = 42") != 1:
            problems.append(f"calculator: history after Enter is {[x for x in t if ' = ' in x]}")
        calc.type("*2")
        calc.enter()
        t = calc.texts()
        if "42*2 = 84" not in t:
            problems.append(f"calculator: the result didn't chain into the next expression ({[x for x in t if ' = ' in x]})")
        calc.close()

        ed = AppDriver(s, "texteditor")
        ed.type("hello freebsd")
        time.sleep(1.5 * TIME_SCALE)   # the wrap fed back on itself over frames
        rows = ed.screen()
        body = [t for x, y, t in rows if y > 40]          # below the File: toolbar
        if "hello freebsd" not in body:
            problems.append(f"texteditor: typing didn't reach the document as one line ({body[:6]})")
        ed.close()

        notes = AppDriver(s, "notes")
        notes.tab(); notes.tab(); notes.enter()           # focus "+ New Note", press it
        notes.type("Buy milk")
        t = notes.texts()
        if "Buy milk" not in t or "Search…" not in t:
            problems.append(f"notes: a new note's title didn't take the typing ({t[:10]})")
        notes.close()

        contacts = AppDriver(s, "contacts")
        contacts.tab(); contacts.tab(); contacts.enter()   # "+ Add Contact"
        contacts.type("Ada")
        t = contacts.texts()
        if "Ada" not in t or "Search contacts…" not in t:
            problems.append(f"contacts: the new-contact form didn't take the typing ({t[:10]})")
        contacts.close()

        for name in ("alpha.txt", "docs/inner.txt"):
            path = os.path.join(s.home, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").write("x\n")
        files = AppDriver(s, "files")
        rows = files.screen()
        names = [(x, t.strip()) for x, _, t in rows if t.strip() in ("alpha.txt", "/ docs")]
        if len(names) != 2 or any(x > 100 for x, _ in names):
            problems.append(f"files: names not listed at the left ({rows[:6]})")
        files.tab(); files.tab(); files.tab(); files.enter()   # Up, Snapshots, then the docs row
        time.sleep(0.7 * TIME_SCALE)
        t = [x.strip() for x in files.texts()]
        if "inner.txt" not in t or "1 item" not in t:
            problems.append(f"files: Enter on a folder didn't open it ({t[:8]})")
        files.close()

        term = AppDriver(s, "terminal")                    # SHELL=/bin/sh: see below
        term.type("cat -t")
        term.enter()
        time.sleep(0.5 * TIME_SCALE)
        term.tab()                                         # Tab goes to the program, not to focus…
        term.type("x")
        term.enter()
        time.sleep(0.7 * TIME_SCALE)
        t = term.texts()
        if not any(l.strip() == "^Ix" for l in t):
            problems.append(f"terminal: Tab or typing didn't reach the shell ({t[-6:]})")
        term.key(32, "d", ctrl=True)                       # end cat: sessions outlive the app
        time.sleep(0.4 * TIME_SCALE)
        term.tab(shift=True)                               # …and Shift+Tab leaves the pane
        term.type("zz")
        term.enter()
        time.sleep(0.7 * TIME_SCALE)
        t = term.texts()
        if any("zz" in l for l in t):
            problems.append("terminal: Shift+Tab didn't move focus out of the pane")
        term.close()

        term = AppDriver(s, "terminal")                    # Ctrl+Shift+R renames the session
        time.sleep(0.5 * TIME_SCALE)
        term.key(19, "R", shift=True, ctrl=True)
        time.sleep(0.4 * TIME_SCALE)
        term.type("x")
        term.enter()
        time.sleep(0.4 * TIME_SCALE)
        term.type("echo back")
        term.enter()
        time.sleep(0.7 * TIME_SCALE)
        t = [l.strip() for l in term.texts()]
        if "session-1x" not in t:
            problems.append(f"terminal: rename didn't reach the tab ({t[:6]})")
        if "back" not in t:
            problems.append(f"terminal: typing didn't return to the shell after renaming ({t[-4:]})")
        term.close()

        weather = AppDriver(s, "weather")
        weather.type("Seoul")
        t = weather.texts()
        if "Seoul" not in t:
            problems.append(f"weather: the city field reads {t[:1]} (its hint must not be its text)")
        weather.close()

        cal = AppDriver(s, "calendar")
        grid_y = [y for _, y, t in cal.screen() if t == "Sun"]
        if not grid_y or grid_y[0] > 150:
            problems.append(f"calendar: the month grid starts at y={grid_y} (toolbars took the window)")
        cal.close()
    if saved_shell is None:
        del os.environ["SHELL"]
    else:
        os.environ["SHELL"] = saved_shell
    check(not problems, " | ".join(problems))


class Watcher:
    """A plain app-level connection that collects what it's sent, to prove what
    other apps do NOT get to see."""

    def __init__(self):
        self.s = connect()
        self.s.send(xbus_msg("com.freebsdx.Notes", 5, struct.pack("<I", 0).ljust(256, b"\0")))   # register
        self.seen = []
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    return
                self.seen.append(d)
        except OSError:
            return

    def saw(self, service):
        tag = service.encode()
        return [d for d in self.seen if len(d) >= 300 and d[8:40].split(b"\0")[0] == tag]

    def close(self):
        self.s.shutdown(socket.SHUT_RDWR)
        self.s.close()


def s_webview(work):
    """Our own web engine draws a page in a window, inside the sandbox: layout lands
    on the exact pixels, images show, what's below the window scrolls into view."""
    with Session(work, "webview") as s:
        s.shell()
        pagedir = os.path.join(work, "webview-page")
        os.makedirs(pagedir, exist_ok=True)
        write_png(os.path.join(pagedir, "dot.png"), 64, 64, (40, 200, 40))
        with open(os.path.join(pagedir, "style.css"), "w") as f:
            f.write("#linked { width: 80px; height: 40px; background: rgb(230, 130, 20); }")
        with open(os.path.join(pagedir, "page.html"), "w") as f:
            f.write("""<!doctype html><title>t</title><link rel=stylesheet href=style.css>
<style>body{margin:0;background:rgb(240,240,240);--c:rgb(220,40,40)}
#r{width:200px;height:100px;background:var(--c);margin:30px 0 0 40px}
#b{height:60px;width:300px;background:rgb(40,40,220);margin-top:3000px}
h1{margin:20px 0;color:rgb(10,10,10)}img{display:block;margin-top:10px}</style>
<div id=r></div><img src=dot.png><div id=linked></div><h1>Heading</h1><div id=b></div>""")
        p = s.start("xwebview", [os.path.join(OUT, "xwebview"), os.path.join(pagedir, "page.html")])
        wait_for(lambda: on_screen(s, (220, 40, 40), "red") > 1000, 20, "the page's first box on screen")
        size = [0]
        def settled():
            now = on_screen(s, (220, 40, 40), "red")
            same = now == size[0]
            size[0] = now
            return same
        wait_for(settled, 10, "the window to finish opening")
        check("Capsicum sandbox active" in s.read_log("xwebview"), "the viewer isn't sandboxed")

        ppm = open(os.path.join(s.logs, "red.ppm"), "rb").read().split(b"\n", 3)
        w, h = map(int, ppm[1].split())
        def find(rgb):
            """Top-left of a colour's pixels."""
            xs, ys = [], []
            for y in range(h):
                row = ppm[3][y * w * 3:(y + 1) * w * 3]
                for x in range(0, w):
                    if all(abs(row[x * 3 + i] - rgb[i]) < 8 for i in range(3)):
                        xs.append(x)
                        ys.append(y)
            return (min(xs), min(ys), len(xs)) if xs else None
        red, green, orange = find((220, 40, 40)), find((40, 200, 40)), find((230, 130, 20))
        check(red and red[2] == 200 * 100, f"the 200x100 box covers {red}")
        check(green and green[2] == 64 * 64, f"the 64x64 image isn't whole: {green}")
        check(orange and orange[2] == 80 * 40, f"the linked stylesheet didn't apply: {orange}")
        # The window's left edge is 40px left of the box (its margin), and so everything
        # else is placed from there.
        left = red[0] - 40
        check(green[0] == left and green[1] == red[1] + 100 + 10, f"the image is at {green[:2]}, the box at {red[:2]}")
        check(orange[0] == left and orange[1] == green[1] + 64, f"the linked box is at {orange[:2]}, the image at {green[:2]}")
        check(on_screen(s, (10, 10, 10), "text") > 50, "the heading's text isn't drawn")
        check(on_screen(s, (40, 40, 220), "before") == 0, "the far box shows before scrolling")

        # A wheel brings the rest of the page into view.
        mouse = Pointer()
        mouse._inject(INPUT_MOUSE_MOVE, -10000, -10000)
        mouse._inject(INPUT_MOUSE_MOVE, left + 400, red[1] + 200)
        def scroll_on():
            mouse._inject(INPUT_MOUSE_WHEEL, 0, 3)
            return on_screen(s, (40, 40, 220), "scrolled") > 100
        wait_for(scroll_on, 60, "the far box to scroll into view")
        mouse.close()
        p.terminate()


class FakeWebServer:
    """A small website on this machine, and a record of what was asked for."""

    def __init__(self, extra=None, delays=None):
        import http.server
        outer = self
        self.requests = []
        delays = delays or {}
        png = os.path.join(tempfile.mkdtemp(prefix="fbx-web."), "dot.png")
        write_png(png, 64, 64, (40, 200, 40))
        dot = open(png, "rb").read()
        box = lambda colour, h=100, more="": f"display:block;width:300px;height:{h}px;background:rgb({colour});{more}"
        pages = {
            "/first": ("text/html", f"""<!doctype html><title>First</title><link rel=stylesheet href=/s.css>
                <link rel=stylesheet href=/l.css?{'x' * 600}>
                <body style="margin:0;background:rgb(240,240,240)"><a id=go href=/second style="{box('220,40,40')}"></a>
                <div id=long></div>
                <div id=linked></div><img src=/dot.png style=display:block>""".encode()),
            "/second": ("text/html", f"""<!doctype html><title>Second</title>
                <body style="margin:0;background:rgb(240,240,240)"><a href=/moved style="{box('40,40,220')}"></a>""".encode()),
            "/third": ("text/html", f"""<!doctype html><title>Third</title>
                <body style="margin:0;background:rgb(240,240,240)"><a href=#far style="{box('140,40,180')}"></a>
                <div id=far style="{box('20,180,180', 100, 'margin-top:3000px')}"></div>""".encode()),
            "/s.css": ("text/css", f"#linked{{{box('230,130,20', 40, 'width:80px')}}}".encode()),
            "/l.css": ("text/css", f"#long{{{box('250,240,100', 30, 'width:90px')}}}".encode()),
            "/dot.png": ("image/png", dot),
            "/plain": ("text/plain", b"plain <b>text</b>"),
        }
        pages.update(extra or {})

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                time.sleep(delays.get(self.path, 0))
                if self.path.startswith("/l.css?"):   # an address too long for a message
                    self.path = "/l.css"
                if self.path == "/moved":
                    self.send_response(302)
                    self.send_header("Location", "/third")
                    self.end_headers()
                    return
                if self.path in pages:
                    kind, body = pages[self.path]
                    self.send_response(200)
                else:
                    kind, body = "text/html", f"""<!doctype html><title>Gone</title><body style=margin:0><div style="{box('200,40,160')}"></div>""".encode()
                    self.send_response(404)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def s_webpaint(work):
    """What a page draws besides boxes and text comes through the sandbox: gradients, SVG
    inline and as files, CSS images from a stylesheet's own folder, masks, rounded and
    dashed borders, and shadows. A box that clips its overflow clips its positioned
    children too."""
    RED, BLUE, GREEN, YELLOW = (220, 40, 40), (40, 40, 220), (20, 180, 60), (250, 200, 0)
    PURPLE, ORANGE, CYAN, PINK = (160, 40, 200), (255, 120, 0), (0, 200, 200), (230, 60, 150)
    RING, SHADOW, DASH = (0, 90, 255), (255, 0, 140), (120, 60, 0)
    rgb = lambda c: "rgb(%d,%d,%d)" % c
    svg = lambda body, size="width='20' height='20'": ("image/svg+xml", f"<svg xmlns='http://www.w3.org/2000/svg' {size}>{body}</svg>".encode())
    page = f"""<!doctype html><title>Paint</title><link rel=stylesheet href=/css/site.css>
        <style>body{{margin:0;background:rgb(240,240,240)}}div{{margin:0 0 10px 10px}}</style>
        <div style="width:300px;height:100px;background:linear-gradient(90deg,{rgb(RED)} 0 50%,{rgb(BLUE)} 50% 100%)"></div>
        <svg width=120 height=120 viewBox="0 0 10 10" style="color:{rgb(GREEN)};margin-left:10px"><circle cx=5 cy=5 r=5 fill=currentColor /></svg>
        <div style="width:200px;height:80px;background:url(/tile.svg)"></div>
        <div style="width:100px;height:100px;background:{rgb(PURPLE)};mask:url(/mask.svg) no-repeat center/contain;-webkit-mask:url(/mask.svg) no-repeat center/contain"></div>
        <img src=/logo.svg width=60 height=60 style="margin-left:10px">
        <div style="position:relative;overflow:hidden;width:100px;height:40px"><div style="position:absolute;left:-100%;top:0;width:300%;height:100%;background:{rgb(CYAN)}"></div></div>
        <div class=sheet></div>
        <div style="position:absolute;left:700px;top:20px;width:100px;height:100px;border-radius:50%;border:10px solid {rgb(RING)}"></div>
        <div style="position:absolute;left:700px;top:200px;width:60px;height:60px;background:white;box-shadow:0 0 0 10px {rgb(SHADOW)}"></div>
        <div style="position:absolute;left:700px;top:320px;width:200px;height:0;border-top:4px dashed {rgb(DASH)}"></div>""".encode()
    site = FakeWebServer(extra={
        "/paint": ("text/html", page),
        "/css/site.css": ("text/css", b".sheet{width:160px;height:60px;background:url(../img/bg.svg) 0 0/40px 30px}"),
        "/img/bg.svg": svg(f"<rect width='20' height='20' fill='{rgb(PINK)}'/>"),
        "/tile.svg": svg(f"<rect width='20' height='20' fill='{rgb(YELLOW)}'/>"),
        "/mask.svg": svg("<circle cx='10' cy='10' r='10'/>"),
        "/logo.svg": svg(f"<rect width='10' height='10' fill='{rgb(ORANGE)}'/>", "viewBox='0 0 10 10'"),
    })
    url = lambda path: f"http://127.0.0.1:{site.port}{path}"
    # on_screen samples one pixel in four.
    area = lambda w, h: w * h / 4
    try:
        os.environ["FBX_TEST_WEB_LOCAL"] = "1"
        with Session(work, "webpaint") as s:
            s.shell()
            p = s.start("xwebview", [os.path.join(OUT, "xwebview"), url("/paint")])
            wait_for(lambda: on_screen(s, RED, "first") > 1000, 30, "the page")
            check("Capsicum sandbox active" in s.read_log("xwebview"), "the browser isn't sandboxed")
            # A gradient with a hard stop: half each.
            red, blue = on_screen(s, RED, "red"), on_screen(s, BLUE, "blue")
            check(abs(red - area(150, 100)) < area(150, 100) * 0.15 and abs(blue - area(150, 100)) < area(150, 100) * 0.15,
                  f"the gradient's halves: {red} red, {blue} blue, want about {area(150, 100):.0f} each")
            # Inline SVG, in the colour its element has.
            green = on_screen(s, GREEN, "green")
            circle = 3.14159 * 60 * 60 / 4
            check(abs(green - circle) < circle * 0.15, f"the inline SVG circle: {green} green, want about {circle:.0f}")
            # Files: a tiled background, a mask, an <img>, each loaded once they're seen.
            for name, colour, want in (("tile", YELLOW, area(200, 80)), ("logo", ORANGE, area(60, 60)), ("sheet", PINK, area(160, 60))):
                wait_for(lambda: abs(on_screen(s, colour, name) - want) < want * 0.15, 20, f"{name}: about {want:.0f} pixels")
            # Through a circular mask: a circle of the colour, not its square.
            disc = 3.14159 * 50 * 50 / 4
            wait_for(lambda: abs(on_screen(s, PURPLE, "mask") - disc) < disc * 0.15, 20, f"the masked colour as a disc of about {disc:.0f}")
            # Relative to the stylesheet's folder, not the page's.
            check("/img/bg.svg" in site.requests, f"the sheet's image wasn't fetched from beside it: {site.requests}")
            # A round border is a ring; a spread shadow a frame round its box; a dashed line has gaps.
            ring = 3.14159 * (60 * 60 - 50 * 50) / 4
            got = on_screen(s, RING, "ring")
            check(abs(got - ring) < ring * 0.2, f"the round border: {got}, want about {ring:.0f}")
            frame = (80 * 80 - 60 * 60) / 4
            got = on_screen(s, SHADOW, "shadow")
            check(abs(got - frame) < frame * 0.2, f"the spread shadow: {got}, want about {frame:.0f}")
            got = on_screen(s, DASH, "dash")
            check(area(200, 4) * 0.25 < got < area(200, 4) * 0.85, f"the dashed line: {got} of a solid {area(200, 4):.0f}")
            # Overflow clips the positioned child: only the parent's 100x40 shows.
            cyan = on_screen(s, CYAN, "cyan")
            check(abs(cyan - area(100, 40)) < area(100, 40) * 0.2, f"the clipped child shows {cyan}, want about {area(100, 40):.0f}")
            p.terminate()
    finally:
        os.environ.pop("FBX_TEST_WEB_LOCAL", None)


def s_webfont(work):
    """A page's own fonts come through the sandbox: text shows at once in another font, then
    takes the web font's shape when it arrives. Only what the text needs is fetched, a bad
    source falls through to the next, and a page doesn't get another page's fonts."""
    RED, BLUE, TEAL = (220, 40, 40), (40, 40, 220), (20, 180, 180)
    fonts = os.path.join(work, "webfonts")
    subprocess.run([sys.executable, "-I", os.path.join(ROOT, "sdk/xweb/tests/make_webfonts.py"), fonts], check=True)
    read = lambda name: open(os.path.join(fonts, name), "rb").read()
    inline = base64.b64encode(read("serif.woff")).decode()
    rules = """@font-face{font-family:Web;src:url(/broken.woff2) format("woff2"),url(/mono.woff) format("woff")}
        @font-face{font-family:Web;src:url(/hangul.woff);unicode-range:U+AC00-D7A3}
        @font-face{font-family:Hidden;src:url(/hidden.woff)}
        @font-face{font-family:Unused;src:url(/unused.woff)}
        @font-face{font-family:Inline;src:url(data:font/woff;base64,%s)}""" % inline
    style = "body{margin:0;background:rgb(240,240,240);font:40px sans-serif}div{display:block;width:max-content;color:white}"
    page = lambda link: f"""<!doctype html><title>Fonts</title><style>{rules}{style}</style>
        <div id=a style="background:rgb(220,40,40);font-family:Web,sans-serif">MMMMMMMM</div>
        <div id=b style="background:rgb(40,40,220);font-family:Inline,sans-serif">MMMMMMMM</div>
        <div style="display:none;font-family:Hidden">not shown</div>
        <a href={link} style="display:block;width:300px;height:80px;background:rgb(20,180,180)"></a>""".encode()
    plain = f"""<!doctype html><title>Plain</title><style>{style}</style>
        <div style="background:rgb(220,40,40);font-family:Web,sans-serif">MMMMMMMM</div>""".encode()
    site = FakeWebServer(
        extra={
            "/fonts": ("text/html", page("/again")),
            "/again": ("text/html", page("/plain-page")),
            "/plain-page": ("text/html", plain),
            "/broken.woff2": ("font/woff2", b"this is not a font at all"),
            "/mono.woff": ("font/woff", read("mono-bold.woff")),
        },
        delays={"/mono.woff": 4 * TIME_SCALE},
    )
    url = lambda path: f"http://127.0.0.1:{site.port}{path}"

    # What layout says those lines should measure, with the fonts as plain files.
    probe = os.path.join(fonts, "probe.html")
    with open(probe, "w") as f:
        f.write(f"""<!doctype html><style>@font-face{{font-family:Web;src:url(mono-bold.woff)}}
            @font-face{{font-family:Inline;src:url(serif.woff)}}{style}</style>
            <div style="font-family:Web">MMMMMMMM</div><div style="font-family:Inline">MMMMMMMM</div>""")
    tool = subprocess.run([os.path.join(OUT, "sdk/xweb/xweb_layout"), probe, "800", "600"], capture_output=True, text=True, check=True).stdout
    widths = [float(line.split()[3]) for line in tool.splitlines() if line.startswith("div ")]
    check(len(widths) == 2 and abs(widths[0] - widths[1]) > 20, f"the two test fonts aren't different enough: {widths}")
    want_mono, want_serif = widths
    # Eight letters of a font whose every letter is 0.6em: a fallback wouldn't come to this.
    check(abs(want_mono - 8 * 0.6 * 40) < 2, f"the font didn't load: {want_mono} wide")

    def extent(s, rgb, tag):
        """How wide a colour runs on screen, and how many pixels inside it aren't that colour."""
        on_screen(s, rgb, tag)
        parts = open(os.path.join(s.logs, tag + ".ppm"), "rb").read().split(b"\n", 3)
        w, h = map(int, parts[1].split())
        data = parts[3]
        near = lambda x, y, c: all(abs(data[(y * w + x) * 3 + i] - c[i]) < 8 for i in range(3))
        xs, ys = [], []
        for y in range(0, h, 2):
            for x in range(0, w):
                if near(x, y, rgb):
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return 0, 0
        lit = 0
        for y in range(min(ys), max(ys) + 1):
            for x in range(min(xs), max(xs) + 1):
                if all(data[(y * w + x) * 3 + i] > 200 for i in range(3)):
                    lit += 1
        return max(xs) - min(xs) + 1, lit

    try:
        os.environ["FBX_TEST_WEB_LOCAL"] = "1"
        with Session(work, "webfont") as s:
            s.shell()
            p = s.start("xwebview", [os.path.join(OUT, "xwebview"), url("/fonts")])
            wait_for(lambda: on_screen(s, RED, "first") > 1000, 30, "the page")
            check("Capsicum sandbox active" in s.read_log("xwebview"), "the browser isn't sandboxed")

            # The font is still on its way: the text is up in another one.
            early, early_lit = extent(s, RED, "early")
            check(abs(early - want_mono) > 10, f"the line is already {early} wide with the font still on its way")
            check(early_lit > 200, f"no text showing while the font loads ({early_lit} pixels)")
            # The one in the stylesheet itself is decoded at once, and it isn't the fallback.
            wait_for(lambda: abs(extent(s, BLUE, "inline")[0] - want_serif) < 4, 15, f"the inline font: want {want_serif} wide")
            # Then it arrives, and the page reflows and draws with it.
            wait_for(lambda: abs(extent(s, RED, "swap")[0] - want_mono) < 4, 30, f"the font to arrive: want {want_mono} wide")
            late_lit = extent(s, RED, "late")[1]
            check(late_lit > 200, f"the web font drew nothing ({late_lit} pixels)")
            check(late_lit != early_lit, "the text looks the same in both fonts")

            check("/broken.woff2" in site.requests, f"the first source wasn't tried: {site.requests}")
            check(site.requests.index("/broken.woff2") < site.requests.index("/mono.woff"), f"sources out of order: {site.requests}")
            for name in ("/hangul.woff", "/hidden.woff", "/unused.woff"):
                check(name not in site.requests, f"{name} was fetched with no text needing it")

            # The next page using it doesn't fetch it again, and has it from its first frame.
            def aim():
                on_screen(s, TEAL, "aim")
                parts = open(os.path.join(s.logs, "aim.ppm"), "rb").read().split(b"\n", 3)
                w, h = map(int, parts[1].split())
                xs, ys = [], []
                for y in range(0, h, 2):
                    row = parts[3][y * w * 3:(y + 1) * w * 3]
                    for x in range(0, w, 2):
                        if all(abs(row[x * 3 + i] - TEAL[i]) < 8 for i in range(3)):
                            xs.append(x)
                            ys.append(y)
                check(xs, "no link to click")
                return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2

            mouse = Pointer()
            time.sleep(0.5)
            mouse.click(*aim())
            wait_for(lambda: "/again" in site.requests, 15, "the link to the second page")
            wait_for(lambda: abs(extent(s, RED, "again")[0] - want_mono) < 4 and on_screen(s, TEAL, "again-teal") > 1000, 15, "the second page in the font")
            check(site.requests.count("/mono.woff") == 1, f"a font was fetched twice: {site.requests}")

            # A page that didn't ask for it doesn't get it, whatever it calls its fonts.
            time.sleep(0.5)
            mouse.click(*aim())
            wait_for(lambda: "/plain-page" in site.requests, 15, "the link to the third page")
            wait_for(lambda: on_screen(s, TEAL, "gone-teal") == 0 and on_screen(s, RED, "plain") > 1000, 15, "the third page")
            plain_w = extent(s, RED, "plain-w")[0]
            check(abs(plain_w - want_mono) > 10, f"a page got another page's font: {plain_w} wide")
            check(abs(plain_w - early) < 4, f"the third page's fallback is {plain_w} wide, not {early}")
            mouse.close()
            p.terminate()
    finally:
        os.environ.pop("FBX_TEST_WEB_LOCAL", None)
        site.close()


def s_webnav(work):
    """Our web engine browses: pages, stylesheets and images come through the sandbox's
    broker, links and redirects are followed, history and the address bar work. Nothing
    can reach this machine's own services unless a test asks, and nobody else may browse."""
    RED, BLUE, GREEN, ORANGE = (220, 40, 40), (40, 40, 220), (40, 200, 40), (230, 130, 20)
    PURPLE, TEAL, PINK = (140, 40, 180), (20, 180, 180), (200, 40, 160)
    site = FakeWebServer()
    url = lambda path: f"http://127.0.0.1:{site.port}{path}"
    SCAN = dict(zip("abcdefghijklmnopqrstuvwxyz", [30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44]))
    try:
        # Unless told otherwise, a page can't reach this machine.
        with Session(work, "webnav-private") as s:
            s.shell()
            p = s.start("xwebview", [os.path.join(OUT, "xwebview"), url("/first")])
            wait_for(lambda: "Capsicum sandbox active" in s.read_log("xwebview"), 20, "the browser to enter its sandbox")
            time.sleep(4 * TIME_SCALE)
            check(on_screen(s, RED, "private") == 0, "a page was fetched from this machine")
            check(not site.requests, f"the local server was reached: {site.requests}")
            # And nobody but the browser may ask for the web at all.
            payload = struct.pack("<I", 1) + url("/first").encode().ljust(252, b"\0")
            reply = call("com.freebsdx.Fetch", 0x01, payload)
            check(reply is not None and struct.unpack("<i", reply[44:48])[0] == 1, "another client was allowed to browse")
            payload = struct.pack("<I", 0) + url("/first").encode().ljust(252, b"\0")
            reply = call("com.freebsdx.Fetch", 0x01, payload)
            check(reply is not None and struct.unpack("<i", reply[44:48])[0] == 1, "another client fetched an unlisted host")
            check(not site.requests, f"the local server was reached by a client: {site.requests}")
            p.terminate()

        os.environ["FBX_TEST_WEB_LOCAL"] = "1"
        with Session(work, "webnav") as s:
            s.shell()
            p = s.start("xwebview", [os.path.join(OUT, "xwebview"), url("/first")])
            wait_for(lambda: on_screen(s, RED, "first") > 1000, 30, "the first page")
            wait_for(lambda: on_screen(s, GREEN, "image") > 500, 20, "its image")
            check(on_screen(s, ORANGE, "linked") > 500, "its stylesheet didn't apply")
            check(on_screen(s, (250, 240, 100), "long") > 300, "the stylesheet with a long address didn't apply")
            check(site.requests[:1] == ["/first"], f"odd requests: {site.requests}")
            check("Capsicum sandbox active" in s.read_log("xwebview"), "the browser isn't sandboxed")

            keys = connect()
            def press(code, ch=0, mods=0):
                for state in (1, 0):
                    keys.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, struct.pack(INPUT_FMT, 2, code, state, mods, ch)))
                    time.sleep(0.06)

            def spot(rgb, tag):
                """Somewhere in the middle of a colour's pixels."""
                on_screen(s, rgb, tag)
                parts = open(os.path.join(s.logs, tag + ".ppm"), "rb").read().split(b"\n", 3)
                w, h = map(int, parts[1].split())
                xs, ys = [], []
                for y in range(0, h, 2):
                    row = parts[3][y * w * 3:(y + 1) * w * 3]
                    for x in range(0, w, 2):
                        if all(abs(row[x * 3 + i] - rgb[i]) < 8 for i in range(3)):
                            xs.append(x)
                            ys.append(y)
                check(xs, f"no {rgb} on screen to click")
                return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2

            # A link. Clicking it has to find the right place on the page, settled.
            time.sleep(0.5)
            mouse = Pointer()
            mouse.click(*spot(RED, "aim"))
            wait_for(lambda: on_screen(s, BLUE, "second") > 1000, 15, "the link to the second page")
            check(on_screen(s, RED, "second-red") == 0, "the old page is still showing")

            # History, by keyboard
            press(105, 0, 4)   # Alt+Left
            wait_for(lambda: on_screen(s, RED, "back") > 1000, 15, "Back to the first page")
            press(106, 0, 4)   # Alt+Right
            wait_for(lambda: on_screen(s, BLUE, "forward") > 1000, 15, "Forward to the second page")

            # A redirect, then a link within the page.
            mouse.click(*spot(BLUE, "aim2"))
            wait_for(lambda: on_screen(s, PURPLE, "third") > 1000, 15, "the redirect to the third page")
            check("/moved" in site.requests and "/third" in site.requests, f"odd requests: {site.requests}")
            check(on_screen(s, TEAL, "not-yet") == 0, "the far box shows before following #far")
            before = len(site.requests)
            mouse.click(*spot(PURPLE, "aim3"))
            wait_for(lambda: on_screen(s, TEAL, "fragment") > 1000, 15, "the in-page link to scroll")
            check(len(site.requests) == before, "an in-page link fetched something")
            mouse.close()

            # A page that isn't there still shows what the site sent, and typing an address
            # takes us there: Ctrl+L selects what's in the field, so typing replaces it.
            press(SCAN["l"], ord("l"), 2)
            time.sleep(0.2)
            for c in url("/nothing"):
                press(SCAN.get(c, 2), ord(c))
            press(28, 13)
            wait_for(lambda: on_screen(s, PINK, "notfound") > 1000, 15, "the typed address's 404 page")
            check("/nothing" in site.requests, f"odd requests: {site.requests}")

            keys.close()
            p.terminate()
    finally:
        os.environ.pop("FBX_TEST_WEB_LOCAL", None)
        site.close()


def s_replies(work):
    """Requests get answered: a service's reply carries the request's id (a client
    waits on that id, and a reply without it never reaches the waiting code), one
    reply per request, and a paged list answers the page asked for."""
    problems = []

    def one(service, opcode, payload=b"", name=""):
        s = connect()
        tid = 777
        s.send(xbus_msg(service, opcode, payload.ljust(256, b"\0"), tid))
        s.settimeout(3.0 * TIME_SCALE)
        reply = None
        try:
            while reply is None:
                d = s.recv(4096)
                if not d:
                    break
                if len(d) >= 300 and d[8:40].split(b"\0")[0] == service.encode():
                    got = struct.unpack("<I", d[4:8])[0]
                    if got == tid:
                        reply = d
                    elif got != 0:
                        problems.append(f"{name}: reply with a stranger's id {got}")
            s.settimeout(0.4 * TIME_SCALE)
            while True:
                d = s.recv(4096)
                if not d:
                    break
                if len(d) >= 300 and struct.unpack("<I", d[4:8])[0] == tid:
                    problems.append(f"{name}: a second reply to one request")
        except socket.timeout:
            pass
        s.close()
        if reply is None:
            problems.append(f"{name}: no reply carrying the request's id")
        return reply[44:] if reply else b""

    with Session(work, "replies"):
        B, N, W, D = ("com.freebsdx.Bookmarks", "com.freebsdx.Notes", "com.freebsdx.Weather", "com.freebsdx.Disk")
        for title, url in ((b"One", b"http://one/"), (b"Two", b"http://two/")):
            one(B, 1, title.ljust(60, b"\0") + url, "bookmarks add")
        p0 = one(B, 3, struct.pack("<I", 0), "bookmarks list")
        p1 = one(B, 3, struct.pack("<I", 1), "bookmarks list page 1")
        p2 = one(B, 3, struct.pack("<I", 2), "bookmarks list past the end")
        def bm(p):
            return struct.unpack("<4I", p[:16]) + (p[16:76].split(b"\0")[0],)
        if bm(p0) != (0, 2, 1, 1, b"One") or bm(p1) != (1, 2, 1, 0, b"Two") or bm(p2)[2] != 0:
            problems.append(f"bookmarks: pages read {bm(p0)} {bm(p1)} {bm(p2)}")

        one(W, 1, b"", "weather current")
        one(W, 2, b"", "weather forecast")
        one(W, 2, struct.pack("<I", 1), "weather forecast page 1")

        for op, what in ((1, "disks"), (7, "pools")):     # ListDisks, ListZpools
            page = one(D, op, struct.pack("<I", 0), what)
            page_no, more, total = struct.unpack("<3I", page[:12]) if page else (0, 0, 0)
            if total > 1 and not more:
                problems.append(f"disk {what}: page 0 of {total} says there is no more")
            if total >= 1:
                page = one(D, op, struct.pack("<I", total - 1), f"{what} last page")
                if struct.unpack("<2I", page[:8]) != (total - 1, 0):
                    problems.append(f"disk {what}: the last page reads {struct.unpack('<2I', page[:8])}")
        one(D, 2, b"nosuch!device".ljust(32, b"\0"), "partitions of a bad name")

        one(N, 5, struct.pack("<I", 0), "notes list")
    check(not problems, " | ".join(problems))


def s_media(work):
    """What the media apps open really shows and plays, opened at launch and,
    inside the sandbox, through the picker: right colours, moving time, readable
    errors."""
    problems = []
    RED, GREEN = (224, 32, 32), (40, 200, 40)

    def pick(d, index, again=False):
        # Open… is first in the tab order (and keeps the focus after a pick); in the
        # picker, Tab * (index + 1) reaches a file, Enter selects it, Enter opens it.
        if not again:
            d.tab()
        d.enter()
        time.sleep(1.2 * TIME_SCALE)
        for _ in range(index + 1):
            d.tab()
        d.enter()
        d.enter()
        time.sleep(1.2 * TIME_SCALE)

    def clock(d):
        # "m:ss" values on screen, each as seconds.
        return [int(m) * 60 + int(ss) for t in d.texts() for m, ss in re.findall(r"(\d+):(\d\d)", t)]

    def advancing(d, what, longest):
        # Its position, the shortest time shown once a total is on screen, moves on.
        start = min(clock(d), default=None)
        time.sleep(3.0 * TIME_SCALE)
        now = min(clock(d), default=None)
        if start is None or now is None or now <= start or now >= longest:
            problems.append(f"{what}: the time doesn't advance ({start} then {now}): {d.texts()[:10]}")

    with Session(work, "media") as s:
        s.shell()
        home = s.home
        def fixture(name):
            return os.path.join(home, name)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0xE02020:s=320x240:d=0.1",
                        "-frames:v", "1", fixture("a_red.jpg")], check=True)
        write_png(fixture("b_green.png"), 64, 64, GREEN)
        with open(fixture("c_junk.jpg"), "wb") as f:
            f.write(b"this is not a picture")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=60:size=160x120:rate=5",
                        "-pix_fmt", "yuv420p", fixture("clip.mp4")], check=True)
        with open(fixture("doc.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 400 300]>>endobj\ntrailer<</Root 1 0 R>>\n")
        with wave.open(fixture("tone.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000 * 60)

        # Image Viewer, started on a JPEG: the picture, then its folder through
        # what it was allowed to keep.
        d = AppDriver(s, "imageviewer", "--open", fixture("a_red.jpg"))
        if on_screen(s, RED, "jpeg") < 5000:
            problems.append("viewer: a red JPEG isn't red on screen")
        if on_screen(s, (RED[2], RED[1], RED[0]), "jpeg") > 500:
            problems.append("viewer: red and blue are swapped")
        d.tab(); d.tab(); d.tab()   # Open…, ←, →
        d.enter()
        time.sleep(1.0 * TIME_SCALE)
        if "b_green.png" not in d.texts() or on_screen(s, GREEN, "next") < 5000:
            problems.append(f"viewer: next file in the folder doesn't show: {d.texts()[:6]}")
        d.enter()
        time.sleep(1.0 * TIME_SCALE)
        if not any("Can't show c_junk.jpg" in t for t in d.texts()):
            problems.append(f"viewer: a damaged file gives no message: {d.texts()[:12]}")
        d.close()

        # Started empty: only the picker can give it anything.
        d = AppDriver(s, "imageviewer")
        pick(d, 1)
        if "b_green.png" not in d.texts() or on_screen(s, GREEN, "picked") < 5000:
            problems.append(f"viewer: a picked PNG doesn't show: {d.texts()[:6]}")
        pick(d, 0, again=True)
        if "a_red.jpg" not in d.texts() or on_screen(s, RED, "picked2") < 5000:
            problems.append(f"viewer: a picked JPEG doesn't show: {d.texts()[:6]}")
        d.close()

        # Music: from the command line and added in the sandbox.
        d = AppDriver(s, "musicplayer", "--open", fixture("tone.wav"))
        advancing(d, "music from the command line", 60)
        d.close()
        d = AppDriver(s, "musicplayer")
        pick(d, 5)
        if not any("1 track" in t for t in d.texts()):
            problems.append(f"music: Add Files didn't add: {d.texts()[:8]}")
        advancing(d, "music added through the picker", 60)
        d.close()

        # Video.
        d = AppDriver(s, "videoplayer", "--open", fixture("clip.mp4"))
        advancing(d, "video from the command line", 60)
        d.close()
        d = AppDriver(s, "videoplayer")
        pick(d, 3)
        if "clip.mp4" not in d.texts():
            problems.append(f"video: a picked file doesn't open: {d.texts()[:6]}")
        advancing(d, "video opened through the picker", 60)
        d.close()

        # PDF: opened in the sandbox. A blank page, drawn white, in a window of grey.
        d = AppDriver(s, "pdfviewer")
        pick(d, 4)
        if "doc.pdf" not in d.texts() or "1 / 1" not in d.texts():
            problems.append(f"pdf: a picked file doesn't open: {d.texts()[:6]}")
        if on_screen(s, (255, 255, 255), "pdf") < 3000:
            problems.append("pdf: no page on screen")
        pick(d, 2, again=True)
        if "doc.pdf" in d.texts() or not any("isn't a readable PDF" in t for t in d.texts()):
            problems.append(f"pdf: a bad file gives no message: {d.texts()[:6]}")
        d.close()

    check(not problems, "; ".join(problems))


def s_sandbox_audit(work):
    """No app is refused a file by its sandbox. Every path an app opens past start-up
    fails, usually with nothing on screen to say so: this reads the kernel's list."""
    # Mesa writing to its shader cache: an optimisation, and it copes.
    harmless = ("mesa_shader_cache",)
    with Session(work, "sandbox-audit") as s:
        media = os.path.join(s.home, "m")
        os.makedirs(media)
        png, wav = os.path.join(media, "p.png"), os.path.join(media, "t.wav")
        write_png(png, 64, 64, (40, 200, 40))
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000)
        args = {"imageviewer": [png], "musicplayer": [wav]}
        denied, audited = [], 0
        for name, path in built_apps():
            trace = os.path.join(s.logs, name + ".ktrace")
            p = s.start(name, ["ktrace", "-f", trace, "-i", "-t", "cnp", path, *args.get(name, [])])
            try:
                wait_for(lambda: "Capsicum sandbox active" in s.read_log(name), 15, f"{name} to enter its sandbox")
            except Failed:
                if name != "gpudemo":     # draws through its own path, unsandboxed
                    denied.append(f"{name}: never entered its sandbox")
            time.sleep(2.0 * TIME_SCALE)
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            dump = subprocess.run(["kdump", "-f", trace, "-t", "cnp"], capture_output=True, text=True, errors="replace").stdout
            os.unlink(trace)
            inside, last = False, ""
            for line in dump.splitlines():
                if "CALL" in line and "cap_enter" in line:
                    inside = True
                m = re.search(r'NAMI\s+"(.*)"', line)
                if m:
                    last = m.group(1)
                elif inside and " CAP " in line and not any(h in last for h in harmless):
                    denied.append(f"{name}: {last or line.strip()}")
            audited += 1
        check(audited >= 20, f"only {audited} apps audited")
        check(not denied, f"refused by the sandbox: {sorted(set(denied))[:12]}")


class FakeMailServer:
    """Just enough IMAP and SMTP for Mail: one thread per connection, and a record
    of what it was told. With `tls`, wrapped in that certificate."""
    IMAP_PORT, IMAPS_PORT, SMTP_PORT = 11143, 11993, 11025

    def __init__(self, cert=None):
        self.logins, self.sent, self.fetches, self.socks, self.cert = [], [], [], [], cert
        self.listen(self.IMAP_PORT, self.imap)
        self.listen(self.SMTP_PORT, self.smtp)
        if cert:
            self.listen(self.IMAPS_PORT, self.imap, tls=True)

    def listen(self, port, handler, tls=False):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        self.socks.append(srv)
        threading.Thread(target=self.accept, args=(srv, handler, tls), daemon=True).start()

    def accept(self, srv, handler, tls):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn, handler, tls), daemon=True).start()

    def serve(self, conn, handler, tls):
        try:
            if tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(self.cert)
                conn = ctx.wrap_socket(conn, server_side=True)
            handler(conn, conn.makefile("rb"))
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    def imap(self, conn, lines):
        conn.sendall(b"* OK ready\r\n")
        for raw in lines:
            tag, _, rest = raw.decode("latin-1").strip().partition(" ")
            cmd = rest.split(" ")[0].upper()
            if cmd == "CAPABILITY":
                conn.sendall(f"* CAPABILITY IMAP4rev1\r\n{tag} OK done\r\n".encode())
            elif cmd == "LOGIN":
                self.logins.append(rest)
                conn.sendall(f"{tag} OK logged in\r\n".encode())
            elif cmd in ("SELECT", "EXAMINE"):
                conn.sendall(f"* 1 EXISTS\r\n* FLAGS (\\Seen)\r\n{tag} OK [READ-WRITE] done\r\n".encode())
            elif cmd == "UID":
                if "SEARCH" in rest:
                    conn.sendall(f"* SEARCH 7\r\n{tag} OK done\r\n".encode())
                elif "BODY[" in rest:
                    text = "The body of the message.\r\n"
                    conn.sendall(f"* 1 FETCH (UID 7 BODY[TEXT] {{{len(text)}}}\r\n{text})\r\n{tag} OK done\r\n".encode())
                else:
                    self.fetches.append(rest)
                    conn.sendall(f'* 1 FETCH (UID 7 FLAGS () BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" 5 1) ENVELOPE ("Thu, 5 Nov 2026 01:00:00 +0900" "=?utf-8?B?7JWI64WV?= Hello from IMAP" (("Ann" NIL "ann" "example.com")) NIL NIL (("Me" NIL "me" "example.com")) NIL NIL NIL "<1@x>"))\r\n{tag} OK done\r\n'.encode())
            elif cmd == "LOGOUT":
                conn.sendall(f"* BYE\r\n{tag} OK bye\r\n".encode())
                return
            else:
                conn.sendall(f"{tag} BAD no\r\n".encode())

    def smtp(self, conn, lines):
        conn.sendall(b"220 ready\r\n")
        for raw in lines:
            cmd = raw.decode("latin-1").strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                conn.sendall(b"250 ok\r\n")
            elif cmd == "DATA":
                conn.sendall(b"354 go\r\n")
                body = b""
                for part in lines:
                    if part in (b".\r\n", b".\n"):
                        break
                    body += part
                self.sent.append(body.decode("latin-1"))
                conn.sendall(b"250 queued\r\n")
            elif cmd.startswith("QUIT"):
                conn.sendall(b"221 bye\r\n")
                return
            else:
                conn.sendall(b"250 ok\r\n")

    def close(self):
        for srv in self.socks:
            try:
                srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            srv.close()


def s_mail(work):
    """Mail works inside its sandbox: fetches and sends through the compositor's
    connections, verifies certificates, keeps accounts and messages, and says when
    it can't reach a server."""
    problems = []
    os.environ["FBX_TEST_CONNECT_PORTS"] = f"{FakeMailServer.IMAP_PORT},{FakeMailServer.IMAPS_PORT},{FakeMailServer.SMTP_PORT}"
    cert = os.path.join(work, "mail-cert.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost", "-keyout", cert, "-out", cert],
                   check=True, capture_output=True)

    def seed(s, imap_port, ssl_on=False):
        store = os.path.join(s.home, ".freebsdx-mail")
        os.makedirs(store, exist_ok=True)
        with open(os.path.join(store, "accounts"), "w") as f:
            f.write("\t".join(["me", "Me", "me@example.com", "localhost", str(imap_port), "1" if ssl_on else "0",
                               "localhost", str(FakeMailServer.SMTP_PORT), "0", "me", "secret"]) + "\n")
        return store

    def status(d):
        return " | ".join(d.texts())

    def wait_text(d, want, what):
        try:
            wait_for(lambda: want in status(d), 10, what)
        except Failed:
            problems.append(f"{what}: {status(d)[:300]}")

    try:
        with Session(work, "mail") as s:
            s.shell()
            server = FakeMailServer(cert)
            store = seed(s, FakeMailServer.IMAP_PORT)

            # Fetching: it starts with a sync.
            d = AppDriver(s, "mail")
            wait_text(d, "안녕 Hello from IMAP", "a message from the server to be listed, its subject decoded")
            wait_text(d, "1 messages", "the count")
            if not any("me" in login and "secret" in login for login in server.logins):
                problems.append(f"the server saw no login: {server.logins}")
            cached = os.path.join(store, "me", "INBOX", "7.eml")
            if not os.path.exists(cached):
                problems.append("the message wasn't kept on disk")
            d.close()

            # With the server gone: what it kept is there, and it says why it's stale.
            server.close()
            d = AppDriver(s, "mail")
            wait_text(d, "Sync failed", "a failed sync to say so")
            if "Hello from IMAP" not in status(d):
                problems.append(f"a failed sync hid the kept messages: {status(d)[:300]}")
            d.close()

            # Sending, with an attachment picked in the sandbox.
            server = FakeMailServer(cert)
            with open(os.path.join(s.home, "att.bin"), "wb") as f:
                f.write(b"attached bytes!")     # 15 bytes: base64 has no padding...
            with open(os.path.join(s.home, "att2.bin"), "wb") as f:
                f.write(b"attached bytes")      # ...and 14 has "="
            d = AppDriver(s, "mail", "--open", "mailto:bob@example.com")
            wait_text(d, "Hello from IMAP", "the sync behind the compose form")
            for _ in range(8):           # the buttons, To, CC, to Subject
                d.tab()
            d.type("Greetings")
            d.tab()                      # Attach…
            for index in (0, 1):
                d.enter()
                time.sleep(1.2 * TIME_SCALE)
                for _ in range(index + 1):
                    d.tab()
                d.enter()
                d.enter()
                time.sleep(1.2 * TIME_SCALE)
            if "2 file(s)" not in d.texts():
                problems.append(f"attachments weren't taken: {status(d)[:300]}")
            d.tab()                      # the message
            d.type("Hi Bob")
            for _ in range(5):
                d.tab(shift=True)        # back to Send
            d.enter()
            wait_text(d, "Sent!", "sending to report success")
            mail = "".join(server.sent)
            for want in ("To: bob@example.com", "Subject: Greetings", "Hi Bob",
                         'filename="att.bin"', "YXR0YWNoZWQgYnl0ZXMh", 'filename="att2.bin"', "YXR0YWNoZWQgYnl0ZXM="):
                if want not in mail:
                    problems.append(f"the server didn't get {want!r}: {mail[:400]!r}")
            if not [n for n in os.listdir(os.path.join(store, "me", "Sent")) if n.endswith(".eml")]:
                problems.append("the sent message wasn't kept")
            d.close()
            server.close()

            # Certificates: a server this machine trusts, then one it doesn't.
            server = FakeMailServer(cert)
            shutil.rmtree(os.path.join(store, "me"))
            seed(s, FakeMailServer.IMAPS_PORT, ssl_on=True)
            os.environ["CURL_CA_BUNDLE"] = cert
            d = AppDriver(s, "mail")
            wait_text(d, "Hello from IMAP", "fetching over TLS from a trusted server")
            d.close()
            del os.environ["CURL_CA_BUNDLE"]
            d = AppDriver(s, "mail")
            wait_text(d, "Sync failed", "refusing a server with an untrusted certificate")
            d.close()
            server.close()

            # A port that isn't for mail: refused by the compositor, and it says so.
            seed(s, 8080)
            d = AppDriver(s, "mail")
            wait_text(d, "that port isn't allowed", "a port outside the mail ports to be refused")
            d.close()
            denied = [l for l in s.read_log("compositor").splitlines() if "DENIED: com.freebsdx.mail connecting" in l]
            if not denied:
                problems.append("the compositor logged no refusal")

            # Only Mail may ask at all.
            r = call("com.freebsdx.Fetch", 2, struct.pack("<HH252s", FakeMailServer.IMAP_PORT, 0, b"localhost"))
            if r is None or struct.unpack("<i", r[-256:-252])[0] != 1:
                problems.append("a client that isn't Mail wasn't refused a connection")

            # Set up by hand: written down, for next time, privately.
            shutil.rmtree(store)
            d = AppDriver(s, "mail")
            for _ in range(3):           # Compose, Sync, + Add Account
                d.tab()
            d.enter()
            time.sleep(0.8 * TIME_SCALE)
            for value in ("Me", "typed@example.com", "imap.example.com", "993", "smtp.example.com", "587", "typed", "pw"):
                d.tab()
                d.type(value)
            d.tab()                      # Cancel
            d.tab()                      # Add Account
            d.enter()
            time.sleep(1.0 * TIME_SCALE)
            accounts = os.path.join(store, "accounts")
            if not os.path.exists(accounts) or "typed@example.com" not in open(accounts).read():
                problems.append(f"the account wasn't saved: {status(d)[:300]}")
            elif os.stat(accounts).st_mode & 0o077:
                problems.append("the account file (it holds the password) is readable by others")
            d.close()
            d = AppDriver(s, "mail")
            if "typed@example.com" not in status(d):
                problems.append(f"the account was gone next time: {status(d)[:300]}")
            d.close()
    finally:
        os.environ.pop("FBX_TEST_CONNECT_PORTS", None)

    check(not problems, "; ".join(problems))


def s_app_paths(work):
    """A client that isn't the built-in app concerned can't make the compositor
    read, write or reveal paths for it, and doesn't hear which files others open."""
    import tarfile
    Q = lambda p, n: p.encode().ljust(n, b"\0")
    problems = []
    with Session(work, "app-paths") as s:
        s.shell()
        home = s.home
        ssh = os.path.join(home, ".ssh")
        os.makedirs(ssh, exist_ok=True)
        key = os.path.join(ssh, "id_rsa")
        with open(key, "w") as f:
            f.write("CANARY-PRIVATE-KEY\n")
        victim = os.path.join(home, "thesis.txt")
        with open(victim, "w") as f:
            f.write("my work\n")
        ics = os.path.join(home, "planted.ics")
        with open(ics, "w") as f:
            f.write("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Planted\r\n"
                    "DTSTART:20270101T090000\r\nDTEND:20270101T100000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        vcf = os.path.join(home, "planted.vcf")
        with open(vcf, "w") as f:
            f.write("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Planted Person\r\nN:Person;Planted;;;\r\nEND:VCARD\r\n")
        tar = os.path.join(home, "planted.tar")
        with tarfile.open(tar, "w") as t:
            t.add(key, arcname="dropped.txt")
        png = os.path.join(home, "planted.png")
        write_png(png, 8, 8, (1, 2, 3))

        def refused(label, service, op, payload, did=None):
            """No answer, and whatever the call would have done didn't happen."""
            r = call(service, op, payload)
            time.sleep(0.3)
            happened = did() if did else False
            if r is not None or happened:
                problems.append(f"{label}: {'answered' if r is not None else ''}{' and did it' if happened else ''}")

        # Reading files by name
        refused("Portal.OpenFile in home", "com.freebsdx.Portal", 0x01, Q(key, 252))
        refused("Portal.OpenFile via /tmp/..", "com.freebsdx.Portal", 0x01, Q("/tmp/.." + key, 252))
        refused("Portal.ListDir", "com.freebsdx.Portal", 0x05, Q(ssh, 252))
        refused("Portal.ZfsSnapshot", "com.freebsdx.Portal", 0x07, Q(home, 252))
        refused("Portal.ZfsRollback", "com.freebsdx.Portal", 0x06, Q(home + "/.zfs/snapshot/x", 252))
        # Archives
        out = os.path.join(home, "stolen.tar")
        refused("Archive.Create", "com.freebsdx.Archive", 0x03, Q(out, 128) + Q(ssh, 128),
                lambda: os.path.exists(out))
        dest = os.path.join(home, "dest")
        os.makedirs(dest, exist_ok=True)
        refused("Archive.Extract", "com.freebsdx.Archive", 0x02, Q(tar, 128) + Q(dest, 128),
                lambda: os.listdir(dest))
        refused("Archive.List", "com.freebsdx.Archive", 0x01, b"\0" * 8 + Q(tar, 248))
        # The screen, and overwriting a file with it
        before = open(victim, "rb").read()
        refused("Screenshot.Capture over a file", "com.freebsdx.Screenshot", 0x01,
                b"\0" * 4 + Q(victim, 220) + b"\0" * 32, lambda: open(victim, "rb").read() != before)
        refused("Screenshot.CaptureRegion", "com.freebsdx.Screenshot", 0x02,
                struct.pack("<IiiII", 0, 0, 0, 16, 16) + Q(os.path.join(home, "r.png"), 200) + b"\0" * 28,
                lambda: os.path.exists(os.path.join(home, "r.png")))
        # Imports the compositor reads for the caller
        refused("Calendar.ImportIcs", "com.freebsdx.Calendar", 0x06, Q(ics, 252))
        refused("Contacts.ImportVCard", "com.freebsdx.Contacts", 0x07, Q(vcf, 256))
        # Wallpaper: the compositor decodes whatever file it's given
        got = lambda: Q(png, 252) in (call("com.freebsdx.Wallpaper", 0x02) or b"")
        refused("Wallpaper.Set", "com.freebsdx.Wallpaper", 0x01, Q(png, 252) + b"\0" * 4, got)
        refused("Wallpaper.SetWorkspace", "com.freebsdx.Wallpaper", 0x03,
                struct.pack("<I", 0) + struct.pack("<I", 0) + Q(png, 248), got)
        # Sizes of any directory
        refused("DiskUsage.Scan", "com.freebsdx.DiskUsage", 0x01, Q(ssh, 64) + b"\0" * 192)
        # Printing a file by name (no app uses this)
        refused("Print.PrintFile", "com.freebsdx.Print", 0x02, Q("", 64) + Q(key, 128) + Q("x", 48) + struct.pack("<IIII", 1, 0, 0, 0))

        # Which file someone opens is private: only the shell and that app hear it.
        spy = connect()
        spy.settimeout(0.3)
        launch_with_file("com.freebsdx.imageviewer", png)
        heard = b""
        end = time.time() + 1.5
        while time.time() < end:
            try:
                heard += spy.recv(4096)
            except socket.timeout:
                pass
        spy.close()
        if png.encode() in heard:
            problems.append("LaunchWithFile: another app heard the opened file's path")
        # ...and the shell still acted on it.
        wait_for(lambda: "planted.png" in s.read_log("compositor"), 15, "the shell to open the file")

        # Reads apps rely on stay open.
        if call("com.freebsdx.Wallpaper", 0x02) is None:
            problems.append("reading the wallpaper was refused")

        # The apps these belong to still work.
        files = AppDriver(s, "files")
        wait_for(lambda: any("thesis.txt" in x for x in files.texts()), 10, "Files to list the home folder")
        files.close()
        arch = AppDriver(s, "archive", "--open", tar)
        wait_for(lambda: any("dropped.txt" in x for x in arch.texts()), 10, "Archive to list its file")
        arch.close()
        # (only that app may ask, so this is where a failed list's reply is checked)
        arch = AppDriver(s, "archive", "--open", "/nonexistent.zip")
        wait_for(lambda: any("Cannot open archive" in x for x in arch.texts()), 10, "Archive to report a missing file")
        arch.close()
        people = AppDriver(s, "contacts", "--open", vcf)
        wait_for(lambda: any("Planted" in x for x in people.texts()), 10, "Contacts to import its file")
        people.close()

        # Calendar imports the file the user picks, by descriptor. Tab reaches the
        # toolbar's six buttons, then Import; the picker lists folders, then files.
        names = [n for n in os.listdir(home) if not n.startswith(".")]
        listed = sorted(n for n in names if os.path.isdir(os.path.join(home, n))) + \
                 sorted(n for n in names if not os.path.isdir(os.path.join(home, n)))
        cal = AppDriver(s, "calendar")
        for _ in range(7):
            cal.tab()
        cal.enter()
        time.sleep(1.2 * TIME_SCALE)
        for _ in range(listed.index("planted.ics") + 1):
            cal.tab()
        cal.enter()
        cal.enter()
        wait_for(lambda: any("Imported 1 event" in x for x in cal.texts()), 10, "Calendar to import the picked file")
        cal.close()

        # The app a file is meant for still hears it: Mail, composing from a link.
        mail = AppDriver(s, "mail")
        launch_with_file("com.freebsdx.mail", "mailto:someone@example.org")
        wait_for(lambda: any("someone@example.org" in x for x in mail.texts()), 10, "a running Mail to hear its link")
        mail.close()
    check(not problems, "app-paths:\n  " + "\n  ".join(problems))


def window_top(ppm, x, below_y):
    """The row of a window's title bar: the dark band at the very top of its column."""
    parts = open(ppm, "rb").read().split(b"\n", 3)
    w = int(parts[1].split()[0])
    y = below_y
    while y > 0:
        r, g, b = parts[3][(y * w + x + 40) * 3:(y * w + x + 40) * 3 + 3]
        if r < 60 and g < 60 and b < 70 and abs(int(b) - int(r)) < 12:   # the title bar's grey
            return y
        y -= 1
    return below_y


def s_browser(work):
    """The Browser opens pages in our own engine, and hands one to Chromium when asked.
    Chromium is real, through the Wayland bridge on this compositor: it shows a page,
    the page gets the mouse and keyboard and keeps drawing, it fits the screen, and it
    asks only for what it was told to open."""
    engine = "/usr/local/bin/chrome"
    if not os.access(engine, os.X_OK):
        print("  (chromium isn't installed: only the missing-engine message is checked)")
    RED, GREEN, BLUE = (220, 40, 40), (40, 200, 40), (40, 40, 220)
    with Session(work, "browser", browser_engine=engine) as s:
        s.shell()
        # No engine: it says so, in a window.
        none = AppDriver(s, "browser", "chromium:/nonexistent.html", env={"FBX_BROWSER_ENGINE": "/nonexistent"})
        wait_for(lambda: any("isn't installed" in x for x in none.texts()), 10, "the missing-engine message")
        none.close()
        if not os.access(engine, os.X_OK):
            return

        page = os.path.join(s.home, "page.html")
        with open(page, "w") as f:
            f.write("""<!doctype html><title>Colour test</title>
<body style="margin:0;height:100vh;background:rgb(220,40,40)"><script>
addEventListener("mousedown", () => document.body.style.background = "rgb(40,200,40)");
addEventListener("keydown",   () => document.body.style.background = "rgb(40,40,220)");
</script>""")
        launch_with_file("com.freebsdx.browser", "chromium:" + page)
        wait_for(lambda: on_screen(s, RED, "page") > 5000, 60, "the page's pixels on screen")
        # The clicks below aim by this capture, so it has to show the window at rest,
        # not still animating open.
        size = [0]
        def settled():
            now = on_screen(s, RED, "page")
            same = now == size[0]
            size[0] = now
            return same
        wait_for(settled, 10, "the window to finish opening")

        # Frame timing goes to the bridge alone: anyone else would be woken every
        # frame, and could watch when other apps draw. (28 bytes is PresentFeedback.)
        spy = connect()
        spy.settimeout(0.2)
        heard, end = 0, time.time() + 1.5
        while time.time() < end:
            try:
                heard += len(spy.recv(4096)) == 28
            except socket.timeout:
                pass
        spy.close()
        check(not heard, f"an ordinary client was sent {heard} frame timings")

        # It fits: nothing of the window is lost below the screen's bottom edge.
        ppm = os.path.join(s.logs, "page.ppm")
        parts = open(ppm, "rb").read().split(b"\n", 3)
        w, h = map(int, parts[1].split())
        red_rows = [y for y in range(h) if any(
            abs(parts[3][(y * w + x) * 3] - 220) < 12 and abs(parts[3][(y * w + x) * 3 + 1] - 40) < 12
            for x in range(0, w, 16))]
        check(red_rows, "no page rows found")
        check(max(red_rows) < h - 40, f"the window runs under the taskbar (page reaches row {max(red_rows)} of {h})")
        cols = [x for x in range(w) if abs(parts[3][(red_rows[0] * w + x) * 3] - 220) < 12]
        mid_x, mid_y = (cols[0] + cols[-1]) // 2, (red_rows[0] + red_rows[-1]) // 2

        # The mouse and the keyboard reach the page, and it redraws for them.
        mouse = Pointer()
        mouse.click(mid_x, mid_y)
        mouse.close()
        wait_for(lambda: on_screen(s, GREEN, "clicked") > 5000, 15, "a click to reach the page")
        c = connect()
        for state in (1, 0):
            c.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, struct.pack(INPUT_FMT, 2, 30, state, 0, ord("a"))))
            time.sleep(0.1)
        c.close()
        wait_for(lambda: on_screen(s, BLUE, "typed") > 5000, 15, "a key to reach the page")

        # Its menu opens at its button, not across the window, and closing it (a popup
        # going away, which used to kill the bridge and every window with it) is safe.
        def blue_at(tag, x, y):
            on_screen(s, BLUE, tag)
            p = open(os.path.join(s.logs, tag + ".ppm"), "rb").read().split(b"\n", 3)
            r, g, b = p[3][(y * w + x) * 3:(y * w + x) * 3 + 3]
            return abs(r - 40) < 12 and abs(g - 40) < 12 and abs(b - 220) < 12
        left, right = (cols[0] + 60, red_rows[0] + 200), (cols[-1] - 60, red_rows[0] + 200)
        check(blue_at("before-menu", *right), "the page isn't where it was before the menu")
        mouse = Pointer()
        mouse.click(cols[-1] - 24, red_rows[0] - 26)      # the ⋮ button, at the toolbar's right end
        mouse.close()
        wait_for(lambda: not blue_at("menu", *right), 10, "the menu to open at its button")
        check(blue_at("menu", *left), "the menu opened at the window's left edge, away from its button")
        keys = connect()
        def press(code, ch=0, mods=0):
            for state in (1, 0):
                keys.send(xbus_msg("com.freebsdx.Headless", HEADLESS_INJECT, struct.pack(INPUT_FMT, 2, code, state, mods, ch)))
                time.sleep(0.06)
        press(1, 27)                                      # Esc
        wait_for(lambda: blue_at("menu-closed", *right), 10, "the menu to close")
        # (A dead bridge takes the windows with it, and the steps below need them.)
        check(blue_at("menu-closed", *left), "closing the menu took the window away")

        # An item in the menu works: "New tab" covers the page, and Ctrl+W brings it back.
        mouse = Pointer()
        mouse.click(cols[-1] - 24, red_rows[0] - 26)
        wait_for(lambda: not blue_at("menu-again", *right), 10, "the menu to open again")
        mouse.click(cols[-1] - 300, red_rows[0] + 22)
        mouse.close()
        wait_for(lambda: on_screen(s, BLUE, "new-tab") < 1000, 10, "the menu's New tab to open one")
        check(on_screen(s, GREEN, "new-tab") < 1000, "the menu had closed: its item's click went through to the page")
        press(17, ord("w"), 2)
        wait_for(lambda: on_screen(s, BLUE, "tab-closed") > 5000, 10, "Ctrl+W to close the new tab")

        # A click lands exactly under the pointer. Chromium's buffer includes its shadow,
        # and positions must count from there: every click was once 16 pixels to the left.
        coords = os.path.join(s.home, "coords.html")
        with open(coords, "w") as f:
            f.write("""<!doctype html><title>Coordinates</title><body style="margin:0;height:100vh;background:rgb(1,2,3)"><script>
addEventListener("mousedown", e => document.body.style.background =
    `rgb(${e.clientX >> 8},${e.clientX & 255},${e.clientY & 255})`);
</script>""")
        launch_with_file("com.freebsdx.browser", "chromium:" + coords)
        wait_for(lambda: on_screen(s, (1, 2, 3), "coords") > 5000, 30, "the coordinates page")
        at_x, at_y = cols[0] + 300, red_rows[0] + 150
        mouse = Pointer()
        mouse.click(at_x, at_y)
        mouse.close()
        expected = ((300 >> 8), 300 & 255, 150)
        wait_for(lambda: on_screen(s, expected, "coords-hit") > 5000, 10,
                 f"a click to report exactly {expected}")
        press(17, ord("w"), 2)                            # back to the page before it

        second = os.path.join(s.home, "second.html")
        with open(second, "w") as f:
            f.write("""<!doctype html><title>Second page</title><body style="margin:0;height:100vh;background:rgb(200,40,200)">
<input autofocus oninput="if (this.value == 'abc') document.body.style.background = 'rgb(40,200,200)'">
<script>addEventListener("keydown", e => { if (e.ctrlKey && e.key == "b") document.body.style.background = "rgb(230,180,30)"; });</script>""")
        launch_with_file("com.freebsdx.browser", "chromium:" + second)
        wait_for(lambda: on_screen(s, (200, 40, 200), "second") > 5000, 30, "the second page")
        time.sleep(1)
        for code, ch in ((30, "a"), (48, "b"), (46, "c")):
            press(code, ord(ch))
        wait_for(lambda: on_screen(s, (40, 200, 200), "typed-text") > 5000, 10, "typed text to reach a field")
        press(48, ord("b"), 2)                            # Ctrl+B: read with its modifier, not the next key's
        wait_for(lambda: on_screen(s, (230, 180, 30), "shortcut") > 5000, 10, "a Ctrl shortcut to reach the page")
        keys.close()
        check(sum(1 for line in subprocess.run(["pgrep", "-f", "type=renderer"], capture_output=True, text=True).stdout.splitlines()) >= 1,
              "no page process running")

        # The window's close button closes it: the browser goes away.
        chrome = lambda: subprocess.run(["pgrep", "-f", "ozone-platform=wayland"], capture_output=True, text=True).stdout.split()
        check(chrome(), "the browser isn't running before closing")
        mouse = Pointer()
        mouse.click(cols[0] + 13, window_top(ppm, cols[0], red_rows[0]))
        mouse.close()
        wait_for(lambda: not chrome(), 20, "the close button to end the browser")
        wait_for(lambda: on_screen(s, (230, 180, 30), "closed") < 100, 5, "the closed window to leave the screen")

        # A page opens in our engine, not Chromium; its button hands the page over.
        ORANGE = (230, 120, 20)
        ours = os.path.join(s.home, "ours.html")
        with open(ours, "w") as f:
            f.write('<!doctype html><title>Ours</title><body style="margin:0;height:100vh;background:rgb(230,120,20)">')
        # Sandboxed, like the other built-in apps (it asks the shell for Chromium; it can't start it).
        direct = AppDriver(s, "browser", ours)
        wait_for(lambda: on_screen(s, ORANGE, "direct") > 5000, 30, "the page in our engine")
        check("Capsicum sandbox active" in s.read_log("browser"), "the Browser isn't sandboxed")
        direct.close()
        wait_for(lambda: on_screen(s, ORANGE, "direct-gone") < 100, 10, "that window to close")
        launch_with_file("com.freebsdx.browser", ours)
        wait_for(lambda: on_screen(s, ORANGE, "ours") > 5000, 30, "the page in our engine")
        check(not chrome(), "Chromium started for a page our engine can show")
        check(procs_running(os.path.join(OUT, "browser")) == 1, "the Browser isn't what's showing the page")
        o = open(os.path.join(s.logs, "ours.ppm"), "rb").read().split(b"\n", 3)
        ow, oh = map(int, o[1].split())
        near = lambda x, y: all(abs(o[3][(y * ow + x) * 3 + i] - ORANGE[i]) < 12 for i in range(3))
        rows = [y for y in range(0, oh, 2) if any(near(x, y) for x in range(0, ow, 16))]
        top = min(rows)
        xs = [x for x in range(ow) if near(x, top + 4)]
        mouse = Pointer()
        mouse.click(max(xs) - 6 - 68, top - 22)            # "Open in Chromium", at the toolbar's right end
        mouse.close()
        wait_for(lambda: chrome(), 60, "Open in Chromium to start Chromium")
        # Ours closed, the page still shows: Chromium has it.
        subprocess.run(["pkill", "-f", "(^|/)out/browser( |$)"])
        wait_for(lambda: procs_running(os.path.join(OUT, "browser")) == 0, 10, "our engine's window to close")
        wait_for(lambda: on_screen(s, ORANGE, "in-chromium") > 5000, 60, "the page in Chromium")
        for pid in chrome():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        wait_for(lambda: not chrome(), 20, "Chromium to end")

    # The compositor is gone, so the bridge has nothing to serve. (It used to stay,
    # using a whole CPU core, until someone noticed.)
    wait_for(lambda: subprocess.run(["pgrep", "-x", "xwayland-bridge"], capture_output=True).returncode != 0,
             5, "the Wayland bridge to exit with the compositor")


def s_persistence(work):
    """What you save is still there: Text Editor saves and opens through the
    powerbox (the user picks in the shell, the app gets one descriptor; apps can
    neither answer the picker nor see what was picked), and notes and contacts
    survive the compositor restarting."""
    problems = []

    def ctrl(d, code):
        d.key(code, ctrl=True)
        time.sleep(1.2 * TIME_SCALE)

    def body(path):
        with open(path) as f:
            return f.read()

    def wait_body(path, want, what):
        try:
            wait_for(lambda: os.path.exists(path) and body(path) == want, 6, what)
        except Failed:
            problems.append(f"{what}: file holds {body(path) if os.path.exists(path) else None!r}, not {want!r}")

    # After Ctrl+O the picker is focused on Back: Tab * (index + 1) reaches a file,
    # Enter selects it, Enter again opens it.
    def pick(d, index):
        for _ in range(index + 1):
            d.tab()
        d.enter()
        d.enter()
        time.sleep(1.0 * TIME_SCALE)

    with Session(work, "persistence") as s:
        s.shell()
        watcher = Watcher()
        note = os.path.join(s.home, "note.txt")

        ed = AppDriver(s, "texteditor")
        ed.type("first draft")
        ctrl(ed, 31)                                  # Ctrl+S: no file yet, so Save As
        ed.type("note.txt")
        ed.enter()
        wait_body(note, "first draft", "save as")
        t = ed.texts()
        if "Saved" not in t or "note.txt" not in t:
            problems.append(f"editor: no 'Saved' / name after saving ({t[:6]})")
        ed.type(" more")
        ctrl(ed, 31)                                  # Ctrl+S again: no second asking
        wait_body(note, "first draft more", "save")
        ctrl(ed, 31)
        ed.close()

        # A file that gets shorter must lose its old tail; the fixtures for the
        # cases below (sorted: fifo, note.txt, zlocked.txt).
        locked = os.path.join(s.home, "zlocked.txt")
        with open(locked, "w") as f:
            f.write("locked text")
        os.chmod(locked, 0o444)
        os.mkfifo(os.path.join(s.home, "fifo"))

        ed = AppDriver(s, "texteditor")
        ctrl(ed, 24)                                  # Ctrl+O
        pick(ed, 1)
        t = ed.texts()
        if "first draft more" not in t or "note.txt" not in t:
            problems.append(f"editor: opening note.txt shows {t[:6]}")
        ed.type("!")
        ctrl(ed, 31)
        wait_body(note, "first draft more!", "save to an opened file")

        ctrl(ed, 24)                                  # Esc cancels: nothing changes
        ed.key(1)
        time.sleep(0.8 * TIME_SCALE)
        if "first draft more!" not in ed.texts():
            problems.append("editor: cancelling the picker changed the document")

        ctrl(ed, 24)                                  # a FIFO: refused, no hang
        pick(ed, 0)
        t = ed.texts()
        if not any("Not a regular file" in x for x in t):
            problems.append(f"editor: opening a FIFO said {t[:6]}")

        # An app can't answer the picker for the user.
        ctrl(ed, 24)
        forger = connect()
        for rid in range(1, 16):
            payload = struct.pack("<4I", 5, rid, 0, 1) + b"/etc/passwd".ljust(240, b"\0")
            forger.send(xbus_msg("com.freebsdx.FileChooser", 5, payload))
        time.sleep(0.5 * TIME_SCALE)
        forger.close()
        if "root:" in " ".join(ed.texts()):
            problems.append("editor: a forged Respond handed over /etc/passwd")
        if "DENIED: com.freebsdx.FileChooser" not in s.read_log("compositor"):
            problems.append("a forged Respond wasn't refused")
        ed.key(1)
        time.sleep(0.8 * TIME_SCALE)

        # Saving over another file takes a second press.
        ctrl(ed, 24)
        pick(ed, 1)                                   # back to note.txt
        ed.key(31, shift=True, ctrl=True)
        time.sleep(1.2 * TIME_SCALE)
        other = os.path.join(s.home, "other.txt")
        with open(other, "w") as f:
            f.write("keep me")
        ed.type("other.txt")
        ed.enter()
        time.sleep(1.0 * TIME_SCALE)
        if body(other) != "keep me":
            problems.append("save as: replaced an existing file without asking")
        ed.enter()
        wait_body(other, "first draft more!", "save as, replace confirmed")
        ed.close()

        # A read-only file opens, says so, and saving asks where.
        ed = AppDriver(s, "texteditor")
        ctrl(ed, 24)
        pick(ed, 3)                                   # fifo, note, other, zlocked
        t = ed.texts()
        if "locked text" not in t or not any("read-only" in x for x in t):
            problems.append(f"editor: a read-only file shows {t[:6]}")
        ctrl(ed, 31)                                  # opens the picker instead
        ed.key(1)
        time.sleep(0.8 * TIME_SCALE)
        if body(locked) != "locked text":
            problems.append("a read-only file was changed")
        ed.close()

        time.sleep(0.5 * TIME_SCALE)
        leaked = watcher.saw("com.freebsdx.FileChooser")
        if leaked:
            problems.append(f"another app saw {len(leaked)} file chooser message(s)")
        watcher.close()

        notes = AppDriver(s, "notes")
        notes.tab(); notes.tab(); notes.enter()       # "+ New Note": the title has focus
        notes.type("Buy milk")
        notes.tab()                                   # the body
        notes.type("abc")
        t = notes.texts()
        if "3 / 163 chars" not in t:
            problems.append(f"notes: the counter doesn't follow typing ({t})")
        notes.tab(shift=True); notes.tab(shift=True)  # title, then Save
        notes.enter()
        time.sleep(1.0 * TIME_SCALE)
        t = notes.texts()
        if t.count("Buy milk") < 1:
            problems.append(f"notes: the saved note isn't listed ({t[:8]})")
        notes.close()

        contacts = AppDriver(s, "contacts")
        contacts.tab(); contacts.tab(); contacts.enter()
        contacts.type("Ada")
        for _ in range(6):                            # the other fields, then Save
            contacts.tab()
        contacts.enter()
        time.sleep(1.0 * TIME_SCALE)
        if "Ada" not in " ".join(contacts.texts()):
            problems.append("contacts: the saved contact isn't shown")
        contacts.close()

    with Session(work, "persistence") as s:           # the compositor restarted
        notes = AppDriver(s, "notes")
        t = notes.texts()
        rows = [x for x in t if x == "Buy milk"]
        if len(rows) != 1:
            problems.append(f"notes after restart: 'Buy milk' listed {len(rows)} times ({t[:8]})")
        notes.close()
        contacts = AppDriver(s, "contacts")
        t = contacts.texts()
        if not any("Ada" in x for x in t):
            problems.append(f"contacts after restart: 'Ada' is gone ({t[:8]})")
        contacts.close()
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
                said = (r.stdout + r.stderr).decode(errors="replace").strip().splitlines()[-3:]
                failed.append(f"{t} (exit {r.returncode}): {' / '.join(said)}")
    check(not failed, "failed: " + ", ".join(failed))


SCENARIOS = {
    "file-open": s_file_open,
    "media": s_media,
    "mail": s_mail,
    "sandbox-audit": s_sandbox_audit,
    "app-paths": s_app_paths,
    "browser": s_browser,
    "webview": s_webview,
    "webnav": s_webnav,
    "webfont": s_webfont,
    "webpaint": s_webpaint,
    "replies": s_replies,
    "window-chrome": s_window_chrome,
    "persistence": s_persistence,
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
    "app-tasks": s_app_tasks,
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
                    help="run the out-asan/ (ASan+UBSan) binaries; any sanitizer report fails the scenario. "
                         "Without named scenarios it runs the sanitizer set (ASAN_DEFAULT, ~5 min), not all")
    ap.add_argument("--all", action="store_true", help="with --asan: every scenario (~14 min)")
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
    names = args.scenarios or (ASAN_DEFAULT if args.asan and not args.all else list(SCENARIOS))
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
            where = traceback.extract_tb(e.__traceback__)[-1]
            print(f"  ERROR {name:18} {type(e).__name__}: {e} (line {where.lineno})")
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
