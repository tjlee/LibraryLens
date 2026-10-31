# LibraryLens — module contract

Local disk-usage viewer for `~/Library`, dev caches, Bazel output and Docker.
Python 3.9 (system `/usr/bin/python3`), **standard library only**. Every module starts with
`from __future__ import annotations`. No `match`, no runtime `X | Y` unions.

Run: `python3 -m librarylens serve` (browser UI) or `python3 -m librarylens scan --top 30` (CLI).
Run from `~/PycharmProjects/LibraryLens` (the package lives in `librarylens/`).

## Layout / ownership

| File | Owner | Purpose |
|---|---|---|
| `__init__.py`, `__main__.py`, `CONTRACT.md` | lead | CLI entry, wiring |
| `scanner.py` | agent: scanner | roots + filesystem walk + scan cache |
| `resolver.py`, `known_apps.json` | agent: resolver | folder -> app attribution |
| `insights.py` | agent: insights | flags, safety, reclaimable summary |
| `server.py`, `trash.py`, `static/index.html` | agent: ui | HTTP API, Move to Trash, browser UI |

Modules must not import each other except as listed below:
- `server.py` imports `scanner`, `resolver`, `insights`, `trash`.
- `__main__.py` imports everything.
- `scanner`, `resolver`, `insights`, `trash` import nothing from the package (pure, testable in isolation).

## Data model (plain JSON-serialisable dicts)

### Node — one folder (or a single big file) in the tree
```python
{
  "path": "/Users/x/Library/Caches/JetBrains",   # absolute, not symlink-resolved
  "name": "JetBrains",
  "size": 164089036800,      # int bytes, ALLOCATED on disk: sum(st_blocks*512), hard links counted once
  "files": 123456,           # int, number of regular files in subtree
  "mtime": 1761820000.0,     # float epoch, NEWEST mtime of any file/dir in subtree
  "is_dir": True,
  "error": None,             # None | "permission" | "<short message>"; partial sizes still reported
  "children": [Node, ...],   # sorted by size desc; present down to max_depth, else []
  # --- added by resolver.annotate (only on nodes it can attribute; may be absent) ---
  "app": AppInfo | None,
  # --- added by insights.annotate (on every node) ---
  "flags": ["orphaned" | "old_version" | "stale" | "regenerable", ...],
  "safety": "safe" | "caution" | "danger",
  "reason": "human readable one-liner explaining flags/safety",
}
```
Children smaller than `min_size` (default 1 MiB) are collapsed into one synthetic child
`{"name": "(N smaller items)", "path": <parent path>, "is_dir": False, "synthetic": True, ...}`.

### Root — a top-level scanned location (a Node plus these keys)
```python
{ **Node, "label": "Caches", "category": <Category>, "nested_in": None | "<label of containing root>" }
```
`Category` is one of:
`"cache" | "app_support" | "container" | "group_container" | "logs" | "dev_cache" | "bazel" | "docker" | "other"`.
`nested_in` is set when the root lies inside another root (e.g. Docker inside Containers); totals must
skip nested roots to avoid double counting.

### ScanResult
```python
{ "scanned_at": 1761820000.0, "duration_s": 42.1, "home": "/Users/x",
  "roots": [Root, ...],
  "summary": {...}   # filled by insights.annotate, see below
}
```

### AppInfo
```python
{ "name": "Slack",                     # display name
  "bundle_id": "com.tinyspeck.slackmacgap" | None,
  "app_path": "/Applications/Slack.app" | None,
  "installed": True,                   # app (or CLI tool) is present on this machine
  "running": False,                    # a matching process is running right now
  "version": "2026.3" | None,          # for versioned folders like IntelliJIdea2026.3
  "installed_versions": ["2027.1"],    # for versioned products, versions actually installed
  "kind": "app" | "cli" | "system",    # system = Apple/macOS component
  "source": "bundle_id" | "container_plist" | "name_match" | "known" | "jetbrains",
}
```

## Module APIs

### scanner.py
```python
def default_roots() -> list[dict]          # [{"label","path","category"}], only existing paths
def scan_path(path: str, max_depth: int = 3, min_size: int = 1 << 20) -> dict   # Node
def scan_all(roots: list[dict] | None = None, max_depth: int = 3, workers: int = 8,
             progress: Callable[[str], None] | None = None) -> dict              # ScanResult (no summary yet)
def save_cache(result: dict) -> None       # ~/.librarylens/scan.json (atomic, owner-only, thread-safe)
def load_cache() -> dict | None
def find_node(result: dict, path: str) -> dict | None
```

### resolver.py
```python
def annotate(result: dict) -> None   # mutates in place: sets node["app"] on attributable nodes
def resolve(path: str, category: str) -> dict | None   # AppInfo for one folder
```

### insights.py
```python
def annotate(result: dict, stale_days: int = 90, now: float | None = None) -> None
    # sets flags/safety/reason on every node and result["summary"]:
    # {"total": int, "reclaimable": int, "by_flag": {"orphaned": int, ...},
    #  "by_category": {"cache": int, ...}, "top_candidates": [ {"path","size","flags","reason"} x 20 ]}
```

### trash.py
```python
def move_to_trash(path: str, allowed_roots: list[str]) -> dict   # {"ok": bool, "error": str | None}
def reveal_in_finder(path: str) -> None
```

### server.py
```python
def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None
```

---

# Iteration 2 — whole disk, detectors, git cleanup

Goal: account for the WHOLE Data volume (~956 GB), not just ~/Library. Explain where every GB is,
find garbage outside app folders (git leftovers, build outputs, VMs), and offer safe actions beyond
Move to Trash (first: git cleanup). **No admin/sudo, ever**: unreadable places (other users' homes,
parts of /private/var) are reported as "unaccounted", never escalated.

Real findings that motivated this (use them as test expectations):
- `~/IdeaProjects/ultimate/.git` = 394 GB: 73 leftover `objects/pack/tmp_pack_*` files from failed
  fetches (177 GB, `git count-objects -v` "garbage"), 53 real packs (209 GB, several duplicate ~8.3 GB
  packs), 7 prunable worktrees. `ultimate/out` 23.6 GB build output.
- `~/Parallels/Windows 11.pvm` 116 GB VM; `~/Documents/PHOTO` 84 GB personal photos.
- APFS container 987.7 GB used: Data 956 GB, System 13.3, Preboot 11.5, Recovery 1.9, "Nix Store" 3.8;
  OS-update local snapshots exist.

## Ownership (iteration 2)

| File | Owner |
|---|---|
| `pipeline.py`, `__main__.py`, `CONTRACT.md` | lead |
| `scanner.py` (v2) | agent B |
| `disk.py` (new), `insights.py` (v2), `resolver.py`/`known_apps.json` (small additions) | agent A |
| `detectors.py` (new), `actions.py` (new) | agent C |
| `server.py`, `trash.py`, `static/index.html` (v2) | agent D |

New import rules: `server.py` imports `pipeline`, `scanner`, `insights`, `actions`, `trash`, `disk`.
`pipeline.py` imports scanner, disk, resolver, detectors, insights. `disk`, `detectors`, `actions` import
nothing from the package. Keep existing tests passing (update them if a contract change requires it).

## Pipeline (lead, `pipeline.py`)
```python
def run(prev: dict | None = None, only: list[str] | None = None, max_depth: int = 3,
        progress: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None) -> dict
# scanner.scan_all(only=only, prev=prev, ...) -> disk.overview() -> resolver.annotate
# -> detectors.annotate -> insights.annotate -> scanner.save_cache ; returns the result.
# Phase names reported through progress as "phase:<name>" strings before each step.
```

## Roots v2 (scanner)
`default_roots()` keeps all iteration-1 roots and adds:

| label | path | category | exclude |
|---|---|---|---|
| Home | `~` | home | every other root inside `~` (e.g. `~/Library`, `~/.gradle`, `~/go/pkg`, ...) |
| Library (other) | `~/Library` | library_other | the 5 Library roots (Caches, Application Support, Containers, Group Containers, Logs) |
| Applications | `/Applications` | applications | |
| System Library | `/Library` | system | |
| /opt | `/opt` | dev_cache | |
| /usr/local | `/usr/local` | dev_cache | |
| Shared | `/Users/Shared` | other | |
| Swap & sleepimage | `/private/var/vm` | system | |
| /private/var/tmp | `/private/var/tmp` | other | Bazel output root |

New Root key `"exclude": [paths]`: the walk skips those paths. In their place the parent gets a stub
child `{"name", "path", "size": 0, "files": 0, "is_dir": True, "children": [], "link_root": "<label>"}`
so the UI can show "scanned separately → <label>". Excluded subtrees are NOT counted in this root, so
no double counting. `nested_in` is computed only from containing roots that do NOT exclude the path
(e.g. Docker stays nested in Containers; `~/.gradle` is excluded from Home, so `nested_in` is None).
Category list grows: `home`, `library_other`, `applications`, `system` (plus iteration-1 ones).

Note: `~` contains `~/Library` which is excluded from Home, while `Library (other)` covers `~/Library`
minus its 5 roots. Totals: sum of non-nested roots.

## Scanner v2 additions
```python
def scan_all(roots=None, max_depth=3, workers=8, progress=None, min_size=1<<20,
             only: list[str] | None = None,      # labels to rescan; others copied from prev
             prev: dict | None = None,
             cancel: threading.Event | None = None,   # set -> stop ASAP, raise scanner.Cancelled
             big_file_min: int = 500 * 10**6, big_files_limit: int = 200) -> dict
```
- Each Root gets `"scanned_at"` and `"duration_s"` (per-root rescan merges into `prev`).
- `result["big_files"]`: up to `big_files_limit` largest regular files ≥ `big_file_min` across all roots,
  sorted desc: `{"path","size","mtime","root": label}` (collected during the walk, heap-based).
- `result["special"]`: special directories found at ANY depth (outermost match only, i.e. don't record
  `node_modules` inside `node_modules`), each `{"path","kind","size","files","mtime","root": label}`
  with exact subtree size:
  - `git_dir`: a directory named `.git` (or a bare repo: dir containing `HEAD`, `objects/`, `refs/`).
  - `build_output`: `node_modules`, `target` (if sibling `pom.xml`/`Cargo.toml`/`build.sbt`), `build`
    (if sibling `build.gradle*`/`CMakeLists.txt`/`package.json`), `out` (if sibling `.idea`, `.git`,
    `build.gradle*`, `BUILD.bazel`/`WORKSPACE`/`MODULE.bazel`, or `*.iml`), `.gradle` (project-level,
    not `~/.gradle`), `__pycache__` (skip — too many; don't record), `.venv`/`venv` (has `pyvenv.cfg`),
    `cmake-build-*`, `DerivedData`, `.next`, `.nuxt`, `dist` (if sibling `package.json`), `.tox`, `.pytest_cache`? (skip small), `bazel-out` is a symlink → skip (Bazel output already a root).
  - `vm`: bundles/dirs/files ending `.pvm`, `.utm`, `.vmwarevm`, `.vbox`-dir, Docker.raw is NOT (Docker root).
  - `disk_image`: files `*.iso`, `*.dmg`, `*.img`, `*.vmdk`, `*.qcow2` ≥ 100 MB.
  - `photos_library`: `*.photoslibrary`; `ios_backup`: `~/Library/Application Support/MobileSync/Backup/*`.
- Performance target: whole scan of ~1 TB / several million files in ≤ ~5 min; progress throttled;
  cancel honoured within ~1 s.

## Disk overview (agent A, `disk.py`)
```python
def overview() -> dict
# {"container": {"size","used","free"}, "data_volume": {"name","used"},
#  "volumes": [{"name","role","used"}],          # from `diskutil apfs list -plist`
#  "snapshots": [{"name","kind": "os_update"|"time_machine"|"other"}],  # `tmutil listlocalsnapshots /`
#  "purgeable": int | None,                       # important-usage free minus free, if obtainable w/o admin
#  "users": [{"name","home","readable": bool}],   # /Users/* other than Shared
#  "collected_at": float}
```
No admin. Must not hang: subprocess timeouts ≤ 10 s, every field optional on failure.

## Detectors (agent C, `detectors.py`)
```python
def annotate(result: dict) -> None
```
Sets on nodes (tree nodes at the matching path, if present) and builds `result["projects"]`/`result["git_repos"]`:
- `node["kind"]`: `"git_repo" | "git_dir" | "build_output" | "vm" | "disk_image" | "user_data" |
  "photos_library" | "ios_backup" | "downloads" | "project"`; inherited by insights for safety.
  `user_data` = `~/Documents`, `~/Pictures`, `~/Movies`, `~/Music`, `~/Desktop`, `~/Library/Mobile Documents`
  (iCloud Drive), `~/Library/Mail`, `~/Library/Messages`. `downloads` = `~/Downloads`. `project` = repo
  work tree (parent of a `git_dir`).
- `node["details"]`: kind-specific dict, e.g. git: `{"repo","pack_size","packs","largest_pack",
  "loose_size","loose_count","garbage_size","garbage_files": [names],"prunable_worktrees": [paths],
  "unreachable_worktrees": [paths],"worktrees": n,"gc_running": bool,"fetch_running": bool,"last_commit": epoch|None}` (read the
  filesystem directly: list `objects/pack`, `worktrees/*/gitdir` targets missing → prunable,
  unless only out of reach (unmounted `/Volumes` drive, unreadable) → unreachable;
  `gc.pid`/`*.lock` → running; use `git` CLI only with timeouts, `-C`, never mutating);
  vm: `{"vm_name","last_used","snapshots": n}`; build_output: `{"project","project_mtime"}`.
- `node["actions"]`: list of available actions (see below), only for kinds that have them.
- `result["git_repos"]`: list of the git `details` dicts (+ `"path"` of the work tree, `"size"` of .git).
Must be fast (< 5 s total; filesystem reads only on special dirs, not the whole tree).

## Actions (agent C, `actions.py`) — allow-listed operations besides Move to Trash
Action descriptor on a node:
`{"id": "git_delete_temp_packs", "label": "Delete 73 leftover fetch temp files", "frees": int,
  "safety": "safe"|"caution", "blocked": None | "reason", "background": bool, "confirm": "text"}`
```python
def available(node: dict) -> list[dict]          # used by detectors to fill node["actions"]
def run(action_id: str, path: str, allowed_roots: list[str],
        on_output: Callable[[str], None] | None = None) -> dict
# {"ok","error","freed","details"}; re-validates EVERYTHING at run time; logs to ~/.librarylens/actions.log
```
Actions:
- `git_delete_temp_packs` — permanently delete `objects/pack/tmp_pack_*`, `tmp_idx_*`, `.tmp-*-pack*`
  ONLY if: path is a git dir inside allowed roots; file name matches strictly; file mtime older than
  10 min; file not open by any process (`lsof -t -- file...`, timeout); no `git fetch`/`index-pack`/
  `gc`/`repack` process with this repo (check `gc.pid` and `ps` + `lsof -t +D objects/pack`… keep it
  bounded). These files are never referenced by git, so deletion is safe; Trash would not free space.
- `git_worktree_prune` — `git -C <worktree> worktree prune -v`; blocked if any worktree is unreachable
  (git would prune it too).
- `git_gc` — background `git -C <worktree> gc` with streamed progress; blocked if gc/fetch running or
  free space < largest pack × 1.2 (message says how much is needed and suggests deleting temp packs first).
- `git_maintenance_off`? no. `docker_prune`? not now. Keep the list to the three above.

## Insights v2 (agent A)
- Kind-aware safety (node kind inherited down the tree): `user_data`/`photos_library`/`ios_backup` →
  caution, never candidates ("personal data — move to an external drive instead");
  `vm` → caution, candidate only if `last_used` older than stale_days; `disk_image` in Downloads → safe
  candidate if stale; `build_output` → safe + `regenerable`, candidate if project mtime older than
  stale_days (else not, "project is active"), except in installed software (`installed`/app-data
  categories, hidden home folders, `*.app` bundles), where the category rules apply; `git_dir` → danger for trash (use the git actions);
  `applications`/`system` categories → danger.
- Actions count toward reclaimable: `summary["actions_reclaimable"]` = sum of `frees` of unblocked
  actions; `summary["reclaimable"]` includes it (no overlap with trash candidates: a git_dir is never a
  trash candidate). `top_candidates` entries may carry `"action": {...}` instead of being trash items.
- `summary["by_kind"]`: bytes per detector kind (maximal nodes).
- `summary["unaccounted"]` = `disk.data_volume.used − summary.total` (≥ 0) when `result["disk"]` exists,
  plus `summary["disk_segments"]`: ordered list for the stacked bar:
  `[{"key","label","size"}]` — System volumes (non-Data APFS volumes), then by category groups
  (Library, Home/projects, VMs, Personal, Dev caches & Bazel, Docker, Applications & system, Other),
  Unaccounted, Free. Sizes must sum to `container.size` (approximately; clamp at 0).

## Server/UI v2 (agent D)
- Scans run via `pipeline.run(...)`; `POST /api/scan {"only": [labels]?}`; `POST /api/scan/cancel`.
- `GET /api/disk` → live `disk.overview()`.
- `POST /api/action {"path","action","confirm": true}` → `actions.run(...)`; background actions become
  jobs: `GET /api/jobs` → `[{"id","action","path","state": running|done|failed,"output_tail","started_at"}]`.
  After success: rescan only the affected root (`only=[label]`) in the background.
- UI: new **Disk** tab (default) with the stacked bar from `summary.disk_segments` + volume/snapshot list
  + "unaccounted" explanation (other users' homes unreadable without admin, snapshots, purgeable);
  **Big files** tab (`result.big_files`, Reveal/Trash); **Git repos** tab (`result.git_repos` with sizes,
  garbage, packs, prunable worktrees, action buttons with confirm modals and live job output);
  kind badges in the folder table; per-root rescan button; cancel scan button; link stubs
  (`link_root`) navigate to the other root.
