"""Allow-listed maintenance actions besides Move to Trash (currently: git cleanup).

Pure module: imports nothing from the package. ``detectors`` uses the read-only git inspection helpers
below and ``available()``; the server calls ``run()``.

Safety principles:
- exactly three actions, selected by id; every check is redone in ``run()`` (descriptors are advisory);
- the target must be a real (non-symlink) git directory strictly inside an allowed root;
- subprocesses use argv lists (never a shell), a sanitised environment and timeouts;
- every attempt is appended as one JSON line to ``ACTIONS_LOG``.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import stat
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

ACTIONS_LOG = os.path.expanduser("~/.librarylens/actions.log")

ACTION_IDS = ("git_delete_temp_packs", "git_worktree_prune", "git_gc")

TEMP_MIN_AGE_S = 10 * 60          # temp pack files younger than this may still be written
LOCK_FRESH_S = 10 * 60            # a *.lock newer than this means a git command is probably running
GC_PID_STALE_S = 12 * 3600        # git itself ignores gc.pid older than 12 h
GC_SPACE_FACTOR = 1.2
GC_TIMEOUT_S = 2 * 3600
PRUNE_TIMEOUT_S = 120
LSOF_TIMEOUT_S = 10
PS_TIMEOUT_S = 5
LSOF_CHUNK = 200

# Strict names of files git writes into objects/pack while receiving/writing a pack. They are only
# renamed to pack-<sha>.* once complete, so leftovers are never referenced by the repository.
GARBAGE_RE = re.compile(
    r"^(?:tmp_(?:pack|idx|rev|bitmap|mtimes)_[A-Za-z0-9]{6}"
    r"|\.tmp-[0-9]+-pack(?:-[0-9a-f]{40}(?:[0-9a-f]{24})?)?(?:\.(?:pack|idx|rev|bitmap|mtimes|keep|promisor))?)\Z")
PACK_RE = re.compile(r"^pack-[0-9a-f]{40}(?:[0-9a-f]{24})?\.(pack|idx|rev|bitmap|mtimes|keep|promisor)\Z")

# git subcommands that write into objects/ (gc-like or transfer-like).
GC_SUBCOMMANDS = {"gc", "repack", "pack-objects", "prune", "prune-packed", "maintenance",
                  "multi-pack-index", "commit-graph"}
FETCH_SUBCOMMANDS = {"fetch", "pull", "clone", "index-pack", "fetch-pack", "unpack-objects",
                     "receive-pack", "remote-http", "remote-https", "remote-ftp", "remote-ftps",
                     "http-fetch", "submodule"}

_log_lock = threading.Lock()


# --------------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------------

def _alloc(st: os.stat_result) -> int:
    return int(getattr(st, "st_blocks", 0) or 0) * 512


def human(n: Optional[float]) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "%.1f TB" % n


def free_bytes(path: str) -> Optional[int]:
    """Free bytes available to the user on the volume holding ``path`` (monkeypatched in tests)."""
    try:
        st = os.statvfs(path)
        return int(st.f_bavail) * int(st.f_frsize)
    except OSError:
        return None


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def _run(argv: List[str], timeout: float) -> Tuple[Optional[int], str, str]:
    """Run a read-only helper; returns (returncode | None on failure/timeout, stdout, stderr)."""
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                            stdin=subprocess.DEVNULL, errors="replace")
        return cp.returncode, cp.stdout or "", cp.stderr or ""
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %ss" % timeout
    except OSError as e:
        return None, "", str(e)


def _within(path: str, parent: str) -> bool:
    """path == parent or inside it (both normalised)."""
    if path == parent:
        return True
    parent = parent.rstrip("/") or "/"
    return path.startswith(parent if parent == "/" else parent + "/")


def _strictly_within(path: str, parent: str) -> bool:
    return path != parent and _within(path, parent)


# --------------------------------------------------------------------------------------------------
# read-only git inspection (shared with detectors)
# --------------------------------------------------------------------------------------------------

def is_git_dir(path: str) -> bool:
    """A real git directory: HEAD file + objects/ and refs/ directories, none of them symlinks."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
        if not stat.S_ISREG(os.lstat(os.path.join(path, "HEAD")).st_mode):
            return False
        for d in ("objects", "refs"):
            if not stat.S_ISDIR(os.lstat(os.path.join(path, d)).st_mode):
                return False
        return True
    except OSError:
        return False


def worktree_of(git_dir: str) -> str:
    """Work tree for ``git -C``: parent of a ``.git`` dir, the directory itself for a bare repo."""
    return os.path.dirname(git_dir) if os.path.basename(git_dir) == ".git" else git_dir


def list_pack_dir(git_dir: str, now: Optional[float] = None) -> dict:
    """Summarise objects/pack: real packs and temp garbage (lstat only, symlinks ignored)."""
    now = time.time() if now is None else now
    out = {"pack_size": 0, "packs": 0, "largest_pack": 0, "pack_sizes": [],
           "garbage_size": 0, "garbage_files": [], "garbage_old_size": 0, "garbage_old_files": 0,
           "garbage_newest_mtime": None, "pack_dir_error": None}
    pack_dir = os.path.join(git_dir, "objects", "pack")
    try:
        it = os.scandir(pack_dir)
    except OSError as e:
        out["pack_dir_error"] = str(e)
        return out
    garbage = []
    with it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            size = _alloc(st)
            name = e.name
            m = PACK_RE.match(name)
            if m:
                out["pack_size"] += size
                if m.group(1) == "pack":
                    out["packs"] += 1
                    out["pack_sizes"].append(size)
                    out["largest_pack"] = max(out["largest_pack"], size)
            elif GARBAGE_RE.match(name):
                garbage.append((name, size, st.st_mtime))
    garbage.sort(key=lambda g: -g[1])
    out["garbage_files"] = [g[0] for g in garbage]
    out["garbage_size"] = sum(g[1] for g in garbage)
    old = [g for g in garbage if now - g[2] > TEMP_MIN_AGE_S]
    out["garbage_old_files"] = len(old)
    out["garbage_old_size"] = sum(g[1] for g in old)
    if garbage:
        out["garbage_newest_mtime"] = max(g[2] for g in garbage)
    out["pack_sizes"].sort(reverse=True)
    # big packs of (almost) the same size are almost certainly the same history fetched again;
    # git gc keeps one copy. Heuristic: >= 100 MB and within 1% of the first pack of the group.
    dup, group_head = 0, None
    for s in out["pack_sizes"]:
        if s < 100 * 10 ** 6:
            break
        if group_head is not None and s >= group_head * 0.99:
            dup += s
        else:
            group_head = s
    out["duplicate_pack_size"] = dup
    return out


def _dir_alloc(path: str, limit: int = 20000) -> int:
    total, n = 0, 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            n += 1
            if n > limit:
                return total
            try:
                total += _alloc(os.lstat(os.path.join(dirpath, name)))
            except OSError:
                pass
    return total


def _out_of_reach(path: str) -> bool:
    """A path that does not look present but may only be out of reach right now: on a /Volumes drive
    that is not mounted, or not checkable at all (permissions, macOS privacy protection)."""
    try:
        os.stat(path)
        return False
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        return True
    parts = path.split("/")
    if len(parts) > 2 and parts[0] == "" and parts[1] == "Volumes":
        return not os.path.ismount(os.path.realpath("/Volumes/" + parts[2]))
    return False


def _unreachable_reason(paths: List[str]) -> str:
    n = len(paths)
    return ("git worktree prune would also drop %d worktree%s on a disconnected drive or an unreadable "
            "location (%s); connect the drive first, or protect %s with `git worktree lock`"
            % (n, "" if n == 1 else "s", ", ".join(paths[:5]) + (", ..." if n > 5 else ""),
               "it" if n == 1 else "them"))


def worktrees(git_dir: str) -> dict:
    """Linked worktrees from ``<git_dir>/worktrees/*``; prunable = git's own rule (gitdir file missing,
    or pointing to a non-existent location) unless the worktree is locked. Worktrees git would prune
    that may only be out of reach (unmounted drive, unreadable) are listed as unreachable instead."""
    base = os.path.join(git_dir, "worktrees")
    out = {"worktrees": 0, "prunable_worktrees": [], "prunable_size": 0, "unreachable_worktrees": []}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        admin = os.path.join(base, name)
        try:
            if not stat.S_ISDIR(os.lstat(admin).st_mode):
                continue
        except OSError:
            continue
        out["worktrees"] += 1
        if os.path.lexists(os.path.join(admin, "locked")):
            continue
        target = None
        try:
            with open(os.path.join(admin, "gitdir"), "r", encoding="utf-8", errors="replace") as f:
                target = f.read().strip()
        except OSError:
            pass
        if target and not os.path.isabs(target):
            target = os.path.normpath(os.path.join(admin, target))
        if not target or not os.path.exists(target):
            wt = os.path.dirname(target) if target else admin
            if target and _out_of_reach(target):
                out["unreachable_worktrees"].append(wt)
                continue
            out["prunable_worktrees"].append(wt)
            out["prunable_size"] += _dir_alloc(admin)
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def gc_pid_running(git_dir: str, now: Optional[float] = None) -> Optional[int]:
    """PID from ``gc.pid`` if that process is alive (and the file is not stale), else None."""
    now = time.time() if now is None else now
    p = os.path.join(git_dir, "gc.pid")
    try:
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or now - st.st_mtime > GC_PID_STALE_S:
            return None
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            first = (f.read(200).split() or [""])[0]
        pid = int(first)
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def fresh_locks(git_dir: str, now: Optional[float] = None) -> List[str]:
    """Fresh ``*.lock`` files in the git dir top level and objects/pack (index.lock excluded: IDEs
    take it constantly for `git status`)."""
    now = time.time() if now is None else now
    found = []
    for d in (git_dir, os.path.join(git_dir, "objects", "pack")):
        try:
            with os.scandir(d) as it:
                for e in it:
                    if not e.name.endswith(".lock") or e.name == "index.lock":
                        continue
                    try:
                        if now - e.stat(follow_symlinks=False).st_mtime < LOCK_FRESH_S:
                            found.append(os.path.join(d, e.name))
                    except OSError:
                        pass
        except OSError:
            pass
    return found


def _parse_git_command(cmd: str) -> Optional[dict]:
    """Recognise a git process from its `ps` command line -> {"sub", "C", "git_dir"} or None."""
    toks = cmd.split()
    for i, t in enumerate(toks):
        b = os.path.basename(t)
        if b == "git":
            j = i + 1
            info = {"sub": None, "C": None, "git_dir": None}
            while j < len(toks):
                a = toks[j]
                if a in ("-C", "-c") and j + 1 < len(toks):
                    if a == "-C":
                        info["C"] = toks[j + 1]
                    j += 2
                    continue
                if a.startswith("--git-dir="):
                    info["git_dir"] = a.split("=", 1)[1]
                    j += 1
                    continue
                if a.startswith("-"):
                    j += 1
                    continue
                info["sub"] = a
                break
            return info if info["sub"] else None
        if b.startswith("git-") and len(b) > 4:
            return {"sub": b[4:], "C": None, "git_dir": None}
        if i >= 1:  # only the executable (first token or so) may be git
            break
    return None


def git_processes(timeout: float = PS_TIMEOUT_S) -> Optional[List[dict]]:
    """Running git processes that write into object stores: [{"pid","sub","type","cmd","cwd"}].
    None if `ps` failed (callers must then assume something may be running)."""
    rc, out, _ = _run(["ps", "-axo", "pid=,command="], timeout)
    if rc != 0:
        return None
    me = os.getpid()
    procs = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == me:
            continue
        info = _parse_git_command(cmd.strip())
        if not info:
            continue
        sub = info["sub"]
        typ = "gc" if sub in GC_SUBCOMMANDS else ("fetch" if sub in FETCH_SUBCOMMANDS else None)
        if not typ:
            continue
        procs.append({"pid": pid, "sub": sub, "type": typ, "cmd": cmd.strip()[:300],
                      "C": info["C"], "git_dir_arg": info["git_dir"], "cwd": None})
    if procs:
        pids = ",".join(str(p["pid"]) for p in procs)
        rc, out, _ = _run(["lsof", "-a", "-d", "cwd", "-p", pids, "-Fpn"], timeout)
        cwd = {}
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                try:
                    cur = int(line[1:])
                except ValueError:
                    cur = None
            elif line.startswith("n") and cur is not None:
                cwd[cur] = line[1:]
        for p in procs:
            p["cwd"] = cwd.get(p["pid"])
    return procs


def _proc_dirs(p: dict) -> List[str]:
    dirs = []
    base = p.get("cwd")
    for arg in (p.get("C"), p.get("git_dir_arg")):
        if arg:
            if not os.path.isabs(arg) and base:
                arg = os.path.join(base, arg)
            if os.path.isabs(arg):
                dirs.append(os.path.normpath(arg))
    if base:
        dirs.append(os.path.normpath(base))
    return dirs


def processes_for_repo(git_dir: str, procs: Optional[List[dict]]) -> List[dict]:
    """Processes whose cwd / -C / --git-dir is inside this repo's work tree or git dir. Processes with
    unknown location are returned too (conservative)."""
    if not procs:
        return []
    gd = os.path.realpath(git_dir)
    wt = os.path.realpath(worktree_of(git_dir))
    hits = []
    for p in procs:
        dirs = _proc_dirs(p)
        if not dirs:
            hits.append(p)
            continue
        for d in dirs:
            rd = os.path.realpath(d)
            if _within(rd, gd) or (wt != gd and _within(rd, wt) and not _nested_repo(rd, wt)):
                hits.append(p)
                break
    return hits


def _nested_repo(d: str, wt: str) -> bool:
    """True if d is inside another repo nested in work tree wt (e.g. a vendored sub-repo)."""
    cur = d
    while _strictly_within(cur, wt):
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(os.path.join(cur, ".git")):
            return True
        cur = os.path.dirname(cur)
    return False


def pack_dir_openers(git_dir: str, timeout: float = LSOF_TIMEOUT_S) -> Optional[List[int]]:
    """PIDs with files in objects/pack open (`lsof -t +d`, non-recursive). None if lsof failed."""
    rc, out, err = _run(["lsof", "-t", "+d", os.path.join(git_dir, "objects", "pack")], timeout)
    if rc is None:
        return None
    if rc != 0 and out.strip() == "":
        return [] if rc == 1 else None
    return sorted({int(x) for x in out.split() if x.isdigit()})


def files_open(paths: List[str], timeout: float = LSOF_TIMEOUT_S) -> Optional[set]:
    """Subset of ``paths`` held open by any process (`lsof -- files`). None if lsof failed."""
    opened = set()
    for i in range(0, len(paths), LSOF_CHUNK):
        chunk = paths[i:i + LSOF_CHUNK]
        by_ino = {}
        for p in chunk:
            try:
                by_ino[os.lstat(p).st_ino] = p
            except OSError:
                pass
        rc, out, err = _run(["lsof", "-F", "pin", "--"] + chunk, timeout)
        if rc is None:
            return None
        if rc != 0 and not out.strip():
            if rc == 1:   # none of the files is open (lsof also warns on stderr for those)
                continue
            return None
        want = set(chunk)
        matched = False
        for line in out.splitlines():
            if line.startswith("n") and line[1:] in want:
                opened.add(line[1:])
                matched = True
            elif line.startswith("i") and line[1:].isdigit() and int(line[1:]) in by_ino:
                opened.add(by_ino[int(line[1:])])
                matched = True
        if not matched and any(l.startswith("p") for l in out.splitlines()):
            opened.update(chunk)  # something is open but we cannot tell which: keep them all
    return opened


def repo_activity(git_dir: str, procs: Optional[List[dict]] = None, check_lsof: bool = False,
                  now: Optional[float] = None) -> dict:
    """{"gc_running","fetch_running","running": [descriptions]} for one repo."""
    now = time.time() if now is None else now
    running = []
    gc_running = False
    fetch_running = False
    pid = gc_pid_running(git_dir, now)
    if pid:
        gc_running = True
        running.append("git gc (pid %d, gc.pid)" % pid)
    for p in processes_for_repo(git_dir, procs):
        if p["type"] == "gc":
            gc_running = True
        else:
            fetch_running = True
        running.append("git %s (pid %d)" % (p["sub"], p["pid"]))
    locks = fresh_locks(git_dir, now)
    if locks:
        fetch_running = True
        running.append("lock file " + ", ".join(os.path.basename(x) for x in locks[:3]))
    if check_lsof and procs:
        openers = pack_dir_openers(git_dir)
        gitpids = {p["pid"]: p for p in procs}
        if openers is None:
            fetch_running = True
            running.append("could not verify objects/pack users (lsof failed)")
        else:
            for op in openers:
                if op in gitpids and not any(op == q["pid"] for q in processes_for_repo(git_dir, procs)):
                    q = gitpids[op]
                    if q["type"] == "gc":
                        gc_running = True
                    else:
                        fetch_running = True
                    running.append("git %s (pid %d) has objects/pack open" % (q["sub"], op))
    if procs is None:
        fetch_running = True
        running.append("could not list processes")
    return {"gc_running": gc_running, "fetch_running": fetch_running, "running": running}


# --------------------------------------------------------------------------------------------------
# descriptors
# --------------------------------------------------------------------------------------------------

def _running_reason(d: dict) -> Optional[str]:
    if d.get("gc_running"):
        return "git gc is running in this repository; wait until it finishes"
    if d.get("fetch_running"):
        return "a git fetch/pack operation is running in this repository; wait until it finishes"
    return None


def gc_space(details: dict, path: str) -> Tuple[int, Optional[int]]:
    need = int((details.get("largest_pack") or 0) * GC_SPACE_FACTOR)
    return need, free_bytes(path)


def _gc_space_reason(need: int, free: Optional[int], details: dict) -> Optional[str]:
    if free is None or free >= need:
        return None
    msg = ("needs at least %s free (1.2 x largest pack %s), only %s free"
           % (human(need), human(details.get("largest_pack")), human(free)))
    if details.get("garbage_size"):
        msg += "; delete the leftover temp pack files first (frees %s)" % human(details["garbage_size"])
    else:
        msg += "; free some space first"
    return msg


def available(node: dict) -> List[dict]:
    """Action descriptors for a node (``git_dir`` kind with git details), else []."""
    if not node or node.get("kind") != "git_dir":
        return []
    d = node.get("details") or {}
    path = d.get("git_dir") or node.get("path")
    if not path:
        return []
    acts = []
    running = _running_reason(d)
    files = d.get("garbage_files") or []
    if files:
        n_old = int(d.get("garbage_old_files") or 0)
        frees = int(d.get("garbage_old_size") or 0)
        blocked = running
        if not blocked and n_old == 0:
            blocked = "the temp files are younger than 10 minutes; a transfer may still be writing them"
        n = n_old or len(files)
        acts.append({
            "id": "git_delete_temp_packs", "path": path,
            "label": "Delete %d leftover fetch temp file%s" % (n, "" if n == 1 else "s"),
            "frees": frees, "safety": "safe", "blocked": blocked, "background": False,
            "confirm": ("Permanently delete %d temporary pack file%s (%s) in %s? They are leftovers of "
                        "interrupted fetches/repacks and are never used by git. Files that are newer "
                        "than 10 minutes or open by a process are skipped. This cannot be undone "
                        "(moving them to the Trash would not free any space)."
                        % (n, "" if n == 1 else "s", human(frees),
                           os.path.join(path, "objects", "pack"))),
        })
    prunable = d.get("prunable_worktrees") or []
    if prunable:
        n = len(prunable)
        unreachable = d.get("unreachable_worktrees") or []
        acts.append({
            "id": "git_worktree_prune", "path": path,
            "label": "Prune %d missing worktree%s" % (n, "" if n == 1 else "s"),
            "frees": int(d.get("prunable_size") or 0), "safety": "safe",
            "blocked": _unreachable_reason(unreachable) if unreachable else None,
            "background": False,
            "confirm": ("Run `git worktree prune -v` in %s? It removes git's bookkeeping for %d worktree%s "
                        "whose directories no longer exist (%s). Existing worktrees are not touched."
                        % (worktree_of(path), n, "" if n == 1 else "s",
                           ", ".join(prunable[:5]) + (", ..." if n > 5 else ""))),
        })
    packs = int(d.get("packs") or 0)
    loose = d.get("loose_count") or 0
    if packs > 1 or loose > 6700:
        need, free = gc_space(d, path)
        blocked = running or _gc_space_reason(need, free, d)
        frees = int(d.get("duplicate_pack_size") or 0) + int((d.get("loose_size") or 0) * 0.5)
        acts.append({
            "id": "git_gc", "path": path,
            "label": "Run git gc (%d packs%s)" % (packs, ", %d loose objects" % loose if loose else ""),
            "frees": frees, "frees_estimated": True, "safety": "caution", "blocked": blocked,
            "background": True, "needs_free": need, "free": free,
            "confirm": ("Run `git gc` in %s? It repacks all objects into one pack and removes duplicates. "
                        "On a repository this size (%s in packs) it can take a long time and use a lot of "
                        "CPU; it needs about %s of free space while it runs. Do not fetch in this "
                        "repository meanwhile." % (worktree_of(path), human(d.get("pack_size")),
                                                   human(need))),
        })
    return acts


# --------------------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------------------

def _log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ACTIONS_LOG), exist_ok=True)
        entry = dict(entry)
        entry.setdefault("ts", datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
        with _log_lock:
            with open(ACTIONS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def validate_git_dir(path: str, allowed_roots: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (real git dir, error). Accepts a ``.git`` dir, a bare repo or a work tree containing a
    ``.git`` directory. Refuses symlinks, linked worktrees and anything not strictly inside a root."""
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        return None, "path must be an absolute path"
    path = os.path.normpath(path)
    roots = []
    for r in allowed_roots or []:
        if isinstance(r, str) and r and os.path.isabs(r) and "\x00" not in r:
            roots.append(os.path.realpath(r))
    if not roots:
        return None, "no allowed roots"
    if not os.path.lexists(path):
        return None, "path does not exist"
    if os.path.islink(path):
        return None, "refusing to follow a symlink"
    real = os.path.realpath(path)
    if not any(_strictly_within(real, r) for r in roots):
        return None, "path is outside the scanned locations"
    cand = real
    if os.path.basename(real) != ".git" and not is_git_dir(real):
        dotgit = os.path.join(real, ".git")
        if os.path.islink(dotgit):
            return None, "refusing to follow a symlinked .git"
        if os.path.isfile(dotgit):
            return None, "this is a linked worktree; run the action on the main repository"
        cand = dotgit
    if os.path.islink(cand):
        return None, "refusing to follow a symlink"
    if not is_git_dir(cand):
        return None, "not a git directory (needs HEAD, objects/, refs/)"
    if not any(_strictly_within(cand, r) for r in roots):
        return None, "path is outside the scanned locations"
    pack = os.path.join(cand, "objects", "pack")
    try:
        if not stat.S_ISDIR(os.lstat(pack).st_mode):
            return None, "objects/pack is not a directory"
    except OSError:
        pass  # a repo without objects/pack is fine for prune/gc; delete reports it
    return cand, None


def _result(ok: bool, error: Optional[str] = None, freed: int = 0, details: Optional[dict] = None) -> dict:
    return {"ok": ok, "error": error, "freed": int(freed or 0), "details": details or {}}


def run(action_id: str, path: str, allowed_roots: List[str],
        on_output: Optional[Callable[[str], None]] = None) -> dict:
    """Run one allow-listed action. Returns {"ok","error","freed","details"}. Never raises."""
    t0 = time.time()
    entry = {"action": action_id, "path": path}
    try:
        if action_id not in ACTION_IDS:
            res = _result(False, "unknown action")
        else:
            git_dir, err = validate_git_dir(path, allowed_roots)
            if err:
                res = _result(False, err)
            else:
                entry["git_dir"] = git_dir
                fn = {"git_delete_temp_packs": _delete_temp_packs, "git_worktree_prune": _worktree_prune,
                      "git_gc": _gc}[action_id]
                if action_id == "git_gc":
                    _log(dict(entry, phase="start"))
                res = fn(git_dir, on_output)
    except Exception as e:  # never let an action crash the server thread
        res = _result(False, "internal error: %s" % e)
    log = dict(entry, ok=res["ok"], error=res["error"], freed=res["freed"],
               duration_s=round(time.time() - t0, 2))
    det = dict(res.get("details") or {})
    for k in ("deleted", "skipped", "output"):
        if isinstance(det.get(k), list) and len(det[k]) > 100:
            det[k] = det[k][:100] + ["... (%d more)" % (len(det[k]) - 100)]
    log["details"] = det
    _log(log)
    return res


def _emit(on_output: Optional[Callable[[str], None]], line: str) -> None:
    if on_output:
        try:
            on_output(line)
        except Exception:
            pass


def _activity_block(git_dir: str) -> Optional[str]:
    procs = git_processes()
    act = repo_activity(git_dir, procs, check_lsof=True)
    if act["gc_running"] or act["fetch_running"]:
        return "%s (%s)" % (_running_reason(act), "; ".join(act["running"][:4]))
    return None


def _delete_temp_packs(git_dir: str, on_output) -> dict:
    pack_dir = os.path.join(git_dir, "objects", "pack")
    try:
        if not stat.S_ISDIR(os.lstat(pack_dir).st_mode):
            return _result(False, "objects/pack is not a directory")
    except OSError as e:
        return _result(False, "cannot access objects/pack: %s" % e)
    blocked = _activity_block(git_dir)
    if blocked:
        return _result(False, blocked)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dfd = os.open(pack_dir, flags)
    except OSError as e:
        return _result(False, "cannot open objects/pack: %s" % e)
    deleted, skipped = [], []
    freed = 0
    try:
        now = time.time()
        cands = {}
        for name in sorted(os.listdir(dfd)):
            if not GARBAGE_RE.match(name):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError as e:
                skipped.append({"name": name, "reason": str(e)})
                continue
            if not stat.S_ISREG(st.st_mode):
                skipped.append({"name": name, "reason": "not a regular file"})
                continue
            if now - st.st_mtime <= TEMP_MIN_AGE_S:
                skipped.append({"name": name, "reason": "modified less than 10 minutes ago"})
                continue
            cands[name] = st
        if not cands:
            return _result(True, None, 0, {"deleted": [], "skipped": skipped, "pack_dir": pack_dir,
                                            "message": "no deletable temp files"})
        paths = [os.path.join(pack_dir, n) for n in cands]
        opened = files_open(paths)
        if opened is None:
            return _result(False, "could not verify that the files are unused (lsof failed)",
                           0, {"skipped": skipped, "pack_dir": pack_dir})
        # last check right before deleting: nothing started meanwhile
        blocked = _activity_block(git_dir)
        if blocked:
            return _result(False, blocked, 0, {"skipped": skipped, "pack_dir": pack_dir})
        for name, st in cands.items():
            if os.path.join(pack_dir, name) in opened:
                skipped.append({"name": name, "reason": "open by a running process"})
                continue
            try:
                st2 = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if (st2.st_ino, st2.st_dev) != (st.st_ino, st.st_dev) or not stat.S_ISREG(st2.st_mode) \
                        or time.time() - st2.st_mtime <= TEMP_MIN_AGE_S:
                    skipped.append({"name": name, "reason": "changed during the check"})
                    continue
                os.unlink(name, dir_fd=dfd)
            except OSError as e:
                skipped.append({"name": name, "reason": str(e)})
                continue
            size = _alloc(st2) if st2.st_nlink <= 1 else 0
            freed += size
            deleted.append(name)
            _emit(on_output, "deleted %s (%s)" % (name, human(size)))
    finally:
        os.close(dfd)
    return _result(True, None, freed, {"deleted": deleted, "skipped": skipped, "pack_dir": pack_dir})


def _worktree_prune(git_dir: str, on_output) -> dict:
    before = worktrees(git_dir)
    if before["unreachable_worktrees"]:
        # git prunes every worktree it cannot find; it has no way to skip these
        return _result(False, _unreachable_reason(before["unreachable_worktrees"]))
    wt = worktree_of(git_dir)
    argv = ["git", "-C", wt, "worktree", "prune", "-v"]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=PRUNE_TIMEOUT_S,
                            env=_git_env(), stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired:
        return _result(False, "git worktree prune timed out")
    except OSError as e:
        return _result(False, "cannot run git: %s" % e)
    lines = [l for l in ((cp.stdout or "") + (cp.stderr or "")).splitlines() if l.strip()]
    for l in lines:
        _emit(on_output, l)
    after = worktrees(git_dir)
    freed = max(0, before["prunable_size"] - after["prunable_size"])
    details = {"output": lines, "pruned": [p for p in before["prunable_worktrees"]
                                           if p not in after["prunable_worktrees"]],
               "worktrees_before": before["worktrees"], "worktrees_after": after["worktrees"]}
    if cp.returncode != 0:
        return _result(False, "git worktree prune failed (exit %d): %s"
                       % (cp.returncode, (lines[-1] if lines else "")), freed, details)
    return _result(True, None, freed, details)


def _objects_size(git_dir: str) -> int:
    """Allocated size of objects/ (bounded walk; used for before/after of gc)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(os.path.join(git_dir, "objects")):
        for n in filenames:
            try:
                total += _alloc(os.lstat(os.path.join(dirpath, n)))
            except OSError:
                pass
    return total


def _gc(git_dir: str, on_output) -> dict:
    blocked = _activity_block(git_dir)
    if blocked:
        return _result(False, blocked)
    d = list_pack_dir(git_dir)
    need, free = gc_space(d, git_dir)
    reason = _gc_space_reason(need, free, d)
    if reason:
        return _result(False, "not enough free space: " + reason, 0, {"needs_free": need, "free": free})
    before = _objects_size(git_dir)
    wt = worktree_of(git_dir)
    argv = ["git", "-C", wt, "gc"]
    _emit(on_output, "$ git -C %s gc" % wt)
    rc, tail, err = _stream(argv, on_output, GC_TIMEOUT_S)
    after = _objects_size(git_dir)
    freed = max(0, before - after)
    details = {"output": tail[-50:], "objects_before": before, "objects_after": after,
               "packs_after": list_pack_dir(git_dir)["packs"]}
    if err:
        return _result(False, err, freed, details)
    if rc != 0:
        return _result(False, "git gc failed (exit %s): %s" % (rc, tail[-1] if tail else ""), freed, details)
    _emit(on_output, "done, freed %s" % human(freed))
    return _result(True, None, freed, details)


def _stream(argv: List[str], on_output, timeout: float) -> Tuple[Optional[int], List[str], Optional[str]]:
    """Run argv, streaming output lines (progress lines ending in \\r are throttled). Uses a pty so git
    prints progress; falls back to pipes. Returns (returncode, tail lines, error)."""
    import select
    master = slave = None
    try:
        import pty
        master, slave = pty.openpty()
        out_fd = slave
    except Exception:
        out_fd = subprocess.PIPE
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out_fd,
                                stderr=out_fd if slave is not None else subprocess.STDOUT,
                                env=_git_env(), start_new_session=True)
    except OSError as e:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        return None, [], "cannot run git: %s" % e
    if slave is not None:
        os.close(slave)
        rfd = master
    else:
        rfd = proc.stdout.fileno()
    deadline = time.time() + timeout
    tail: List[str] = []
    buf = b""
    last_progress = 0.0
    err = None

    def handle(seg: bytes, final: bool) -> None:
        nonlocal last_progress
        text = seg.decode("utf-8", "replace").strip()
        if not text:
            return
        now = time.time()
        if final or now - last_progress >= 0.5:
            last_progress = now
            _emit(on_output, text)
            if tail and not final and tail[-1].split(":")[0] == text.split(":")[0]:
                tail[-1] = text
            else:
                tail.append(text)
            del tail[:-200]

    try:
        while True:
            if time.time() > deadline:
                err = "timed out after %d s" % timeout
                try:
                    os.killpg(proc.pid, 15)
                except OSError:
                    pass
                break
            r, _, _ = select.select([rfd], [], [], 1.0)
            if not r:
                if proc.poll() is not None and slave is None:
                    break
                continue
            try:
                chunk = os.read(rfd, 65536)
            except OSError:  # EIO: pty closed after the child exited
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while True:
                idx = [i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0]
                if not idx:
                    break
                i = min(idx)
                sep = buf[i:i + 1]
                seg, buf = buf[:i], buf[i + 1:]
                handle(seg, sep == b"\n" or b"done" in seg)
        if buf:
            handle(buf, True)
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
            rc = proc.wait()
    finally:
        if master is not None:
            os.close(master)
        elif proc.stdout:
            proc.stdout.close()
    return rc, tail, err
