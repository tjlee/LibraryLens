from __future__ import annotations

import copy
import gzip
import http.client
import json
import threading
import time
import types
import unittest

from librarylens import server

H = "/Users/fake-ll"
L = H + "/Library"


def node(path, size, files=1, children=None, **kw):
    n = {"path": path, "name": path.rsplit("/", 1)[-1], "size": size, "files": files, "mtime": 1700000000.0,
         "is_dir": True, "error": None, "children": children or [], "flags": [], "safety": "caution", "reason": "test"}
    n.update(kw)
    return n


def synthetic(parent, size, n=3):
    return node(parent, size, files=n, name="(%d smaller items)" % n, is_dir=False, synthetic=True)


JB = {"name": "IntelliJ IDEA", "bundle_id": "com.jetbrains.intellij", "app_path": "/Applications/IntelliJ IDEA.app",
      "installed": True, "running": False, "version": "2025.1", "installed_versions": ["2027.1"],
      "kind": "app", "source": "jetbrains"}
JB_RUN = dict(JB, version="2027.1", running=True)

A_TEMP = {"id": "git_delete_temp_packs", "label": "Delete 2 leftover fetch temp files", "frees": 3000,
          "safety": "safe", "blocked": None, "background": False, "confirm": "Permanently delete 2 files"}
A_PRUNE = {"id": "git_worktree_prune", "label": "Prune 1 stale worktree", "frees": 0, "safety": "safe",
           "blocked": None, "background": False, "confirm": "Run git worktree prune"}
A_GC = {"id": "git_gc", "label": "Run git gc", "frees": 2000, "safety": "caution", "blocked": None,
        "background": True, "confirm": "Run git gc in the background"}


def fixture():
    caches = node(L + "/Caches", 1000, 100, [
        node(L + "/Caches/JetBrains", 600, 60, [
            node(L + "/Caches/JetBrains/IntelliJIdea2025.1", 400, 40, app=JB, flags=["old_version"], safety="safe",
                 reason="IntelliJ IDEA 2025.1 is not installed (installed: 2027.1)"),
            node(L + "/Caches/JetBrains/IntelliJIdea2027.1", 150, 15, app=JB_RUN, safety="caution", in_use=True),
            synthetic(L + "/Caches/JetBrains", 50),
        ]),
        node(L + "/Caches/Danger", 100, 10, safety="danger", reason="irreplaceable"),
        node(L + "/Caches/com.example.tool", 300, 30, flags=["regenerable"], safety="safe"),
    ], label="Caches", category="cache", nested_in=None)
    containers = node(L + "/Containers", 5000, 500, [
        node(L + "/Containers/com.docker.docker", 4000, 400, [
            node(L + "/Containers/com.docker.docker/Data", 3900, 390),  # depth-limited: no children here
        ]),
        node(L + "/Containers/other", 1000, 100),
    ], label="Containers", category="container", nested_in=None)
    docker = node(L + "/Containers/com.docker.docker", 4000, 400, [
        node(L + "/Containers/com.docker.docker/Data", 3900, 390, [
            node(L + "/Containers/com.docker.docker/Data/vms", 3800, 2, flags=["regenerable"], safety="caution"),
            synthetic(L + "/Containers/com.docker.docker/Data", 100),
        ]),
    ], label="Docker", category="docker", nested_in="Containers")
    git_dir = H + "/IdeaProjects/ultimate/.git"
    git_details = {"repo": git_dir, "pack_size": 5000, "packs": 53, "largest_pack": 900, "loose_size": 10,
                   "loose_count": 4, "garbage_size": 3000, "garbage_files": ["tmp_pack_a", "tmp_pack_b"],
                   "prunable_worktrees": [H + "/wt1"], "worktrees": 7, "gc_running": False,
                   "fetch_running": False, "last_commit": 1761800000.0}
    home = node(H, 10000, 1000, [
        node(H + "/IdeaProjects", 9000, 900, [
            node(H + "/IdeaProjects/ultimate", 9000, 900, [
                node(git_dir, 8000, 800, kind="git_dir", safety="danger", details=git_details,
                     actions=[dict(a) for a in (A_TEMP, A_PRUNE, A_GC)]),
                node(H + "/IdeaProjects/ultimate/out", 1000, 100, kind="build_output", safety="safe"),
            ], kind="project"),
        ]),
        {"name": "Library", "path": L, "size": 0, "files": 0, "is_dir": True, "children": [],
         "link_root": "Library (other)"},
    ], label="Home", category="home", nested_in=None, exclude=[L])
    r = {"scanned_at": time.time() - 3600, "duration_s": 12.5, "home": H,
         "roots": [caches, containers, docker, home],
         "big_files": [{"path": git_dir + "/objects/pack/tmp_pack_a", "size": 1500, "mtime": 1.0, "root": "Home"},
                       {"path": L + "/Caches/com.example.tool/huge.bin", "size": 200, "mtime": 1.0, "root": "Caches"}],
         "special": [{"path": git_dir, "kind": "git_dir", "size": 8000, "files": 800, "mtime": 1.0, "root": "Home"},
                     {"path": H + "/IdeaProjects/ultimate/out", "kind": "build_output", "size": 1000, "files": 100,
                      "mtime": 1.0, "root": "Home"}],
         "git_repos": [dict(git_details, path=H + "/IdeaProjects/ultimate", size=8000)],
         "disk": {"container": {"size": 994, "used": 980, "free": 14}, "collected_at": 1.0}}
    fake_insights_annotate(r)
    return r


def _walk(n):
    yield n
    for c in n.get("children") or []:
        yield from _walk(c)


def fake_insights_annotate(r, stale_days=90, now=None):
    total = sum(x["size"] for x in r["roots"] if not x.get("nested_in"))
    cands, rec = [], 0
    for root in r["roots"]:
        for n in _walk(root):
            if n.get("flags") and not n.get("synthetic"):
                cands.append({"path": n["path"], "size": n["size"], "flags": n["flags"], "reason": n["reason"]})
    seen = set()
    for c in cands:
        if c["path"] not in seen:
            seen.add(c["path"])
            rec += c["size"]
    r["summary"] = {"total": total, "reclaimable": rec, "by_flag": {}, "by_category": {}, "top_candidates": cands[:20]}


def fake_find_node(result, path):
    for root in result["roots"]:
        for n in _walk(root):
            if n["path"] == path and not n.get("synthetic"):
                return n
    return None


class Cancelled(Exception):
    pass


class Stubs:
    def __init__(self):
        self.saved = []
        self.trashed = []
        self.runs = []
        self.reannotated = 0
        self.action_calls = []
        self.disk_calls = 0
        self.scan_gate = threading.Event()
        self.scan_gate.set()
        self.action_gate = threading.Event()
        self.action_gate.set()
        self.action_result = {"ok": True, "error": None, "freed": 3000, "details": {"deleted": 2}}
        self.walk_s = 0.0           # how long each rescanned root's walk takes (its duration_s)
        self.ignore_cancel = False  # True: like the pipeline phases after the walk, which never check it
        self.scanner = types.SimpleNamespace(find_node=fake_find_node, save_cache=self.saved.append,
                                             load_cache=lambda: None, Cancelled=Cancelled)
        self.pipeline = types.SimpleNamespace(run=self.pipeline_run, reannotate=self.reannotate)
        self.insights = types.SimpleNamespace(annotate=fake_insights_annotate)
        self.trash = types.SimpleNamespace(move_to_trash=self.move_to_trash, reveal_in_finder=lambda p: None)
        self.actions = types.SimpleNamespace(run=self.actions_run)
        self.disk = types.SimpleNamespace(overview=self.overview)

    def pipeline_run(self, prev=None, only=None, max_depth=3, progress=None, cancel=None):
        self.runs.append({"prev": prev, "only": only, "max_depth": max_depth})
        t0 = time.time()
        if progress:
            progress("phase:scan")
            progress(L + "/Caches")
        while not self.scan_gate.wait(0.01):
            if cancel is not None and cancel.is_set() and not self.ignore_cancel:
                raise Cancelled("scan cancelled")
        r = fixture()
        r.pop("summary")
        r["scanned_at"] = 42.0
        for n in r["roots"]:  # walked roots, as scan_all marks them; copied ones keep prev's
            n["scanned_at"], n["duration_s"] = t0, self.walk_s
        if only and prev:
            r["roots"] = [n if n["label"] in only else o for n, o in zip(r["roots"], prev["roots"])]
        if progress:
            progress("phase:insights")
        fake_insights_annotate(r)
        self.saved.append(r)
        return r

    def reannotate(self, r):
        self.reannotated += 1
        fake_insights_annotate(r)
        self.saved.append(r)
        return r

    def actions_run(self, action_id, path, allowed_roots, on_output=None):
        self.action_calls.append((action_id, path, list(allowed_roots)))
        if on_output:
            on_output("Counting objects:  50% (1/2)\r")
            on_output("Counting objects: 100% (2/2), done.\n")
            on_output("line2")
        self.action_gate.wait(10)
        return dict(self.action_result)

    def overview(self):
        self.disk_calls += 1
        return {"container": {"size": 994, "used": 980, "free": 14}, "data_volume": {"name": "Data", "used": 956},
                "volumes": [], "snapshots": [], "purgeable": None, "users": [], "collected_at": 2.0}

    def move_to_trash(self, path, allowed_roots, size=None):
        self.trashed.append((path, list(allowed_roots), size))
        return {"ok": True, "error": None, "method": "finder", "trashed_to": "/Users/fake-ll/.Trash/x"}

    def modules(self):
        return {"scanner": self.scanner, "pipeline": self.pipeline, "insights": self.insights,
                "trash": self.trash, "actions": self.actions, "disk": self.disk}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.stubs = Stubs()
        self.app = server.App(result=fixture(), modules=self.stubs.modules())
        self.httpd = server.make_server(self.app, port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stubs.scan_gate.set()
        self.stubs.action_gate.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        for t in list(self.app.job_threads):
            t.join(5)
        self.join_scans()

    def join_scans(self):
        for _ in range(10):
            t = self.app.scan_thread
            if t:
                t.join(5)
            if self.app.scan_thread is t and not (t and t.is_alive()):
                return

    def wait_for(self, cond, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    # ---- helpers ----
    def req(self, method, path, body=None, token=True, host=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or "127.0.0.1:%d" % self.port)
        hdrs = dict(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if method == "POST":
            hdrs.setdefault("Content-Type", "application/json")
            if token:
                hdrs.setdefault("X-LL-Token", self.app.token)
        if data is not None:
            hdrs["Content-Length"] = str(len(data))
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders(data)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def jreq(self, *a, **kw):
        resp, payload = self.req(*a, **kw)
        return resp.status, json.loads(payload.decode())

    def find(self, path, root_label):
        root = next(r for r in self.app.result["roots"] if r["label"] == root_label)
        for n in _walk(root):
            if n["path"] == path and not n.get("synthetic"):
                return n
        return None

    # ---- tests ----
    def test_index_has_token(self):
        resp, body = self.req("GET", "/")
        self.assertEqual(resp.status, 200)
        html = body.decode()
        self.assertIn(self.app.token, html)
        self.assertNotIn(server.TOKEN_PLACEHOLDER, html)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))

    def test_bad_host_rejected(self):
        for host in ("evil.example:%d" % self.port, "127.0.0.1", "localhost:1", "evil.example"):
            resp, _ = self.req("GET", "/api/scan", host=host)
            self.assertEqual(resp.status, 403, host)
        resp, _ = self.req("GET", "/api/status", host="localhost:%d" % self.port)
        self.assertEqual(resp.status, 200)
        status, _ = self.jreq("POST", "/api/trash", {"path": L + "/Caches/com.example.tool"}, host="evil:%d" % self.port)
        self.assertEqual(status, 403)
        self.assertEqual(self.stubs.trashed, [])

    def test_post_requires_token(self):
        p = {"path": L + "/Caches/com.example.tool"}
        status, j = self.jreq("POST", "/api/trash", p, token=False)
        self.assertEqual(status, 403)
        self.assertFalse(j["ok"])
        status, _ = self.jreq("POST", "/api/trash", p, headers={"X-LL-Token": "wrong"})
        self.assertEqual(status, 403)
        status, _ = self.jreq("POST", "/api/scan", {}, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.stubs.trashed, [])
        self.assertIsNone(self.app.scan_thread)

    def test_bad_origin_content_type_and_size(self):
        p = {"path": L + "/Caches/com.example.tool"}
        status, _ = self.jreq("POST", "/api/trash", p, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.jreq("POST", "/api/trash", p, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _ = self.jreq("POST", "/api/trash", raw=b'{"path": "' + b"a" * (server.MAX_BODY + 10) + b'"}')
        self.assertEqual(status, 413)
        status, _ = self.jreq("POST", "/api/trash", raw=b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(self.stubs.trashed, [])

    def test_get_scan_returns_fixture_plain_and_gzip(self):
        status, j = self.jreq("GET", "/api/scan")
        self.assertEqual(status, 200)
        self.assertEqual([r["label"] for r in j["roots"]], ["Caches", "Containers", "Docker", "Home"])
        self.assertEqual(j["summary"]["total"], 16000)
        resp, body = self.req("GET", "/api/scan", headers={"Accept-Encoding": "gzip, deflate"})
        self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(body)), j)

    def test_get_scan_none(self):
        self.app.result = None
        self.app.version += 1
        status, j = self.jreq("GET", "/api/scan")
        self.assertEqual((status, j), (200, {"status": "none"}))
        status, j = self.jreq("GET", "/api/status")
        self.assertFalse(j["has_result"])

    def test_trash_root_refused(self):
        for p in (L + "/Caches", L + "/Containers/com.docker.docker", L + "/Containers/"):
            status, j = self.jreq("POST", "/api/trash", {"path": p})
            self.assertEqual(status, 403, p)
            self.assertFalse(j["ok"])
        status, _ = self.jreq("POST", "/api/trash", {"path": "/Users/someone-else/x"})
        self.assertEqual(status, 403)
        status, _ = self.jreq("POST", "/api/trash", {"path": H})
        self.assertEqual(status, 403)
        status, _ = self.jreq("POST", "/api/trash", {"path": "relative"})
        self.assertEqual(status, 400)
        status, _ = self.jreq("POST", "/api/trash", {"path": L + "/Caches/nope"})
        self.assertEqual(status, 404)
        self.assertEqual(self.stubs.trashed, [])

    def test_trash_running_app_refused(self):
        status, j = self.jreq("POST", "/api/trash", {"path": L + "/Caches/JetBrains/IntelliJIdea2027.1"})
        self.assertEqual(status, 409)
        self.assertIn("running", j["error"])
        # parent folder contains the running app's data
        status, j = self.jreq("POST", "/api/trash", {"path": L + "/Caches/JetBrains"})
        self.assertEqual(status, 409)
        self.assertIn("running", j["error"])
        self.assertEqual(self.stubs.trashed, [])

    def test_trash_running_descendant_with_inherited_app_refused(self):
        # the running node has no "app" of its own; that must not read as "nothing is running"
        self.find(H + "/IdeaProjects/ultimate/out", "Home")["in_use"] = True
        status, j = self.jreq("POST", "/api/trash", {"path": H + "/IdeaProjects/ultimate", "force": True})
        self.assertEqual(status, 409, j)
        self.assertIn("contains data of a running app", j["error"])
        self.find(H + "/IdeaProjects", "Home")["app"] = JB_RUN
        status, j = self.jreq("POST", "/api/trash", {"path": H + "/IdeaProjects/ultimate", "force": True})
        self.assertEqual(status, 409, j)
        self.assertIn("contains data of IntelliJ IDEA", j["error"])
        self.assertEqual(self.stubs.trashed, [])

    def test_trash_danger_requires_force(self):
        p = L + "/Caches/Danger"
        status, j = self.jreq("POST", "/api/trash", {"path": p})
        self.assertEqual(status, 409)
        self.assertTrue(j.get("needs_force"))
        status, j = self.jreq("POST", "/api/trash", {"path": p, "force": True})
        self.assertEqual(status, 200, j)
        self.assertEqual(j["freed"], 100)
        self.assertIsNone(self.find(p, "Caches"))
        self.assertEqual(self.find(L + "/Caches", "Caches")["size"], 900)

    def test_trash_happy_path_updates_ancestors_and_summary(self):
        p = L + "/Containers/com.docker.docker/Data/vms"
        before = self.app.result["summary"]
        status, j = self.jreq("POST", "/api/trash", {"path": p})
        self.assertEqual(status, 200, j)
        self.assertTrue(j["ok"])
        self.assertEqual(j["freed"], 3800)
        path, roots, size = self.stubs.trashed[0]
        self.assertEqual(path, p)
        self.assertEqual(size, 3800)
        self.assertEqual(sorted(roots), sorted([L + "/Caches", L + "/Containers", L + "/Containers/com.docker.docker", H]))
        # Home excludes ~/Library: its tree is untouched
        self.assertEqual(self.find(H, "Home")["size"], 10000)
        # nested root (has the node) and containing root (only down to Data) both updated
        self.assertIsNone(self.find(p, "Docker"))
        self.assertEqual(self.find(L + "/Containers/com.docker.docker", "Docker")["size"], 200)
        self.assertEqual(self.find(L + "/Containers/com.docker.docker/Data", "Docker")["size"], 100)
        self.assertEqual(self.find(L + "/Containers/com.docker.docker/Data", "Docker")["files"], 388)
        self.assertEqual(self.find(L + "/Containers", "Containers")["size"], 1200)
        cd = self.find(L + "/Containers/com.docker.docker", "Containers")
        self.assertEqual(cd["size"], 200)
        self.assertEqual(self.find(L + "/Containers/com.docker.docker/Data", "Containers")["size"], 100)
        # children re-sorted by size
        kids = self.find(L + "/Containers", "Containers")["children"]
        self.assertEqual([k["name"] for k in kids], ["other", "com.docker.docker"])
        # summary recomputed and returned
        self.assertEqual(j["summary"]["total"], before["total"] - 3800)
        self.assertEqual(j["summary"]["reclaimable"], before["reclaimable"] - 3800)
        self.assertNotIn(p, [c["path"] for c in j["summary"]["top_candidates"]])
        self.assertEqual(len(self.stubs.saved), 1)
        self.assertNotIn("rescan", j)  # no scan running: nothing to rescan
        self.assertEqual(self.app.rescan_log, [])
        # served result reflects the change
        status, scan = self.jreq("GET", "/api/scan")
        self.assertEqual(scan["summary"]["total"], before["total"] - 3800)
        # second attempt: gone
        status, _ = self.jreq("POST", "/api/trash", {"path": p})
        self.assertEqual(status, 404)

    def test_trash_failure_reported(self):
        self.stubs.trash.move_to_trash = lambda path, roots, size=None: {"ok": False, "error": "nope"}
        status, j = self.jreq("POST", "/api/trash", {"path": L + "/Caches/com.example.tool"})
        self.assertEqual(status, 500)
        self.assertEqual(j["error"], "nope")
        self.assertEqual(self.find(L + "/Caches", "Caches")["size"], 1000)

    def test_reveal_only_inside_roots(self):
        calls = []
        self.stubs.trash.reveal_in_finder = calls.append
        status, _ = self.jreq("POST", "/api/reveal", {"path": "/etc"})
        self.assertEqual(status, 403)
        status, _ = self.jreq("POST", "/api/reveal", {"path": L + "/Caches/JetBrains"})
        self.assertEqual(status, 404)  # fake path does not exist on disk
        self.assertEqual(calls, [])

    def test_scan_background_and_conflict(self):
        self.stubs.scan_gate.clear()
        status, j = self.jreq("POST", "/api/scan", {"max_depth": 2})
        self.assertEqual(status, 202, j)
        status, s = self.jreq("GET", "/api/status")
        self.assertTrue(s["running"])
        self.assertTrue(s["has_result"])
        for k in ("current", "started_at", "elapsed", "error"):
            self.assertIn(k, s)
        status, j = self.jreq("POST", "/api/scan", {})
        self.assertEqual(status, 409)
        self.stubs.scan_gate.set()
        self.app.scan_thread.join(5)
        status, s = self.jreq("GET", "/api/status")
        self.assertFalse(s["running"])
        self.assertIsNone(s["error"])
        status, r = self.jreq("GET", "/api/scan")
        self.assertEqual(r["scanned_at"], 42.0)
        self.assertIn("summary", r)
        self.assertEqual(len(self.stubs.saved), 1)
        status, j = self.jreq("POST", "/api/scan", {"max_depth": "deep"})
        self.assertEqual(status, 400)

    def test_scan_error_reported(self):
        def boom(**kw):
            raise RuntimeError("disk on fire")
        self.stubs.pipeline.run = boom
        status, _ = self.jreq("POST", "/api/scan", {})
        self.assertEqual(status, 202)
        self.app.scan_thread.join(5)
        status, s = self.jreq("GET", "/api/status")
        self.assertIn("disk on fire", s["error"])
        self.assertFalse(s["running"])

    def test_missing_module_is_clear_error(self):
        app = server.App(result=fixture(), modules={})
        with self.assertRaises(server.ApiError) as cm:
            app.mod("definitely_missing_module")
        self.assertEqual(cm.exception.status, 503)
        self.assertIn("definitely_missing_module", cm.exception.message)

    def test_port_busy_tries_next(self):
        other = server.make_server(server.App(result=None, modules={}), port=self.port)
        try:
            self.assertNotEqual(other.server_address[1], self.port)
            self.assertLess(other.server_address[1], self.port + server.PORT_TRIES)
        finally:
            other.server_close()

    # ---- iteration 2: scans via pipeline ----
    def test_scan_only_merges_with_previous_result(self):
        old = self.app.result
        status, j = self.jreq("POST", "/api/scan", {"only": ["Caches"]})
        self.assertEqual(status, 202, j)
        self.assertEqual(j["status"]["only"], ["Caches"])
        self.join_scans()
        run = self.stubs.runs[-1]
        self.assertEqual(run["only"], ["Caches"])
        self.assertIsNot(run["prev"], old)  # a private copy, see test_trash_during_partial_scan_...
        self.assertEqual(run["prev"], old)
        status, s = self.jreq("GET", "/api/status")
        self.assertEqual(s["state"], "done")
        self.assertFalse(s["cancelled"])
        # full scans don't pass prev
        status, _ = self.jreq("POST", "/api/scan", {})
        self.join_scans()
        self.assertIsNone(self.stubs.runs[-1]["prev"])
        self.assertIsNone(self.stubs.runs[-1]["only"])
        for bad in (["Nope"], "Caches", [], [1]):
            status, j = self.jreq("POST", "/api/scan", {"only": bad})
            self.assertEqual(status, 400, bad)
        self.assertEqual(len(self.stubs.runs), 2)

    def test_scan_cancel_keeps_previous_result(self):
        old = self.app.result
        status, _ = self.jreq("POST", "/api/scan/cancel", {})
        self.assertEqual(status, 409)  # nothing running
        self.stubs.scan_gate.clear()
        status, _ = self.jreq("POST", "/api/scan", {})
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for(lambda: self.app.status_json()["current"] == L + "/Caches"))
        status, s = self.jreq("GET", "/api/status")
        self.assertEqual((s["phase"], s["state"], s["running"]), ("scan", "running", True))
        status, _ = self.jreq("POST", "/api/scan/cancel", {}, token=False)
        self.assertEqual(status, 403)
        status, j = self.jreq("POST", "/api/scan/cancel", {})
        self.assertEqual(status, 200, j)
        self.assertTrue(j["status"]["cancel_requested"])
        self.join_scans()
        status, s = self.jreq("GET", "/api/status")
        self.assertFalse(s["running"])
        self.assertTrue(s["cancelled"])
        self.assertEqual(s["state"], "cancelled")
        self.assertIsNone(s["error"])
        self.assertIs(self.app.result, old)
        status, r = self.jreq("GET", "/api/scan")
        self.assertNotEqual(r["scanned_at"], 42.0)

    def test_rescan_request_queued_while_scanning(self):
        self.stubs.scan_gate.clear()
        self.jreq("POST", "/api/scan", {})
        self.app.request_rescan(["Home"])
        status, s = self.jreq("GET", "/api/status")
        self.assertEqual(s["pending_rescan"], ["Home"])
        self.stubs.scan_gate.set()
        self.assertTrue(self.wait_for(lambda: len(self.stubs.runs) == 2))
        self.join_scans()
        self.assertEqual(self.stubs.runs[1]["only"], ["Home"])
        self.assertEqual(self.app.status_json()["pending_rescan"], [])

    def trash_during_scan(self, only, path):
        self.stubs.scan_gate.clear()
        status, _ = self.jreq("POST", "/api/scan", {"only": only})
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for(lambda: self.stubs.runs))
        status, j = self.jreq("POST", "/api/trash", {"path": path})
        self.assertEqual(status, 200, j)
        return j

    def test_trash_during_partial_scan_is_replayed_on_copied_roots(self):
        p = H + "/IdeaProjects/ultimate/out"
        j = self.trash_during_scan(["Caches"], p)
        prev = self.stubs.runs[-1]["prev"]
        # scan_all copies the roots it doesn't rescan from prev while trash mutates the live result
        self.assertIsNot(prev, self.app.result)
        self.assertIsNotNone(fake_find_node(prev, p))
        self.assertNotIn("rescan", j)
        self.stubs.scan_gate.set()
        self.join_scans()
        self.assertEqual(self.app.result["scanned_at"], 42.0)  # the scan's result is swapped in...
        self.assertIsNone(self.find(p, "Home"))  # ...with the trash replayed onto its copy of Home
        self.assertEqual(self.find(H, "Home")["size"], 9000)
        self.assertNotIn(p, [e["path"] for e in self.app.result["special"]])
        self.assertEqual(self.app.rescan_log, [])
        self.assertIs(self.stubs.saved[-1], self.app.result)  # saved again: the cache matches the view

    def test_trash_in_root_walked_before_the_trash_is_replayed(self):
        p = L + "/Caches/com.example.tool"
        self.trash_during_scan(["Caches"], p)
        self.stubs.scan_gate.set()
        self.join_scans()
        self.assertIsNone(self.find(p, "Caches"))
        self.assertEqual(self.find(L + "/Caches", "Caches")["size"], 700)
        self.assertEqual(self.app.rescan_log, [])

    def test_trash_in_root_walked_during_the_trash_is_rescanned(self):
        self.stubs.walk_s = 3600.0  # the walk of Caches may or may not have counted the item
        self.trash_during_scan(["Caches"], L + "/Caches/com.example.tool")
        self.stubs.scan_gate.set()
        self.assertTrue(self.wait_for(lambda: len(self.stubs.runs) == 2))
        self.join_scans()
        self.assertEqual(self.app.rescan_log, [["Caches"]])
        self.assertEqual(self.stubs.runs[1]["only"], ["Caches"])

    def test_trash_then_cancel_that_comes_too_late(self):
        # the cancel lands in a phase that doesn't check it: the scan still finishes and is swapped in
        self.stubs.ignore_cancel = True
        self.stubs.walk_s = 3600.0
        out, tool = H + "/IdeaProjects/ultimate/out", L + "/Caches/com.example.tool"
        self.trash_during_scan(["Caches"], out)
        status, j = self.jreq("POST", "/api/trash", {"path": tool})
        self.assertEqual(status, 200, j)
        status, _ = self.jreq("POST", "/api/scan/cancel", {})
        self.assertEqual(status, 200)
        self.stubs.scan_gate.set()
        self.assertTrue(self.wait_for(lambda: len(self.stubs.runs) == 2))
        self.join_scans()
        self.assertIsNone(self.find(out, "Home"))  # replayed: the trashed folder doesn't come back
        self.assertEqual(self.stubs.runs[1]["only"], ["Caches"])  # the follow-up survives the cancel

    def test_trash_finishing_after_a_scan_replaced_the_result_rescans(self):
        def slow_move(path, allowed_roots, size=None):  # a whole scan lands while Finder is working
            self.app.start_scan()
            self.app.scan_thread.join(5)
            return {"ok": True, "error": None, "method": "finder", "trashed_to": H + "/.Trash/x"}
        self.stubs.trash.move_to_trash = slow_move
        status, j = self.jreq("POST", "/api/trash", {"path": L + "/Caches/com.example.tool"})
        self.assertEqual(status, 200, j)
        self.assertEqual(j["rescan"], ["Caches"])  # the new result was walked while Finder worked
        self.assertTrue(self.wait_for(lambda: len(self.stubs.runs) == 2))
        self.join_scans()
        self.assertEqual(self.app.rescan_log, [["Caches"]])
        self.assertEqual(self.stubs.runs[1]["only"], ["Caches"])

    def test_action_spanning_a_scan_leaves_the_new_result_alone(self):
        self.stubs.action_gate.clear()
        status, j = self.jreq("POST", "/api/action", {"path": self.GIT, "action": "git_gc", "confirm": True})
        self.assertEqual(status, 202, j)
        self.app.start_scan()  # a full scan lands while gc runs; it may already show gc's effect
        self.app.scan_thread.join(5)
        self.stubs.scan_gate.clear()  # hold the action's follow-up rescan
        self.stubs.action_gate.set()
        jid = j["job"]["id"]
        self.assertTrue(self.wait_for(lambda: self.app.job_by_id(jid)["state"] == "done"))
        self.assertTrue(self.wait_for(lambda: self.app.job_by_id(jid)["rescan"] is not None))
        self.assertEqual(self.find(H, "Home")["size"], 10000)  # not shrunk a second time
        self.assertEqual(self.app.result["git_repos"][0]["size"], 8000)
        self.assertEqual(self.app.job_by_id(jid)["rescan"], ["Home"])
        self.stubs.scan_gate.set()
        self.join_scans()

    # ---- iteration 2: disk ----
    def test_disk_overview_cached(self):
        status, d = self.jreq("GET", "/api/disk")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["container"]["size"], 994)
        status, d = self.jreq("GET", "/api/disk")
        self.assertEqual(self.stubs.disk_calls, 1)
        self.app._disk = None
        def boom():
            raise RuntimeError("diskutil hung")
        self.stubs.disk.overview = boom
        status, d = self.jreq("GET", "/api/disk")  # falls back to the scan's copy
        self.assertEqual(status, 200)
        self.assertTrue(d["stale"])
        self.assertIn("diskutil hung", d["live_error"])
        self.app.result.pop("disk")
        status, d = self.jreq("GET", "/api/disk")
        self.assertEqual(status, 500)
        self.assertIn("diskutil hung", d["error"])
        resp, _ = self.req("GET", "/api/disk", host="evil:%d" % self.port)
        self.assertEqual(resp.status, 403)

    # ---- iteration 2: actions ----
    GIT = H + "/IdeaProjects/ultimate/.git"

    def act(self, action, path=None, **kw):
        body = {"path": path or self.GIT, "action": action, "confirm": True}
        body.update(kw.pop("body", {}))
        return self.jreq("POST", "/api/action", body, **kw)

    def test_action_requires_token_host_and_confirm(self):
        status, _ = self.act("git_delete_temp_packs", token=False)
        self.assertEqual(status, 403)
        status, _ = self.act("git_delete_temp_packs", headers={"X-LL-Token": "nope"})
        self.assertEqual(status, 403)
        status, _ = self.act("git_delete_temp_packs", host="evil.example:%d" % self.port)
        self.assertEqual(status, 403)
        status, _ = self.act("git_delete_temp_packs", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, j = self.act("git_delete_temp_packs", body={"confirm": False})
        self.assertEqual(status, 400)
        self.assertIn("confirm", j["error"])
        status, j = self.act("git_delete_temp_packs", body={"confirm": "yes"})
        self.assertEqual(status, 400)
        self.assertEqual(self.stubs.action_calls, [])

    def test_action_validation(self):
        status, j = self.act("rm_rf")
        self.assertEqual(status, 400)
        self.assertIn("unknown action", j["error"])
        status, _ = self.act("git_gc", path="relative/.git")
        self.assertEqual(status, 400)
        status, _ = self.act("git_gc", path="/etc")
        self.assertEqual(status, 403)
        status, _ = self.act("git_gc", path=H + "/nope/.git")
        self.assertEqual(status, 404)
        # build output node has no actions
        status, j = self.act("git_gc", path=H + "/IdeaProjects/ultimate/out")
        self.assertEqual(status, 409)
        self.assertIn("not available", j["error"])
        # blocked
        n = self.find(self.GIT, "Home")
        n["actions"][2]["blocked"] = "needs 1.1 TB free; delete temp packs first"
        status, j = self.act("git_gc")
        self.assertEqual(status, 409)
        self.assertIn("needs 1.1 TB free", j["error"])
        # not in this node's list
        n["actions"] = [a for a in n["actions"] if a["id"] != "git_worktree_prune"]
        status, j = self.act("git_worktree_prune")
        self.assertEqual(status, 409)
        self.assertEqual(self.stubs.action_calls, [])
        status, jobs = self.jreq("GET", "/api/jobs")
        self.assertEqual(jobs, [])

    def test_action_falls_back_to_git_repos_descriptors(self):
        n = self.find(self.GIT, "Home")
        n.pop("actions")
        self.app.result["git_repos"][0]["actions"] = [dict(A_PRUNE)]
        status, j = self.act("git_worktree_prune", path=H + "/IdeaProjects/ultimate")
        self.assertEqual(status, 200, j)
        self.assertEqual(self.stubs.action_calls[0][:2], ("git_worktree_prune", H + "/IdeaProjects/ultimate"))

    def test_sync_action_success_updates_and_rescans_root(self):
        self.stubs.scan_gate.clear()  # hold the follow-up rescan so the immediate update can be inspected
        status, j = self.act("git_delete_temp_packs")
        self.assertEqual(status, 200, j)
        self.assertTrue(j["ok"])
        self.assertEqual(j["freed"], 3000)
        self.assertEqual(j["job"]["state"], "done")
        self.assertEqual(j["job"]["rescan"], ["Home"])
        aid, path, roots = self.stubs.action_calls[0]
        self.assertEqual((aid, path), ("git_delete_temp_packs", self.GIT))
        self.assertIn(H, roots)
        self.assertIn(L + "/Caches", roots)
        # immediate update: sizes, details, lists, insights recomputed
        self.assertEqual(self.find(self.GIT, "Home")["size"], 5000)
        self.assertEqual(self.find(H, "Home")["size"], 7000)
        repo = self.app.result["git_repos"][0]
        self.assertEqual((repo["size"], repo["garbage_size"], repo["garbage_files"]), (5000, 0, []))
        self.assertNotIn("git_delete_temp_packs", [a["id"] for a in self.find(self.GIT, "Home")["actions"]])
        self.assertEqual([b["path"] for b in self.app.result["big_files"]], [L + "/Caches/com.example.tool/huge.bin"])
        self.assertEqual(self.stubs.reannotated, 1)
        self.assertEqual(self.app.result["summary"]["total"], 13000)
        # background rescan of only the affected root
        self.assertEqual(self.app.rescan_log, [["Home"]])
        self.assertTrue(self.app.status_json()["running"])
        self.stubs.scan_gate.set()
        self.join_scans()
        self.assertEqual(self.stubs.runs[-1]["only"], ["Home"])
        status, jobs = self.jreq("GET", "/api/jobs")
        self.assertEqual([x["state"] for x in jobs], ["done"])

    def test_background_job_lifecycle(self):
        self.stubs.action_gate.clear()
        self.stubs.action_result = {"ok": True, "error": None, "freed": 2000, "details": None}
        status, j = self.act("git_gc")
        self.assertEqual(status, 202, j)
        self.assertTrue(j["background"])
        jid = j["job"]["id"]
        self.assertEqual(j["job"]["state"], "running")
        tail = lambda: (self.jreq("GET", "/api/jobs")[1][0]["output_tail"])
        self.assertTrue(self.wait_for(lambda: "line2" in tail()))
        status, jobs = self.jreq("GET", "/api/jobs")
        self.assertEqual(status, 200)
        job = jobs[0]
        for k in ("id", "action", "path", "state", "output_tail", "started_at"):
            self.assertIn(k, job)
        self.assertEqual((job["id"], job["action"], job["path"], job["state"]), (jid, "git_gc", self.GIT, "running"))
        self.assertEqual(job["output_tail"], "Counting objects: 100% (2/2), done.\nline2")
        status, st = self.jreq("GET", "/api/status")
        self.assertEqual(st["jobs_running"], 1)
        # a second action on the same repo while the job runs
        status, j2 = self.act("git_delete_temp_packs")
        self.assertEqual(status, 409)
        self.assertIn("still running", j2["error"])
        self.assertEqual(self.app.rescan_log, [])
        self.stubs.action_gate.set()
        for t in self.app.job_threads:
            t.join(5)
        status, jobs = self.jreq("GET", "/api/jobs")
        self.assertEqual(jobs[0]["state"], "done")
        self.assertEqual(jobs[0]["freed"], 2000)
        self.assertIsNotNone(jobs[0]["finished_at"])
        self.assertEqual(self.app.rescan_log, [["Home"]])
        self.join_scans()
        self.assertEqual(self.stubs.runs[-1]["only"], ["Home"])

    def test_failed_action_no_rescan(self):
        self.stubs.action_result = {"ok": False, "error": "gc is running", "freed": 0, "details": None}
        status, j = self.act("git_delete_temp_packs")
        self.assertEqual(status, 500)
        self.assertEqual(j["error"], "gc is running")
        self.assertEqual(j["job"]["state"], "failed")
        self.assertEqual(self.find(self.GIT, "Home")["size"], 8000)
        self.assertEqual(self.app.rescan_log, [])
        def boom(*a, **kw):
            raise OSError("lsof missing")
        self.stubs.actions.run = boom
        status, j = self.act("git_worktree_prune")
        self.assertEqual(status, 500)
        self.assertIn("lsof missing", j["error"])
        self.assertEqual(self.stubs.runs, [])

    # ---- iteration 2: trash ----
    def test_trash_git_dir_refused(self):
        for p in (self.GIT, self.GIT + "/objects/pack/tmp_pack_a"):
            status, j = self.jreq("POST", "/api/trash", {"path": p, "force": True})
            self.assertEqual(status, 409, p)
            self.assertIn("git cleanup actions", j["error"])
        self.assertEqual(self.stubs.trashed, [])

    def test_trash_inside_bare_repo_below_tree_refused(self):
        # a bare repo deeper than the tree: no '.git' component, no git_dir node, only a special entry
        bare = H + "/IdeaProjects/mirrors/a/mirror.git"
        pack = bare + "/objects/pack/pack-x.pack"
        self.app.result["special"].append({"path": bare, "kind": "git_dir", "size": 1200, "files": 3,
                                           "mtime": 1.0, "root": "Home"})
        self.app.result["big_files"].append({"path": pack, "size": 1200, "mtime": 1.0, "root": "Home"})
        status, j = self.jreq("POST", "/api/trash", {"path": pack, "force": True})
        self.assertEqual(status, 409, j)
        self.assertIn("git cleanup actions", j["error"])
        self.assertEqual(self.stubs.trashed, [])

    def test_trash_big_file_not_in_tree(self):
        p = L + "/Caches/com.example.tool/huge.bin"
        status, j = self.jreq("POST", "/api/trash", {"path": p})
        self.assertEqual(status, 200, j)
        self.assertEqual(j["freed"], 200)
        self.assertEqual(self.find(L + "/Caches/com.example.tool", "Caches")["size"], 100)
        self.assertEqual(self.find(L + "/Caches", "Caches")["size"], 800)
        self.assertNotIn(p, [b["path"] for b in self.app.result["big_files"]])
        self.assertEqual(self.stubs.reannotated, 1)

    def test_trash_prunes_special_entries(self):
        p = H + "/IdeaProjects/ultimate/out"
        status, j = self.jreq("POST", "/api/trash", {"path": p})
        self.assertEqual(status, 200, j)
        self.assertEqual([e["path"] for e in self.app.result["special"]], [self.GIT])
        self.assertEqual(self.find(H, "Home")["size"], 9000)
        self.assertEqual(len(self.app.result["git_repos"]), 1)

    def test_trash_link_stub_refused(self):
        status, j = self.jreq("POST", "/api/trash", {"path": L})
        self.assertEqual(status, 409)
        self.assertEqual(self.stubs.trashed, [])

    def test_trash_falls_back_to_insights_without_pipeline(self):
        def broken(r):
            raise ImportError("No module named 'librarylens.detectors'")
        self.stubs.pipeline.reannotate = broken
        status, j = self.jreq("POST", "/api/trash", {"path": L + "/Caches/com.example.tool"})
        self.assertEqual(status, 200, j)
        self.assertNotIn("warning", j)
        self.assertEqual(j["summary"]["total"], 15700)
        self.assertEqual(len(self.stubs.saved), 1)


if __name__ == "__main__":
    unittest.main()
