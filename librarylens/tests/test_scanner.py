from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from librarylens import scanner

KB = 1024


def _write(path: str, size: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))


def _alloc(path: str) -> int:
    return os.lstat(path).st_blocks * 512


def _tree_alloc(path: str) -> int:
    """Allocated bytes of ``path`` and everything below it (hard links once)."""
    total = _alloc(path)
    seen = set()
    for dirpath, dirnames, filenames in os.walk(path):
        for d in dirnames:
            if not os.path.islink(os.path.join(dirpath, d)):
                total += _alloc(os.path.join(dirpath, d))
        for f in filenames:
            st = os.lstat(os.path.join(dirpath, f))
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_blocks * 512
    return total


def _touch(path: str, data: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _child(node: dict, name: str) -> dict:
    for c in node["children"]:
        if c["name"] == name:
            return c
    raise AssertionError("no child %r in %r" % (name, [c["name"] for c in node["children"]]))


class ScannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="librarylens-test-"))

    def tearDown(self) -> None:
        for dirpath, dirnames, _ in os.walk(self.tmp):
            for d in dirnames:
                p = os.path.join(dirpath, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def test_size_and_files_match_allocation(self) -> None:
        _write(self.p("a", "f1"), 64 * KB)
        _write(self.p("a", "b", "f2"), 32 * KB)
        _write(self.p("f3"), 8 * KB)
        expected = 0
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            expected += _alloc(dirpath)
            expected += sum(_alloc(os.path.join(dirpath, f)) for f in filenames)
        node = scanner.scan_path(self.tmp, min_size=1)
        self.assertEqual(node["size"], expected)
        self.assertEqual(node["files"], 3)
        self.assertTrue(node["is_dir"])
        self.assertIsNone(node["error"])
        self.assertGreaterEqual(node["mtime"], os.lstat(self.p("a", "b", "f2")).st_mtime)

    def test_hard_links_counted_once(self) -> None:
        _write(self.p("x", "big"), 128 * KB)
        os.makedirs(self.p("y"))
        os.link(self.p("x", "big"), self.p("y", "big-link"))
        os.link(self.p("x", "big"), self.p("x", "big-link2"))
        node = scanner.scan_path(self.tmp, min_size=1)
        file_alloc = _alloc(self.p("x", "big"))
        dirs = sum(_alloc(self.p(d)) for d in ("", "x", "y"))
        self.assertEqual(node["size"], dirs + file_alloc)
        self.assertEqual(node["files"], 1)

    def test_symlinks_not_followed(self) -> None:
        outside = tempfile.mkdtemp(prefix="librarylens-outside-")
        try:
            _write(os.path.join(outside, "huge"), 256 * KB)
            _write(self.p("real", "f"), 16 * KB)
            os.symlink(outside, self.p("link-out"))
            os.symlink(self.p("real"), self.p("link-in"))
            os.symlink(os.path.join(outside, "huge"), self.p("file-link"))
            node = scanner.scan_path(self.tmp, min_size=1)
            self.assertLess(node["size"], 256 * KB)
            self.assertEqual(node["files"], 1)
            names = {c["name"] for c in node["children"] if c["is_dir"]}
            self.assertEqual(names, {"real"})
            # a symlink given as the root is reported, not followed
            link_node = scanner.scan_path(self.p("link-out"))
            self.assertFalse(link_node["is_dir"])
            self.assertLess(link_node["size"], 64 * KB)
            self.assertTrue(link_node["error"].startswith("symlink"))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_min_size_collapsing(self) -> None:
        _write(self.p("big.bin"), 200 * KB)
        _write(self.p("bigdir", "f"), 150 * KB)
        _write(self.p("s1"), 4 * KB)
        _write(self.p("s2"), 4 * KB)
        _write(self.p("smalldir", "a"), 4 * KB)
        _write(self.p("smalldir", "b"), 4 * KB)
        node = scanner.scan_path(self.tmp, min_size=100 * KB)
        names = [c["name"] for c in node["children"]]
        self.assertEqual(names[:2], ["big.bin", "bigdir"])  # sorted by size desc
        big = _child(node, "big.bin")
        self.assertFalse(big["is_dir"])
        self.assertEqual(big["files"], 1)
        syn = [c for c in node["children"] if c.get("synthetic")]
        self.assertEqual(len(syn), 1)
        syn = syn[0]
        self.assertEqual(syn["name"], "(3 smaller items)")
        self.assertEqual(syn["path"], self.tmp)
        self.assertFalse(syn["is_dir"])
        self.assertEqual(syn["files"], 4)
        self.assertEqual(syn["size"], _alloc(self.p("s1")) + _alloc(self.p("s2"))
                         + _alloc(self.p("smalldir")) + _alloc(self.p("smalldir", "a"))
                         + _alloc(self.p("smalldir", "b")))
        self.assertEqual(sum(c["files"] for c in node["children"]), node["files"])
        self.assertEqual(sum(c["size"] for c in node["children"]) + _alloc(self.tmp),
                         node["size"])
        sizes = [c["size"] for c in node["children"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        # find_node skips synthetic nodes, finds real ones
        result = {"roots": [node]}
        self.assertIs(scanner.find_node(result, self.p("bigdir")), _child(node, "bigdir"))
        self.assertIs(scanner.find_node(result, self.tmp), node)
        self.assertIsNone(scanner.find_node(result, self.p("smalldir")))

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_permission_error(self) -> None:
        _write(self.p("ok", "f"), 64 * KB)
        _write(self.p("locked", "secret"), 64 * KB)
        _write(self.p("deep", "x", "locked2", "s"), 64 * KB)
        os.chmod(self.p("locked"), 0)
        os.chmod(self.p("deep", "x", "locked2"), 0)
        try:
            node = scanner.scan_path(self.tmp, max_depth=1, min_size=0)
            collapsed = scanner.scan_path(self.tmp, max_depth=1, min_size=1 << 30)
        finally:
            os.chmod(self.p("locked"), 0o755)
            os.chmod(self.p("deep", "x", "locked2"), 0o755)
        self.assertIsNone(node["error"])
        locked = _child(node, "locked")
        self.assertEqual(locked["error"], "permission")
        self.assertEqual(locked["files"], 0)
        self.assertEqual(locked["size"], _alloc(self.p("locked")))  # partial: own blocks
        self.assertIsNone(_child(node, "ok")["error"])
        self.assertEqual(_child(node, "ok")["files"], 1)
        # error below max_depth is reported on the deepest kept node
        self.assertEqual(_child(node, "deep")["error"], "permission")
        self.assertEqual(node["files"], 1)
        # errors of collapsed children are summarised on the synthetic node
        self.assertEqual(collapsed["children"][0]["error"], "2 unreadable")

    def test_max_depth(self) -> None:
        _write(self.p("a", "b", "c", "d", "f"), 64 * KB)
        _write(self.p("a", "b", "big"), 64 * KB)
        node = scanner.scan_path(self.tmp, max_depth=2, min_size=1)
        a = _child(node, "a")
        b = _child(a, "b")
        self.assertEqual(b["children"], [])
        self.assertEqual(b["files"], 2)
        self.assertEqual(node["files"], 2)
        self.assertGreaterEqual(b["size"], 128 * KB)
        self.assertEqual(scanner.scan_path(self.tmp, max_depth=0)["children"], [])
        # deep tree: no recursion limit issues
        deep = self.p("deep")
        rel = os.path.join(*(["d"] * 300))
        os.makedirs(os.path.join(deep, rel))
        _write(os.path.join(deep, rel, "leaf"), 4 * KB)
        self.assertEqual(scanner.scan_path(deep)["files"], 1)

    def test_scan_all_nested_and_cache(self) -> None:
        _write(self.p("outer", "inner", "f"), 64 * KB)
        _write(self.p("outer", "g"), 64 * KB)
        _write(self.p("other", "h"), 64 * KB)
        roots = [
            {"label": "Outer", "path": self.p("outer"), "category": "cache"},
            {"label": "Inner", "path": self.p("outer", "inner"), "category": "docker"},
            {"label": "Other", "path": self.p("other"), "category": "dev_cache"},
        ]
        seen = []
        res = scanner.scan_all(roots, workers=3, progress=seen.append)
        self.assertEqual(set(res), {"scanned_at", "duration_s", "home", "roots", "big_files",
                                    "special"})
        for r in res["roots"]:
            self.assertEqual(r["exclude"], [])
            self.assertGreaterEqual(r["scanned_at"], res["scanned_at"])
            self.assertGreaterEqual(r["duration_s"], 0)
            self.assertLessEqual(r["duration_s"], res["duration_s"] + 0.01)
        by = {r["label"]: r for r in res["roots"]}
        self.assertIsNone(by["Outer"]["nested_in"])
        self.assertEqual(by["Inner"]["nested_in"], "Outer")
        self.assertIsNone(by["Other"]["nested_in"])
        self.assertEqual(by["Inner"]["files"], 1)
        self.assertEqual(by["Outer"]["files"], 2)
        self.assertEqual(by["Inner"]["category"], "docker")
        self.assertIs(scanner.find_node(res, self.p("outer", "inner")), by["Inner"])
        self.assertIsNone(scanner.find_node(res, self.p("nope")))
        cache = self.p("cache", "scan.json")
        with mock.patch.object(scanner, "CACHE_DIR", self.p("cache")), \
                mock.patch.object(scanner, "CACHE_FILE", cache):
            self.assertIsNone(scanner.load_cache())
            scanner.save_cache(res)
            self.assertEqual(scanner.load_cache(), res)
            self.assertEqual(os.listdir(self.p("cache")), ["scan.json"])
            with open(cache, "w") as f:
                f.write("{broken")
            self.assertIsNone(scanner.load_cache())

    def test_save_cache_from_concurrent_threads(self) -> None:
        # the server saves from the scan thread and from trash/action threads at the same time
        cache = self.p("cache", "scan.json")
        results = [{"roots": [], "n": i, "pad": "x" * 200000} for i in range(6)]
        errors = []

        def save(r: dict) -> None:
            try:
                for _ in range(8):
                    scanner.save_cache(r)
            except Exception as e:
                errors.append(e)

        with mock.patch.object(scanner, "CACHE_DIR", self.p("cache")), \
                mock.patch.object(scanner, "CACHE_FILE", cache):
            threads = [threading.Thread(target=save, args=(r,)) for r in results]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertFalse(any(t.is_alive() for t in threads), "save_cache threads did not finish")
            self.assertEqual(errors, [])
            self.assertIn(scanner.load_cache(), results)
            self.assertEqual(os.listdir(self.p("cache")), ["scan.json"])

    def test_default_roots(self) -> None:
        roots = scanner.default_roots()
        labels = [r["label"] for r in roots]
        self.assertEqual(len(labels), len(set(labels)))
        cats = {"cache", "app_support", "container", "group_container", "logs",
                "dev_cache", "bazel", "docker", "other", "home", "library_other",
                "applications", "system"}
        for r in roots:
            self.assertTrue(os.path.exists(r["path"]), r)
            self.assertIn(r["category"], cats)
            self.assertNotIn("~", r["path"])
            self.assertNotIn("$USER", r["path"])
            self.assertIsInstance(r["exclude"], list)
        by = {r["label"]: r for r in roots}
        home = os.path.expanduser("~")
        self.assertEqual(by["Home"]["path"], home)
        lib = os.path.join(home, "Library")
        if "Library (other)" in by:
            self.assertIn(lib, by["Home"]["exclude"])
            self.assertEqual(by["Library (other)"]["category"], "library_other")
            for label in ("Caches", "Application Support", "Logs"):
                if label in by:
                    self.assertIn(by[label]["path"], by["Library (other)"]["exclude"])
                    self.assertNotIn(by[label]["path"], by["Home"]["exclude"])  # outermost only
        for r in roots:  # every root inside ~ is excluded from Home or nested/excluded deeper
            if r["label"] != "Home" and scanner._is_within(r["path"], home):
                self.assertTrue(any(r["path"] == e or scanner._is_within(r["path"], e)
                                    for e in by["Home"]["exclude"]), r)
        if "Bazel output" in by and "/private/var/tmp" in by:
            self.assertEqual(by["/private/var/tmp"]["exclude"], [by["Bazel output"]["path"]])
        nested = dict(zip([r["label"] for r in roots], scanner._compute_nested(roots)))
        self.assertIsNone(nested["Home"])
        for label in ("Caches", "Gradle", "Library (other)", "Bazel output"):
            if label in nested:
                self.assertIsNone(nested[label], label)
        if "Docker Desktop" in nested and "Containers" in nested:
            self.assertEqual(nested["Docker Desktop"], "Containers")


class ScannerScandirTest(ScannerTest):
    """Same tests through the portable os.scandir + lstat fallback."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(scanner, "_USE_BULK", False)
        patcher.start()
        self.addCleanup(patcher.stop)


class ScannerV2Test(unittest.TestCase):
    """Iteration 2: exclude/stubs, nested_in, only/prev, cancel, big_files, special."""

    use_bulk = True

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="librarylens-test2-"))
        patcher = mock.patch.object(scanner, "_USE_BULK", self.use_bulk and scanner._BULK is not None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # location-specific rules point into the temp dir, never at the real home
        sp = mock.patch.object(scanner, "_special_paths", lambda: (
            self.p("home", ".gradle"),
            self.p("home", "Library", "Application Support", "MobileSync", "Backup")))
        sp.start()
        self.addCleanup(sp.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def scan(self, roots, **kw) -> dict:
        kw.setdefault("workers", 3)
        kw.setdefault("min_size", 1)
        return scanner.scan_all(roots, **kw)

    def specials(self, res: dict) -> dict:
        return {s["path"]: s for s in res["special"]}

    # -- exclude / stubs / nested_in ----------------------------------------

    def test_symlinked_inner_root_scans_its_target(self) -> None:
        # ~/.gradle -> ~/dev/gradle-home: Home excludes the target, so the Gradle root must walk it
        home = self.p("home")
        _write(self.p("home", "doc", "a"), 16 * KB)
        _write(self.p("home", "dev", "gradle-home", "caches", "c"), 64 * KB)
        _write(self.p("home", "dev", "notes"), 8 * KB)
        os.symlink(self.p("home", "dev", "gradle-home"), self.p("home", ".gradle"))
        cands = [("Gradle", self.p("home", ".gradle"), "dev_cache"), ("Home", home, "home")]
        with mock.patch.object(scanner, "_CANDIDATES", cands):
            roots = scanner.default_roots()
        by = {r["label"]: r for r in roots}
        self.assertEqual(by["Gradle"]["path"], self.p("home", "dev", "gradle-home"))
        self.assertEqual(by["Home"]["exclude"], [self.p("home", "dev", "gradle-home")])
        res = self.scan(roots)
        got = {r["label"]: r for r in res["roots"]}
        self.assertIsNone(got["Gradle"]["error"])
        self.assertGreaterEqual(got["Gradle"]["size"], 64 * KB)  # counted once, by Gradle...
        dev = next(c for c in got["Home"]["children"] if c["path"] == self.p("home", "dev"))
        self.assertIn("Gradle", [c.get("link_root") for c in dev["children"]])  # ...and stubbed in Home
        expected = sum(_alloc(os.path.join(d, f)) for d, _, fs in os.walk(home) for f in fs) \
            + sum(_alloc(d) for d, _, _ in os.walk(home)) + os.lstat(self.p("home", ".gradle")).st_blocks * 512
        self.assertEqual(got["Gradle"]["size"] + got["Home"]["size"], expected)

    def test_exclude_stub_and_nested(self) -> None:
        home = self.p("home")
        _write(self.p("home", "doc", "a"), 16 * KB)
        _write(self.p("home", "Library", "Caches", "c"), 32 * KB)
        _write(self.p("home", "Library", "Prefs", "p"), 8 * KB)
        _write(self.p("home", "Library", "Containers", "docker", "d"), 8 * KB)
        _write(self.p("home", "go", "pkg", "mod", "m"), 64 * KB)
        _write(self.p("home", "go", "src", "s"), 4 * KB)
        _write(self.p("home", "a", "b", "c", "deep", "z"), 64 * KB)  # excluded below max_depth
        _write(self.p("home", "a", "b", "c", "keep"), 4 * KB)
        roots = [
            {"label": "Caches", "path": self.p("home", "Library", "Caches"), "category": "cache"},
            {"label": "Containers", "path": self.p("home", "Library", "Containers"),
             "category": "container"},
            {"label": "Docker", "path": self.p("home", "Library", "Containers", "docker"),
             "category": "docker"},
            {"label": "Go", "path": self.p("home", "go", "pkg"), "category": "dev_cache"},
            {"label": "Deep", "path": self.p("home", "a", "b", "c", "deep"), "category": "other"},
            {"label": "Home", "path": home, "category": "home",
             "exclude": [self.p("home", "Library"), self.p("home", "go", "pkg"),
                         self.p("home", "a", "b", "c", "deep")]},
            {"label": "Library (other)", "path": self.p("home", "Library"),
             "category": "library_other",
             "exclude": [self.p("home", "Library", "Caches"),
                         self.p("home", "Library", "Containers")]},
        ]
        res = self.scan(roots, max_depth=3)
        by = {r["label"]: r for r in res["roots"]}
        self.assertEqual(by["Home"]["exclude"], roots[5]["exclude"])
        self.assertIsNone(by["Home"]["nested_in"])
        self.assertIsNone(by["Library (other)"]["nested_in"])
        self.assertIsNone(by["Caches"]["nested_in"])
        self.assertIsNone(by["Go"]["nested_in"])
        self.assertIsNone(by["Deep"]["nested_in"])
        self.assertEqual(by["Docker"]["nested_in"], "Containers")  # Containers doesn't exclude it
        # Home counts doc, go/src, a/b/c/keep only
        self.assertEqual(by["Home"]["files"], 3)
        stub = _child(by["Home"], "Library")
        self.assertEqual(stub["link_root"], "Library (other)")
        self.assertEqual((stub["size"], stub["files"], stub["children"]), (0, 0, []))
        self.assertTrue(stub["is_dir"])
        go = _child(by["Home"], "go")
        self.assertEqual(go["files"], 1)
        self.assertEqual(_child(go, "pkg")["link_root"], "Go")  # stub at depth 2
        self.assertEqual(_child(go, "src")["files"], 1)
        c = _child(_child(_child(by["Home"], "a"), "b"), "c")  # depth 3: no children kept
        self.assertEqual((c["files"], c["children"]), (1, []))
        self.assertLess(c["size"], 64 * KB)
        lib = by["Library (other)"]
        self.assertEqual(lib["files"], 1)
        self.assertEqual(_child(lib, "Caches")["link_root"], "Caches")
        self.assertEqual(_child(lib, "Containers")["link_root"], "Containers")
        # stubs survive min_size collapsing
        res2 = self.scan(roots, max_depth=3, min_size=1 << 30)
        names = [ch["name"] for ch in {r["label"]: r for r in res2["roots"]}["Home"]["children"]]
        self.assertIn("Library", names)
        # non-nested roots partition the files: no double counting
        total = sum(r["files"] for r in res["roots"] if not r["nested_in"])
        self.assertEqual(total, 8)

    def test_exclude_scan_path(self) -> None:
        _write(self.p("x", "skip", "f"), 64 * KB)
        _write(self.p("x", "keep", "f"), 8 * KB)
        node = scanner.scan_path(self.p("x"), min_size=1, exclude=[self.p("x", "skip")])
        self.assertEqual(node["files"], 1)
        self.assertIsNone(_child(node, "skip")["link_root"])

    # -- only / prev ----------------------------------------------------------

    def test_only_prev_merge(self) -> None:
        _write(self.p("a", "big1"), 64 * KB)
        _write(self.p("a", "proj", ".git", "objects", "o"), 8 * KB)
        _write(self.p("b", "big2"), 48 * KB)
        _write(self.p("b", "proj", "node_modules", "m"), 8 * KB)
        roots = [{"label": "A", "path": self.p("a"), "category": "other"},
                 {"label": "B", "path": self.p("b"), "category": "other"}]
        first = self.scan(roots, big_file_min=40 * KB)
        self.assertEqual([e["root"] for e in first["big_files"]], ["A", "B"])
        a0 = {r["label"]: r for r in first["roots"]}["A"]
        time.sleep(0.05)
        _write(self.p("b", "big3"), 96 * KB)
        shutil.rmtree(self.p("b", "proj", "node_modules"))
        _write(self.p("b", "out", "x"), 4 * KB)
        _touch(self.p("b", "proj.iml"))
        _write(self.p("a", "ignored-new"), 200 * KB)  # A is not rescanned -> not seen
        second = self.scan(roots, only=["B"], prev=first, big_file_min=40 * KB)
        by = {r["label"]: r for r in second["roots"]}
        self.assertEqual(by["A"]["files"], a0["files"])
        self.assertEqual(by["A"]["scanned_at"], a0["scanned_at"])
        self.assertIsNot(by["A"], a0)  # copied, not shared
        self.assertGreater(by["B"]["scanned_at"], a0["scanned_at"])
        self.assertEqual([(e["root"], os.path.basename(e["path"])) for e in second["big_files"]],
                         [("B", "big3"), ("A", "big1"), ("B", "big2")])
        kinds = {(s["root"], os.path.basename(s["path"]), s["kind"]) for s in second["special"]}
        self.assertEqual(kinds, {("A", ".git", "git_dir"), ("B", "out", "build_output")})
        self.assertEqual([r["label"] for r in second["roots"]], ["A", "B"])
        # only without prev -> full scan; unknown labels ignored
        full = self.scan(roots, only=["nope"], big_file_min=40 * KB)
        self.assertIn("ignored-new", {os.path.basename(e["path"]) for e in full["big_files"]})
        # big_files_limit applies after the merge
        lim = self.scan(roots, only=["B"], prev=first, big_file_min=40 * KB, big_files_limit=2)
        self.assertEqual([os.path.basename(e["path"]) for e in lim["big_files"]],
                         ["big3", "big1"])

    # -- cancel ---------------------------------------------------------------

    def test_cancel(self) -> None:
        for i in range(40):
            for j in range(40):
                os.makedirs(self.p("t", "d%d" % i, "e%d" % j))
        ev = threading.Event()
        calls = []

        def progress(msg: str) -> None:
            calls.append(msg)
            ev.set()

        ev.set()  # already set: must stop immediately
        t0 = time.time()
        with self.assertRaises(scanner.Cancelled):
            self.scan([{"label": "T", "path": self.p("t"), "category": "other"}], cancel=ev,
                      workers=4)
        self.assertLess(time.time() - t0, 2.0)
        self.assertFalse([t for t in threading.enumerate()
                          if t.name.startswith("librarylens-scan") and t.is_alive()])
        # set from another thread while scanning
        ev2 = threading.Event()
        timer = threading.Timer(0.01, ev2.set)
        timer.start()
        try:
            with self.assertRaises(scanner.Cancelled):
                with mock.patch.object(scanner._Walker, "_post", _slow_post):
                    self.scan([{"label": "T", "path": self.p("t"), "category": "other"}],
                              cancel=ev2, workers=2)
        finally:
            timer.cancel()
        self.assertFalse([t for t in threading.enumerate()
                          if t.name.startswith("librarylens-scan") and t.is_alive()])
        self.assertTrue(issubclass(scanner.Cancelled, Exception))

    # -- big files -------------------------------------------------------------

    def test_big_files(self) -> None:
        sizes = {"a1": 40, "a2": 80, "deep/x/y/z/a3": 120, "small": 4, "h/a4": 60}
        for name, kb in sizes.items():
            _write(self.p("r", *name.split("/")), kb * KB)
        os.link(self.p("r", "h", "a4"), self.p("r", "h", "a4-link"))
        os.symlink(self.p("r", "a2"), self.p("r", "a2-symlink"))
        sparse = self.p("r", "sparse.img")
        with open(sparse, "wb") as f:
            f.truncate(1 << 30)  # 1 GiB apparent, ~0 allocated: not a big file
        res = self.scan([{"label": "R", "path": self.p("r"), "category": "other"}],
                        big_file_min=32 * KB, big_files_limit=3, max_depth=1)
        bf = res["big_files"]
        self.assertEqual([os.path.basename(e["path"]) for e in bf], ["a3", "a2", "a4"])
        self.assertEqual(bf[0]["path"], self.p("r", "deep", "x", "y", "z", "a3"))
        self.assertEqual(bf[0]["size"], _alloc(self.p("r", "deep", "x", "y", "z", "a3")))
        self.assertEqual(bf[0]["mtime"], os.lstat(bf[0]["path"]).st_mtime)
        self.assertEqual(bf[0]["root"], "R")
        sizes_ = [e["size"] for e in bf]
        self.assertEqual(sizes_, sorted(sizes_, reverse=True))
        res = self.scan([{"label": "R", "path": self.p("r"), "category": "other"}],
                        big_file_min=32 * KB)
        self.assertEqual(len(res["big_files"]), 4)  # hard link counted once, symlink skipped

    # -- special dirs ------------------------------------------------------------

    def test_special_kinds(self) -> None:
        r = self.p("home")
        mk = lambda *parts: _write(self.p("home", *parts), 8 * KB)  # noqa: E731
        mk("proj", ".git", "objects", "pack", "p.pack")
        mk("proj", ".git", "HEAD")
        mk("bare.git", "objects", "o")
        _touch(self.p("home", "bare.git", "refs", "heads", "main"))
        _touch(self.p("home", "bare.git", "HEAD"))
        mk("notbare", "objects", "o")  # objects/ + refs/ but no HEAD
        os.makedirs(self.p("home", "notbare", "refs"))
        mk("web", "node_modules", "lib", "i.js")
        mk("web", "node_modules", "lib", "node_modules", "inner", "j.js")  # outermost only
        mk("web", "node_modules", "pkg", ".git", "HEAD")
        _touch(self.p("home", "web", "package.json"))
        mk("web", "dist", "bundle.js")
        mk("web", "build", "b.js")
        mk("web", ".next", "n")
        mk("mvn", "target", "classes", "A.class")
        _touch(self.p("home", "mvn", "pom.xml"))
        mk("plain", "target", "t")  # no marker -> not special
        mk("plain", "build", "t")
        mk("plain", "out", "t")
        mk("plain", "dist", "t")
        mk("rust", "target", "debug", "x")
        _touch(self.p("home", "rust", "Cargo.toml"))
        mk("gradle", "build", "classes", "x")
        _touch(self.p("home", "gradle", "build.gradle.kts"))
        mk("gradle", ".gradle", "8.0", "x")
        mk(".gradle", "caches", "x")  # ~/.gradle is not a project-level .gradle
        mk("ij", "out", "production", "x")
        _touch(self.p("home", "ij", "ij.iml"))
        mk("bz", "out", "x")
        _touch(self.p("home", "bz", "MODULE.bazel"))
        mk("cm", "cmake-build-debug", "x")
        mk("py", ".venv", "lib", "x")
        _touch(self.p("home", "py", ".venv", "pyvenv.cfg"))
        mk("py", "venv", "x")  # no pyvenv.cfg
        mk("py", "__pycache__", "x.pyc")
        mk("py", ".tox", "x")
        mk("Library", "Developer", "Xcode", "DerivedData", "App-abc", "x")
        mk("Parallels", "Windows 11.pvm", "disk.hdd", "img")
        mk("Parallels", "Windows 11.pvm", "inner.dmg")  # inside a VM: not separate
        mk("UTM", "Linux.utm", "Data", "x")
        mk("Pictures", "Photos Library.photoslibrary", "originals", "x")
        mk("Library", "Application Support", "MobileSync", "Backup", "00008110-AAAA", "Manifest.db")
        mk("Library", "Application Support", "MobileSync", "Backup", "00008110-BBBB", "x")
        mk("Downloads", "ubuntu.iso")
        mk("Downloads", "small.dmg")
        mk("Downloads", "notes.txt")
        mk("VirtualBox VMs", "Win", "Win.vdi")
        _touch(self.p("home", "VirtualBox VMs", "Win", "Win.vbox"))
        mk("VirtualBox VMs", "Win", "Logs", "x.log")
        with mock.patch.object(scanner, "_IMG_MIN", 6 * KB):
            with mock.patch.object(scanner, "_IMG_EXT", scanner._IMG_EXT):
                res = self.scan([{"label": "Home", "path": r, "category": "home"}], max_depth=1)
        sp = self.specials(res)
        rel = {os.path.relpath(p, r): s["kind"] for p, s in sp.items()}
        expected = {
            "proj/.git": "git_dir",
            "bare.git": "git_dir",
            "web/node_modules": "build_output",
            "web/dist": "build_output",
            "web/build": "build_output",
            "web/.next": "build_output",
            "mvn/target": "build_output",
            "rust/target": "build_output",
            "gradle/build": "build_output",
            "gradle/.gradle": "build_output",
            "ij/out": "build_output",
            "bz/out": "build_output",
            "cm/cmake-build-debug": "build_output",
            "py/.venv": "build_output",
            "py/.tox": "build_output",
            "Library/Developer/Xcode/DerivedData": "build_output",
            "Parallels/Windows 11.pvm": "vm",
            "UTM/Linux.utm": "vm",
            "Pictures/Photos Library.photoslibrary": "photos_library",
            "Library/Application Support/MobileSync/Backup/00008110-AAAA": "ios_backup",
            "Library/Application Support/MobileSync/Backup/00008110-BBBB": "ios_backup",
            "Downloads/ubuntu.iso": "disk_image",
            "Downloads/small.dmg": "disk_image",
            "VirtualBox VMs/Win": "vm",
        }
        self.assertEqual(rel, expected)
        for path, s in sp.items():
            self.assertEqual(s["root"], "Home")
            if os.path.isdir(path):
                self.assertEqual(s["size"], _tree_alloc(path), path)
                files = sum(len(f) for _, _, f in os.walk(path))
                self.assertEqual(s["files"], files, path)
                newest = max([os.lstat(path).st_mtime] + [
                    os.lstat(os.path.join(dp, n)).st_mtime
                    for dp, dn, fn in os.walk(path) for n in dn + fn])
                self.assertEqual(s["mtime"], newest, path)
            else:
                self.assertEqual((s["size"], s["files"]), (_alloc(path), 1))
        # special dirs far below max_depth still roll up into the kept ancestor
        root = res["roots"][0]
        self.assertEqual(root["files"], sum(len(f) for _, _, f in os.walk(r)))
        self.assertEqual(root["size"], _tree_alloc(r))
        # default 100 MB threshold: tiny images are not special
        res = self.scan([{"label": "Home", "path": r, "category": "home"}], max_depth=1)
        kinds = {s["kind"] for s in res["special"]}
        self.assertNotIn("disk_image", kinds)
        self.assertNotIn(self.p("home", "VirtualBox VMs", "Win"), self.specials(res))

    def test_special_root_label_and_nested_dedupe(self) -> None:
        _write(self.p("outer", "inner", "app", "node_modules", "x"), 8 * KB)
        _write(self.p("outer", "repo.git", "objects", "o"), 8 * KB)
        _touch(self.p("outer", "repo.git", "HEAD"))
        os.makedirs(self.p("outer", "repo.git", "refs"))
        roots = [{"label": "Outer", "path": self.p("outer"), "category": "other"},
                 {"label": "Inner", "path": self.p("outer", "inner"), "category": "docker"}]
        res = self.scan(roots)
        sp = self.specials(res)
        self.assertEqual(len(res["special"]), 2)
        self.assertEqual(sp[self.p("outer", "inner", "app", "node_modules")]["root"], "Inner")
        self.assertEqual(sp[self.p("outer", "repo.git")]["root"], "Outer")
        self.assertEqual(sp[self.p("outer", "repo.git")]["files"], 2)

    def test_special_hard_links_once(self) -> None:
        _write(self.p("r", "node_modules", "a", "f"), 64 * KB)
        os.makedirs(self.p("r", "node_modules", "b"))
        os.link(self.p("r", "node_modules", "a", "f"), self.p("r", "node_modules", "b", "f"))
        res = self.scan([{"label": "R", "path": self.p("r"), "category": "other"}])
        s = res["special"][0]
        self.assertEqual(s["files"], 1)
        self.assertEqual(s["size"], _tree_alloc(self.p("r", "node_modules")))

    def test_unreadable_special(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores permissions")
        _write(self.p("r", ".git", "objects", "o"), 8 * KB)
        os.chmod(self.p("r", ".git", "objects"), 0)
        try:
            res = self.scan([{"label": "R", "path": self.p("r"), "category": "other"}],
                            max_depth=3, min_size=0)
        finally:
            os.chmod(self.p("r", ".git", "objects"), 0o755)
        git = _child(res["roots"][0], ".git")
        self.assertEqual(_child(git, "objects")["error"], "permission")
        self.assertEqual(res["special"][0]["files"], 0)


class ScannerV2ScandirTest(ScannerV2Test):
    use_bulk = False


_orig_post = scanner._Walker._post


def _slow_post(self, *args, **kwargs):
    time.sleep(0.002)
    return _orig_post(self, *args, **kwargs)


if __name__ == "__main__":
    unittest.main()
