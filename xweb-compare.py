#!/usr/bin/env python3
"""Compare xweb's layout with Chromium's, box by box.

  python3 -I scripts/xweb-compare.py [--width N] [--height N] [-v] page.html...

Each page is loaded in headless Chromium with a script that reports every element's
border box; xweb_layout prints ours. Elements are paired in document order by tag.
Exit status is 1 if any box differs by more than --tolerance pixels. Needs a chromium
install, so it isn't part of the unit suite; it's run on sdk/xweb/tests/pages.
"""
import argparse, html, json, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "out", "sdk", "xweb", "xweb_layout")
PROBE = """<script>addEventListener('load',()=>{const o=[];
for(const e of document.querySelectorAll('html,html *')){const s=getComputedStyle(e);
if(s.display==='none'||s.display==='inline'||s.display==='contents')continue;const n=e.tagName.toLowerCase();if(n==='script')continue;
if(!e.getClientRects().length)continue;const r=e.getBoundingClientRect();o.push([n,r.x,r.y,r.width,r.height])}
o.push(['#viewport',innerWidth,innerHeight,0,0]);const p=document.createElement('pre');p.id='__xweb';p.textContent=JSON.stringify(o);p.style.display='none';document.documentElement.appendChild(p)})</script>"""


def chromium_boxes(page, width, height, work):
    # Its stylesheets and images still load from where the page really is; its scripts
    # are dropped, since we don't run any.
    src = open(page, "rb").read().decode("utf-8", "replace")
    src = re.sub(r"<script\b.*?</script\s*>", "", src, flags=re.S | re.I)
    base = '<base href="file://%s/">' % os.path.dirname(os.path.abspath(page))
    src, n = re.subn(r"(<head\b[^>]*>)", lambda m: m.group(1) + base, src, count=1, flags=re.I)
    if not n:
        src, n = re.subn(r"(<!doctype[^>]*>)", lambda m: m.group(1) + base, src, count=1, flags=re.I)
        if not n:
            src = base + src
    probed = os.path.join(work, "probe.html")
    with open(probed, "w") as f:
        f.write(src + PROBE)
    chrome = shutil.which("chrome") or shutil.which("chromium")
    out = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                          f"--user-data-dir={work}/profile", "--disable-background-networking",
                          f"--window-size={width},{height}", "--virtual-time-budget=2000", "--dump-dom", "file://" + probed],
                         capture_output=True, text=True, errors="replace", timeout=90).stdout
    m = re.search(r'<pre id="__xweb"[^>]*>(.*?)</pre>', out, re.S)
    if not m:
        raise RuntimeError("Chromium produced no boxes")
    return [b for b in json.loads(html.unescape(m.group(1)))]


def our_boxes(page, width, height):
    out = subprocess.run([TOOL, page, str(width), str(height)], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"xweb_layout exit {out.returncode}: {out.stderr[-200:]}")
    boxes = []
    for line in out.stdout.splitlines():
        if not re.fullmatch(r"\S+( -?[\d.]+){4}", line):    # font library chatter
            continue
        tag, x, y, w, h = line.split()
        boxes.append([tag, float(x), float(y), float(w), float(h)])
    return boxes


def compare(ours, theirs, tolerance, verbose, name):
    # Both lists are in document order except that we paint positioned boxes last,
    # so pair by tag, nearest candidate first.
    remaining = list(ours)
    bad = 0
    for b in theirs:
        candidates = [o for o in remaining if o[0] == b[0]]
        if not candidates:
            bad += 1
            if verbose:
                print(f"  {name}: no box for <{b[0]}> {b[1:]}")
            continue
        best = min(candidates, key=lambda o: sum(abs(o[i] - b[i]) for i in range(1, 5)))
        first = candidates[0]
        pick = first if all(abs(first[i] - b[i]) <= tolerance for i in range(1, 5)) else best
        remaining.remove(pick)
        if any(abs(pick[i] - b[i]) > tolerance for i in range(1, 5)):
            bad += 1
            if verbose:
                print(f"  {name}: <{b[0]}> chromium {[round(v, 1) for v in b[1:]]} ours {[round(v, 1) for v in pick[1:]]}")
    return bad, len(theirs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--height", type=int, default=600)
    ap.add_argument("--tolerance", type=float, default=1.5)
    ap.add_argument("-v", action="store_true")
    ap.add_argument("pages", nargs="+")
    a = ap.parse_args()
    work = tempfile.mkdtemp(prefix="xweb-compare.")
    total = wrong = 0
    try:
        for page in a.pages:
            name = os.path.basename(page)
            try:
                theirs = chromium_boxes(page, a.width, a.height, work)
                viewport = theirs.pop()
                bad, n = compare(our_boxes(page, viewport[1], viewport[2]), theirs, a.tolerance, a.v, name)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                print(f"  ERROR {name}: {e}")
                wrong += 1
                continue
            total += n
            wrong += bad
            print(f"  {'ok  ' if bad == 0 else 'DIFF'} {name:24} {n - bad}/{n} boxes match")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"{total - wrong} of {total} boxes within {a.tolerance}px of Chromium")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
