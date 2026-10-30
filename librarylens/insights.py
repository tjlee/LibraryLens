"""Flags, safety levels and the reclaimable-space summary for a ScanResult.

Pure function of the result dict (+ ``now``): no filesystem access, no package imports.
Rules live in the small tables / functions at the top so they are easy to tweak.

Iteration 2 adds:
- kind-aware rules: detector kinds (``node["kind"]``, see detectors.py) are inherited down the
  tree, the nearest one wins;
- new root categories (home, library_other, applications, system) and link stubs (``link_root``);
- allow-listed actions (``node["actions"]``) counted toward the reclaimable total;
- ``by_kind``, ``unaccounted`` and ``disk_segments`` (whole-disk stacked bar) in the summary.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

Node = Dict[str, Any]

# --------------------------------------------------------------------------- rule tables

FLAG_ORDER: Tuple[str, ...] = ("regenerable", "orphaned", "old_version", "stale")
REGENERABLE_CATEGORIES = frozenset({"cache", "logs", "dev_cache", "bazel"})
DATA_CATEGORIES = frozenset({"app_support", "container", "group_container"})
USER_CATEGORIES = frozenset({"home", "library_other"})      # mix of personal and app data
LOCKED_CATEGORIES = frozenset({"applications", "system"})   # always danger
SAFETY_LEVELS: Tuple[str, ...] = ("safe", "caution", "danger")
BASE_SAFETY: Dict[str, str] = {
    "cache": "safe", "logs": "safe", "dev_cache": "safe", "bazel": "safe",
    "app_support": "caution",
    "container": "danger", "group_container": "danger",
    "docker": "danger",
    "home": "caution", "library_other": "caution",
    "applications": "danger", "system": "danger",
    "installed": "caution",
    "other": "caution",
}
# dev_cache roots that hold installed software rather than caches (rule category "installed").
INSTALL_ROOTS = ("/opt", "/usr/local")
TOP_N = 20
DAY = 86400.0

# Detector kinds (CONTRACT "Detectors").
PERSONAL_KINDS = frozenset({"user_data", "photos_library", "ios_backup"})
GIT_KINDS = frozenset({"git_dir", "git_repo"})
CODE_KINDS = frozenset({"git_dir", "git_repo", "project", "build_output"})
NO_BUMP_KINDS = PERSONAL_KINDS | GIT_KINDS | {"project"}  # quitting an app does not make these safer
# Hidden home folders that hold credentials: never suggested even if their tool is gone.
SENSITIVE_DOTDIRS = frozenset({".aws", ".ssh", ".gnupg", ".azure", ".kube", ".docker", ".config",
                               ".password-store", ".gcloud", ".oci"})

# Subject phrase per category: (with app, without app). "{app}" is the app label.
SUBJECTS: Dict[str, Tuple[str, str]] = {
    "cache": ("Cache of {app}", "Cache"),
    "logs": ("Logs of {app}", "Logs"),
    "dev_cache": ("{app} developer cache", "Developer cache"),
    "bazel": ("Bazel build output", "Bazel build output"),
    "app_support": ("{app} data", "Application data"),
    "container": ("{app} sandbox container", "App sandbox container"),
    "group_container": ("{app} shared group container", "Shared group container"),
    "docker": ("Docker Desktop data", "Docker Desktop data"),
    "home": ("{app} data", "Folder in your home"),
    "library_other": ("{app} data", "Library data"),
    "applications": ("Application {app}", "Application"),
    "system": ("{app}", "System data"),
    "installed": ("{app} (installed software)", "Installed software"),
    "other": ("{app} data", "Data"),
}
# App names that already read as a noun phrase ("PyCharm projects", "User-installed tools (~/.local)").
_NOUN_APP_RE = re.compile(r"(projects|folder|files|keys|config|settings|drive|trash|\))$", re.I)
# Well-known developer cache folders (matched on folder name, then on the root's folder name).
DEV_CACHE_NAMES: Dict[str, str] = {
    ".gradle": "Gradle dependency cache",
    ".m2": "Maven local repository",
    ".npm": "npm cache",
    ".yarn": "Yarn cache",
    ".pnpm-store": "pnpm store",
    ".cache": "User cache directory (~/.cache)",
    ".cargo": "Cargo registry cache",
    ".rustup": "Rust toolchains",
    ".ivy2": "Ivy dependency cache",
    ".konan": "Kotlin/Native toolchain cache",
    ".bun": "Bun cache",
    ".nuget": "NuGet package cache",
    ".pub-cache": "Dart/Flutter package cache",
    "go": "Go module cache",
    "pip": "pip cache",
    "wrapper": "Gradle wrapper distributions",
}
USER_DATA_LABELS: Dict[str, str] = {
    "Library/Mobile Documents": "iCloud Drive files",
    "Library/Mail": "Mail messages",
    "Library/Messages": "Messages history",
}
_PHOTO_RE = re.compile(r"photo|picture|dcim|camera|\bimg|фото|snimki", re.I)
_VIDEO_RE = re.compile(r"movie|video|film|видео", re.I)
# Verdict for safe, regenerable data that is not garbage.
REGEN_VERDICT: Dict[str, str] = {
    "cache": "regenerated on demand",
    "logs": "safe to delete",
    "dev_cache": "regenerated on next build",
    "bazel": "regenerated on next build",
}
DEFAULT_VERDICT: Dict[str, str] = {
    "safe": "safe to delete",
    "caution": "review before deleting",
    "danger": "do not delete by hand",
}
ROOT_NOTE = "top-level location — clean inside it"
SYSTEM_NOTE = "macOS system component — do not delete"
SYSTEM_LIB_NOTE = "system-wide data, needs admin — do not delete by hand"
SWAP_NOTE = "managed by macOS — shrinks after a restart"
APP_NOTE = "to uninstall, quit it and drag it to the Trash in Finder (or use its uninstaller)"
INSTALLED_NOTE = "remove it with its package manager (e.g. `brew uninstall`, `brew cleanup`)"
DOCKER_NOTE = ('use `docker system prune` or Docker Desktop "Clean / Purge data" '
               "instead of deleting the VM disk")
ORPHAN_DATA_NOTE = "app is gone, but it may hold your documents — review before deleting"
OLD_DATA_NOTE = "settings of an older version — review before deleting"
ERROR_NOTE = "partially unreadable (grant Full Disk Access)"
PERSONAL_NOTE = "move to an external drive instead"
GIT_NOTE = "don't move it to the Trash — use the git actions"
GIT_PART_NOTE = "do not delete by hand — use the git actions on the .git folder"
PROJECT_NOTE = "source code — keep it; clean its build output or git data instead"
TRASH_NOTE = "empty the Trash in Finder to free this space"
DOWNLOADS_NOTE = "review it and delete what you no longer need"

# Disk overview stacked-bar groups, in display order.
SEGMENTS: Tuple[Tuple[str, str], ...] = (
    ("system_volumes", "System volumes"),
    ("library", "Library"),
    ("home", "Home/projects"),
    ("vms", "VMs"),
    ("personal", "Personal"),
    ("dev", "Dev caches & Bazel"),
    ("docker", "Docker"),
    ("apps_system", "Applications & system"),
    ("other", "Other"),
    ("unaccounted", "Unaccounted"),
    ("free", "Free"),
)
CATEGORY_GROUP: Dict[str, str] = {
    "cache": "library", "app_support": "library", "container": "library",
    "group_container": "library", "logs": "library", "library_other": "library",
    "home": "home",
    "dev_cache": "dev", "bazel": "dev",
    "docker": "docker",
    "applications": "apps_system", "system": "apps_system",
    "other": "other",
}
KIND_GROUP: Dict[str, str] = {
    "vm": "vms",
    "user_data": "personal", "photos_library": "personal", "ios_backup": "personal",
    "downloads": "personal",
}


# --------------------------------------------------------------------------- formatting

def human(n: Optional[float]) -> str:
    """Decimal size like Finder: "177 GB", "23.6 GB", "512 MB"."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000:
            if unit == "B":
                return "%d B" % n
            return ("%d %s" % (round(n), unit)) if abs(n) >= 100 else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "%.2f TB" % n


def days_ago(ts: float, now: float) -> int:
    return max(0, int((now - ts) / DAY))


def ago(ts: float, now: float) -> str:
    d = days_ago(ts, now)
    return "today" if d == 0 else "yesterday" if d == 1 else "%d days ago" % d


def _base(path: Optional[str]) -> str:
    return (path or "").rstrip("/").rsplit("/", 1)[-1]


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# --------------------------------------------------------------------------- small rules

def app_label(app: Optional[dict]) -> Optional[str]:
    """Display name of an app, with its folder version ("IntelliJ IDEA 2026.3")."""
    if not app:
        return None
    name = app.get("name") or "unknown app"
    version = app.get("version")
    if version and str(version) not in name:
        return f"{name} {version}"
    return name


def quit_name(app: Optional[dict]) -> str:
    """App name for "quit X first": "IntelliJ IDEA projects" -> "IntelliJ IDEA"."""
    name = (app or {}).get("name") or "the app"
    return re.sub(r"\s+(projects|\(.*\))$", "", name, flags=re.I) or name


def is_orphaned(app: Optional[dict]) -> bool:
    """App is known, not installed, and not a macOS component."""
    return bool(app) and app.get("installed") is False and app.get("kind") != "system"


def is_old_version(app: Optional[dict]) -> bool:
    """Folder belongs to a version that is not among the installed versions."""
    if not app or not app.get("version"):
        return False
    installed = app.get("installed_versions") or []
    return bool(installed) and app["version"] not in installed


def is_stale(node: Node, cutoff: float) -> bool:
    """Newest mtime older than cutoff. Synthetic/unreadable nodes are never stale."""
    if node.get("synthetic") or node.get("error"):
        return False
    mtime = node.get("mtime")
    return bool(mtime) and mtime < cutoff


def activity_time(node: Node, kind: Optional[str], kdet: dict) -> Optional[float]:
    """Time of last real use: VM last_used, project mtime for build output, else newest mtime."""
    if kind == "vm" and kdet.get("last_used"):
        return float(kdet["last_used"])
    if kind == "build_output" and kdet.get("project_mtime"):
        return float(kdet["project_mtime"])
    if kind == "ios_backup" and kdet.get("last_backup"):
        return float(kdet["last_backup"])
    return node.get("mtime")


def compute_flags(node: Node, app: Optional[dict], category: str, cutoff: float,
                  kind: Optional[str] = None, kdet: Optional[dict] = None) -> List[str]:
    """Flags in FLAG_ORDER order."""
    flags = []
    if kind == "build_output" or (category in REGENERABLE_CATEGORIES
                                  and kind not in PERSONAL_KINDS and kind != "vm"):
        flags.append("regenerable")
    if is_orphaned(app):
        flags.append("orphaned")
    if is_old_version(app):
        flags.append("old_version")
    stale_ok = (category not in LOCKED_CATEGORIES and kind not in PERSONAL_KINDS
                and not node.get("synthetic") and not node.get("error"))
    if stale_ok:
        t = activity_time(node, kind, kdet or {})
        if t and t < cutoff:
            flags.append("stale")
    return flags


def bump(safety: str) -> str:
    """One level riskier."""
    return SAFETY_LEVELS[min(SAFETY_LEVELS.index(safety) + 1, len(SAFETY_LEVELS) - 1)]


def base_safety(category: str, depth: int, app: Optional[dict],
                orphaned: bool, old_version: bool) -> Tuple[str, Optional[str]]:
    """(safety, specific note or None) by category, before kinds and the "app is running" bump."""
    if depth == 0:
        return "danger", ROOT_NOTE
    if category == "applications":
        return "danger", APP_NOTE
    if category == "system":
        return "danger", SYSTEM_NOTE if app and app.get("kind") == "system" else SYSTEM_LIB_NOTE
    if app and app.get("kind") == "system" and (category in DATA_CATEGORIES or category in USER_CATEGORIES):
        return "danger", SYSTEM_NOTE
    if category == "docker":
        return "danger", DOCKER_NOTE
    if category == "installed":
        return "caution", INSTALLED_NOTE
    if category in REGENERABLE_CATEGORIES:
        return "safe", None
    base = BASE_SAFETY.get(category, "caution")
    if (category in DATA_CATEGORIES or category in USER_CATEGORIES) and (orphaned or old_version):
        return "caution", ORPHAN_DATA_NOTE if orphaned else OLD_DATA_NOTE
    return base, None


def subject(node: Node, depth: int, category: str, app: Optional[dict], ctx_name: str) -> str:
    """Leading phrase of the reason, e.g. "Cache of Slack"."""
    if depth == 0:
        return node.get("label") or node.get("name") or "Location"
    if category == "dev_cache":
        known = DEV_CACHE_NAMES.get((node.get("name") or "").lower()) \
            or DEV_CACHE_NAMES.get(ctx_name.lower())
        if known:
            return known
    with_app, without_app = SUBJECTS.get(category, SUBJECTS["other"])
    label = app_label(app)
    if label and category in USER_CATEGORIES and _NOUN_APP_RE.search(label):
        return label
    return with_app.format(app=label) if label else without_app


def version_clause(app: Optional[dict], orphaned: bool, old_version: bool) -> str:
    """", which is not installed (installed: 2027.1)" and friends."""
    installed = ", ".join(str(v) for v in (app or {}).get("installed_versions") or [])
    extra = f" (installed: {installed})" if installed and (old_version or orphaned) else ""
    if orphaned:
        return ", which is not installed" + extra
    if old_version:
        return ", an old version" + extra
    return ""


# --------------------------------------------------------------------------- kind rules

def unblocked_actions(node: Node) -> List[dict]:
    return [a for a in node.get("actions") or ()
            if isinstance(a, dict) and not a.get("blocked") and int(a.get("frees") or 0) > 0]


def _vm_name(kdet: dict, knode: Node) -> str:
    name = kdet.get("vm_name") or _base(knode.get("path")).rsplit(".", 1)[0]
    if kdet.get("type") == "parallels" or (knode.get("path") or "").endswith(".pvm"):
        return f"Parallels VM '{name}'"
    return f"Virtual machine '{name}'"


def _personal_subject(kind: str, own: bool, node: Node, kdet: dict) -> str:
    if kind == "photos_library":
        return "Photos library"
    if kind == "ios_backup":
        dev = kdet.get("device")
        return f"iPhone/iPad backup ({dev})" if dev else "iPhone/iPad backup"
    label = kdet.get("label") or ""
    if own and label:
        return USER_DATA_LABELS.get(label) or f"Your {_base(label)} folder"
    name = node.get("name") or ""
    if _PHOTO_RE.search(name) or _base(label) == "Pictures":
        return "Personal photos"
    if _VIDEO_RE.search(name) or _base(label) == "Movies":
        return "Personal videos"
    return "Personal files"


def kind_rules(node: Node, kind: str, knode: Node, app: Optional[dict], stale: bool,
               now: float) -> Tuple[str, str, List[str], Optional[str], bool]:
    """(safety, subject, extra clauses, note, may_be_candidate) for a node with an effective kind.

    ``knode`` is the nearest node carrying the kind (``node`` itself or an ancestor)."""
    own = knode is node
    kdet = knode.get("details") or {}
    clauses: List[str] = []
    if kind in PERSONAL_KINDS:
        if kind == "ios_backup":
            if kdet.get("last_backup"):
                clauses.append("last backup " + ago(kdet["last_backup"], now))
            note = "delete old backups in Finder (device ▸ Manage Backups) or " + PERSONAL_NOTE
        elif kind == "photos_library":
            note = PERSONAL_NOTE + " (Photos ▸ Settings ▸ Library location)"
        else:
            note = PERSONAL_NOTE
        return "caution", _personal_subject(kind, own, node, kdet), clauses, note, False

    if kind in GIT_KINDS:
        repo = kdet.get("repo") or _base(kdet.get("worktree")) or _base(knode.get("path"))
        what = "bare Git repository" if kind == "git_repo" or kdet.get("bare") else "Git data"
        if not own:
            return "danger", f"Part of the {what} of {repo}", [], GIT_PART_NOTE, False
        garbage = int(kdet.get("garbage_size") or 0)
        if garbage:
            clauses.append(f"{human(garbage)} of temp files from failed fetches")
        prunable = kdet.get("prunable_worktrees") or []
        if prunable:
            clauses.append(_plural(len(prunable), "prunable worktree"))
        if kdet.get("gc_running"):
            clauses.append("git gc is running — wait until it finishes")
        elif kdet.get("fetch_running"):
            clauses.append("a git fetch is running — wait until it finishes")
        note = GIT_NOTE
        acts = sorted(unblocked_actions(node),
                      key=lambda a: (bool(a.get("frees_estimated")), -int(a.get("frees") or 0)))
        if acts:
            frees = min(int(acts[0].get("frees") or 0), _size(node) or int(acts[0].get("frees") or 0))
            note += "; '%s' frees %s" % (acts[0].get("label") or acts[0].get("id"),
                                          frees_text(acts[0], frees))
        return "danger", f"{what[0].upper()}{what[1:]} of {repo}", clauses, note, False

    if kind == "project":
        name = _base(knode.get("path"))
        if not own:
            return "caution", f"Part of project {name}", [], None, False
        if kdet.get("last_commit"):
            clauses.append("last commit " + ago(kdet["last_commit"], now))
        return "caution", f"Project {name}", clauses, PROJECT_NOTE, False

    if kind == "build_output":
        proj = _base(kdet.get("project")) or _base((knode.get("path") or "").rsplit("/", 1)[0])
        pm = kdet.get("project_mtime") or knode.get("mtime")
        if stale:
            if pm:
                clauses.append("project untouched for %d days" % days_ago(pm, now))
            note = REGEN_VERDICT["dev_cache"]
        else:
            if pm:
                clauses.append("project active (changed %s)" % ago(pm, now))
            note = None
        return "safe", f"Build output of project {proj}", clauses, note, True

    if kind == "vm":
        vm = _vm_name(kdet, knode)
        subj = vm if own else "Part of " + (vm[0].lower() + vm[1:] if vm.startswith("Virtual") else vm)
        snaps = int(kdet.get("snapshots") or 0)
        if snaps:
            clauses.append(_plural(snaps, "snapshot"))
        t = kdet.get("last_used") or knode.get("mtime")
        app_name = (app or {}).get("name") or "its VM app"
        if stale:
            if t:
                clauses.append("not used for %d days" % days_ago(t, now))
            note = f"delete it in {app_name} if you no longer need it, or move it to an external drive"
        else:
            if t:
                clauses.append("last used " + ago(t, now))
            note = ("in use — delete old snapshots" if snaps else "in use — keep it") \
                + ", or move it to an external drive"
        return "caution", subj, clauses, note, True

    if kind == "disk_image":
        in_dl = bool(kdet.get("in_downloads"))
        subj = "Disk image " + (node.get("name") or "")
        if in_dl:
            note = "downloaded installer — safe to delete once installed"
            return "safe", subj, clauses, note, True
        return "caution", subj, clauses, None, False

    if kind == "downloads":
        if own:
            return "caution", "Downloads folder", clauses, DOWNLOADS_NOTE, False
        return "caution", "Downloaded item", clauses, None, True

    return "caution", node.get("name") or "Folder", clauses, None, False


# --------------------------------------------------------------------------- assess one node

def assess(node: Node, depth: int, app: Optional[dict], category: str,
           ctx_name: str, cutoff: float, now: float, kind: Optional[str] = None,
           knode: Optional[Node] = None, top: str = "") -> Tuple[List[str], str, str, bool]:
    """(flags, safety, reason, in_use) for one node.

    ``kind``/``knode``: effective detector kind and the node carrying it (nearest ancestor-or-self).
    ``top``: name of the depth-1 ancestor (used for home-folder rules)."""
    if node.get("link_root"):
        return [], "danger", "Scanned separately → %s" % node["link_root"], False
    knode = knode if knode is not None else node
    kdet = (knode.get("details") or {}) if kind else {}
    flags = compute_flags(node, app, category, cutoff, kind, kdet)
    orphaned = "orphaned" in flags
    old_version = "old_version" in flags
    stale = "stale" in flags
    safety, note = base_safety(category, depth, app, orphaned, old_version)
    subj = None
    clauses: List[str] = []
    locked = depth == 0 or category in LOCKED_CATEGORIES or category == "docker"
    if kind and not locked:
        safety, subj, clauses, note, _ = kind_rules(node, kind, knode, app, stale, now)
    elif category == "home" and top == ".Trash" and depth > 0:
        safety, subj, note = "caution", "Trash", TRASH_NOTE
    elif category == "system" and depth > 0 and app and "swap" in (app.get("name") or "").lower():
        note = SWAP_NOTE
    # A running app only matters for its own version's folders, and not for
    # folders it hasn't touched in stale_days (e.g. old Bazel output bases).
    # Visible home folders (~/IdeaProjects, ~/Parallels) are user content: quitting the app only
    # matters for what it writes there (build output, VM bundles).
    user_content = (category == "home" and not top.startswith(".")
                    and kind not in ("build_output", "vm"))
    running = (depth > 0 and bool(app) and bool(app.get("running"))
               and not old_version and not stale and not user_content
               and (kind not in NO_BUMP_KINDS or locked))
    if running:
        safety = bump(safety)

    if subj is None:
        subj = subject(node, depth, category, app, ctx_name) + version_clause(app, orphaned, old_version)
    parts = [subj] + clauses
    if stale and kind not in ("vm", "build_output", "ios_backup") \
            and not (kind == "project" and kdet.get("last_commit")):
        parts.append(f"untouched for {days_ago(node['mtime'], now)} days")
    if note:
        parts.append(note)
    if running:
        parts.append(f"app is running, quit {quit_name(app)} first")
    elif not note and not (kind == "build_output" and not stale):
        if safety == "safe" and not (orphaned or old_version):
            parts.append(REGEN_VERDICT.get(category, DEFAULT_VERDICT["safe"]))
        else:
            parts.append(DEFAULT_VERDICT[safety])
    if node.get("error"):
        parts.append(ERROR_NOTE)
    return flags, safety, " — ".join(parts), running


def is_candidate(node: Node, depth: int, flags: List[str], safety: str,
                 kind: Optional[str] = None, knode: Optional[Node] = None,
                 category: str = "", top: str = "") -> bool:
    """Worth suggesting for Move to Trash."""
    if depth == 0 or node.get("synthetic") or node.get("link_root") or safety == "danger":
        return False
    stale = "stale" in flags
    if kind and category not in LOCKED_CATEGORIES and category != "docker":
        own = knode is None or knode is node
        if kind in PERSONAL_KINDS or kind in CODE_KINDS - {"build_output"} or kind == "project":
            return False
        if kind == "vm":
            return stale
        if kind == "disk_image":
            return stale and bool(((knode or node).get("details") or {}).get("in_downloads"))
        if kind == "downloads":
            return stale and not own
        if kind == "build_output":
            return stale
    if category == "home" and top == ".Trash":
        return False
    if "regenerable" in flags and stale:
        return True
    if "orphaned" in flags or "old_version" in flags:
        # Visible home folders are user content; hidden ones may hold credentials.
        if category == "home" and (not top.startswith(".") or top in SENSITIVE_DOTDIRS):
            return False
        return True
    return False


# --------------------------------------------------------------------------- aggregation

def _path_key(path: str) -> str:
    return path.rstrip("/") + "/"


def dedupe_by_prefix(nodes: List[Node]) -> List[Node]:
    """Drop nodes whose path equals or lies under another node's path (keeps the ancestor).

    Sorting by ``path + "/"`` makes every subtree contiguous right after its top.
    """
    kept: List[Node] = []
    last = None
    for n in sorted(nodes, key=lambda n: _path_key(n.get("path") or "")):
        key = _path_key(n.get("path") or "")
        if last is not None and key.startswith(last):
            continue
        kept.append(n)
        last = key
    return kept


def _under_any(path: str, keys: List[str]) -> bool:
    k = _path_key(path)
    return any(k.startswith(p) for p in keys)


def _size(n: Node) -> int:
    return int(n.get("size") or 0)


def category_totals(roots: List[Node]) -> Tuple[int, Dict[str, int]]:
    """(total, by_category). Nested roots count under their own category and are subtracted
    from the containing root's category, so sum(by_category) == total. A nested_in label not
    matching any root is treated as top-level."""
    by_label = {r.get("label"): r for r in roots}
    total = 0
    by_cat: Dict[str, int] = {}
    for r in roots:
        cat = r.get("category") or "other"
        size = _size(r)
        by_cat[cat] = by_cat.get(cat, 0) + size
        parent = by_label.get(r.get("nested_in")) if r.get("nested_in") else None
        if parent is not None and parent is not r:
            pcat = parent.get("category") or "other"
            by_cat[pcat] = by_cat.get(pcat, 0) - size
        else:
            total += size
    return total, by_cat


def _top_entry(n: Node, app: Optional[dict]) -> dict:
    return {"path": n.get("path"), "name": n.get("name"), "size": _size(n),
            "flags": list(n.get("flags") or []), "safety": n.get("safety"),
            "reason": n.get("reason"), "app": app.get("name") if app else None,
            "kind": n.get("kind")}


def frees_text(action: dict, frees: int) -> str:
    """"177 GB" for exact frees, "up to ~132 GB (estimate)" for estimated ones."""
    if action.get("frees_estimated"):
        return f"up to ~{human(frees)} (estimate)"
    return human(frees)


def action_reason(node: Node, action: dict, frees: int) -> str:
    d = node.get("details") or {}
    repo = d.get("repo") or _base(d.get("worktree")) or _base((node.get("path") or "").rsplit("/", 1)[0])
    label = action.get("label") or action.get("id") or "action"
    aid = action.get("id")
    amount = frees_text(action, frees)
    if aid == "git_delete_temp_packs":
        text = f"Git temp files from failed fetches in {repo} — {amount} can be deleted with '{label}'"
    elif aid == "git_worktree_prune":
        text = f"Missing git worktrees of {repo} — clean up with '{label}'"
    elif aid == "git_gc":
        text = f"Duplicate packs / loose objects in {repo} — '{label}' frees {amount}"
    else:
        text = f"{label} — frees {amount}"
    if action.get("blocked"):
        text += " — blocked: " + str(action["blocked"])
    return text


def _action_entries(node: Node, app: Optional[dict]) -> Tuple[Dict[str, int], List[dict]]:
    """({"exact","estimated","blocked"} bytes, top-candidate entries) for a node's actions.

    Only unblocked actions with exact frees count toward the reclaimable total; estimated ones
    (git gc) and blocked ones are reported separately. Each bucket is capped at the node size."""
    cap = _size(node) or None
    sums = {"exact": 0, "estimated": 0, "blocked": 0}
    entries = []
    for a in node.get("actions") or ():
        if not isinstance(a, dict):
            continue
        frees = int(a.get("frees") or 0)
        if frees <= 0:
            continue
        if cap is not None:
            frees = min(frees, cap)
        bucket = "blocked" if a.get("blocked") else "estimated" if a.get("frees_estimated") else "exact"
        sums[bucket] += frees
        entries.append({"path": node.get("path"), "name": node.get("name"), "size": frees,
                        "flags": list(node.get("flags") or []), "safety": a.get("safety") or "caution",
                        "reason": action_reason(node, a, frees), "app": app.get("name") if app else None,
                        "kind": node.get("kind"), "action": a, "blocked": a.get("blocked"),
                        "estimated": bool(a.get("frees_estimated"))})
    if cap is not None:
        sums = {k: min(v, cap) for k, v in sums.items()}
    return sums, entries


def by_kind_totals(kind_nodes: List[Node], specials: List[dict]) -> Dict[str, int]:
    """Bytes per detector kind, maximal nodes only (tree nodes + scanner specials)."""
    groups: Dict[str, List[Node]] = {}
    for n in list(kind_nodes) + [s for s in specials if isinstance(s, dict)]:
        k = n.get("kind")
        if k and n.get("path"):
            groups.setdefault(k, []).append(n)
    out = {}
    for k, nodes in groups.items():
        seen: Dict[str, Node] = {}
        for n in nodes:  # same path from the tree and from specials: keep the larger size
            p = _path_key(n["path"])
            if p not in seen or _size(n) > _size(seen[p]):
                seen[p] = n
        out[k] = sum(_size(n) for n in dedupe_by_prefix(list(seen.values())))
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _group_sizes(roots: List[Node], specials: List[dict]) -> Dict[str, int]:
    """Bytes per disk-segment group: root category groups, minus nested roots, with detector kinds
    (VMs, personal data) moved to their own groups."""
    by_label = {r.get("label"): r for r in roots}
    nested_keys = {_path_key(r["path"]) for r in roots if r.get("nested_in") in by_label and r.get("path")}
    seg: Dict[str, int] = {}

    def add(g: str, v: int) -> None:
        seg[g] = seg.get(g, 0) + v

    group_at: Dict[str, str] = {}  # tree path key -> group (for specials deeper than the tree)
    for r in roots:
        g0 = CATEGORY_GROUP.get(r.get("category") or "other", "other")
        add(g0, _size(r))
        parent = by_label.get(r.get("nested_in")) if r.get("nested_in") else None
        if parent is not None and parent is not r:
            add(CATEGORY_GROUP.get(parent.get("category") or "other", "other"), -_size(r))
        movable = r.get("category") != "docker"
        stack = [(r, g0, True)]
        while stack:
            node, g, is_root = stack.pop()
            if node.get("synthetic") or node.get("link_root"):
                continue
            key = _path_key(node.get("path") or "")
            if not is_root and key in nested_keys:
                continue  # accounted by the nested root itself
            kg = KIND_GROUP.get(node.get("kind") or "") if movable else None
            if kg and kg != g:
                add(kg, _size(node))
                add(g, -_size(node))
                g = kg
            group_at.setdefault(key, g)
            for c in node.get("children") or ():
                stack.append((c, g, False))
    # scanner specials below the scanned depth: move them from their nearest scanned ancestor
    moved: Dict[str, str] = {}
    for sp in sorted((s for s in specials if isinstance(s, dict) and s.get("path")),
                     key=lambda s: _path_key(s["path"])):
        kg = KIND_GROUP.get(sp.get("kind") or "")
        key = _path_key(sp["path"])
        if not kg or key in group_at:
            continue
        cur = None
        for mk, mg in moved.items():
            if key.startswith(mk):
                cur = mg
        if cur is None:
            p = key.rstrip("/")
            while p and cur is None:
                p = p.rsplit("/", 1)[0]
                cur = group_at.get(p + "/")
        if cur is None or cur == kg or cur == "docker":
            continue
        add(kg, _size(sp))
        add(cur, -_size(sp))
        moved[key] = kg
    return seg


def disk_summary(disk: Optional[dict], total: int, roots: List[Node],
                 specials: List[dict]) -> Tuple[Optional[int], List[dict], List[str]]:
    """(unaccounted, disk_segments, unaccounted_notes) from result["disk"] (disk.overview())."""
    if not isinstance(disk, dict) or not disk.get("container") or not disk.get("data_volume"):
        return None, [], []
    container = disk.get("container") or {}
    data_used = int((disk.get("data_volume") or {}).get("used") or 0)
    unaccounted = max(0, data_used - total)
    groups = _group_sizes(roots, specials)
    scanned = sum(max(0, groups.get(k, 0)) for k, _ in SEGMENTS)
    # allocated sizes can exceed the volume's usage (APFS clones): scale down to fit the volume
    scale = data_used / scanned if data_used and scanned > data_used else 1.0
    vols = [v for v in disk.get("volumes") or [] if isinstance(v, dict) and v.get("role") != "Data"]
    segs: List[dict] = []
    for key, label in SEGMENTS:
        if key == "system_volumes":
            size = sum(int(v.get("used") or 0) for v in vols)
            seg = {"key": key, "label": label, "size": size,
                   "items": [{"name": v.get("name"), "role": v.get("role"), "size": int(v.get("used") or 0)}
                             for v in vols]}
        elif key == "unaccounted":
            seg = {"key": key, "label": label, "size": unaccounted}
        elif key == "free":
            seg = {"key": key, "label": label, "size": max(0, int(container.get("free") or 0))}
        else:
            seg = {"key": key, "label": label, "size": int(max(0, groups.get(key, 0)) * scale)}
        segs.append(seg)

    notes: List[str] = []
    others = [u for u in disk.get("users") or [] if isinstance(u, dict) and not u.get("readable")]
    if others:
        notes.append("Other users' home folders can't be measured without admin: "
                     + ", ".join(str(u.get("name")) for u in others))
    snaps = [s for s in disk.get("snapshots") or [] if isinstance(s, dict)]
    if snaps:
        kinds = sorted({s.get("kind") or "other" for s in snaps})
        label = {"os_update": "macOS update", "time_machine": "Time Machine"}
        notes.append("%s (%s) keep deleted/changed files until macOS removes them"
                     % (_plural(len(snaps), "local APFS snapshot"),
                        ", ".join(label.get(k, k) for k in kinds)))
    if disk.get("purgeable"):
        notes.append("%s is purgeable (macOS frees it automatically when space is needed)"
                     % human(disk["purgeable"]))
    notes.append("System files outside the scanned folders (/private/var, /usr, ...) and APFS metadata")
    return unaccounted, segs, notes


# --------------------------------------------------------------------------- entry point

def annotate(result: dict, stale_days: int = 90, now: Optional[float] = None) -> None:
    """Set flags/safety/reason/in_use on every node and fill ``result["summary"]`` (in place)."""
    now = time.time() if now is None else float(now)
    cutoff = now - stale_days * DAY
    roots: List[Node] = result.get("roots") or []
    # Paths of nested roots -> (category, name): reached via the parent root, the subtree
    # takes the nested root's category so both views annotate it the same way.
    nested = {_path_key(r["path"]): (r.get("category") or "other", r.get("name") or "")
              for r in roots if r.get("nested_in") and r.get("path")}

    candidates: List[Node] = []
    flagged: Dict[str, List[Node]] = {f: [] for f in FLAG_ORDER}
    eff_apps: Dict[int, Optional[dict]] = {}
    action_nodes: Dict[str, Tuple[Node, Optional[dict]]] = {}
    action_seen: set = set()
    kind_nodes: List[Node] = []
    cand_bit = 1 << len(FLAG_ORDER)

    for root in roots:
        cat0 = root.get("category") or "other"
        if cat0 == "dev_cache" and (root.get("path") or "").rstrip("/") in INSTALL_ROOTS:
            cat0 = "installed"
        ctx0 = _base(root.get("path")) or root.get("name") or ""
        # stack item: node, depth, effective app, category, ctx name, covered bitmask,
        #             effective kind, node carrying it, depth-1 ancestor name
        stack: List[tuple] = [(root, 0, None, cat0, ctx0, 0, None, None, "")]
        while stack:
            node, depth, inherited, cat, ctx, covered, kind, knode, top = stack.pop()
            if depth == 1:
                top = node.get("name") or ""
            if node.get("link_root"):
                node["flags"], node["safety"], node["reason"], node["in_use"] = \
                    assess(node, depth, None, cat, ctx, cutoff, now)
                continue
            app = node.get("app") or inherited
            real = depth > 0 and not node.get("synthetic")
            if real:
                sw = nested.get(_path_key(node.get("path") or ""))
                if sw is not None:
                    cat, ctx = sw
            own_kind = None if node.get("synthetic") else node.get("kind")
            if own_kind:
                kind_nodes.append(node)
                # repos/build dirs inside caches (Cargo git db, Bazel externals) stay cache
                if not (own_kind in CODE_KINDS and cat in REGENERABLE_CATEGORIES):
                    kind, knode = own_kind, node
            flags, safety, reason, in_use = assess(node, depth, app, cat, ctx, cutoff, now,
                                                   kind, knode, top)
            node["flags"], node["safety"], node["reason"] = flags, safety, reason
            node["in_use"] = in_use

            if real:
                for i, f in enumerate(FLAG_ORDER):
                    if f in flags and not covered & (1 << i):
                        flagged[f].append(node)
                        covered |= 1 << i
                if node.get("actions"):
                    p = node.get("path") or ""
                    action_seen.add(_path_key(p))
                    if not covered & cand_bit and _path_key(p) not in action_nodes:
                        action_nodes[_path_key(p)] = (node, app)
                if not covered & cand_bit and is_candidate(node, depth, flags, safety, kind, knode,
                                                           cat, top):
                    candidates.append(node)
                    eff_apps[id(node)] = app
                    covered |= cand_bit
            for child in node.get("children") or ():
                stack.append((child, depth + 1, app, cat, ctx, covered, kind, knode, top))

    # git repos whose .git folder is deeper than the scanned tree
    for repo in result.get("git_repos") or []:
        if not isinstance(repo, dict) or not repo.get("actions"):
            continue
        gd = repo.get("git_dir") or ((repo.get("path") or "").rstrip("/") + "/.git")
        if _path_key(gd) in action_seen:
            continue
        stub = {"path": gd, "name": _base(gd), "size": int(repo.get("size") or 0), "kind": "git_dir",
                "details": repo, "actions": repo["actions"], "flags": []}
        action_nodes[_path_key(gd)] = (stub, None)

    total, by_category = category_totals(roots)
    maximal = dedupe_by_prefix(candidates)
    cand_keys = [_path_key(n.get("path") or "") for n in maximal]
    trash_reclaimable = sum(_size(n) for n in maximal)
    entries = [_top_entry(n, eff_apps.get(id(n))) for n in maximal]
    act_sums = {"exact": 0, "estimated": 0, "blocked": 0}
    action_count = 0
    for key, (node, app) in sorted(action_nodes.items()):
        if _under_any(node.get("path") or "", cand_keys):
            continue  # already counted as part of a trash candidate
        sums, acts = _action_entries(node, app)
        for k, v in sums.items():
            act_sums[k] += v
        action_count += sum(1 for a in acts if not a["blocked"])
        entries.extend(acts)
    actions_reclaimable = act_sums["exact"]
    entries.sort(key=lambda e: e["size"], reverse=True)

    specials = [s for s in result.get("special") or [] if isinstance(s, dict)]
    unaccounted, segments, notes = disk_summary(result.get("disk"), total, roots, specials)

    result["summary"] = {
        "total": total,
        "reclaimable": trash_reclaimable + actions_reclaimable,
        "trash_reclaimable": trash_reclaimable,
        "actions_reclaimable": actions_reclaimable,
        "actions_reclaimable_estimated": act_sums["estimated"],
        "actions_blocked_frees": act_sums["blocked"],
        "by_flag": {f: sum(_size(n) for n in dedupe_by_prefix(flagged[f])) for f in FLAG_ORDER},
        "by_category": by_category,
        "by_kind": by_kind_totals(kind_nodes, specials),
        "regenerable_total": sum(v for c, v in by_category.items() if c in REGENERABLE_CATEGORIES),
        "candidate_count": len(maximal),
        "action_count": action_count,
        "stale_days": stale_days,
        "top_candidates": entries[:TOP_N],
        "unaccounted": unaccounted,
        "unaccounted_notes": notes,
        "disk_segments": segments,
    }
