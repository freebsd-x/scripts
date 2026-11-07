#!/usr/bin/env python3
"""Compare xweb's painting with Chromium's, pixel by pixel, text hidden on both sides.

  python3 -I scripts/xweb-pixels.py [--width N] [--height N] [--tolerance N] [--diff DIR] page.html...

xweb_paint draws the first screenful on the CPU without glyphs; Chromium takes a
screenshot of the same page with glyphs made transparent. A pixel matches when no
channel differs by more than --tolerance. Exit status is 1 if a page matches less than
--min percent. Needs a chrome install, so it isn't part of the unit suite.
"""
import argparse, functools, http.server, os, re, shutil, struct, subprocess, sys, tempfile, threading, zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "out", "sdk", "xweb", "xweb_paint")
# The same for both: no glyphs and no text decorations, which are drawn with them.
HIDE_TEXT = ("<style>*,*::before,*::after{-webkit-text-fill-color:transparent!important;"
             "text-decoration:none!important;text-shadow:none!important;caret-color:transparent!important}</style>")


def read_png(path):
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG: " + path)
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or ctype not in (2, 6) or interlace:
                raise ValueError("unsupported PNG layout in " + path)
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    out = []
    for line in rows:
        out.append([tuple(line[i:i + 3]) for i in range(0, stride, bpp)])
    return w, h, out


def write_png(path, w, h, rows):
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def with_hidden_text(page, work, base_tag):
    src = open(page, "rb").read().decode("utf-8", "replace")
    src = re.sub(r"<script\b.*?</script\s*>", "", src, flags=re.S | re.I)
    inject = (base_tag if base_tag else "") + HIDE_TEXT
    src, n = re.subn(r"(<head\b[^>]*>)", lambda m: m.group(1) + inject, src, count=1, flags=re.I)
    if not n:
        src = inject + src
    return src


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def chromium_shot(page, width, height, work):
    # Over HTTP from the page's own folder: masks, like fonts, aren't drawn from file:.
    folder = os.path.dirname(os.path.abspath(page))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=folder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return chromium_shot_from(page, width, height, work, folder, server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def chromium_shot_from(page, width, height, work, folder, port):
    name = ".xweb-pixels-chrome-" + os.path.basename(page)
    probed = os.path.join(folder, name)
    with open(probed, "w") as f:
        f.write(with_hidden_text(page, work, None))
    shot = os.path.join(work, "chrome.png")
    chrome = shutil.which("chrome") or shutil.which("chromium")
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                    f"--user-data-dir={work}/profile", "--disable-background-networking", "--force-device-scale-factor=1",
                    f"--window-size={width},{height}", "--virtual-time-budget=2000", f"--screenshot={shot}",
                    f"http://127.0.0.1:{port}/{name}"], capture_output=True, timeout=90)
    os.unlink(probed)
    if not os.path.exists(shot):
        raise RuntimeError("Chromium took no screenshot")
    return read_png(shot)


def our_shot(page, width, height, work):
    # Next to the page, so its relative URLs still work.
    ours_html = os.path.join(os.path.dirname(os.path.abspath(page)), ".xweb-pixels-" + os.path.basename(page))
    with open(ours_html, "w") as f:
        f.write(with_hidden_text(page, work, None))
    shot = os.path.join(work, "ours.png")
    try:
        out = subprocess.run([TOOL, ours_html, str(width), str(height), shot], capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(ours_html)
    if out.returncode != 0:
        raise RuntimeError(f"xweb_paint exit {out.returncode}: {out.stderr[-300:]}")
    return read_png(shot)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="+")
    ap.add_argument("--width", type=int, default=1000)
    ap.add_argument("--height", type=int, default=600)
    ap.add_argument("--tolerance", type=int, default=24)
    ap.add_argument("--min", type=float, default=98.0, help="percent of pixels that must match")
    ap.add_argument("--diff", help="write ours / chromium / difference images here")
    args = ap.parse_args()
    if not os.access(TOOL, os.X_OK):
        sys.exit(f"build {TOOL} first (ninja -C out sdk/xweb/xweb_paint)")
    failed = 0
    for page in args.pages:
        with tempfile.TemporaryDirectory() as work:
            cw, ch, theirs = chromium_shot(page, args.width, args.height, work)
            ow, oh, ours = our_shot(page, args.width, args.height, work)
        w, h = min(cw, ow), min(ch, oh)
        same = 0
        diff_rows = []
        for y in range(h):
            row = []
            for x in range(w):
                a, b = ours[y][x], theirs[y][x]
                d = max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))
                if d <= args.tolerance:
                    same += 1
                    row.append(tuple(v // 4 + 190 for v in b))
                else:
                    row.append((255, 0, 0) if d > 96 else (255, 160, 0))
            diff_rows.append(row)
        pct = 100.0 * same / max(1, w * h)
        ok = pct >= args.min
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {os.path.basename(page)}: {pct:.2f}% of pixels within {args.tolerance} of Chromium")
        if args.diff:
            os.makedirs(args.diff, exist_ok=True)
            stem = os.path.splitext(os.path.basename(page))[0]
            write_png(os.path.join(args.diff, stem + "-diff.png"), w, h, diff_rows)
            write_png(os.path.join(args.diff, stem + "-ours.png"), w, h, [r[:w] for r in ours[:h]])
            write_png(os.path.join(args.diff, stem + "-chromium.png"), w, h, [r[:w] for r in theirs[:h]])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
