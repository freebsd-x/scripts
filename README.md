# scripts

Helper scripts for the FreeBSD-X workspace.

Part of **FreeBSD-X**, a GUI platform for FreeBSD. The full workspace is a
multi-repo project assembled with the [`repo`](https://gerrit.googlesource.com/git-repo)
tool:

```sh
repo init -u https://github.com/freebsd-x/manifest.git
repo sync
```

## Testing

| Script | What it checks |
|---|---|
| `sanitize-tests.sh` | builds the xserver + xkit test binaries in `out-asan/` and runs them under ASan + UBSan |
| `headless-e2e.py` | end-to-end scenarios against a real headless compositor, shell and apps: file opening, the Screen Time card, the shell-trust and per-service gates, window ownership, sandboxed network fetches (Maps tiles through `com.freebsdx.Fetch`, offline from a seeded cache), every app starting cleanly, the layout audit (nothing laid out off-screen), and all `freebsdx-*-smoke` tools |

```sh
ninja -C out                                    # build first
python3 scripts/headless-e2e.py                 # every scenario (~4 min)
python3 scripts/headless-e2e.py --list          # scenario names
python3 scripts/headless-e2e.py gates times-up  # just these
python3 scripts/headless-e2e.py --shots /tmp/s  # + a capture of every app and a contact sheet
python3 scripts/headless-e2e.py --asan          # the sanitizer set on the out-asan/ (ASan+UBSan) build (~5 min)
python3 scripts/headless-e2e.py --asan --all    # every scenario under ASan (~14 min)
```

`--asan` runs the compositor, shell and every app from `out-asan/` (build it with
`ninja -C out-asan -j2` — more parallel jobs run an 8 GB machine out of memory on
the shell's sources) and fails a scenario on any sanitizer report in any log.
Timeouts are scaled 3×. Apps the shell launches itself still come from `out/`.
By default `--asan` runs a focused set (every app via `apps-start`, plus the
compositor paths: file-open, trust, gates, consent, shell keyboard, fetch); the
layout/contrast audits and the smoke tools are left to the normal run.

`headless-e2e.py` gives each scenario a fresh compositor and a throwaway
`$HOME`, and refuses to run while a desktop session is up — the compositor
must never run without `--headless` on a live seat.

## License

Released under the MIT License — see [LICENSE](LICENSE).
