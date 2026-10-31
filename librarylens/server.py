"""Local HTTP server + JSON API for the LibraryLens browser UI.

Binds 127.0.0.1 only. Every POST needs the per-process token (``X-LL-Token``) that is injected into
index.html; every request must carry a ``Host`` of 127.0.0.1:<port> / localhost:<port> (DNS rebinding).
pipeline / scanner / insights / actions / disk / trash are imported lazily so this module imports
even if some of them are missing (``App(modules=...)`` lets tests inject stubs).

Endpoints:
  GET  /                  UI (token injected)
  GET  /api/scan          current ScanResult ({"status": "none"} before the first scan)
  GET  /api/status        scan status (phase, current path, elapsed, cancelled, ...)
  GET  /api/disk          live disk.overview() (cached 30 s)
  GET  /api/jobs          action jobs, newest first
  POST /api/scan          {"only": [labels]?, "max_depth"?}  start a (partial) scan in the background
  POST /api/scan/cancel   cancel the running scan (previous result is kept)
  POST /api/trash         {"path", "force"?}
  POST /api/reveal        {"path"}
  POST /api/action        {"path", "action", "confirm": true}
"""
from __future__ import annotations

import collections
import gzip
import importlib
import itertools
import json
import os
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TOKEN_PLACEHOLDER = "__LL_TOKEN__"
MAX_BODY = 64 * 1024
PORT_TRIES = 10
DISK_TTL_S = 30.0
JOB_TAIL_LINES = 50
MAX_FINISHED_JOBS = 30
# The allow-list of actions (contract "Actions"); actions.ACTIONS / actions.ACTION_IDS extend it if present.
KNOWN_ACTIONS = ("git_delete_temp_packs", "git_worktree_prune", "git_gc")
GIT_TRASH_MSG = ("this is (inside) a git repository's .git folder — Move to Trash is not offered here; "
                 "use the git cleanup actions instead")


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def _norm(p: str) -> str:
    return os.path.normpath(p)


def _inside_or_equal(child: str, parent: str) -> bool:
    if not parent:
        return False
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _root_covers(root: dict, path: str) -> bool:
    """True if ``path`` is counted in ``root``'s tree (inside it and not in one of its excludes)."""
    rp = root.get("path") or ""
    if not _inside_or_equal(path, rp):
        return False
    for ex in root.get("exclude") or []:
        if isinstance(ex, str) and _inside_or_equal(path, ex):
            return False
    return True


def _abs_path_arg(path: Any) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ApiError(HTTPStatus.BAD_REQUEST, "'path' must be an absolute path")
    return _norm(path)


def _progress_prefix(line: str) -> str:
    return line.split(":", 1)[0] if ":" in line else line


class App:
    """Server state: current ScanResult, scan status, jobs, token. ``modules`` lets tests inject stubs."""

    def __init__(self, result: Optional[dict] = None, modules: Optional[dict] = None,
                 token: Optional[str] = None) -> None:
        self.token = token or secrets.token_urlsafe(32)
        self.result = result
        self.modules = dict(modules or {})
        self.lock = threading.RLock()        # guards self.result (mutation + serialisation)
        self.trash_lock = threading.Lock()   # serialises trash / post-action updates
        self.slock = threading.RLock()       # guards self.status / scan bookkeeping
        self.jobs_lock = threading.Lock()
        self.disk_lock = threading.Lock()
        self.version = 0
        self.result_gen = 0      # bumped whenever self.result is replaced by another result
        self._journal = None     # trash/action changes made while a scan runs (list), else None
        self._json_cache = None  # (version, raw_bytes, gzip_bytes|None)
        self.status = {"running": False, "state": "idle", "current": None, "started_at": None,
                       "finished_at": None, "error": None, "phase": None, "only": None,
                       "cancelled": False, "cancel_requested": False}
        self.scan_thread = None
        self.cancel_event = None
        self.pending_rescan = []     # root labels queued while a scan is running
        self.rescan_log = []         # every rescan request (labels), for diagnostics/tests
        self.jobs = collections.OrderedDict()
        self.job_threads = []
        self._job_ids = itertools.count(1)
        self._disk = None            # (fetched_at, overview)
        self.port = None

    # ---- modules -------------------------------------------------------------------------
    def mod(self, name: str):
        if name in self.modules:
            return self.modules[name]
        pkg = __package__ or ""
        try:
            if pkg:
                return importlib.import_module("." + name, pkg)
            return importlib.import_module(name)
        except Exception as e:  # ImportError or a syntax error in a sibling module
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE,
                           "LibraryLens module '%s' is missing or failed to import: %s: %s"
                           % (name, type(e).__name__, e))

    def load_cache(self) -> None:
        try:
            r = self.mod("scanner").load_cache()
        except Exception as e:
            print("librarylens: could not load scan cache: %s" % e, file=sys.stderr)
            return
        if r:
            with self.lock:
                self.result = r
                self.result_gen += 1
                self.version += 1

    def root_labels(self) -> list:
        with self.lock:
            return [r.get("label") for r in (self.result or {}).get("roots") or [] if r.get("label")]

    # ---- scan ----------------------------------------------------------------------------
    def status_json(self) -> dict:
        with self.slock:
            s = dict(self.status)
            s["pending_rescan"] = list(self.pending_rescan)
        s["has_result"] = self.result is not None
        now = time.time()
        if s["started_at"]:
            s["elapsed"] = round((now if s["running"] else (s["finished_at"] or now)) - s["started_at"], 1)
        else:
            s["elapsed"] = None
        with self.jobs_lock:
            s["jobs_running"] = sum(1 for j in self.jobs.values() if j["state"] == "running")
        return s

    def validate_only(self, only: Any) -> Optional[list]:
        if only is None:
            return None
        if not isinstance(only, list) or not only or not all(isinstance(x, str) and x for x in only):
            raise ApiError(HTTPStatus.BAD_REQUEST, "'only' must be a non-empty list of root labels")
        if self.result is None:
            raise ApiError(HTTPStatus.CONFLICT, "no previous scan to merge into; run a full scan first")
        known = set(self.root_labels())
        unknown = [x for x in only if x not in known]
        if unknown:
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown root label(s): %s" % ", ".join(unknown))
        out = []
        for x in only:
            if x not in out:
                out.append(x)
        return out

    def start_scan(self, max_depth: Optional[int] = None, only: Optional[list] = None) -> None:
        pipeline = self.mod("pipeline")  # resolve up front so a missing module is reported synchronously
        with self.slock:
            if self.status["running"]:
                raise ApiError(HTTPStatus.CONFLICT, "a scan is already running")
            cancel = threading.Event()
            self.cancel_event = cancel
            self.status.update(running=True, state="running", current="Starting…", phase="scan",
                               started_at=time.time(), finished_at=None, error=None,
                               only=list(only) if only else None, cancelled=False, cancel_requested=False)
            t = threading.Thread(target=self._run_scan, args=(pipeline, max_depth, only, cancel),
                                 name="librarylens-scan", daemon=True)
            self.scan_thread = t
        t.start()

    def cancel_scan(self) -> dict:
        with self.slock:
            if not self.status["running"] or self.cancel_event is None:
                raise ApiError(HTTPStatus.CONFLICT, "no scan is running")
            self.cancel_event.set()
            self.status.update(cancel_requested=True, state="cancelling")
            self.pending_rescan = []
        return self.status_json()

    def _progress(self, msg: Any) -> None:
        msg = str(msg)
        # plain item assignment: atomic in CPython, no lock needed on this hot path
        if msg.startswith("phase:"):
            self.status["phase"] = msg[6:] or None
        else:
            self.status["current"] = msg

    def _is_cancel(self, exc: BaseException, cancel: threading.Event) -> bool:
        if type(exc).__name__ == "Cancelled":
            return True
        try:
            c = getattr(self.mod("scanner"), "Cancelled", None)
            if isinstance(c, type) and isinstance(exc, c):
                return True
        except ApiError:
            pass
        return cancel.is_set()

    def _run_scan(self, pipeline, max_depth: Optional[int], only: Optional[list],
                  cancel: threading.Event) -> None:
        state, error, followup = "done", None, []
        try:
            with self.lock:
                # Trash/action threads keep changing self.result while the scan runs: from here on
                # they journal what they do, and it is replayed onto the new result before the swap.
                self._journal = []
                raw = self.result_bytes(False)[0] if only and self.result is not None else None
            # scan_all copies the roots it does not rescan from prev, so it gets a private copy
            prev = json.loads(raw) if raw is not None else None
            kwargs = {"prev": prev, "only": list(only) if only else None, "progress": self._progress,
                      "cancel": cancel}
            if max_depth is not None:
                kwargs["max_depth"] = max_depth
            r = pipeline.run(**kwargs)
            if not isinstance(r, dict) or not isinstance(r.get("roots"), list):
                raise RuntimeError("the scan pipeline returned no result")
            with self.lock:
                changes, self._journal = self._journal or [], None
                if changes and self.result is not None:
                    followup = self._replay(changes, r, self.result)
                    # also saves: the old result was saved after the pipeline saved this one
                    self._reannotate(r)
                self.result = r
                self.result_gen += 1
                self.version += 1
        except Exception as e:
            if self._is_cancel(e, cancel):
                state = "cancelled"
            else:
                traceback.print_exc()
                state, error = "failed", "%s: %s" % (type(e).__name__, e)
        finally:
            with self.lock:
                self._journal = None
            with self.slock:
                self.status.update(running=False, current=None, phase=None, finished_at=time.time(),
                                   state=state, error=error, cancelled=(state == "cancelled"),
                                   cancel_requested=False)
                pending = [] if state == "cancelled" else list(self.pending_rescan)
                # follow-ups of this scan's own replay survive a cancel that came too late to stop it
                pending += [x for x in followup if x not in pending]
                self.pending_rescan = []
            if pending:
                self.request_rescan(pending)

    def request_rescan(self, labels: list) -> None:
        """Rescan ``labels`` in the background now, or right after the running scan finishes."""
        labels = [x for x in labels if x]
        if not labels or self.result is None:
            return
        with self.slock:
            self.rescan_log.append(list(labels))
            if self.status["running"]:
                for x in labels:
                    if x not in self.pending_rescan:
                        self.pending_rescan.append(x)
                return
        try:
            self.start_scan(only=labels)
        except ApiError as e:
            if e.status == HTTPStatus.CONFLICT:  # raced with another scan: queue it
                with self.slock:
                    for x in labels:
                        if x not in self.pending_rescan:
                            self.pending_rescan.append(x)
            else:
                print("librarylens: could not start rescan: %s" % e.message, file=sys.stderr)

    # ---- disk ----------------------------------------------------------------------------
    def disk_overview(self) -> dict:
        with self.disk_lock:
            now = time.time()
            if self._disk and now - self._disk[0] < DISK_TTL_S:
                d = dict(self._disk[1])
                d["age_s"] = round(now - self._disk[0], 1)
                return d
            try:
                d = self.mod("disk").overview()
                if not isinstance(d, dict):
                    raise RuntimeError("disk.overview() returned %s" % type(d).__name__)
            except Exception as e:
                with self.lock:
                    old = (self.result or {}).get("disk")
                if isinstance(old, dict) and not old.get("error"):
                    d = dict(old)
                    d["stale"] = True
                    d["live_error"] = e.message if isinstance(e, ApiError) else "%s: %s" % (type(e).__name__, e)
                    return d
                if isinstance(e, ApiError):
                    raise
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "disk overview failed: %s: %s"
                               % (type(e).__name__, e))
            self._disk = (now, d)
            out = dict(d)
            out["age_s"] = 0.0
            return out

    # ---- result JSON ---------------------------------------------------------------------
    def result_bytes(self, want_gzip: bool) -> tuple:
        with self.lock:
            if self.result is None:
                return json.dumps({"status": "none"}).encode(), False
            c = self._json_cache
            if c is None or c[0] != self.version:
                raw = json.dumps(self.result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                c = [self.version, raw, None]
                self._json_cache = c
        if want_gzip and len(c[1]) > 1024:
            if c[2] is None:
                c[2] = gzip.compress(c[1], compresslevel=5)
            return c[2], True
        return c[1], False

    # ---- tree helpers --------------------------------------------------------------------
    @staticmethod
    def _chain(node: dict, path: str) -> list:
        """Nodes from ``node`` down towards ``path`` (deepest existing ancestor-or-self last)."""
        chain = [node]
        cur = node
        while True:
            nxt = None
            for ch in cur.get("children") or []:
                if ch.get("synthetic") or ch.get("link_root"):
                    continue
                cp = ch.get("path") or ""
                if cp == path or path.startswith(cp.rstrip("/") + "/"):
                    nxt = ch
                    break
            if nxt is None:
                return chain
            chain.append(nxt)
            if nxt.get("path") == path:
                return chain
            cur = nxt

    def _chains(self, r: dict, path: str) -> list:
        return [self._chain(root, path) for root in r.get("roots") or [] if _root_covers(root, path)]

    def _effective_app(self, path: str) -> Optional[dict]:
        best, best_len = None, -1
        for chain in self._chains(self.result, path):
            for n in chain:
                if n.get("app") and len(n.get("path") or "") > best_len:
                    best, best_len = n["app"], len(n["path"])
        return best

    @staticmethod
    def _running_in_subtree(node: dict, app: Optional[dict] = None) -> Optional[dict]:
        """The app of the first in-use node at/below ``node`` (inherited from the nearest node that has
        one, starting from ``app``; ``{}`` if none has), or None if nothing in the subtree is in use."""
        stack = [(node, app)]
        while stack:
            n, inherited = stack.pop()
            a = n.get("app") or inherited
            if n.get("in_use"):
                return a or {}
            stack.extend((c, a) for c in n.get("children") or [])
        return None

    def _remove_from_tree(self, r: dict, path: str, size: int, files: int) -> None:
        for root in r.get("roots") or []:
            self._remove_from_root(root, path, size, files)

    def _remove_from_root(self, root: dict, path: str, size: int, files: int) -> None:
        """Take a trashed ``path`` out of one root's tree. ``size``/``files`` are used when the item has
        no node of its own (deeper than the tree, or collapsed into "(N smaller items)")."""
        rp = root.get("path") or ""
        if not path.startswith(rp.rstrip("/") + "/") or not _root_covers(root, path):
            return
        chain = self._chain(root, path)
        last = chain[-1]
        if last.get("path") == path and len(chain) > 1:
            size, files = int(last.get("size") or 0), int(last.get("files") or 0)
            parent = chain[-2]
            parent["children"] = [c for c in parent.get("children") or [] if c is not last]
            ancestors = chain[:-1]
        else:
            ancestors = chain
            # The item may be collapsed into the "(N smaller items)" child of its parent.
            if (last.get("path") or "") == os.path.dirname(path):
                for c in last.get("children") or []:
                    if c.get("synthetic"):
                        c["size"] = max(0, int(c.get("size") or 0) - size)
                        c["files"] = max(0, int(c.get("files") or 0) - files)
        for a in ancestors:
            a["size"] = max(0, int(a.get("size") or 0) - size)
            a["files"] = max(0, int(a.get("files") or 0) - files)
        for a in ancestors:
            if a.get("children"):
                a["children"].sort(key=lambda c: (bool(c.get("synthetic")), -int(c.get("size") or 0)))

    def _shrink_in_tree(self, path: str, freed: int) -> None:
        """An action freed ``freed`` bytes somewhere at/below ``path``: shrink it and its ancestors."""
        if freed <= 0:
            return
        for chain in self._chains(self.result, path):
            for n in chain:
                n["size"] = max(0, int(n.get("size") or 0) - freed)
            for n in chain:
                if n.get("children"):
                    n["children"].sort(key=lambda c: (bool(c.get("synthetic")), -int(c.get("size") or 0)))

    @staticmethod
    def _prune_lists(r: dict, path: str) -> None:
        """Drop big_files / special / git_repos / projects entries at or under a removed ``path``."""
        for key in ("big_files", "special", "projects"):
            lst = r.get(key)
            if isinstance(lst, list):
                r[key] = [e for e in lst if not (isinstance(e, dict) and _inside_or_equal(str(e.get("path") or ""), path))]
        lst = r.get("git_repos")
        if isinstance(lst, list):
            keep = []
            for e in lst:
                if isinstance(e, dict) and any(_inside_or_equal(str(e.get(k) or ""), path) for k in ("path", "repo", "git_dir")):
                    continue
                keep.append(e)
            r["git_repos"] = keep

    def _replay(self, changes: list, new: dict, old: dict) -> list:
        """Apply ``changes`` made on ``old`` (journaled while a scan ran) to ``new``, the scan result that
        replaces it. Returns the root labels that still need a rescan.

        A trashed item is still in every root of ``new`` that was copied from ``old`` (same
        ``scanned_at``), and in every root whose walk finished before the trash started: take it out
        there. A root walked while the item was being moved may or may not have counted it, and action
        effects are not replayed, so those roots are rescanned instead."""
        old_at = {x.get("label"): x.get("scanned_at") for x in old.get("roots") or []}
        rescan = []
        for c in changes:
            path = c["path"]
            if c["kind"] == "trash":
                self._prune_lists(new, path)
            for root in new.get("roots") or []:
                label = root.get("label")
                if not label or not _root_covers(root, path):
                    continue
                if c["kind"] == "trash":
                    copied = label in old_at and root.get("scanned_at") == old_at[label]
                    walked_end = float(root.get("scanned_at") or 0) + float(root.get("duration_s") or 0)
                    if copied or walked_end < c["started_at"]:
                        self._remove_from_root(root, path, c["size"], c["files"])
                        continue
                if label not in rescan:
                    rescan.append(label)
        return rescan

    def _list_entry(self, r: dict, path: str) -> Optional[dict]:
        """A big_files / special entry with exactly ``path`` (items deeper than the tree depth)."""
        for key in ("big_files", "special"):
            for e in r.get(key) or []:
                if isinstance(e, dict) and e.get("path") == path:
                    return e
        return None

    @staticmethod
    def _in_special_git_dir(r: dict, path: str) -> bool:
        """Is ``path`` at/inside a ``special`` git_dir (found at any depth, so this also covers bare
        repos deeper than the tree, whose paths have no '.git' component)?"""
        for e in r.get("special") or []:
            if isinstance(e, dict) and e.get("kind") == "git_dir" and _inside_or_equal(path, str(e.get("path") or "")):
                return True
        return False

    def _refresh_running(self, r: dict) -> None:
        """Re-check which apps run right now (``in_use`` comes from the scan, which may be days old, e.g.
        loaded from the cache at startup): fresh ``running`` flags, then insights recomputes ``in_use``."""
        try:
            self.mod("resolver").annotate(r)
            self.mod("insights").annotate(r)
            self.version += 1
        except Exception as e:  # keep the scan-time state; the checks below still apply
            print("librarylens: could not re-check running apps: %s: %s" % (type(e).__name__, e), file=sys.stderr)

    def _reannotate(self, r: dict) -> Optional[str]:
        """pipeline.reannotate (apps + detectors + insights + save); falls back to insights + save."""
        try:
            self.mod("pipeline").reannotate(r)
            return None
        except Exception as e:
            print("librarylens: full re-annotation failed (%s: %s); recomputing insights only"
                  % (type(e).__name__, e), file=sys.stderr)
        warning = None
        try:
            self.mod("insights").annotate(r)
        except Exception as e:
            warning = "could not recompute insights: %s" % e
        try:
            self.mod("scanner").save_cache(r)
        except Exception as e:
            warning = "could not save scan cache: %s" % e
        return warning

    # ---- trash ---------------------------------------------------------------------------
    def trash(self, path: Any, force: bool = False) -> dict:
        path = _abs_path_arg(path)
        scanner = self.mod("scanner")
        trash_mod = self.mod("trash")
        with self.trash_lock:
            with self.lock:
                r = self.result
                if r is None:
                    raise ApiError(HTTPStatus.CONFLICT, "no scan result yet")
                roots = r.get("roots") or []
                root_paths = [_norm(x["path"]) for x in roots if x.get("path")]
                if path in root_paths:
                    raise ApiError(HTTPStatus.FORBIDDEN, "refusing to trash a scan root; open it and trash its contents")
                if not any(_inside_or_equal(path, rp) for rp in root_paths):
                    raise ApiError(HTTPStatus.FORBIDDEN, "path is outside the scanned locations")
                self._refresh_running(r)
                node = scanner.find_node(r, path)
                entry = None
                if node is None or node.get("synthetic") or node.get("link_root"):
                    if node is not None and node.get("link_root"):
                        raise ApiError(HTTPStatus.CONFLICT, "this is a separately scanned location (%s)"
                                       % node["link_root"])
                    entry = self._list_entry(r, path)
                    if entry is None:
                        raise ApiError(HTTPStatus.NOT_FOUND, "path not found in the current scan")
                    node = None
                chains = self._chains(r, path)
                ancestors = [n for ch in chains for n in ch if n.get("path") != path]
                if (".git" in path.split("/") or (node or {}).get("kind") == "git_dir"
                        or (entry or {}).get("kind") == "git_dir"
                        or any(a.get("kind") == "git_dir" for a in ancestors)
                        or self._in_special_git_dir(r, path)):
                    raise ApiError(HTTPStatus.CONFLICT, GIT_TRASH_MSG, use_actions=True)
                app = self._effective_app(path)
                if (node or {}).get("in_use"):
                    raise ApiError(HTTPStatus.CONFLICT, "%s is running — quit it first"
                                   % ((app or {}).get("name") or "The app"))
                for a in ancestors:
                    if a.get("in_use"):
                        raise ApiError(HTTPStatus.CONFLICT, "belongs to %s, which is running — quit it first"
                                       % ((a.get("app") or app or {}).get("name") or "an app"))
                if node is not None:
                    sub = self._running_in_subtree(node, app)
                    if sub is not None:
                        raise ApiError(HTTPStatus.CONFLICT, "contains data of %s, which is running — quit it first"
                                       % (sub.get("name") or "a running app"))
                    safety = node.get("safety")
                    size = int(node.get("size") or 0)
                    files = int(node.get("files") or 0)
                else:
                    deepest = max(ancestors, key=lambda a: len(a.get("path") or ""), default=None)
                    safety = (deepest or {}).get("safety")
                    size = int(entry.get("size") or 0)
                    files = int(entry.get("files") or 1)
                if safety == "danger" and not force:
                    raise ApiError(HTTPStatus.CONFLICT, "marked as dangerous; confirm with force",
                                   needs_force=True)
            started_at = time.time()
            res = trash_mod.move_to_trash(path, root_paths, size=size)
            if not res.get("ok"):
                raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, res.get("error") or "move to Trash failed")
            change = {"kind": "trash", "path": path, "size": size, "files": files, "started_at": started_at}
            warning, rescan = None, []
            with self.lock:
                cur = self.result
                if self._journal is not None:  # a running scan replays it onto its result
                    self._journal.append(change)
                try:
                    if cur is r:
                        self._remove_from_tree(cur, path, size, files)
                        self._prune_lists(cur, path)
                    elif cur is not None:  # a scan replaced the result while Finder was moving the item
                        rescan = self._replay([change], cur, r)
                    if cur is not None:
                        warning = self._reannotate(cur)
                        self.version += 1
                except Exception as e:  # the item is in the Trash: report that, and repair by rescanning
                    traceback.print_exc()
                    rescan = [x["label"] for x in (cur or {}).get("roots") or []
                              if x.get("label") and _root_covers(x, path)]
                    warning = "the view could not be updated (%s: %s); rescanning" % (type(e).__name__, e)
                summary = (self.result or {}).get("summary")
        self.request_rescan(rescan)
        out = {"ok": True, "freed": size, "summary": summary, "method": res.get("method"),
               "trashed_to": res.get("trashed_to")}
        if rescan:
            out["rescan"] = rescan
        if warning:
            out["warning"] = warning
        return out

    def reveal(self, path: Any) -> dict:
        path = _abs_path_arg(path)
        with self.lock:
            if self.result is None:
                raise ApiError(HTTPStatus.CONFLICT, "no scan result yet")
            root_paths = [_norm(x["path"]) for x in self.result.get("roots") or [] if x.get("path")]
        if not any(_inside_or_equal(path, rp) for rp in root_paths):
            raise ApiError(HTTPStatus.FORBIDDEN, "path is outside the scanned locations")
        if not os.path.lexists(path):
            raise ApiError(HTTPStatus.NOT_FOUND, "path no longer exists")
        self.mod("trash").reveal_in_finder(path)
        return {"ok": True}

    # ---- actions -------------------------------------------------------------------------
    def known_actions(self) -> set:
        known = set(KNOWN_ACTIONS)
        try:
            mod = self.mod("actions")
        except ApiError:
            return known
        for attr in ("ACTIONS", "ACTION_IDS"):
            v = getattr(mod, attr, None)
            if isinstance(v, (dict, list, tuple, set, frozenset)):
                known.update(x for x in v if isinstance(x, str))
        return known

    @staticmethod
    def _git_entries(r: dict, path: str) -> list:
        out = []
        for e in r.get("git_repos") or []:
            if not isinstance(e, dict):
                continue
            wt = e.get("path") or ""
            cands = {wt, e.get("repo") or "", e.get("git_dir") or "", (wt.rstrip("/") + "/.git") if wt else ""}
            if path in cands:
                out.append(e)
        return out

    def _descriptors(self, r: dict, path: str) -> Optional[list]:
        """Action descriptors offered for ``path`` (node["actions"], else the git_repos entry's), or
        None if the path is unknown."""
        node = self.mod("scanner").find_node(r, path)
        entries = self._git_entries(r, path)
        if node is None and not entries:
            for c in (r.get("summary") or {}).get("top_candidates") or []:
                if isinstance(c, dict) and c.get("path") == path and isinstance(c.get("action"), dict):
                    return [c["action"]]
            return None
        descs = [d for d in ((node or {}).get("actions") or []) if isinstance(d, dict)]
        if not descs:
            for e in entries:
                descs.extend(d for d in e.get("actions") or [] if isinstance(d, dict))
        return descs

    def _public_job(self, j: dict) -> dict:
        return {"id": j["id"], "action": j["action"], "label": j.get("label"), "path": j["path"],
                "state": j["state"], "output_tail": "\n".join(j["_tail"]), "started_at": j["started_at"],
                "finished_at": j.get("finished_at"), "error": j.get("error"), "freed": j.get("freed"),
                "details": j.get("details"), "background": j.get("background"),
                "rescan": j.get("rescan"), "warning": j.get("warning")}

    def jobs_json(self) -> list:
        with self.jobs_lock:
            return [self._public_job(j) for j in reversed(list(self.jobs.values()))]

    def _job_output(self, job: dict, text: Any) -> None:
        text = str(text).replace("\r\n", "\n")
        with self.jobs_lock:
            tail = job["_tail"]
            for piece in text.split("\n"):
                segs = [s for s in piece.split("\r") if s.strip()]
                if not segs:
                    continue
                line = segs[-1].rstrip()[:2000]
                progressy = len(piece.split("\r")) > 1 or "%" in line
                if (progressy and job.get("_progress") and tail
                        and _progress_prefix(tail[-1]) == _progress_prefix(line)):
                    tail[-1] = line  # git-style "\r" progress: overwrite the previous line
                else:
                    tail.append(line)
                job["_progress"] = progressy

    def _trim_jobs(self) -> None:
        done = [k for k, j in self.jobs.items() if j["state"] != "running"]
        for k in done[:max(0, len(done) - MAX_FINISHED_JOBS)]:
            del self.jobs[k]

    def run_action(self, path: Any, action: Any, confirm: Any) -> tuple:
        """Returns (http_status, body)."""
        path = _abs_path_arg(path)
        if not isinstance(action, str) or not action:
            raise ApiError(HTTPStatus.BAD_REQUEST, "'action' must be an action id")
        if action not in self.known_actions():
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown action '%s'" % action)
        if confirm is not True:
            raise ApiError(HTTPStatus.BAD_REQUEST, "actions must be confirmed ('confirm': true)")
        actions_mod = self.mod("actions")
        with self.lock:
            r = self.result
            if r is None:
                raise ApiError(HTTPStatus.CONFLICT, "no scan result yet")
            root_paths = [_norm(x["path"]) for x in r.get("roots") or [] if x.get("path")]
            if not any(_inside_or_equal(path, rp) for rp in root_paths):
                raise ApiError(HTTPStatus.FORBIDDEN, "path is outside the scanned locations")
            descs = self._descriptors(r, path)
            if descs is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "path not found in the current scan")
            desc = next((d for d in descs if d.get("id") == action), None)
            if desc is None:
                raise ApiError(HTTPStatus.CONFLICT, "action '%s' is not available for this item" % action)
            if desc.get("blocked"):
                raise ApiError(HTTPStatus.CONFLICT, "blocked: %s" % desc["blocked"], blocked=desc["blocked"])
            desc = dict(desc)
            gen = self.result_gen
        with self.jobs_lock:
            for j in self.jobs.values():
                if j["state"] == "running" and (_inside_or_equal(j["path"], path) or _inside_or_equal(path, j["path"])):
                    raise ApiError(HTTPStatus.CONFLICT, "another action (%s) is still running for this item"
                                   % j["action"])
            job = {"id": "j%d" % next(self._job_ids), "action": action, "label": desc.get("label"),
                   "path": path, "state": "running", "started_at": time.time(), "finished_at": None,
                   "error": None, "freed": None, "details": None, "background": bool(desc.get("background")),
                   "_tail": collections.deque(maxlen=JOB_TAIL_LINES), "_progress": False, "_gen": gen}
            self.jobs[job["id"]] = job
            self._trim_jobs()
        if job["background"]:
            t = threading.Thread(target=self._execute_job, args=(job, actions_mod, root_paths),
                                 name="librarylens-job-" + job["id"], daemon=True)
            self.job_threads = [x for x in self.job_threads if x.is_alive()] + [t]
            t.start()
            return HTTPStatus.ACCEPTED, {"ok": True, "background": True, "job": self.job_by_id(job["id"])}
        self._execute_job(job, actions_mod, root_paths)
        pj = self.job_by_id(job["id"])
        body = {"ok": pj["state"] == "done", "background": False, "job": pj, "freed": pj["freed"],
                "error": pj["error"], "details": pj["details"]}
        if pj.get("warning"):
            body["warning"] = pj["warning"]
        with self.lock:
            body["summary"] = (self.result or {}).get("summary")
        return (HTTPStatus.OK if body["ok"] else HTTPStatus.INTERNAL_SERVER_ERROR), body

    def job_by_id(self, jid: str) -> Optional[dict]:
        with self.jobs_lock:
            j = self.jobs.get(jid)
            return self._public_job(j) if j else None

    def _execute_job(self, job: dict, actions_mod, root_paths: list) -> None:
        try:
            res = actions_mod.run(job["action"], job["path"], list(root_paths),
                                  on_output=lambda s: self._job_output(job, s))
            if not isinstance(res, dict):
                res = {"ok": False, "error": "the action returned no result"}
        except Exception as e:
            traceback.print_exc()
            res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        ok = bool(res.get("ok"))
        try:
            freed = int(res.get("freed") or 0)
        except (TypeError, ValueError):
            freed = 0
        with self.jobs_lock:
            job.update(state="done" if ok else "failed", finished_at=time.time(),
                       error=None if ok else (res.get("error") or "the action failed"),
                       freed=freed, details=res.get("details"))
        if ok:
            try:
                self._after_action(job, freed)
            except Exception as e:  # never turn a successful action into a failed job
                traceback.print_exc()
                with self.jobs_lock:
                    job["warning"] = "updating the view failed: %s: %s" % (type(e).__name__, e)

    def _after_action(self, job: dict, freed: int) -> None:
        path, action = job["path"], job["action"]
        labels, warning = [], None
        with self.trash_lock:
            with self.lock:
                r = self.result
                if self._journal is not None:  # a running scan rescans these roots after its swap
                    self._journal.append({"kind": "action", "path": path})
                if r is not None:
                    covering = [x for x in r.get("roots") or [] if _root_covers(x, path)]
                    covering.sort(key=lambda x: -len(x.get("path") or ""))
                    labels = [x.get("label") for x in covering if x.get("label")]
                    # A result swapped in while the action ran may already reflect it (if its scan
                    # walked the repo afterwards): don't subtract twice, the rescan below settles it.
                    if self.result_gen == job.get("_gen"):
                        self._shrink_in_tree(path, freed)
                        self._mark_action_done(r, path, action, freed)
                        warning = self._reannotate(r)
                        self.version += 1
        with self.jobs_lock:
            job["rescan"] = labels
            if warning:
                job["warning"] = warning
        self.request_rescan(labels)

    def _mark_action_done(self, r: dict, path: str, action: str, freed: int) -> None:
        """Best-effort immediate update until the rescan lands (detectors may recompute it anyway)."""
        targets = list(self._git_entries(r, path))
        node = self.mod("scanner").find_node(r, path)
        if node is not None:
            targets.append(node)
            if isinstance(node.get("details"), dict):
                targets.append(node["details"])
        seen = set()
        for t in targets:
            if id(t) in seen:  # a git_repos entry may be the very same dict as node["details"]
                continue
            seen.add(id(t))
            if isinstance(t.get("actions"), list):
                t["actions"] = [d for d in t["actions"] if not (isinstance(d, dict) and d.get("id") == action)]
            if t is not node and "size" in t and freed:
                try:
                    t["size"] = max(0, int(t.get("size") or 0) - freed)
                except (TypeError, ValueError):
                    pass
            if action == "git_delete_temp_packs":
                for k, v in (("garbage_size", 0), ("garbage_files", []), ("garbage_count", 0),
                             ("garbage_old_size", 0), ("garbage_old_files", 0)):
                    if k in t:
                        t[k] = v
            elif action == "git_worktree_prune" and "prunable_worktrees" in t:
                t["prunable_worktrees"] = []
        for e in r.get("special") or []:
            if isinstance(e, dict) and e.get("path") == path and freed:
                e["size"] = max(0, int(e.get("size") or 0) - freed)
        bf = r.get("big_files")
        if isinstance(bf, list):  # deleted temp packs / rewritten packs are often among the big files
            r["big_files"] = [e for e in bf if not (isinstance(e, dict) and _inside_or_equal(str(e.get("path") or ""), path)
                                                    and not os.path.lexists(str(e.get("path"))))]

    def index_html(self) -> bytes:
        with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        return html.replace(TOKEN_PLACEHOLDER, self.token).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LibraryLens"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- plumbing ------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, gz: bool = False, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, **extra: Any) -> None:
        d = {"ok": False, "error": message}
        d.update(extra)
        self._json(status, d)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin.lower() in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

    def _read_json(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        raw = self.rfile.read(n) if n else b""
        self._body_read = True
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid JSON body")
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _guard(self) -> bool:
        if not self._host_ok():
            self.close_connection = True
            self._error(HTTPStatus.FORBIDDEN, "bad Host header")
            return False
        return True

    # ---- verbs ---------------------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, self.app.index_html(), "text/html; charset=utf-8",
                           extra={"Content-Security-Policy":
                                  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                  "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                  "frame-ancestors 'none'",
                                  "X-Frame-Options": "DENY"})
            elif path == "/api/scan":
                want_gz = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
                body, gz = self.app.result_bytes(want_gz)
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8", gz=gz)
            elif path == "/api/status":
                self._json(HTTPStatus.OK, self.app.status_json())
            elif path == "/api/disk":
                self._json(HTTPStatus.OK, self.app.disk_overview())
            elif path == "/api/jobs":
                self._json(HTTPStatus.OK, self.app.jobs_json())
            elif path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as e:
            self._error(e.status, e.message, **e.extra)
        except Exception as e:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "%s: %s" % (type(e).__name__, e))

    def do_POST(self) -> None:
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        self._body_read = False
        try:
            if not self._origin_ok():
                raise ApiError(HTTPStatus.FORBIDDEN, "bad Origin")
            tok = self.headers.get("X-LL-Token") or ""
            if not secrets.compare_digest(tok.encode(), self.app.token.encode()):
                self.close_connection = True
                raise ApiError(HTTPStatus.FORBIDDEN, "missing or invalid token")
            data = self._read_json()
            if path == "/api/scan":
                md = data.get("max_depth")
                if md is not None and (not isinstance(md, int) or isinstance(md, bool) or not 1 <= md <= 12):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "max_depth must be an integer 1..12")
                only = self.app.validate_only(data.get("only"))
                self.app.start_scan(md, only=only)
                self._json(HTTPStatus.ACCEPTED, {"ok": True, "status": self.app.status_json()})
            elif path == "/api/scan/cancel":
                self._json(HTTPStatus.OK, {"ok": True, "status": self.app.cancel_scan()})
            elif path == "/api/trash":
                self._json(HTTPStatus.OK, self.app.trash(data.get("path"), force=data.get("force") is True))
            elif path == "/api/reveal":
                self._json(HTTPStatus.OK, self.app.reveal(data.get("path")))
            elif path == "/api/action":
                status, body = self.app.run_action(data.get("path"), data.get("action"), data.get("confirm"))
                self._json(status, body)
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        except ApiError as e:
            self._error(e.status, e.message, **e.extra)
        except Exception as e:
            traceback.print_exc()
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "%s: %s" % (type(e).__name__, e))
        finally:
            if not self._body_read:  # rejected before reading it: the body would be parsed as the next request
                self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765, tries: int = PORT_TRIES) -> ThreadingHTTPServer:
    """Bind ``host:port`` (trying the next few ports if busy; port 0 = any free port)."""
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("LibraryLens only binds to 127.0.0.1")
    host = "127.0.0.1"
    last = None
    for p in ([0] if port == 0 else range(port, port + max(1, tries))):
        try:
            httpd = _Server((host, p), Handler)
        except OSError as e:
            last = e
            continue
        httpd.app = app  # type: ignore[attr-defined]
        app.port = httpd.server_address[1]
        return httpd
    raise OSError("no free port in %d..%d: %s" % (port, port + tries - 1, last))


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    app = App()
    app.load_cache()
    httpd = make_server(app, host, port)
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("LibraryLens running at %s  (Ctrl+C to stop)" % url, flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
