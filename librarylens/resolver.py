"""Folder -> owning app attribution for LibraryLens.

Public API (see CONTRACT.md):
    annotate(result)          -- sets node["app"] (AppInfo) on attributable nodes, in place
    resolve(path, category)   -- AppInfo dict for a single folder, or None

Resolution order for one folder:
    1. JetBrains / Android Studio product folders inside a vendor dir (IntelliJIdea2026.3 ...)
    2. known_apps.json entries (CLI tools, dev caches, aliases like "Code" -> VS Code)
    3. (Group) Containers: container metadata plist / stripped team-id prefix -> bundle id
    4. bundle-id-looking names (com.x.y[.ShipIt]) -> installed-apps index, mdfind fallback
    5. plain names -> normalised installed app names
Stdlib only; nothing here writes to disk.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

HOME = os.path.expanduser("~")
_KNOWN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_apps.json")

APP_DIRS: List[Tuple[str, int]] = [
    ("/Applications", 2),
    ("/Applications/Utilities", 1),
    (os.path.join(HOME, "Applications"), 2),
    ("/System/Applications", 2),
    (os.path.join(HOME, "Library/Application Support/JetBrains/Toolbox/apps"), 6),
]
EXTRA_BIN_DIRS = [
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin",
    os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".cargo/bin"), os.path.join(HOME, "go/bin"),
    os.path.join(HOME, ".bun/bin"), os.path.join(HOME, ".deno/bin"), os.path.join(HOME, ".volta/bin"),
    os.path.join(HOME, ".npm-global/bin"), os.path.join(HOME, ".nix-profile/bin"),
    "/nix/var/nix/profiles/default/bin", os.path.join(HOME, ".opencode/bin"),
    os.path.join(HOME, ".pyenv/shims"), os.path.join(HOME, ".rbenv/shims"),
    os.path.join(HOME, ".dotnet/tools"), "/usr/local/share/dotnet",
    os.path.join(HOME, "Library/Android/sdk/platform-tools"),
]
# mdfind hits in these places are downloads / updates / trash, not installations
_MDFIND_EXCLUDE = (
    os.path.join(HOME, "Library/Caches") + "/", os.path.join(HOME, ".Trash") + "/",
    os.path.join(HOME, "Downloads") + "/", "/Volumes/", "/private/var/", "/var/", "/tmp/",
)
_STANDARD_PREFIXES = ("/Applications/", os.path.join(HOME, "Applications") + "/", "/System/Applications/")

_TLDS = {"com", "org", "net", "io", "de", "ru", "us", "im", "app", "dev", "co", "uk", "fr", "jp", "cn",
         "me", "tv", "is", "md", "ch", "nl", "se", "eu", "info", "company", "notion", "ai", "cc", "at", "be"}
_BUNDLE_RE = re.compile(r"^[a-z]{2,8}\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+$")
_BUNDLE_SUFFIXES = ("shipit", "helper", "savedstate", "plist", "binarycookies", "agent", "updater")
_TEAM_RE = re.compile(r"^([A-Z0-9]{10})\.(.+)$")
_JB_VER_RE = re.compile(r"^([A-Za-z][A-Za-z-]*?)-?(\d{3,4}(?:\.\d+)+)$")
_INVISIBLE = dict.fromkeys(map(ord, "‎‏​⁨⁩﻿"), None)
_INDEX_TTL = 300.0
_PS_TTL = 5.0


def _norm(s: str) -> str:
    """Normalise a name for matching: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", s.translate(_INVISIBLE).lower())


def _clean(s: Any) -> Optional[str]:
    if not isinstance(s, str):
        return None
    s = s.translate(_INVISIBLE).strip()
    return s or None


# --------------------------------------------------------------------------- known_apps.json

def _load_known() -> Dict[str, Any]:
    try:
        with open(_KNOWN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("entries", [])
    data.setdefault("jetbrains", {})
    data.setdefault("vendors", {})
    data.setdefault("team_ids", {})
    exact: Dict[str, List[dict]] = {}
    globs: List[Tuple[int, Any, dict]] = []
    for e in data["entries"]:
        for pat in (p.lower() for p in e.get("match", [])):
            if any(ch in pat for ch in "*?["):
                globs.append((pat.count("/") + 1, re.compile(fnmatch.translate(pat)), e))
            else:
                exact.setdefault(pat, []).append(e)
        ids = ([e["bundle_id"]] if e.get("bundle_id") else []) + list(e.get("bundle_ids", []))
        e["_ids"] = [i.lower() for i in ids]
        b = e.get("binary")
        e["_bins"] = [b] if isinstance(b, str) else list(b or [])
    data["_exact"], data["_globs"] = exact, globs
    data["_depths"] = sorted({k.count("/") + 1 for k in exact} | {g[0] for g in globs}, reverse=True)
    data["_memo"] = {}
    return data


# --------------------------------------------------------------------------- installed apps index

class _App:
    """One installed .app bundle."""

    __slots__ = ("path", "name", "bundle_id", "version", "names", "jb_prefix", "jb_version", "jb_name", "exe")

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.bundle_id: Optional[str] = None
        self.version: Optional[str] = None
        self.names: set = {_norm(self.name)}
        self.jb_prefix: Optional[str] = None
        self.jb_version: Optional[str] = None
        self.jb_name: Optional[str] = None
        self.exe: Optional[str] = None
        try:
            with open(os.path.join(path, "Contents/Info.plist"), "rb") as f:
                pl = plistlib.load(f)
        except Exception:
            pl = {}
        if isinstance(pl, dict):
            self.bundle_id = _clean(pl.get("CFBundleIdentifier"))
            self.version = _clean(pl.get("CFBundleShortVersionString"))
            self.exe = _clean(pl.get("CFBundleExecutable"))
            for k in ("CFBundleName", "CFBundleDisplayName"):
                v = _clean(pl.get(k))
                if v:
                    self.names.add(_norm(v))
        pi = os.path.join(path, "Contents/Resources/product-info.json")
        if os.path.isfile(pi):
            try:
                with open(pi, "r", encoding="utf-8") as f:
                    info = json.load(f)
                dd = info.get("dataDirectoryName") or ""
                m = _JB_VER_RE.match(dd)
                if m:
                    self.jb_prefix, self.jb_version = m.group(1), m.group(2)
                self.jb_name = _clean(info.get("name"))
                if self.jb_name:
                    self.names.add(_norm(self.jb_name))
                self.version = _clean(info.get("version")) or self.version
            except (OSError, ValueError, AttributeError):
                pass
        self.names.discard("")

    @property
    def is_jetbrains(self) -> bool:
        return bool(self.jb_prefix) or (self.bundle_id or "").lower().startswith("com.jetbrains.")


def _find_apps(root: str, depth: int) -> Iterable[str]:
    """Yield *.app bundles under root, descending at most `depth` levels, never into bundles."""
    try:
        entries = os.listdir(root)
    except OSError:
        return
    for n in entries:
        p = os.path.join(root, n)
        if n.endswith(".app"):
            yield p
        elif depth > 1 and not n.startswith(".") and os.path.isdir(p) and not os.path.islink(p):
            yield from _find_apps(p, depth - 1)


class _Index:
    """Installed-apps index: by bundle id, normalised name and JetBrains product."""

    def __init__(self) -> None:
        self.built_at = time.time()
        self.apps: List[_App] = []
        self.by_bundle: Dict[str, _App] = {}
        self.by_name: Dict[str, _App] = {}
        self.by_path: Dict[str, _App] = {}
        self.jb: Dict[str, List[_App]] = {}  # lower(product prefix) -> apps
        seen: set = set()
        for root, depth in APP_DIRS:
            for p in _find_apps(root, depth):
                rp = os.path.realpath(p)
                if rp in seen:
                    continue
                seen.add(rp)
                self.add(_App(p))

    def add(self, app: _App) -> None:
        self.apps.append(app)
        self.by_path.setdefault(app.path, app)
        if app.bundle_id:
            self.by_bundle.setdefault(app.bundle_id.lower(), app)
        for n in app.names:
            self.by_name.setdefault(n, app)
        if app.jb_prefix:
            self.jb.setdefault(app.jb_prefix.lower(), []).append(app)

    def by_bundle_prefix(self, bid: str) -> Optional[_App]:
        """Longest installed bundle id that equals `bid` or is a dotted/dashed prefix of it."""
        b = bid.lower()
        best: Optional[_App] = None
        best_len = 0
        for key, app in self.by_bundle.items():
            if len(key) > best_len and (b == key or (b.startswith(key) and b[len(key)] in ".-_")):
                best, best_len = app, len(key)
        return best

    def with_bundle_prefix(self, prefix: str) -> List[_App]:
        p = prefix.lower()
        return [a for a in self.apps if (a.bundle_id or "").lower().startswith(p)]

    @property
    def jetbrains_apps(self) -> List[_App]:
        return [a for a in self.apps if a.is_jetbrains]


# --------------------------------------------------------------------------- state

_lock = threading.RLock()
_known: Optional[Dict[str, Any]] = None
_index: Optional[_Index] = None
_cache: Dict[Tuple[str, str], Optional[Tuple[dict, Tuple[str, ...], Tuple[str, ...]]]] = {}
_mdfind_cache: Dict[str, Optional[str]] = {}
_which_cache: Dict[str, bool] = {}
_ps: Tuple[float, set, set] = (0.0, set(), set())


def _state() -> Tuple[Dict[str, Any], _Index]:
    global _known, _index
    with _lock:
        if _known is None:
            _known = _load_known()
        if _index is None or time.time() - _index.built_at > _INDEX_TTL:
            _index = _Index()
            _cache.clear()
            _which_cache.clear()
            _mdfind_cache.clear()
        return _known, _index


def reset() -> None:
    """Drop all caches (installed-apps index, resolutions, mdfind, ps)."""
    global _known, _index, _ps
    with _lock:
        _known, _index, _ps = None, None, (0.0, set(), set())
        _cache.clear()
        _mdfind_cache.clear()
        _which_cache.clear()


def _which(binary: str) -> bool:
    if binary not in _which_cache:
        path = os.pathsep.join([os.environ.get("PATH", "")] + EXTRA_BIN_DIRS)
        _which_cache[binary] = shutil.which(binary, path=path) is not None
    return _which_cache[binary]


def _running_sets(max_age: float = _PS_TTL) -> Tuple[set, set]:
    """(outermost .app paths, executable basenames) of running processes; one `ps` per max_age."""
    global _ps
    with _lock:
        if time.time() - _ps[0] <= max_age:
            return _ps[1], _ps[2]
        apps: set = set()
        bins: set = set()
        try:
            out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            comm = line.strip()
            if not comm:
                continue
            i = comm.find(".app/")
            if i >= 0:
                apps.add(comm[: i + 4])
            bins.add(os.path.basename(comm))
        _ps = (time.time(), apps, bins)
        return apps, bins


def _mdfind(candidates: List[str]) -> Optional[str]:
    """Locate an installed app for any of the bundle ids via Spotlight (cached, 3 s timeout)."""
    todo = [c for c in candidates if c.lower() not in _mdfind_cache and "'" not in c and "\\" not in c]
    if todo:
        query = " || ".join("kMDItemCFBundleIdentifier == '%s'c" % c for c in todo)
        try:
            out = subprocess.run(["mdfind", query], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        hits: List[str] = []
        for line in out.splitlines():
            p = line.strip()
            i = p.find(".app")
            if i < 0 or p.startswith(_MDFIND_EXCLUDE):
                continue
            hits.append(p[: i + 4])  # outermost bundle
        hits.sort(key=lambda p: (not p.startswith(_STANDARD_PREFIXES), len(p)))
        found: Dict[str, str] = {}
        for p in hits:
            app = _App(p)
            if app.bundle_id:
                found.setdefault(app.bundle_id.lower(), p)
        for c in todo:
            _mdfind_cache[c.lower()] = None
        for bid, p in found.items():
            # attribute the hit to every queried id it is a prefix of (or equal to)
            for c in todo:
                cl = c.lower()
                if cl == bid or cl.startswith(bid + ".") or bid.startswith(cl + "."):
                    _mdfind_cache[cl] = _mdfind_cache[cl] or p
    for c in candidates:
        p = _mdfind_cache.get(c.lower())
        if p:
            return p
    return None


# --------------------------------------------------------------------------- AppInfo building

def _info(name: str, kind: str, source: str, *, bundle_id: Optional[str] = None,
          app_path: Optional[str] = None, installed: Optional[bool] = False,
          version: Optional[str] = None, installed_versions: Optional[List[str]] = None) -> dict:
    return {"name": name, "bundle_id": bundle_id, "app_path": app_path, "installed": installed,
            "running": False, "version": version, "installed_versions": list(installed_versions or []),
            "kind": kind, "source": source}


_Res = Tuple[dict, Tuple[str, ...], Tuple[str, ...]]  # (AppInfo w/o running, run app paths, run binaries)


def _from_app(app: _App, source: str, kind: str = "app", name: Optional[str] = None) -> _Res:
    if (app.bundle_id or "").lower().startswith("com.apple.") or app.path.startswith("/System/"):
        kind = "system"
    return (_info(name or app.jb_name or app.name, kind, source, bundle_id=app.bundle_id,
                  app_path=app.path, installed=True), (app.path,), tuple(filter(None, [app.exe])))


def _lookup_bundle(idx: _Index, ids: List[str]) -> Optional[_App]:
    for b in ids:
        a = idx.by_bundle_prefix(b)
        if a:
            return a
    p = _mdfind([b for b in ids if not b.lower().startswith("com.apple.")])
    if p:
        app = _App(p)
        idx.add(app)
        return app
    return None


def _jb_installed(idx: _Index, requires: str = "any") -> List[_App]:
    apps = idx.jetbrains_apps
    if requires == "toolbox":
        apps = [a for a in apps if (a.bundle_id or "").lower() == "com.jetbrains.toolbox"]
    return apps


def _from_entry(e: dict, known: dict, idx: _Index) -> _Res:
    """AppInfo for a known_apps.json entry, checking installation."""
    kind = e.get("kind", "cli")
    if kind == "system":
        return _info(e["name"], "system", "known", installed=True), (), ()
    app = _lookup_bundle(idx, e["_ids"]) if e["_ids"] else None
    if app is None and e.get("app"):
        app = idx.by_name.get(_norm(e["app"]))
    bins = tuple(e["_bins"])
    run_paths: Tuple[str, ...] = ()
    if app is not None:
        info = _info(e["name"], kind, "known", bundle_id=app.bundle_id, app_path=app.path, installed=True)
        return info, (app.path,), bins + tuple(filter(None, [app.exe]))
    installed: Optional[bool] = None
    checks = bool(e["_ids"] or e.get("app") or bins or e.get("installed_paths") or e.get("jetbrains"))
    if checks:
        installed = any(_which(b) for b in bins)
        if not installed:
            installed = any(glob.glob(os.path.expanduser(g)) for g in e.get("installed_paths", []))
        if not installed and e.get("jetbrains"):
            jb = _jb_installed(idx)
            installed = bool(jb)
            run_paths = tuple(a.path for a in jb)
    bid = e.get("bundle_id")
    return _info(e["name"], kind, "known", bundle_id=bid, installed=installed), run_paths, bins


def _match_known(path: str, category: str, known: dict) -> Optional[dict]:
    """known_apps.json entry for a path (exact names first, then globs; longest suffix wins)."""
    memo = known["_memo"]
    key = (path, category)
    if key in memo:
        return memo[key]
    parts = [p.lower() for p in path.rstrip("/").split("/") if p]
    found: Optional[dict] = None
    for n in known["_depths"]:
        if n > len(parts):
            continue
        suffix = "/".join(parts[-n:])
        # entries may share a pattern with different "category" restrictions: first allowed wins
        cands = list(known["_exact"].get(suffix, ()))
        cands += [g[2] for g in known["_globs"] if g[0] == n and g[1].match(suffix)]
        found = next((e for e in cands if not e.get("category") or category in e["category"]), None)
        if found is not None:
            break
    memo[key] = found
    return found


def _known_by_bundle(bid: str, known: dict) -> Optional[dict]:
    b = bid.lower()
    for e in known["entries"]:
        for key in e["_ids"]:
            if b == key or (b.startswith(key) and b[len(key)] in ".-_"):
                return e
    return None


def _strip_bundle_suffix(bid: str) -> str:
    parts = bid.split(".")
    while len(parts) > 3 and parts[-1].lower() in _BUNDLE_SUFFIXES:
        parts.pop()
    return ".".join(parts)


def _looks_like_bundle(name: str) -> bool:
    return bool(_BUNDLE_RE.match(name)) and name.split(".", 1)[0].lower() in _TLDS


def _resolve_bundle(bid: str, source: str, known: dict, idx: _Index) -> _Res:
    """Bundle id -> AppInfo (installed app, known entry, mdfind hit, or not-installed placeholder)."""
    if bid.lower().startswith("com.apple."):
        app = idx.by_bundle_prefix(bid)
        if app:
            return _from_app(app, source, "system")
        return _info("macOS (%s)" % bid[len("com.apple."):], "system", source, bundle_id=bid, installed=True), (), ()
    app = idx.by_bundle_prefix(bid)
    if app:
        return _from_app(app, source)
    e = _known_by_bundle(bid, known)
    if e:
        return _from_entry(e, known, idx)
    base = _strip_bundle_suffix(bid)
    parts = base.split(".")
    cands = [".".join(parts[:i]) for i in range(len(parts), 2, -1)] or [base]
    app = _lookup_bundle(idx, cands)
    if app:
        return _from_app(app, source)
    return _info(base, "app", source, bundle_id=base, installed=False), (), ()


def _container_id(path: str, category: str) -> Tuple[Optional[str], str]:
    """Bundle id of a (group) container from its metadata plist or its folder name."""
    try:
        with open(os.path.join(path, ".com.apple.containermanagerd.metadata.plist"), "rb") as f:
            ident = _clean(plistlib.load(f).get("MCMMetadataIdentifier"))
        if ident:
            return ident, "container_plist"
    except Exception:
        pass
    return os.path.basename(path.rstrip("/")), "bundle_id"


def _strip_group(name: str) -> Tuple[str, Optional[str]]:
    """'group.2BUA8C4S2C.com.1password' -> ('com.1password', '2BUA8C4S2C')."""
    team = None
    for _ in range(3):
        low = name.lower()
        if low.startswith("group."):
            name = name[6:]
        elif low.startswith("groups."):
            name = name[7:]
        else:
            m = _TEAM_RE.match(name)
            if m and not m.group(1).islower():
                team, name = m.group(1), m.group(2)
            else:
                break
    return name, team


def _resolve_jetbrains(name: str, parent: str, known: dict, idx: _Index) -> Optional[_Res]:
    """JetBrains/Android Studio folders: versioned product dirs and named JetBrains subfolders."""
    jbk = known.get("jetbrains", {})
    products: Dict[str, str] = jbk.get("products", {})
    plower = {k.lower(): k for k in products}
    in_jb = parent.lower() in ("jetbrains", ".jetbrains")
    in_google = parent.lower() == "google"
    m = _JB_VER_RE.match(name)
    prefix: Optional[str] = None
    version: Optional[str] = None
    if m and (in_jb or m.group(1).lower() in plower):
        prefix, version = m.group(1), m.group(2)
        if in_google and not prefix.lower().startswith("androidstudio"):
            return None
    elif in_jb:
        low = name.lower()
        alias = jbk.get("aliases", {}).get(low)
        named = low in {k.lower() for k in jbk.get("folders", {})}
        if not named and (low in plower or alias):
            prefix = alias or plower[low]
    if prefix is None:
        if not in_jb:
            return None
        folders = {k.lower(): v for k, v in jbk.get("folders", {}).items()}
        f = folders.get(name.lower(), {"name": jbk.get("default", "JetBrains IDEs (shared)")})
        apps = _jb_installed(idx, f.get("requires", "any"))
        return (_info(f["name"], "app", "jetbrains", bundle_id=apps[0].bundle_id if len(apps) == 1 else None,
                      installed=bool(apps)), tuple(a.path for a in apps), ())
    apps = idx.jb.get(prefix.lower(), [])
    versions = sorted({a.jb_version for a in apps if a.jb_version}, key=_ver_key)
    exact = next((a for a in apps if a.jb_version == version), None) if version else None
    display = products.get(plower.get(prefix.lower(), prefix), prefix)
    if version is None:  # unversioned product dir such as "Pycharm"
        return (_info(display, "app", "jetbrains", bundle_id=apps[0].bundle_id if apps else None,
                      installed=bool(apps), installed_versions=versions), tuple(a.path for a in apps), ())
    ref = exact or (apps[-1] if apps else None)
    return (_info(display, "app", "jetbrains", bundle_id=ref.bundle_id if ref else None,
                  app_path=exact.path if exact else None, installed=exact is not None,
                  version=version, installed_versions=versions),
            (exact.path,) if exact else (), ())


def _ver_key(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _resolve_raw(path: str, category: str) -> Optional[_Res]:
    known, idx = _state()
    path = path.rstrip("/") or "/"
    name = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    if not name:
        return None
    if name.endswith(".app"):  # an application bundle (e.g. /Applications/Slack.app) is the app itself
        app = idx.by_path.get(path)
        if app is None and os.path.isdir(path):
            app = _App(path)
        if app is not None:
            return _from_app(app, "bundle_id")
    r = _resolve_jetbrains(name, parent, known, idx)
    if r:
        return r
    e = _match_known(path, category, known)
    if e:
        return _from_entry(e, known, idx)
    if category in ("container", "group_container") and parent in ("Containers", "Group Containers"):
        cid, src = _container_id(path, category)
        stripped, team = _strip_group(cid)
        e = _match_known(stripped, category, known)
        if e:
            return _from_entry(e, known, idx)
        if _looks_like_bundle(stripped):
            res = _resolve_bundle(stripped, src, known, idx)
            placeholder = not res[0]["installed"] and res[0]["name"] == res[0]["bundle_id"]
            if not placeholder or not team or team not in known["team_ids"]:
                return res
        if team and team in known["team_ids"]:
            t = known["team_ids"][team]
            apps = idx.with_bundle_prefix(t["bundle_prefix"] + ".")
            kind = "system" if t["bundle_prefix"] == "com.apple" else "app"
            return (_info(t["name"], kind, "bundle_id", installed=bool(apps) or kind == "system",
                          bundle_id=apps[0].bundle_id if len(apps) == 1 else None,
                          app_path=apps[0].path if len(apps) == 1 else None),
                    tuple(a.path for a in apps), ())
        return None
    if _looks_like_bundle(name):
        return _resolve_bundle(name, "bundle_id", known, idx)
    if name.lower() in {k.lower() for k, v in known["vendors"].items() if v}:
        v = next(v for k, v in known["vendors"].items() if v and k.lower() == name.lower())
        apps = idx.with_bundle_prefix(v["bundle_prefix"] + ".")
        return (_info(v["name"], "app", "known", installed=bool(apps)), tuple(a.path for a in apps), ())
    n = _norm(name)
    cands = [n] + ([n[: -len("desktop")]] if n.endswith("desktop") and len(n) > 7 else [])
    for c in cands:
        app = idx.by_name.get(c)
        if app:
            return _from_app(app, "name_match")
    return None


def _resolve_cached(path: str, category: str) -> Optional[_Res]:
    key = (path, category)
    with _lock:
        if key not in _cache:
            _cache[key] = _resolve_raw(path, category)
        return _cache[key]


def _finish(res: Optional[_Res], running: Tuple[set, set]) -> Optional[dict]:
    if res is None:
        return None
    info, paths, bins = res
    out = dict(info)
    out["installed_versions"] = list(info["installed_versions"])
    out["running"] = any(p in running[0] for p in paths) or any(b in running[1] for b in bins)
    return out


def resolve(path: str, category: str) -> Optional[dict]:
    """AppInfo for one folder (cached), or None if the owner is unknown."""
    _state()
    return _finish(_resolve_cached(path, category), _running_sets())


# --------------------------------------------------------------------------- annotate

def _descend_mode(node: dict, res: Optional[_Res], known: dict, category: str = "") -> Optional[str]:
    """How depth-2 children of a depth-1 node are attributed: "resolve" (vendor dir, or a
    known_apps entry with "descend": "resolve"), "inherit" (known_apps entry with "descend": true)
    or None (not at all)."""
    name = node.get("name") or os.path.basename(node.get("path", ""))
    if name.lower() in {k.lower() for k in known["vendors"]}:
        return "resolve"
    if res is not None:
        e = _match_known(node.get("path", ""), category, known)
        if e is not None and e.get("descend"):
            return "resolve" if e["descend"] == "resolve" else "inherit"
    return None


def _kids(node: dict) -> List[dict]:
    """Real children: no synthetic "(N smaller items)" nodes, no link stubs to other roots."""
    return [c for c in node.get("children") or []
            if isinstance(c, dict) and not c.get("synthetic") and not c.get("link_root")]


# Roots that are just containers of unrelated folders: never attribute the root itself, so its
# children are resolved one by one instead of inheriting the root's owner.
_NO_ROOT_APP = frozenset({"home", "library_other", "applications"})


def annotate(result: dict) -> None:
    """Set node["app"] on roots, their depth-1 children and depth-2 children of vendor dirs."""
    known, _ = _state()
    running = _running_sets(max_age=0.0)
    for root in result.get("roots") or []:
        cat = root.get("category") or "other"
        if root.get("synthetic"):
            continue
        rres = None if cat in _NO_ROOT_APP else _resolve_cached(root.get("path", ""), cat)
        if rres:
            root["app"] = _finish(rres, running)
        for child in _kids(root):
            # children of an attributable root (~/.gradle, Bazel output, Docker) belong to it
            cres = rres or _resolve_cached(child.get("path", ""), cat)
            if cres:
                child["app"] = _finish(cres, running)
            mode = _descend_mode(child, cres, known, cat)
            if mode is None:
                continue
            for gc in _kids(child):
                gres = (_resolve_cached(gc.get("path", ""), cat) if mode == "resolve" else None) or cres
                if gres:
                    gc["app"] = _finish(gres, running)
