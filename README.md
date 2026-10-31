# LibraryLens

A local disk-usage viewer for macOS that explains where the space on your Data volume went.

LibraryLens scans `~/Library`, your home folder, developer caches (Gradle, Maven, npm, Cargo, Go,
pyenv, …), Bazel output, Docker, VMs, `/Applications`, `/opt`, `/usr/local` and more. It works out
which app or tool owns each folder and flags what is safe to reclaim. It can then clean up safely:
Move to Trash (so Finder's "Put Back" still works), plus three allow-listed git maintenance actions.

It runs entirely on your Mac: a small web server bound to `127.0.0.1` with a browser UI, and a
terminal report. Nothing is sent anywhere, and it never asks for admin rights.

## Requirements

- macOS (APFS).
- Python 3.9 or newer. The system `/usr/bin/python3` is enough; if it is missing, run
  `xcode-select --install`.
- Nothing else: standard library only, no `pip install`, no build step.

## Quick start

```bash
git clone git@github.com:tjlee/LibraryLens.git
cd LibraryLens
python3 -m librarylens
```

This starts the UI at <http://127.0.0.1:8765/> and opens it in your browser. On first start, click
**Scan now**. A scan takes from under a minute to a few minutes, depending on how much is on the
disk. The result is cached, so the next start shows the last scan immediately. Stop the server with
`Ctrl+C`.

Run all commands from the repository root (the folder that contains `librarylens/`).

## Using the browser UI

The sidebar lists every scanned location with its size, grouped into Library, Home, Dev caches &
Bazel, Docker, Applications & System and Other. **⟳ Rescan** rescans a single location; **Rescan all** runs a full scan, and
**Cancel** stops a running one (the previous result is kept).

| View | What it shows |
|---|---|
| **Disk** | A stacked bar of the whole APFS container: system volumes, each group of locations, *Unaccounted* and free space. Lists the volumes and local snapshots, and explains what *Unaccounted* covers (other users' homes, snapshots, purgeable space, unreadable folders). |
| **Folders** | Drill down into any location. Each row shows size, owning app, flags, a safety rating and a one-line reason (click **why?**). Folders scanned as a separate location appear as links to it. |
| **Big files** | The largest single files (500 MB and up) anywhere in the scan, with **Reveal** and **Move to Trash**. |
| **Git repos** | Every repository found, with `.git` size, packs, leftover temp files ("garbage"), loose objects, prunable worktrees and last commit, plus the git cleanup actions. |
| **Candidates** | The top suggestions for reclaiming space: trash candidates and git actions, largest first. |

### Safety ratings and flags

Every folder gets a rating:

- **safe**: regenerable data nobody will miss, e.g. caches, logs and build output of a project you
  haven't touched in 90 days.
- **caution**: app data, projects, VMs, personal files, installed software. Look before you delete;
  Move to Trash asks you to tick a box first.
- **danger**: scan roots, `/Applications`, system folders, Docker's disk image, git internals.
  LibraryLens never suggests these, and Move to Trash makes you type the folder's name.

Flags explain why something is a candidate: `regenerable` (rebuilt or re-downloaded on demand),
`orphaned` (the app that owns it is no longer installed), `old_version` (data of an IDE version you
no longer have), `stale` (untouched for 90 days). The rating is raised when the owning app is
running; quit the app first.

### Cleaning up

- **Move to Trash** asks Finder to move the item, so **Put Back** works. If Finder can't (for
  example because automation permission was denied), LibraryLens renames the item into `~/.Trash`
  instead, without Put Back. Nothing is deleted permanently, and no space is freed until you empty
  the Trash. If the folder contains locations that are scanned separately (for example `~/.local`
  holds uv's cache), they go to the Trash with it; the dialog lists them and shows the combined size.
- **Git actions** (Git repos view), each with its own confirmation:
  - *Delete leftover fetch temp files*: permanently deletes `tmp_pack_*` and similar leftovers of
    interrupted fetches in `objects/pack`. Git never uses them, and moving them to the Trash would
    free nothing. Files newer than 10 minutes or still open are skipped.
  - *Prune missing worktrees*: runs `git worktree prune`. Blocked while any worktree is on a
    disconnected drive, because git would prune that one too.
  - *Run git gc*: repacks the repository in the background with live output. Blocked while a fetch
    or gc is running, or if there isn't enough free space for it.

After Move to Trash the view updates right away. After a git action, the affected location is also
rescanned in the background.

## Command line

```bash
python3 -m librarylens                          # same as `serve`
python3 -m librarylens serve [--port 8765] [--no-browser]
python3 -m librarylens scan [--top 30] [--depth 3] [--cached] [-q]
```

`serve` starts the browser UI. If the port is taken, it tries the next nine. `--no-browser`
prints the URL without opening it.

`scan` runs a full scan and prints a terminal report: the disk breakdown, every location by size,
the top cleanup candidates with their reasons, git repositories and the biggest files.

| Option | Meaning |
|---|---|
| `--top N` | Number of cleanup candidates to list (default 30). |
| `--depth N` | Folder depth kept in the tree (default 3). Sizes are always complete; deeper levels are just not listed. |
| `--cached` | Print the report from the last scan instead of scanning again. |
| `-q`, `--quiet` | No progress line while scanning. |

Two developer helpers:

```bash
python3 -m librarylens.scanner ~/Library/Caches 25   # quick scan of one folder (benchmark)
python3 -m librarylens.disk                          # disk overview as JSON
```

## macOS permissions

- **Full Disk Access.** macOS hides some folders (Mail, Messages, Safari, some app containers) from
  apps without Full Disk Access. LibraryLens reports those as *partially unreadable* and counts
  their space under *Unaccounted*. To include them, grant Full Disk Access to the app you run
  LibraryLens from (Terminal, iTerm, your IDE) in *System Settings ▸ Privacy & Security ▸ Full Disk
  Access*, then restart it.
- **Automation (Finder).** The first **Move to Trash** makes macOS ask whether your terminal may
  control Finder. Allow it to keep **Put Back** working. If you deny it, items are still moved to
  the Trash, without Put Back.
- **No admin rights, ever.** Places that need `sudo` (other users' homes, some system folders) are
  reported as unreadable rather than escalated.

## Safety model

- **Nothing happens without a click.** Scans only read. Every change is Move to Trash or one of the
  three git actions, and each needs an explicit confirmation in the UI.
- **Every change is checked again at the moment it runs.** Ratings and buttons in the UI are only
  advice. Move to Trash refuses `/`, your home folder, `~/Library`, the Trash itself, any scanned
  location's root, anything outside the scanned locations, anything inside a `.git` folder or a bare
  repository, and anything an app is using right now (checked again at that moment, not taken from the
  scan). These checks ignore letter case, as APFS does.
- **Scans never follow symlinks** and never cross into other volumes.
- **Local only.** The server listens on `127.0.0.1` only, checks the `Host` header, and accepts
  changes only with a token that is generated per run and embedded in the page.
- **Audit log.** Every trash and git action, successful or not, is appended as one JSON line to
  `~/.librarylens/actions.log`.

## Files LibraryLens writes

| Path | Purpose |
|---|---|
| `~/.librarylens/scan.json` | The last scan (written atomically, readable only by you). Delete it to start fresh. |
| `~/.librarylens/actions.log` | Audit log of trash and git actions. |

## Development

```bash
python3 -m unittest discover -s librarylens/tests -t .        # all tests, about 12 s
python3 -m unittest librarylens.tests.test_trash              # one module
```

`test_resolver.ResolverRealDataTest` checks app attribution against real folder names on your Mac
and prints a long table; that output is expected.

How it works: one pipeline (`librarylens/pipeline.py`) runs scan → disk overview → app attribution →
detectors (git repos, build output, VMs, personal data) → insights (flags, safety, reasons, summary)
→ cache. The browser UI is a single file, `librarylens/static/index.html`.
[`librarylens/CONTRACT.md`](librarylens/CONTRACT.md) is the spec for every data shape and module
API. [`CLAUDE.md`](CLAUDE.md) has the architecture notes and coding constraints (Python 3.9,
standard library only).

To teach LibraryLens about another app or tool, add an entry to `librarylens/known_apps.json`.

## License

Apache License 2.0, see [LICENSE](LICENSE).
