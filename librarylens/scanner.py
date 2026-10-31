"""Filesystem scanner: default roots, parallel allocated-size walk, scan cache.

Pure stdlib, imports nothing from the package. See CONTRACT.md for the Node /
Root / ScanResult dict shapes (iteration 2: roots with ``exclude`` + link stubs,
per-root ``scanned_at``/``duration_s``, ``big_files``, ``special``, partial
rescans via ``only``/``prev``, cancellation).

Sizes are ALLOCATED bytes (``st_blocks * 512``) so sparse files (Docker.raw)
and APFS are reported as they really cost. Symlinks are never followed,
filesystem boundaries are never crossed, and hard-linked files are counted once.
Nothing is ever escalated: unreadable directories just get ``error`` set.

Speed: on macOS directories are listed with ``getattrlistbulk(2)`` (via ctypes),
which returns type/size/mtime/inode/link count for hundreds of entries per
syscall and releases the GIL, so worker threads really run in parallel. Other
platforms / filesystems that don't support it fall back to ``os.scandir`` +
``lstat``. Memory stays bounded: per-file data is never kept, only the nodes
down to ``max_depth``, the special-dir accumulators and the big-file heaps.
"""
from __future__ import annotations

import collections
import copy
import errno
import getpass
import heapq
import json
import os
import queue
import sys
import tempfile
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

_S_IFMT = 0o170000
_S_IFDIR = 0o040000
_S_IFREG = 0o100000
_S_IFLNK = 0o120000

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".librarylens")
CACHE_FILE = os.path.join(CACHE_DIR, "scan.json")


class Cancelled(Exception):
    """Raised by :func:`scan_all` / :func:`scan_path` when the ``cancel`` event was set."""


# (label, path template, category). "~" and "$USER" are expanded.
_CANDIDATES: List[Tuple[str, str, str]] = [
    ("Caches", "~/Library/Caches", "cache"),
    ("Application Support", "~/Library/Application Support", "app_support"),
    ("Containers", "~/Library/Containers", "container"),
    ("Group Containers", "~/Library/Group Containers", "group_container"),
    ("Logs", "~/Library/Logs", "logs"),
    # developer caches / toolchains
    ("~/.cache", "~/.cache", "dev_cache"),
    ("Gradle", "~/.gradle", "dev_cache"),
    ("Maven (~/.m2)", "~/.m2", "dev_cache"),
    ("npm", "~/.npm", "dev_cache"),
    ("pnpm store", "~/.pnpm-store", "dev_cache"),
    ("Yarn", "~/.yarn", "dev_cache"),
    ("pnpm", "~/Library/pnpm", "dev_cache"),
    ("Cargo", "~/.cargo", "dev_cache"),
    ("rustup", "~/.rustup", "dev_cache"),
    ("pyenv", "~/.pyenv", "dev_cache"),
    ("Conda (~/.conda)", "~/.conda", "dev_cache"),
    ("Miniconda", "~/miniconda3", "dev_cache"),
    ("Anaconda", "~/anaconda3", "dev_cache"),
    ("Miniforge", "~/miniforge3", "dev_cache"),
    ("Pipenv virtualenvs", "~/.local/share/virtualenvs", "dev_cache"),
    ("virtualenvs (~/.virtualenvs)", "~/.virtualenvs", "dev_cache"),
    ("uv", "~/.local/share/uv", "dev_cache"),
    ("~/.local/lib", "~/.local/lib", "dev_cache"),
    ("Python user site", "~/Library/Python", "dev_cache"),
    ("Kotlin/Native (~/.konan)", "~/.konan", "dev_cache"),
    ("NuGet", "~/.nuget", "dev_cache"),
    ("Go modules", "~/go/pkg", "dev_cache"),
    ("Ivy (~/.ivy2)", "~/.ivy2", "dev_cache"),
    ("sbt", "~/.sbt", "dev_cache"),
    ("Android (~/.android)", "~/.android", "dev_cache"),
    ("CocoaPods", "~/.cocoapods", "dev_cache"),
    ("Bun", "~/.bun", "dev_cache"),
    ("Deno", "~/.deno", "dev_cache"),
    ("nvm", "~/.nvm", "dev_cache"),
    # Bazel
    ("Bazel output", "/private/var/tmp/_bazel_$USER", "bazel"),
    ("Bazel cache (~/.cache/bazel)", "~/.cache/bazel", "bazel"),
    ("~/.bazel", "~/.bazel", "bazel"),
    ("Bazelisk", "~/.bazelisk", "bazel"),
    # Docker & friends
    ("Docker Desktop", "~/Library/Containers/com.docker.docker", "docker"),
    ("Docker Desktop (group)", "~/Library/Group Containers/group.com.docker", "docker"),
    ("~/.docker", "~/.docker", "docker"),
    ("Colima", "~/.colima", "docker"),
    ("OrbStack", "~/.orbstack", "docker"),
    # iteration 2: whole Data volume
    ("Home", "~", "home"),
    ("Library (other)", "~/Library", "library_other"),
    ("Applications", "/Applications", "applications"),
    ("System Library", "/Library", "system"),
    ("/opt", "/opt", "dev_cache"),
    ("/usr/local", "/usr/local", "dev_cache"),
    ("Shared", "/Users/Shared", "other"),
    ("Swap & sleepimage", "/private/var/vm", "system"),
    ("/private/var/tmp", "/private/var/tmp", "other"),
]

# Roots that exclude every other (outermost) root inside them instead of nesting them.
_EXCLUDE_INNER = ("Home", "Library (other)", "/private/var/tmp")


def _user() -> str:
    try:
        return os.environ.get("USER") or getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments
        return os.path.basename(os.path.expanduser("~"))


def _expand(template: str) -> str:
    return os.path.normpath(os.path.expanduser(template.replace("$USER", _user())))


def _is_within(child: str, parent: str) -> bool:
    return child != parent and child.startswith(parent.rstrip(os.sep) + os.sep)


def _outermost(paths: List[str]) -> List[str]:
    """Drop paths that lie inside another path of the list (keeps order)."""
    reals = [os.path.realpath(p) for p in paths]
    return [p for p, rp in zip(paths, reals)
            if not any(_is_within(rp, other) for other in reals)]


def default_roots() -> list[dict]:
    """Return the existing candidate roots as ``{"label","path","category","exclude"}`` dicts.

    Iteration-1 roots (``~/Library/*``, dev caches, Bazel, Docker) come first, then the
    whole-disk roots: Home (``~``), Library (other) (``~/Library``), /Applications,
    System Library (``/Library``), /opt, /usr/local, Shared, Swap & sleepimage
    (``/private/var/vm``) and /private/var/tmp. Home, Library (other) and
    /private/var/tmp ``exclude`` every other root inside them (outermost ones only),
    so each byte is counted by exactly one non-nested root; the remaining roots have
    an empty ``exclude`` list (e.g. Docker Desktop stays nested in Containers).
    Paths that don't exist (or alias an earlier root) are skipped.
    """
    out: list[dict] = []
    seen_real: set = set()
    home_dev = _dev(os.path.expanduser("~"))
    for label, template, category in _CANDIDATES:
        path = _expand(template)
        if not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        if real in seen_real:
            continue
        seen_real.add(real)
        if os.path.islink(path) and os.path.isdir(real) and _dev(real) == home_dev:
            # e.g. ~/.gradle -> ~/dev/gradle-home: the walker never follows symlinks, and the root
            # that contains the target excludes it, so scan the target itself or nobody counts it.
            path = real
        out.append({"label": label, "path": path, "category": category, "exclude": []})
    for r in out:
        if r["label"] not in _EXCLUDE_INNER:
            continue
        real = os.path.realpath(r["path"])
        inside = [o["path"] for o in out
                  if o is not r and _is_within(os.path.realpath(o["path"]), real)]
        r["exclude"] = _outermost(inside)
    return out


def _dev(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _excludes(excl_reals: List[str], real: str) -> bool:
    return any(real == e or _is_within(real, e) for e in excl_reals)


def _compute_nested(roots: list[dict]) -> List[Optional[str]]:
    """For each root, the label of the closest *other* root containing it (or None).

    A containing root that excludes the path (or one of its ancestors) does not count.
    """
    reals = [os.path.realpath(_expand(r["path"])) for r in roots]
    excl = [[os.path.realpath(_expand(e)) for e in (r.get("exclude") or [])] for r in roots]
    out: List[Optional[str]] = []
    for i, ri in enumerate(reals):
        best, best_len = None, -1
        for j, rj in enumerate(reals):
            if (i != j and _is_within(ri, rj) and len(rj) > best_len
                    and not _excludes(excl[j], ri)):
                best, best_len = roots[j].get("label"), len(rj)
        out.append(best)
    return out


# --------------------------------------------------------------------------- special dirs

_IMG_MIN = 100 * 10 ** 6          # disk images smaller than this are not "special"
_IMG_EXT = frozenset([b".iso", b".dmg", b".img", b".vmdk", b".qcow2", b".vdi"])
_VBOX_DISK_EXT = frozenset([b".vdi", b".vmdk", b".vhd", b".vhdx"])
_VM_SUFFIXES = (b".pvm", b".utm", b".vmwarevm")
_PHOTOS_SUFFIX = b".photoslibrary"

# directory-name rules (checked only on directory entries, outside any special dir)
_R_GIT, _R_BUILD, _R_GRADLE, _R_SIB, _R_VENV, _R_OBJECTS, _R_REFS = range(1, 8)
_DIR_RULES: Dict[bytes, int] = {
    b".git": _R_GIT,
    b"node_modules": _R_BUILD, b".next": _R_BUILD, b".nuxt": _R_BUILD, b".tox": _R_BUILD,
    b"DerivedData": _R_BUILD,
    b".gradle": _R_GRADLE,
    b"target": _R_SIB, b"build": _R_SIB, b"out": _R_SIB, b"dist": _R_SIB,
    b"venv": _R_VENV, b".venv": _R_VENV,
    b"objects": _R_OBJECTS, b"refs": _R_REFS,
}


def _sibling_rule(name: bytes, names: Set[bytes]) -> bool:
    """Is a dir called ``name`` a build output, given the names of its siblings?"""
    if name == b"target":
        return bool(names & {b"pom.xml", b"Cargo.toml", b"build.sbt"})
    if name == b"dist":
        return b"package.json" in names
    if name == b"build":
        return bool(names & {b"CMakeLists.txt", b"package.json"}) or any(
            n.startswith(b"build.gradle") for n in names)
    if name == b"out":
        return bool(names & {b".idea", b".git", b"BUILD.bazel", b"WORKSPACE", b"MODULE.bazel"}) or any(
            n.startswith(b"build.gradle") or n.endswith(b".iml") for n in names)
    return False


def _special_paths() -> Tuple[str, str]:
    """(``~/.gradle``, iOS backup dir) — paths with location-specific special rules."""
    home = os.path.expanduser("~")
    return (os.path.join(home, ".gradle"),
            os.path.join(home, "Library", "Application Support", "MobileSync", "Backup"))


# --------------------------------------------------------------------------- getattrlistbulk


_BUFSZ = 256 * 1024
_HEAD_LEN = 76
# record: length, [returned attrs: common, vol, dir, file, fork], ATTR_CMN_ERROR, NAME (attrreference),
# OBJTYPE, MODTIME (timespec), FILEID, then for dirs MOUNTSTATUS + ALLOCSIZE, for files LINKCOUNT +
# ALLOCSIZE (same 4+8 byte layout).
_HEAD_FMT = "<I8xII4xIiIIqqQIq"
_VREG, _VDIR = 1, 2
_DIRATTR_OK = 0x4 | 0x8     # ATTR_DIR_MOUNTSTATUS | ATTR_DIR_ALLOCSIZE
_FILEATTR_OK = 0x1 | 0x4    # ATTR_FILE_LINKCOUNT | ATTR_FILE_ALLOCSIZE
_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | \
    getattr(os, "O_CLOEXEC", 0)
_UNSUPPORTED = {errno.ENOTSUP, errno.EINVAL, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
                getattr(errno, "ENOSYS", errno.EINVAL)}


def _init_bulk():
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import struct

        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.getattrlistbulk
    except Exception:
        return None

    class _AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                    ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                    ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                    ("forkattr", ctypes.c_uint32)]

    fn.argtypes = [ctypes.c_int, ctypes.POINTER(_AttrList), ctypes.c_void_p, ctypes.c_size_t,
                   ctypes.c_uint64]
    fn.restype = ctypes.c_int
    common = (0x80000000      # ATTR_CMN_RETURNED_ATTRS
              | 0x00000001    # ATTR_CMN_NAME
              | 0x00000008    # ATTR_CMN_OBJTYPE
              | 0x00000400    # ATTR_CMN_MODTIME
              | 0x02000000    # ATTR_CMN_FILEID
              | 0x20000000)   # ATTR_CMN_ERROR
    al = _AttrList(5, 0, common, 0, _DIRATTR_OK, _FILEATTR_OK, 0)
    fsopt_pack_inval_attrs = 0x8

    def new_buffer():
        buf = ctypes.create_string_buffer(_BUFSZ + 1024)  # slack: header unpack never overruns
        return buf, ctypes.addressof(buf), memoryview(buf).cast("B")

    return {"fn": fn, "al": ctypes.pointer(al), "opts": fsopt_pack_inval_attrs,
            "errno": ctypes.get_errno, "new_buffer": new_buffer,
            "unpack": struct.Struct(_HEAD_FMT).unpack_from}


_BULK = _init_bulk()
_USE_BULK = _BULK is not None   # tests flip this to exercise the scandir fallback


# --------------------------------------------------------------------------- walk


def _errmsg(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return "permission"
    msg = getattr(exc, "strerror", None) or exc.__class__.__name__
    return str(msg)[:80]


def _errno_msg(e: int) -> str:
    return _errmsg(OSError(e, os.strerror(e)))


_fsdecode = os.fsdecode


class _Acc:
    """Accumulator for one retained node (a node that appears in the output tree)."""

    __slots__ = ("path", "name", "depth", "is_dir", "size", "files", "mtime", "error",
                 "subdirs", "bigfiles", "stubs", "small_n", "small_size", "small_files",
                 "small_mtime")

    def __init__(self, path: str, name: str, depth: int, is_dir: bool = True) -> None:
        self.path = path
        self.name = name
        self.depth = depth
        self.is_dir = is_dir
        self.size = 0
        self.files = 0
        self.mtime = 0.0
        self.error: Optional[str] = None
        self.subdirs: List[_Acc] = []
        self.bigfiles: List[dict] = []
        self.stubs: List[dict] = []
        self.small_n = 0
        self.small_size = 0
        self.small_files = 0
        self.small_mtime = 0.0


class _Spec:
    """Exact-size accumulator of one special directory (or file)."""

    __slots__ = ("path", "kind", "size", "files", "mtime", "root")

    def __init__(self, path: str, kind: str, root: str, size: int = 0, files: int = 0,
                 mtime: float = 0.0) -> None:
        self.path = path
        self.kind = kind
        self.root = root
        self.size = size
        self.files = files
        self.mtime = mtime

    def as_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "size": self.size, "files": self.files,
                "mtime": self.mtime, "root": self.root}


class _RootCtx:
    """Per-root walk state shared by all work items of that root."""

    __slots__ = ("label", "dev", "seen", "exclude", "home_gradle", "ios_parent", "big",
                 "specials", "pending", "t_end")

    def __init__(self, label: str, dev: int, seen: set) -> None:
        self.label = label
        self.dev = dev
        self.seen = seen
        self.exclude: Dict[bytes, Dict[bytes, Optional[str]]] = {}  # parent -> {name: link label}
        self.home_gradle = b""
        self.ios_parent = b""
        self.big: List[Tuple[int, str, float]] = []   # min-heap (size, path, mtime)
        self.specials: List[_Spec] = []
        self.pending = 1
        self.t_end = 0.0


class _Pass:
    """Totals of one directory listing (direct entries only)."""

    __slots__ = ("size", "files", "mtime", "sm_n", "sm_size", "sm_files", "sm_mtime", "bigs",
                 "dirs", "rare", "err")

    def __init__(self) -> None:
        self.size = 0
        self.files = 0
        self.mtime = 0.0
        self.sm_n = 0
        self.sm_size = 0
        self.sm_files = 0
        self.sm_mtime = 0.0
        self.bigs: List[dict] = []                         # kept file nodes (>= min_size)
        self.dirs: List[Tuple[bytes, float, int]] = []    # (name, mtime, own alloc)
        self.rare: List[Tuple[bytes, int, float]] = []    # files >= rare_min: (name, alloc, mtime)
        self.err: Optional[str] = None


# work item: (dir path bytes, depth, accumulator, dir-is-the-acc's-own-dir, root ctx, special acc)
_Item = Tuple[bytes, int, _Acc, bool, _RootCtx, Optional[_Spec]]


class _Walker:
    """Work-sharing parallel directory walker.

    Each worker walks its subtree depth-first with a local stack and hands the
    shallowest pending directories to the shared queue whenever it runs low, so
    one huge subtree (e.g. a monorepo) is spread across all workers.
    """

    def __init__(self, max_depth: int, min_size: int, workers: int,
                 big_file_min: int = 500 * 10 ** 6, big_files_limit: int = 200,
                 cancel: Optional[threading.Event] = None, detect: bool = True) -> None:
        self.max_depth = max_depth
        self.min_size = min_size
        self.workers = max(1, int(workers))
        self.big_file_min = max(1, int(big_file_min))
        self.big_limit = max(0, int(big_files_limit))
        self.rare_min = min(self.big_file_min, _IMG_MIN) if detect else self.big_file_min
        self.detect = detect
        self.cancel = cancel
        self.cancelled = False
        self.q: "queue.Queue[Optional[_Item]]" = queue.Queue()
        self.lock = threading.Lock()
        self.current = b""
        self.n_files = 0
        self.n_dirs = 0
        self.use_bulk = _USE_BULK and _BULK is not None

    # -- setup ---------------------------------------------------------------

    def add_root(self, path: str, seen: set, label: str = "",
                 exclude: Optional[Dict[str, Optional[str]]] = None) -> Tuple[_Acc, Optional[_RootCtx]]:
        """Stat the root and enqueue it; returns its accumulator and walk context.

        ``exclude`` maps paths to skip -> label of the root that covers them (for stubs).
        """
        path = os.path.abspath(path)
        acc = _Acc(path, os.path.basename(path.rstrip(os.sep)) or path, 0)
        try:
            st = os.lstat(path)
        except OSError as exc:
            acc.error = _errmsg(exc)
            return acc, None
        acc.mtime = st.st_mtime
        fmt = st.st_mode & _S_IFMT
        acc.size = st.st_blocks * 512
        if fmt != _S_IFDIR:
            acc.is_dir = False
            if fmt == _S_IFREG:
                acc.files = 1
            elif fmt == _S_IFLNK:
                try:
                    acc.error = "symlink -> " + os.readlink(path)
                except OSError:
                    acc.error = "symlink"
            return acc, None
        rc = _RootCtx(label or acc.name, st.st_dev, seen)
        gradle, ios = _special_paths()
        rc.home_gradle = os.fsencode(gradle)
        rc.ios_parent = os.fsencode(ios)
        if exclude:
            real_root = os.path.realpath(path)
            for ex, link in exclude.items():
                rex = os.path.realpath(ex)
                if not _is_within(rex, real_root):
                    continue
                walk_path = path.rstrip(os.sep) + rex[len(real_root.rstrip(os.sep)):]
                parent, name = os.path.split(walk_path)
                rc.exclude.setdefault(os.fsencode(parent), {})[os.fsencode(name)] = link
        self.q.put((os.fsencode(path), 0, acc, True, rc, None))
        return acc, rc

    # -- run -----------------------------------------------------------------

    def run(self, progress: Optional[Callable[[str], None]] = None) -> None:
        """Process the queue with worker threads; call ``progress`` <= 5x/s from this thread.

        Raises :class:`Cancelled` (after all workers stopped) if the cancel event got set.
        """
        if self.cancel is not None and self.cancel.is_set():
            self.cancelled = True
        threads = [threading.Thread(target=self._worker, name="librarylens-scan-%d" % i,
                                    daemon=True) for i in range(self.workers)]
        for t in threads:
            t.start()
        q = self.q
        cancel = self.cancel
        last = b""
        while True:
            with q.all_tasks_done:
                if not q.unfinished_tasks:
                    break
                q.all_tasks_done.wait(0.2)
            if cancel is not None and cancel.is_set():
                self.cancelled = True
            cur = self.current
            if progress is not None and cur and cur != last and not self.cancelled:
                last = cur
                try:
                    progress(_fsdecode(cur))
                except Exception:
                    pass
        for _ in threads:
            q.put(None)
        for t in threads:
            t.join()
        if self.cancelled or (cancel is not None and cancel.is_set()):
            self.cancelled = True
            raise Cancelled("scan cancelled")

    def _worker(self) -> None:
        q = self.q
        tl = _BULK["new_buffer"]() if self.use_bulk else None
        while True:
            item = q.get()
            if item is None:
                q.task_done()
                return
            try:
                if not self.cancelled:
                    self._walk(item, tl)
            finally:
                q.task_done()

    def _walk(self, first: _Item, tl) -> None:
        stack: Deque[_Item] = collections.deque([first])
        q = self.q
        nworkers = self.workers
        cancel = self.cancel
        while stack:
            if self.cancelled or (cancel is not None and cancel.is_set()):
                self.cancelled = True
                stack.clear()
                return
            item = stack.pop()
            try:
                self._process(item, stack, tl)
            except Exception as exc:  # never let one dir kill the worker
                self._fail(item, exc)
            # share work: give away the shallowest pending dirs when others may idle
            while len(stack) > 1 and q.qsize() < nworkers:
                q.put(stack.popleft())

    def _fail(self, item: _Item, exc: BaseException) -> None:
        acc, is_node, rc = item[2], item[3], item[4]
        with self.lock:
            if is_node or acc.error is None:
                acc.error = _errmsg(exc)
            rc.pending -= 1
            if rc.pending <= 0:
                rc.t_end = time.time()

    # -- one directory -------------------------------------------------------

    def _process(self, item: _Item, stack: Deque[_Item], tl) -> None:
        path = item[0]
        self.current = path
        keep = item[3] and item[1] < self.max_depth
        p: Optional[_Pass] = None
        if tl is not None:
            fd = os.open(path, _O_DIR)  # OSError -> _fail (error on the node)
            try:
                p = self._list_bulk(fd, path, keep, item[4], tl)
            finally:
                os.close(fd)
        if p is None:
            p = self._list_scandir(path, keep, item[4])
        self._post(item, p, keep, stack)

    def _add_file(self, p: _Pass, base: bytes, name: bytes, isreg: bool, b: int, mt: float,
                  nlink: int, ino: int, keep: bool, rc: _RootCtx) -> None:
        """Generic per-file accounting (scandir fallback and odd bulk records)."""
        if nlink > 1:
            key = (rc.dev, ino)
            with self.lock:
                if key in rc.seen:
                    return
                rc.seen.add(key)
        p.size += b
        if isreg:
            p.files += 1
        if mt > p.mtime:
            p.mtime = mt
        if b >= self.rare_min and isreg:
            p.rare.append((name, b, mt))
        if keep:
            if isreg and b >= self.min_size:
                p.bigs.append(_node(_fsdecode(base + name), _fsdecode(name), b, 1, mt, False))
            else:
                p.sm_n += 1
                p.sm_size += b
                p.sm_files += isreg
                if mt > p.sm_mtime:
                    p.sm_mtime = mt

    def _add_lstat(self, p: _Pass, base: bytes, name: bytes, keep: bool, rc: _RootCtx) -> None:
        try:
            st = os.lstat(base + name)
        except OSError:
            return
        if st.st_dev != rc.dev:
            return
        fmt = st.st_mode & _S_IFMT
        if fmt == _S_IFDIR:
            p.dirs.append((name, st.st_mtime, st.st_blocks * 512))
        else:
            self._add_file(p, base, name, fmt == _S_IFREG, st.st_blocks * 512, st.st_mtime,
                           st.st_nlink, st.st_ino, keep, rc)

    def _list_scandir(self, path: bytes, keep: bool, rc: _RootCtx) -> _Pass:
        p = _Pass()
        base = path if path.endswith(b"/") else path + b"/"
        dev = rc.dev
        it = os.scandir(path)  # OSError -> _fail
        with it:
            n = 0
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as exc:  # readdir failure mid-way: keep partial
                    p.err = _errmsg(exc)
                    break
                n += 1
                if not n & 1023 and self.cancelled:
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # vanished or unreadable entry
                if st.st_dev != dev:
                    continue  # mount point: don't cross devices
                fmt = st.st_mode & _S_IFMT
                if fmt == _S_IFDIR:
                    p.dirs.append((entry.name, st.st_mtime, st.st_blocks * 512))
                else:
                    self._add_file(p, base, entry.name, fmt == _S_IFREG, st.st_blocks * 512,
                                   st.st_mtime, st.st_nlink, st.st_ino, keep, rc)
        return p

    def _list_bulk(self, fd: int, path: bytes, keep: bool, rc: _RootCtx, tl) -> Optional[_Pass]:
        """List one directory with getattrlistbulk; None if the filesystem doesn't support it."""
        _, addr, mv = tl
        bulk = _BULK
        gab, alp, opts, get_errno, unpack = (bulk["fn"], bulk["al"], bulk["opts"], bulk["errno"],
                                             bulk["unpack"])
        lock = self.lock
        seen = rc.seen
        dev = rc.dev
        min_size = self.min_size
        rare_min = self.rare_min
        base = path if path.endswith(b"/") else path + b"/"
        size = files = 0
        mtime = 0.0
        mhi = -(1 << 62)
        sm_n = sm_size = sm_files = 0
        sm_mtime = 0.0
        bigs: List[dict] = []
        dirs: List[Tuple[bytes, float, int]] = []
        rare: List[Tuple[bytes, int, float]] = []
        odd: List[bytes] = []
        err: Optional[str] = None
        first = True
        while not self.cancelled:
            n = gab(fd, alp, addr, _BUFSZ, opts)
            if n <= 0:
                if n < 0:
                    e = get_errno()
                    if e == errno.EINTR:
                        continue
                    if first and e in _UNSUPPORTED:
                        return None
                    err = _errno_msg(e)
                break
            first = False
            off = 0
            for _ in range(n):
                (ln, dattr, fattr, eno, noff, nlen, typ, msec, mnsec, ino, lc, b) = unpack(mv, off)
                if ln < 40:  # malformed record: stop parsing this batch (never seen in practice)
                    err = "bad getattrlistbulk record"
                    break
                o = off
                off += ln
                if eno:
                    continue  # entry vanished / unreadable: like a failed lstat
                if typ == _VDIR:
                    if dattr != _DIRATTR_OK or ln < _HEAD_LEN:
                        s = o + 28 + noff
                        odd.append(mv[s:s + nlen - 1].tobytes())
                    elif not lc & 3:  # DIR_MNTSTATUS_MNTPOINT / _TRIGGER: don't cross mounts
                        s = o + 28 + noff
                        dirs.append((mv[s:s + nlen - 1].tobytes(), msec + mnsec * 1e-9, b))
                    continue
                if fattr != _FILEATTR_OK or ln < _HEAD_LEN:
                    s = o + 28 + noff
                    odd.append(mv[s:s + nlen - 1].tobytes())
                    continue
                if lc > 1:
                    key = (dev, ino)
                    with lock:
                        if key in seen:
                            continue
                        seen.add(key)
                size += b
                isreg = typ == _VREG
                if isreg:
                    files += 1
                if msec >= mhi:
                    mt = msec + mnsec * 1e-9
                    if mt > mtime:
                        mtime = mt
                        mhi = msec
                if b >= rare_min and isreg:
                    s = o + 28 + noff
                    rare.append((mv[s:s + nlen - 1].tobytes(), b, msec + mnsec * 1e-9))
                if keep:
                    mt = msec + mnsec * 1e-9
                    if isreg and b >= min_size:
                        s = o + 28 + noff
                        name = mv[s:s + nlen - 1].tobytes()
                        bigs.append(_node(_fsdecode(base + name), _fsdecode(name), b, 1, mt, False))
                    else:
                        sm_n += 1
                        sm_size += b
                        sm_files += isreg
                        if mt > sm_mtime:
                            sm_mtime = mt
        p = _Pass()
        p.size, p.files, p.mtime, p.err = size, files, mtime, err
        p.sm_n, p.sm_size, p.sm_files, p.sm_mtime = sm_n, sm_size, sm_files, sm_mtime
        p.bigs, p.dirs, p.rare = bigs, dirs, rare
        for name in odd:  # unexpected record layout: fall back to lstat for these entries
            self._add_lstat(p, base, name, keep, rc)
        return p

    def _post(self, item: _Item, p: _Pass, keep: bool, stack: Deque[_Item]) -> None:
        """Classify subdirectories (exclude / special), build child items, commit totals."""
        path, depth, acc, is_node, rc, spec = item
        base = path if path.endswith(b"/") else path + b"/"
        dirs = p.dirs
        label = rc.label
        kinds: Optional[Dict[int, str]] = None
        file_specs: List[_Spec] = []
        self_spec: Optional[_Spec] = None
        big_new: List[Tuple[int, str, float]] = []
        maybe_vbox = False
        for name, b, mt in p.rare:
            if b >= self.big_file_min:
                big_new.append((b, _fsdecode(base + name), mt))
            if spec is None and self.detect and b >= _IMG_MIN:
                dot = name.rfind(b".")
                ext = name[dot:].lower() if dot > 0 else b""
                if ext in _VBOX_DISK_EXT:
                    maybe_vbox = True
                if ext in _IMG_EXT:
                    file_specs.append(_Spec(_fsdecode(base + name), "disk_image", label, b, 1, mt))
                elif ext in _VM_SUFFIXES:
                    file_specs.append(_Spec(_fsdecode(base + name), "vm", label, b, 1, mt))

        if spec is None and self.detect:
            kinds = {}
            sib: List[int] = []
            has_obj = has_refs = False
            if rc.ios_parent == path:
                for i in range(len(dirs)):
                    kinds[i] = "ios_backup"
            else:
                rules = _DIR_RULES
                for i, (name, _mt, _b) in enumerate(dirs):
                    r = rules.get(name)
                    if r is None:
                        if name.endswith(_VM_SUFFIXES):
                            kinds[i] = "vm"
                        elif name.endswith(_PHOTOS_SUFFIX):
                            kinds[i] = "photos_library"
                        elif name.startswith(b"cmake-build-"):
                            kinds[i] = "build_output"
                    elif r == _R_GIT:
                        kinds[i] = "git_dir"
                    elif r == _R_BUILD:
                        kinds[i] = "build_output"
                    elif r == _R_SIB:
                        sib.append(i)
                    elif r == _R_GRADLE:
                        if base + name != rc.home_gradle:
                            kinds[i] = "build_output"
                    elif r == _R_VENV:
                        if os.path.isfile(base + name + b"/pyvenv.cfg"):
                            kinds[i] = "build_output"
                    elif r == _R_OBJECTS:
                        has_obj = True
                    elif r == _R_REFS:
                        has_refs = True
            self_kind = None
            names: Optional[Set[bytes]] = None
            if has_obj and has_refs and os.path.isfile(base + b"HEAD"):
                self_kind = "git_dir"  # bare repository
            elif maybe_vbox:
                names = _listdir_set(path)
                if any(n.endswith(b".vbox") for n in names):
                    self_kind = "vm"
            if self_kind is not None:
                try:
                    st = os.lstat(path)
                    own, own_mt = st.st_blocks * 512, st.st_mtime
                except OSError:
                    own, own_mt = 0, 0.0
                self_spec = _Spec(_fsdecode(path), self_kind, label, own, 0, own_mt)
                kinds = None
                file_specs = []
            elif sib:
                if names is None:
                    names = _listdir_set(path)
                for i in sib:
                    if _sibling_rule(dirs[i][0], names):
                        kinds[i] = "build_output"

        target = self_spec if self_spec is not None else spec
        exm = rc.exclude.get(path) if rc.exclude else None
        new_items: List[_Item] = []
        new_subdirs: List[_Acc] = []
        new_specs: List[_Spec] = []
        stubs: List[dict] = []
        dsize = 0
        dmt = 0.0
        tsize = 0
        tmt = 0.0
        d1 = depth + 1
        for i, (name, mt, b) in enumerate(dirs):
            if exm is not None and name in exm:
                if keep:
                    stub = _node(_fsdecode(base + name), _fsdecode(name), 0, 0, 0.0, True)
                    stub["link_root"] = exm[name]
                    stubs.append(stub)
                continue
            cpath = base + name
            cspec = target
            if kinds:
                k = kinds.get(i)
                if k is not None:
                    cspec = _Spec(_fsdecode(cpath), k, label, b, 0, mt)
                    new_specs.append(cspec)
            if cspec is target and target is not None:
                tsize += b
                if mt > tmt:
                    tmt = mt
            if keep:
                child = _Acc(_fsdecode(cpath), _fsdecode(name), d1)
                child.size = b
                child.mtime = mt
                new_subdirs.append(child)
                new_items.append((cpath, d1, child, True, rc, cspec))
            else:
                dsize += b
                if mt > dmt:
                    dmt = mt
                new_items.append((cpath, d1, acc, False, rc, cspec))
        if file_specs:
            new_specs.extend(file_specs)

        with self.lock:
            acc.size += p.size + dsize
            acc.files += p.files
            if p.mtime > acc.mtime:
                acc.mtime = p.mtime
            if dmt > acc.mtime:
                acc.mtime = dmt
            if p.err is not None and (is_node or acc.error is None):
                acc.error = p.err
            if keep:
                acc.subdirs.extend(new_subdirs)
                acc.bigfiles.extend(p.bigs)
                acc.stubs.extend(stubs)
                acc.small_n += p.sm_n
                acc.small_size += p.sm_size
                acc.small_files += p.sm_files
                if p.sm_mtime > acc.small_mtime:
                    acc.small_mtime = p.sm_mtime
            if target is not None:
                target.size += p.size + tsize
                target.files += p.files
                m = p.mtime if p.mtime > tmt else tmt
                if m > target.mtime:
                    target.mtime = m
            if self_spec is not None:
                rc.specials.append(self_spec)
            if new_specs:
                rc.specials.extend(new_specs)
            if big_new and self.big_limit:
                heap = rc.big
                for e in big_new:
                    if len(heap) < self.big_limit:
                        heapq.heappush(heap, e)
                    elif e > heap[0]:
                        heapq.heappushpop(heap, e)
            rc.pending += len(new_items) - 1
            if rc.pending <= 0:
                rc.t_end = time.time()
            self.n_files += p.files
            self.n_dirs += 1
        stack.extend(new_items)


def _listdir_set(path: bytes) -> Set[bytes]:
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _node(path: str, name: str, size: int, files: int, mtime: float, is_dir: bool,
          error: Optional[str] = None) -> dict:
    return {"path": path, "name": name, "size": size, "files": files, "mtime": mtime,
            "is_dir": is_dir, "error": error, "children": []}


def _finalize(root: _Acc, max_depth: int, min_size: int) -> dict:
    """Turn the accumulator tree into Node dicts (iterative post-order)."""
    built: dict = {}
    order: List[_Acc] = []
    todo = [root]
    while todo:
        a = todo.pop()
        order.append(a)
        todo.extend(a.subdirs)
    for a in reversed(order):  # children before parents
        kids = [built.pop(id(c)) for c in a.subdirs]
        size, files, mtime = a.size, a.files, a.mtime
        for k in kids:
            size += k["size"]
            files += k["files"]
            if k["mtime"] > mtime:
                mtime = k["mtime"]
        node = _node(a.path, a.name, size, files, mtime, a.is_dir, a.error)
        if a.is_dir and a.depth < max_depth:
            children = list(a.bigfiles)
            n, s_size, s_files, s_mtime = a.small_n, a.small_size, a.small_files, a.small_mtime
            n_err = 0
            for k in kids:
                if k["size"] >= min_size:
                    children.append(k)
                else:
                    n += 1
                    n_err += k["error"] is not None
                    s_size += k["size"]
                    s_files += k["files"]
                    if k["mtime"] > s_mtime:
                        s_mtime = k["mtime"]
            if n:
                syn = _node(a.path, "(%d smaller item%s)" % (n, "" if n == 1 else "s"),
                            s_size, s_files, s_mtime, False,
                            "%d unreadable" % n_err if n_err else None)
                syn["synthetic"] = True
                children.append(syn)
            children.sort(key=lambda c: c["size"], reverse=True)
            children.extend(sorted(a.stubs, key=lambda c: c["name"]))  # never collapsed
            node["children"] = children
        built[id(a)] = node
    return built[id(root)]


# --------------------------------------------------------------------------- API


def scan_path(path: str, max_depth: int = 3, min_size: int = 1 << 20, workers: int = 4,
              progress: Optional[Callable[[str], None]] = None,
              exclude: Optional[List[str]] = None,
              cancel: Optional[threading.Event] = None) -> dict:
    """Scan one path and return its Node (children kept down to ``max_depth``).

    ``exclude`` paths are skipped (stub children with ``link_root: None``).
    """
    walker = _Walker(max_depth, min_size, workers, cancel=cancel, detect=False, big_files_limit=0)
    acc, _ = walker.add_root(path, set(), exclude={_expand(e): None for e in exclude or []})
    walker.run(progress)
    return _finalize(acc, max_depth, min_size)


def _merge_entries(new: List[dict], old: List[dict], depth_of: Dict[str, int]) -> List[dict]:
    """Dedupe entries by path: freshly scanned beat ``old``, then the deepest (most specific) root."""
    best: Dict[str, Tuple[Tuple[int, int], dict]] = {}
    for fresh, entries in ((1, new), (0, old)):
        for e in entries:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            key = (fresh, depth_of.get(e.get("root") or "", -1))
            cur = best.get(e["path"])
            if cur is None or key > cur[0]:
                best[e["path"]] = (key, e)
    out = [v[1] for v in best.values()]
    out.sort(key=lambda e: (-int(e.get("size") or 0), e["path"]))
    return out


def _drop_inner_specials(entries: List[dict]) -> List[dict]:
    """Outermost-only across roots (e.g. a nested root's own walk vs. its parent's)."""
    paths = set(e["path"] for e in entries)
    out = []
    for e in entries:
        parent = os.path.dirname(e["path"])
        inner = False
        while parent and parent != os.path.dirname(parent):
            if parent in paths:
                inner = True
                break
            parent = os.path.dirname(parent)
        if not inner:
            out.append(e)
    return out


def scan_all(roots: list[dict] | None = None, max_depth: int = 3, workers: int = 8,
             progress: Callable[[str], None] | None = None,
             min_size: int = 1 << 20,
             only: list[str] | None = None,
             prev: dict | None = None,
             cancel: threading.Event | None = None,
             big_file_min: int = 500 * 10 ** 6,
             big_files_limit: int = 200) -> dict:
    """Scan all roots in parallel and return a ScanResult (without ``summary``).

    * Hard links are deduplicated across all non-nested roots; nested roots
      (which are also part of their parent's tree) use their own dedupe set so
      they are complete on their own.
    * ``exclude`` paths of a root are not walked; their parent node gets a
      ``link_root`` stub child (size 0) instead.
    * ``only`` + ``prev``: rescan just those labels, copy the other roots (and
      their ``big_files``/``special`` entries) from ``prev``. Roots missing from
      ``prev`` (or whose path/exclude changed) are scanned too. Without ``prev``
      everything is scanned.
    * ``cancel`` set -> workers stop within ~1 s and :class:`Cancelled` is raised.
    * ``result["big_files"]``: top ``big_files_limit`` regular files >= ``big_file_min``.
    * ``result["special"]``: outermost special dirs/files (git_dir, build_output, vm,
      disk_image, photos_library, ios_backup) with exact subtree size at any depth.
    """
    t0 = time.time()
    if roots is None:
        roots = default_roots()
    roots = [dict(r, path=_expand(r["path"]), label=r.get("label") or os.path.basename(
        _expand(r["path"])) or r["path"], exclude=[_expand(e) for e in r.get("exclude") or []])
        for r in roots]
    nested = _compute_nested(roots)
    label_of_real = {os.path.realpath(r["path"]): r["label"] for r in roots}

    prev_by: Dict[str, dict] = {}
    if prev and only is not None:
        for pr in prev.get("roots") or []:
            if isinstance(pr, dict) and pr.get("label"):
                prev_by[pr["label"]] = pr
    only_set = set(only or [])
    to_scan: List[int] = []
    copied: Dict[int, dict] = {}
    for i, r in enumerate(roots):
        pr = prev_by.get(r["label"])
        if (pr is not None and r["label"] not in only_set and pr.get("path") == r["path"]
                and sorted(pr.get("exclude") or []) == sorted(r["exclude"])):
            copied[i] = pr
        else:
            to_scan.append(i)

    walker = _Walker(max_depth, min_size, workers, big_file_min, big_files_limit, cancel)
    shared_seen: set = set()
    accs: Dict[int, Tuple[_Acc, Optional[_RootCtx]]] = {}
    for i in to_scan:
        r = roots[i]
        excl = {e: label_of_real.get(os.path.realpath(e)) for e in r["exclude"]}
        accs[i] = walker.add_root(r["path"], set() if nested[i] else shared_seen, r["label"], excl)
    walker.run(progress)
    t_done = time.time()

    out_roots = []
    new_big: List[dict] = []
    new_special: List[dict] = []
    for i, r in enumerate(roots):
        if i in copied:
            node = copy.deepcopy(copied[i])
        else:
            acc, rc = accs[i]
            node = _finalize(acc, max_depth, min_size)
            node["scanned_at"] = t0
            end = rc.t_end if rc is not None and rc.t_end else t_done
            node["duration_s"] = round(max(0.0, end - t0), 3)
            if rc is not None:
                for size, p, mt in rc.big:
                    new_big.append({"path": p, "size": size, "mtime": mt, "root": r["label"]})
                new_special.extend(s.as_dict() for s in rc.specials)
        node["label"] = r["label"]
        node["category"] = r.get("category") or "other"
        node["nested_in"] = nested[i]
        node["exclude"] = list(r["exclude"])
        node.setdefault("scanned_at", (prev or {}).get("scanned_at") or t0)
        node.setdefault("duration_s", 0.0)
        out_roots.append(node)

    copied_labels = set(roots[i]["label"] for i in copied)
    depth_of = {r["label"]: len(os.path.realpath(r["path"])) for r in roots}
    old_big = [e for e in (prev or {}).get("big_files") or []
               if isinstance(e, dict) and e.get("root") in copied_labels] if copied else []
    old_special = [e for e in (prev or {}).get("special") or []
                   if isinstance(e, dict) and e.get("root") in copied_labels] if copied else []
    big = _merge_entries(new_big, old_big, depth_of)
    big = [e for e in big if int(e.get("size") or 0) >= big_file_min][:max(0, big_files_limit)]
    special = _drop_inner_specials(_merge_entries(new_special, old_special, depth_of))
    return {"scanned_at": t0, "duration_s": round(time.time() - t0, 3),
            "home": os.path.expanduser("~"), "roots": out_roots,
            "big_files": big, "special": special}


def save_cache(result: dict) -> None:
    """Atomically write ``result`` to ~/.librarylens/scan.json (compact JSON, owner-only).

    Safe to call from several threads at once (the server saves from scan and trash/action threads):
    every call writes its own temp file, so the cache is always one complete result. Which concurrent
    call wins is up to the caller; the server saves again when it swaps in a new result."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(CACHE_FILE) + ".", suffix=".tmp",
                               dir=os.path.dirname(CACHE_FILE))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(result, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_cache() -> dict | None:
    """Return the cached ScanResult, or None if missing/corrupt."""
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("roots"), list):
        return None
    return data


def find_node(result: dict, path: str) -> dict | None:
    """Find the (non-synthetic) Node with exactly ``path`` in a ScanResult."""
    target = os.path.normpath(path)
    roots = result.get("roots") or []
    for r in roots:  # exact root match wins (nested roots are also children)
        if r.get("path") == target:
            return r
    stack = [r for r in roots if _is_within(target, r.get("path", ""))]
    while stack:
        n = stack.pop()
        if n.get("synthetic"):
            continue
        p = n.get("path", "")
        if p == target:
            return n
        if _is_within(target, p):
            stack.extend(n.get("children") or [])
    return None


def human_size(n: float) -> str:
    """Format a byte count like ``1.5 GB`` (decimal units, as Finder does)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "%d B" % n  # pragma: no cover


def _main(argv: List[str]) -> int:
    path = os.path.expanduser(argv[1]) if len(argv) > 1 else _expand("~/Library/Logs")
    top = int(argv[2]) if len(argv) > 2 else 25
    t0 = time.time()
    node = scan_path(path, max_depth=2, workers=8)
    dt = time.time() - t0
    print("%s  %s  %d files  %.2fs  (%.0f files/s)%s" % (
        node["path"], human_size(node["size"]), node["files"], dt,
        node["files"] / dt if dt > 0 else 0,
        "  error=" + node["error"] if node["error"] else ""))
    for c in node["children"][:top]:
        kind = "" if c["is_dir"] or c.get("synthetic") else "  [file]"
        err = "  error=" + c["error"] if c["error"] else ""
        print("  %10s  %8d files  %s%s%s" % (human_size(c["size"]), c["files"], c["name"],
                                              kind, err))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
