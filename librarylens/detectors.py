"""Detect special folders (git repos, build outputs, VMs, personal data, ...) and describe them.

``annotate(result)`` consumes ``result["special"]`` (scanner v2) plus the tree, sets
``node["kind"] / node["details"] / node["actions"]`` on tree nodes that exist and builds
``result["git_repos"]`` and ``result["projects"]``. Only the special directories themselves are read
(never the whole tree), with a time budget so it stays fast on every rescan.

Imports only ``actions`` from the package (same owner; read-only git helpers + descriptors).
"""
from __future__ import annotations

import os
import plistlib
import re
import stat
import time
from typing import Dict, List, Optional

try:
    from . import actions
except ImportError:  # pragma: no cover - loaded outside the package
    import actions  # type: ignore

USER_DATA_REL = ("Documents", "Pictures", "Movies", "Music", "Desktop",
                 "Library/Mobile Documents", "Library/Mail", "Library/Messages")
DOWNLOADS_REL = "Downloads"

BUILD_NAMES = {"node_modules", "target", "build", "out", ".gradle", ".venv", "venv", "DerivedData",
               ".next", ".nuxt", "dist", ".tox", ".pytest_cache", "__pycache__", "bazel-out"}
VM_SUFFIXES = (".pvm", ".utm", ".vmwarevm", ".vbox")
# Build-output-looking folders (node_modules, dist, ...) that ship with installed software rather than
# being rebuilt by the user: global npm packages (dev_cache roots /opt, /usr/local), app data, hidden
# home folders (~/.vscode/extensions) and app bundles. Same rule as insights.is_installed_build.
INSTALL_ROOTS = ("/opt", "/usr/local")
INSTALLED_CATEGORIES = frozenset({"app_support", "container", "group_container"})
OWN_KEYS = ("kind", "details", "actions")

TIME_BUDGET_S = 4.0           # after this, git details are computed in "light" mode (no loose objects)
LOOSE_EXACT_MAX = 50000       # above this many loose objects, estimate from a sample
LOOSE_SAMPLE_DIRS = 8
MAX_LSOF_REPOS = 3

_HEX2 = re.compile(r"^[0-9a-f]{2}$")


# --------------------------------------------------------------------------------------------------
# tree helpers
# --------------------------------------------------------------------------------------------------

def _index(result: dict) -> Dict[str, List[dict]]:
    """path -> real (non-synthetic, non-stub) nodes with that path; clears our keys on every node."""
    idx: Dict[str, List[dict]] = {}
    stack = list(result.get("roots") or [])
    while stack:
        n = stack.pop()
        if not isinstance(n, dict):
            continue
        for k in OWN_KEYS:
            n.pop(k, None)
        stack.extend(n.get("children") or [])
        if n.get("synthetic") or n.get("link_root"):
            continue
        p = n.get("path")
        if p:
            idx.setdefault(os.path.normpath(p), []).append(n)
    return idx


def _set(idx: Dict[str, List[dict]], path: str, kind: str, details: dict,
         acts: Optional[List[dict]] = None, override: bool = True) -> bool:
    nodes = idx.get(os.path.normpath(path)) or []
    for n in nodes:
        if not override and n.get("kind"):
            continue
        n["kind"] = kind
        n["details"] = details
        if acts:
            n["actions"] = acts
        else:
            n.pop("actions", None)
    return bool(nodes)


def _lmtime(path: str) -> Optional[float]:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


# --------------------------------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------------------------------

def loose_objects(git_dir: str) -> dict:
    """Count/size of loose objects (``objects/??/*``); estimated from a sample when there are many."""
    objdir = os.path.join(git_dir, "objects")
    dirs = []
    try:
        with os.scandir(objdir) as it:
            for e in it:
                if _HEX2.match(e.name) and e.is_dir(follow_symlinks=False):
                    dirs.append(e.name)
    except OSError:
        return {"loose_count": 0, "loose_size": 0, "loose_estimated": False}
    dirs.sort()

    def read(name: str):
        c, s = 0, 0
        try:
            with os.scandir(os.path.join(objdir, name)) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        c += 1
                        s += st.st_blocks * 512
        except OSError:
            pass
        return c, s

    if len(dirs) > 2 * LOOSE_SAMPLE_DIRS:
        step = len(dirs) / float(LOOSE_SAMPLE_DIRS)
        sample = [dirs[int(i * step)] for i in range(LOOSE_SAMPLE_DIRS)]
        sc = ss = 0
        for d in sample:
            c, s = read(d)
            sc += c
            ss += s
        est_count = sc * len(dirs) // len(sample)
        if est_count > LOOSE_EXACT_MAX:  # object names are uniformly distributed hashes
            return {"loose_count": int(est_count), "loose_size": int(ss * len(dirs) // len(sample)),
                    "loose_estimated": True}
    tc = ts = 0
    for d in dirs:
        c, s = read(d)
        tc += c
        ts += s
    return {"loose_count": tc, "loose_size": ts, "loose_estimated": False}


def last_commit(git_dir: str) -> Optional[float]:
    """mtime of the ref HEAD points to (loose ref or its reflog), else logs/HEAD."""
    try:
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8", errors="replace") as f:
            head = f.read(512).strip()
    except OSError:
        return None
    times = []
    if head.startswith("ref:"):
        ref = head[4:].strip()
        if ref.startswith("refs/") and ".." not in ref:
            for p in (os.path.join(git_dir, ref), os.path.join(git_dir, "logs", ref)):
                m = _lmtime(p)
                if m:
                    times.append(m)
    if times:
        return max(times)
    return _lmtime(os.path.join(git_dir, "logs", "HEAD")) or _lmtime(os.path.join(git_dir, "HEAD"))


def git_details(git_dir: str, procs: Optional[List[dict]] = None, light: bool = False,
                check_lsof: bool = False, now: Optional[float] = None) -> dict:
    """Read-only description of a git dir (``.git`` or bare repo) from the filesystem."""
    now = time.time() if now is None else now
    git_dir = os.path.normpath(git_dir)
    bare = os.path.basename(git_dir) != ".git"
    wt = actions.worktree_of(git_dir)
    d = {"repo": os.path.basename(wt) or wt, "git_dir": git_dir, "worktree": wt, "bare": bare}
    pk = actions.list_pack_dir(git_dir, now)
    pk["pack_sizes"] = pk["pack_sizes"][:10]
    d.update(pk)
    if light:
        d.update({"loose_count": None, "loose_size": None, "loose_estimated": True})
    else:
        d.update(loose_objects(git_dir))
    d.update(actions.worktrees(git_dir))
    d.update(actions.repo_activity(git_dir, procs, check_lsof=check_lsof, now=now))
    d["last_commit"] = last_commit(git_dir)
    return d


# --------------------------------------------------------------------------------------------------
# other kinds
# --------------------------------------------------------------------------------------------------

def _read_text(path: str, limit: int = 1 << 20) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def vm_details(path: str) -> dict:
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    d = {"vm_name": stem, "last_used": None, "snapshots": 0, "suspended": False, "type": None}
    low = name.lower()
    d["type"] = {".pvm": "parallels", ".utm": "utm", ".vmwarevm": "vmware", ".vbox": "virtualbox"}.get(
        os.path.splitext(low)[1])
    if not os.path.isdir(path):
        d["last_used"] = _lmtime(path)
        return d
    newest = None
    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                entries.append(e.name)
                try:
                    m = e.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if e.name.startswith(".") or e.name.endswith(".log") or e.name.endswith(".gz"):
                    continue
                newest = m if newest is None or m > newest else newest
    except OSError:
        pass
    d["last_used"] = newest or _lmtime(path)
    snaps = 0
    if d["type"] == "parallels":
        m = re.search(r"<VmName>([^<]*)</VmName>", _read_text(os.path.join(path, "config.pvs")))
        if m and m.group(1).strip():
            d["vm_name"] = m.group(1).strip()
        d["suspended"] = any(n.endswith(".mem") or n == "config.sav" for n in entries)
        for n in entries:
            if n.endswith(".hdd"):
                shots = _read_text(os.path.join(path, n, "DiskDescriptor.xml")).count("<Shot>")
                snaps = max(snaps, shots - 1)
    elif d["type"] == "utm":
        try:
            with open(os.path.join(path, "config.plist"), "rb") as f:
                pl = plistlib.load(f)
            nm = (pl.get("Information") or {}).get("Name") or pl.get("Name")
            if isinstance(nm, str) and nm:
                d["vm_name"] = nm
        except Exception:
            pass
    elif d["type"] == "vmware":
        for n in entries:
            if n.endswith(".vmx"):
                m = re.search(r'^displayName\s*=\s*"([^"]*)"', _read_text(os.path.join(path, n)), re.M)
                if m and m.group(1):
                    d["vm_name"] = m.group(1)
            if n.endswith(".vmsn"):
                snaps += 1
            if n.endswith(".vmss"):
                d["suspended"] = True
    snap_dir = os.path.join(path, "Snapshots")
    try:
        stems = {n.split(".")[0] for n in os.listdir(snap_dir) if not n.startswith(".")}
        snaps = max(snaps, len(stems))
    except OSError:
        pass
    d["snapshots"] = snaps
    return d


def _project_mtime(project: str, idx: Dict[str, List[dict]]) -> Optional[float]:
    """Newest activity in a project, ignoring its build outputs: top-level entries + subtree mtimes of
    tree children (if scanned) + git index/HEAD."""
    newest = None

    def bump(m):
        nonlocal newest
        if m and (newest is None or m > newest):
            newest = m

    try:
        with os.scandir(project) as it:
            for i, e in enumerate(it):
                if i > 5000:
                    break
                n = e.name
                if n in BUILD_NAMES or n.startswith("cmake-build-") or n in (".DS_Store", ".git"):
                    continue
                try:
                    bump(e.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    pass
    except OSError:
        return None
    for p in ("index", "HEAD", "logs/HEAD"):
        bump(_lmtime(os.path.join(project, ".git", p)))
    for node in idx.get(os.path.normpath(project)) or []:
        for c in node.get("children") or []:
            n = c.get("name") or ""
            if c.get("synthetic") or n in BUILD_NAMES or n.startswith("cmake-build-") or n == ".git":
                continue
            bump(c.get("mtime"))
    return newest


def _ios_details(path: str) -> dict:
    d = {"device": None, "last_backup": None}
    try:
        with open(os.path.join(path, "Info.plist"), "rb") as f:
            pl = plistlib.load(f)
        d["device"] = pl.get("Device Name") or pl.get("Display Name")
        lb = pl.get("Last Backup Date")
        if hasattr(lb, "timestamp"):
            d["last_backup"] = lb.timestamp()
    except Exception:
        pass
    if d["last_backup"] is None:
        d["last_backup"] = _lmtime(os.path.join(path, "Manifest.db")) or _lmtime(path)
    return d


# --------------------------------------------------------------------------------------------------
# annotate
# --------------------------------------------------------------------------------------------------

def _installed_software(path: str, root: Optional[dict], home: str) -> bool:
    if root is None:
        return ".app/" in path
    cat = root.get("category") or "other"
    if ".app/" in path or cat in INSTALLED_CATEGORIES or \
            (cat == "dev_cache" and os.path.normpath(root.get("path") or "") in INSTALL_ROOTS):
        return True
    return cat == "home" and path.startswith(home + "/") and path[len(home) + 1:].startswith(".")


def _specials(result: dict, idx: Dict[str, List[dict]]) -> List[dict]:
    seen = {}
    for sp in result.get("special") or []:
        if isinstance(sp, dict) and sp.get("path") and sp.get("kind"):
            seen.setdefault(os.path.normpath(sp["path"]), dict(sp, path=os.path.normpath(sp["path"])))
    # fallback for scans without "special" (iteration 1 caches): .git dirs and VM bundles in the tree
    for p, nodes in idx.items():
        if p in seen:
            continue
        n = nodes[0]
        name = n.get("name") or os.path.basename(p)
        kind = None
        if name == ".git" and n.get("is_dir"):
            kind = "git_dir"
        elif name.lower().endswith(VM_SUFFIXES):
            kind = "vm"
        if kind:
            seen[p] = {"path": p, "kind": kind, "size": n.get("size", 0), "files": n.get("files", 0),
                       "mtime": n.get("mtime"), "root": None}
    # outermost match only
    out = []
    for p in sorted(seen):
        parent, nested = os.path.dirname(p), False
        while parent and parent != os.path.dirname(parent):
            if parent in seen:
                nested = True
                break
            parent = os.path.dirname(parent)
        if not nested:
            out.append(seen[p])
    return out


def annotate(result: dict, now: Optional[float] = None) -> None:
    t0 = time.time()
    now = time.time() if now is None else now
    home = os.path.normpath(result.get("home") or os.path.expanduser("~"))
    idx = _index(result)
    root_paths = {os.path.normpath(r.get("path") or "") for r in result.get("roots") or []}

    for rel in USER_DATA_REL:
        p = os.path.join(home, rel)
        _set(idx, p, "user_data", {"label": rel})
    dl = os.path.join(home, DOWNLOADS_REL)
    _set(idx, dl, "downloads", {"label": DOWNLOADS_REL})

    specials = _specials(result, idx)
    roots_by_label = {r.get("label"): r for r in result.get("roots") or [] if isinstance(r, dict)}
    installed = set()
    for sp in specials:
        if sp["kind"] == "build_output" and _installed_software(sp["path"], roots_by_label.get(sp.get("root")), home):
            installed.add(sp["path"])
    for e in result.get("special") or []:  # marked for insights' by_kind; recomputed on every run
        if isinstance(e, dict):
            if e.get("kind") == "build_output" and os.path.normpath(str(e.get("path") or "")) in installed:
                e["installed"] = True
            else:
                e.pop("installed", None)
    git_specials = [s for s in specials if s["kind"] in ("git_dir", "git_repo")]
    procs = actions.git_processes() if git_specials else []

    git_repos: List[dict] = []
    projects: Dict[str, dict] = {}

    def project(path: str, root) -> dict:
        pr = projects.get(path)
        if pr is None:
            pr = projects[path] = {"path": path, "name": os.path.basename(path), "root": root,
                                   "git_dir": None, "git_size": 0, "last_commit": None,
                                   "build_outputs": [], "build_size": 0, "project_mtime": None}
        return pr

    lsof_budget = MAX_LSOF_REPOS
    for sp in sorted(git_specials, key=lambda s: -(s.get("size") or 0)):
        gd = sp["path"]
        if not actions.is_git_dir(gd):
            continue
        light = time.time() - t0 > TIME_BUDGET_S
        check_lsof = False
        if procs and lsof_budget > 0 and not light:
            pm = _lmtime(os.path.join(gd, "objects", "pack"))
            if pm and now - pm < actions.TEMP_MIN_AGE_S:
                check_lsof = True
                lsof_budget -= 1
        d = git_details(gd, procs, light=light, check_lsof=check_lsof, now=now)
        d["size"] = int(sp.get("size") or 0)
        node_stub = {"path": gd, "kind": "git_dir", "details": d}
        acts = actions.available(node_stub)
        _set(idx, gd, "git_dir", d, acts)
        wt = d["worktree"]
        entry = dict(d, path=wt, root=sp.get("root"), actions=acts)
        git_repos.append(entry)
        if not d["bare"]:
            pr = project(wt, sp.get("root"))
            pr["git_dir"] = gd
            pr["git_size"] = d["size"]
            pr["last_commit"] = d["last_commit"]
            if wt != home and wt not in root_paths:
                _set(idx, wt, "project", {"git_dir": gd, "git_size": d["size"],
                                          "last_commit": d["last_commit"]}, override=False)

    for sp in specials:
        kind = sp["kind"]
        p = sp["path"]
        if kind == "build_output" and p in installed:
            continue  # part of installed software: neither build output nor a project
        if kind == "build_output":
            proj = os.path.dirname(p)
            pm = _project_mtime(proj, idx)
            det = {"project": proj, "project_mtime": pm, "type": os.path.basename(p),
                   "size": int(sp.get("size") or 0)}
            _set(idx, p, "build_output", det)
            pr = project(proj, sp.get("root"))
            pr["build_outputs"].append({"path": p, "name": os.path.basename(p),
                                        "size": int(sp.get("size") or 0)})
            pr["build_size"] += int(sp.get("size") or 0)
            pr["project_mtime"] = pm
        elif kind == "vm":
            _set(idx, p, "vm", vm_details(p))
        elif kind == "disk_image":
            _set(idx, p, "disk_image", {"in_downloads": p.startswith(dl + "/"),
                                        "ext": os.path.splitext(p)[1].lower(), "mtime": sp.get("mtime")})
        elif kind == "photos_library":
            _set(idx, p, "photos_library", {"name": os.path.splitext(os.path.basename(p))[0]})
        elif kind == "ios_backup":
            _set(idx, p, "ios_backup", _ios_details(p))
        elif kind not in ("git_dir", "git_repo"):
            _set(idx, p, kind, {})

    for pr in projects.values():
        pr["size"] = pr["git_size"] + pr["build_size"]
        if pr["project_mtime"] is None:
            pr["project_mtime"] = _project_mtime(pr["path"], idx)
        pr["build_outputs"].sort(key=lambda b: -b["size"])
    git_repos.sort(key=lambda r: -(r.get("size") or 0))
    result["git_repos"] = git_repos
    result["projects"] = sorted(projects.values(), key=lambda p: -p["size"])
    result["detect_duration_s"] = round(time.time() - t0, 3)
