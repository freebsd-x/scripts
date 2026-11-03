#!/usr/bin/env python3
"""The compositor never runs a shell on its own command strings.

Every process it starts goes through the argv helpers in
core/xserver/include/xserver/async_shell.h (spawnArgvCapture / spawnArgvRun /
AsyncShellCache / runCapped / runDetached), so no argument — an SSID, a device,
a path, a printer name, a package — can ever be read as shell syntax. This
fails on any way back to a shell in core/xserver (src, include, daemons),
except the uses listed in ALLOWED, each of which is a shell by design.

    python3 scripts/lint-no-shell.py [repo-root]
"""
import os
import re
import sys

# (file, reason) — places where running a shell IS the feature.
ALLOWED = {
    "core/xserver/src/terminal_service.cc": "the terminal starts the user's login shell (/bin/sh as fallback)",
    "core/xserver/src/user_services.cc": "cron 'run now': a crontab entry is a shell command line, as cron runs it",
}

PATTERNS = [
    (re.compile(r'"/bin/sh"'), "/bin/sh"),
    (re.compile(r'\bpopen\s*\('), "popen()"),
    (re.compile(r'(?<![\w.>:])(?:std::|::)?system\s*\('), "system()"),   # not obj.system() / obj->system()
    (re.compile(r'"sh"\s*,\s*"-c"|"-c"\s*,\s*cmd'), 'sh -c'),
]


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
    problems = []
    for sub in ("core/xserver/src", "core/xserver/include", "core/xserver/daemons"):
        for dirpath, _, files in os.walk(os.path.join(root, sub)):
            for fn in files:
                if not fn.endswith((".cc", ".h")):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, root)
                if rel in ALLOWED:
                    continue
                for n, line in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
                    code = line.split("//", 1)[0]
                    for pat, what in PATTERNS:
                        if pat.search(code):
                            problems.append(f"{rel}:{n}: {what}: {line.strip()[:100]}")
    for p in problems:
        print(p)
    print(f"lint-no-shell: {len(problems)} shell use(s) outside the allowlist" if problems
          else "lint-no-shell: ok — no shell in the compositor's own commands")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
