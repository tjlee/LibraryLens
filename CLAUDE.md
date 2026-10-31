# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LibraryLens (`librarylens/`): a local macOS disk-usage viewer covering the whole Data volume, including `~/Library`, home, dev caches, Bazel, Docker, VMs and git repos. It works out which app owns each folder, flags what is reclaimable, and offers safe cleanup (Move to Trash plus three allow-listed git actions). It has a browser UI and a CLI.

**`librarylens/CONTRACT.md` is the spec.** It defines every dict shape (Node, Root, ScanResult, AppInfo, action descriptors), the public API of each module, and the import rules. Read it before changing any data shape or cross-module call. Update it when a contract changes.

## Hard constraints

- Python 3.9 (system `/usr/bin/python3`), **standard library only**. No third-party packages and no packaging/build files.
- Every module starts with `from __future__ import annotations`. No `match`, and no runtime `X | Y` unions: use `Optional`/`List` etc. anywhere the annotation is evaluated.
- **Never use admin/sudo.** Unreadable places are reported as `error`/"unaccounted", never escalated.
- Subprocesses use argv lists (never a shell), have timeouts (≤ 10 s in `disk.py`), and use `git -C` only, never in a mutating way, outside `actions.run`.

## Commands

Run everything from the repo root (`~/PycharmProjects/LibraryLens`), not from inside `librarylens/`:

```bash
python3 -m librarylens serve [--port 8765] [--no-browser]   # browser UI (also the default with no subcommand)
python3 -m librarylens scan --top 30 [--depth 3] [--cached] [-q]   # terminal report
python3 -m librarylens.scanner ~/Library/Caches 25           # quick single-path scan benchmark
python3 -m librarylens.disk                                  # dump disk.overview() JSON

python3 -m unittest discover -s librarylens/tests -t .       # all tests (~170, ~12 s)
python3 -m unittest librarylens.tests.test_trash             # one module
python3 -m unittest librarylens.tests.test_scanner.SomeTest.test_name   # one test
```

`test_resolver.ResolverRealDataTest` runs against real folder names on this machine and prints a long attribution table. That output is expected, not a failure.

## Architecture

Data flows through one shared pipeline (`pipeline.run`), used by both the CLI and the server:

```
scanner.scan_all → disk.overview → resolver.annotate → detectors.annotate → insights.annotate → scanner.save_cache
```

Each stage **mutates the ScanResult dict in place** and adds keys to nodes: `app`, then `kind`/`details`/`actions`, then `flags`/`safety`/`reason`, and finally `result["summary"]`. `pipeline.reannotate` re-runs the stages after the scan, without rescanning, once a trash or action has been applied. Progress is reported as plain strings, and stage boundaries are sent as `"phase:<name>"`.

Module isolation matters. `scanner`, `resolver`, `insights`, `disk`, `trash` and `actions` import nothing from the package, so each one can be tested alone. The one exception is `detectors`, which imports `actions` (for `available()` and the read-only git helpers). Only `pipeline`, `server` and `__main__` wire modules together. `server` imports lazily through `App.mod(name)`, and tests inject stubs via `App(modules={...})`.

Key cross-module ideas:
- **Roots** (`scanner.default_roots`) overlap by design. A Root's `exclude` list removes subtrees that another root scans; in their place the parent gets a zero-size `link_root` stub. `nested_in` marks roots that lie inside another root without being excluded from it (e.g. Docker inside Containers). Totals sum only non-nested roots. Get this wrong and you double-count.
- **Sizes are allocated bytes** (`st_blocks*512`, hard links counted once). Symlinks are never followed and filesystem boundaries are never crossed. On macOS the scanner lists directories with `getattrlistbulk(2)` via ctypes, which releases the GIL so worker threads run in parallel. The fallback is `os.scandir`. Tests toggle `scanner._USE_BULK` to cover both paths.
- **Partial rescans**: `scan_all(only=[labels], prev=result)` rescans those roots and copies the rest from `prev`. `cancel` (a `threading.Event`) raises `scanner.Cancelled`.
- **App attribution** (`resolver.py`) works in a fixed order: JetBrains versioned folders, then `known_apps.json` entries (fnmatch globs, `descend`, install checks), then container plists, then bundle IDs, then name matching. Add new tools or caches to `known_apps.json` rather than to code.
- **Safety rules** live in the tables at the top of `insights.py`. A detector `kind` is inherited down the tree, and the nearest one wins.
- **Destructive operations re-validate everything at run time.** UI and descriptors are only advisory. `trash.validate` refuses `/`, `~`, `~/Library`, the Trash, scan roots themselves, anything outside the allowed roots, and anything containing `.git` or inside a bare repo (path checks are case-insensitive, like APFS). `actions.run` allows only `git_delete_temp_packs`, `git_worktree_prune` and `git_gc`. Both append JSON lines to `~/.librarylens/actions.log`.

## Server / UI

- `server.py` is a `ThreadingHTTPServer` that binds only to 127.0.0.1. It checks the Host header (DNS-rebinding protection), and every POST needs the `X-LL-Token` header. The token is generated per process and injected into `static/index.html` by replacing `__LL_TOKEN__`. The endpoint list is in the module docstring.
- Scans and background actions run on threads. `App` keeps separate locks for the result, trash, status, jobs and disk cache. Once an action succeeds, only the affected root is rescanned. While a scan runs, trash and action threads keep changing the live result and journal what they do; before the new result is swapped in, trashes are replayed onto it (roots copied from `prev` or walked before the trash; roots whose walk overlapped it, and roots touched by an action, are rescanned once more) and it is saved again, so the cache matches the view. A partial scan gets a private copy of the result as `prev`.
- `static/index.html` is the whole UI in one file of vanilla JS/CSS with no build step.

## State on disk

- `~/.librarylens/scan.json` holds the scan cache (written atomically). `serve` and `scan --cached` load it.
- `~/.librarylens/actions.log` is the audit log for trash and actions.
