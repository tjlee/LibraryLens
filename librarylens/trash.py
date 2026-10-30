"""Move folders to the macOS Trash (Finder first, so "Put Back" works) and reveal them in Finder.

Pure module: imports nothing from the package.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
import unicodedata
from typing import Optional

ACTIONS_LOG = os.path.expanduser("~/.librarylens/actions.log")
TRASH_DIR = os.path.expanduser("~/.Trash")
FINDER_TIMEOUT_S = 60

_log_lock = threading.Lock()

# The path is passed as argv (never interpolated into the script) -> no AppleScript injection.
_FINDER_SCRIPT = [
    "on run argv",
    'tell application "Finder" to set t to delete (POSIX file (item 1 of argv) as alias)',
    "try",
    "return POSIX path of (t as alias)",
    "on error",
    'return ""',
    "end try",
    "end run",
]


def _norm(p: str) -> str:
    p = os.path.normpath(p)
    return p


def _fold(p: str) -> str:
    """Comparison key for the refusal checks. APFS/HFS+ are case- and normalization-insensitive by
    default, so '~/library' is '~/Library'. Folding only ever makes a refusal check stricter."""
    return unicodedata.normalize("NFD", p).casefold()


def _is_strictly_inside(child: str, parent: str) -> bool:
    if child == parent:
        return False
    parent = parent.rstrip("/") or "/"
    prefix = parent if parent == "/" else parent + "/"
    return child.startswith(prefix)


def _forbidden_targets() -> set:
    home = os.path.expanduser("~")
    out = {"/"}
    for p in (home, os.path.join(home, "Library"), TRASH_DIR):
        out.add(_norm(p))
        out.add(os.path.realpath(p))
    return out


def validate(path: str, allowed_roots: list) -> tuple:
    """Return (target, error). ``target`` is the item to move: the original path with its parent
    directories resolved but the last component kept as-is (so a symlink is moved, not its target)."""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return None, "path must be an absolute path"
    if "\x00" in path:
        return None, "invalid path"
    path = _norm(path)
    if path == "/":
        return None, "refusing to trash /"
    base = os.path.basename(path)
    if base in ("", ".", ".."):
        return None, "invalid path"
    target = os.path.join(os.path.realpath(os.path.dirname(path)), base)
    if not os.path.lexists(target):
        return None, "path does not exist"
    if ".git" in _fold(path).split("/") or ".git" in _fold(target).split("/"):
        # Trashing git internals (a .git dir, its packs) breaks the repo and frees nothing until the
        # Trash is emptied; LibraryLens offers dedicated git cleanup actions instead.
        return None, "refusing to trash a .git folder or its contents; use the git cleanup actions"
    candidates = {_fold(p) for p in (path, target, os.path.realpath(target))}
    if candidates & {_fold(p) for p in _forbidden_targets()}:
        return None, "refusing to trash a protected location"
    roots = []
    for r in allowed_roots or []:
        if isinstance(r, str) and r and os.path.isabs(r):
            roots.append(os.path.realpath(r))
    if not roots:
        return None, "no allowed roots"
    if candidates & {_fold(r) for r in roots}:
        return None, "refusing to trash a scan root itself"
    if not any(_is_strictly_inside(target, r) for r in roots):
        return None, "path is outside the scanned locations"
    if os.path.islink(target):
        real = os.path.realpath(target)
        if not any(_is_strictly_inside(real, r) for r in roots):
            return None, "path is a symlink pointing outside the scanned locations"
    return target, None


def _log(entry: dict) -> None:
    try:
        d = os.path.dirname(ACTIONS_LOG)
        os.makedirs(d, exist_ok=True)
        entry = dict(entry)
        entry.setdefault("ts", datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
        with _log_lock:
            with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _finder_delete(target: str) -> tuple:
    """Return (ok, trashed_to, error)."""
    cmd = ["osascript"]
    for line in _FINDER_SCRIPT:
        cmd += ["-e", line]
    cmd += ["--", target]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=FINDER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, None, "Finder did not respond within %ds" % FINDER_TIMEOUT_S
    except OSError as e:
        return False, None, "osascript failed: %s" % e
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        if "-1743" in err:
            err = "Finder automation not permitted (-1743): " + err
        return False, None, err or ("osascript exit code %d" % cp.returncode)
    if os.path.lexists(target):
        return False, None, "Finder reported success but the item is still there"
    out = (cp.stdout or "").strip()
    return True, (out.rstrip("/") or None), None


def _unique_trash_name(name: str) -> str:
    stamp = time.strftime("%H.%M.%S")
    stem, ext = name, ""
    if not name.startswith("."):
        stem, ext = os.path.splitext(name)
        if len(ext) > 8 or " " in ext:
            stem, ext = name, ""
    candidate = "%s %s%s" % (stem, stamp, ext)
    n = 2
    while os.path.lexists(os.path.join(TRASH_DIR, candidate)):
        candidate = "%s %s %d%s" % (stem, stamp, n, ext)
        n += 1
    return candidate


def _rename_to_trash(target: str) -> tuple:
    """Fallback: rename into ~/.Trash (same device only). Return (ok, trashed_to, error)."""
    try:
        if not os.path.isdir(TRASH_DIR):
            os.makedirs(TRASH_DIR, mode=0o700, exist_ok=True)
        if os.lstat(target).st_dev != os.stat(TRASH_DIR).st_dev:
            return False, None, "item is on a different volume than ~/.Trash"
        name = os.path.basename(target)
        dest = os.path.join(TRASH_DIR, name)
        if os.path.lexists(dest):
            dest = os.path.join(TRASH_DIR, _unique_trash_name(name))
        os.rename(target, dest)
        return True, dest, None
    except OSError as e:
        return False, None, "rename into ~/.Trash failed: %s" % e


def move_to_trash(path: str, allowed_roots: list, size: Optional[int] = None) -> dict:
    """Move ``path`` to the Trash. Returns {"ok", "error", "method", "trashed_to"}."""
    target, err = validate(path, allowed_roots)
    if err:
        _log({"path": path, "size": size, "ok": False, "method": "validate", "error": err})
        return {"ok": False, "error": err, "method": None, "trashed_to": None}

    errors = []
    if not os.path.islink(target):  # Finder's "as alias" would resolve a symlink; rename it instead
        ok, dest, ferr = _finder_delete(target)
        if ok:
            _log({"path": target, "size": size, "ok": True, "method": "finder", "trashed_to": dest})
            return {"ok": True, "error": None, "method": "finder", "trashed_to": dest}
        errors.append("Finder: %s" % ferr)
        _log({"path": target, "size": size, "ok": False, "method": "finder", "error": ferr})

    ok, dest, rerr = _rename_to_trash(target)
    if ok:
        _log({"path": target, "size": size, "ok": True, "method": "rename", "trashed_to": dest})
        return {"ok": True, "error": None, "method": "rename", "trashed_to": dest}
    errors.append(rerr)
    _log({"path": target, "size": size, "ok": False, "method": "rename", "error": rerr})
    return {"ok": False, "error": "; ".join(e for e in errors if e), "method": None, "trashed_to": None}


def reveal_in_finder(path: str) -> None:
    subprocess.run(["open", "-R", path], capture_output=True, timeout=15)
