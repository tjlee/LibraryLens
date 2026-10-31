"""Tests for insights.annotate on synthetic fixtures mirroring a real machine."""
from __future__ import annotations

import copy
import time
import unittest
from typing import Any, Dict, List, Optional

from librarylens import insights

GB = 1 << 30
NOW = 1_800_000_000.0
DAY = 86400.0
FRESH = NOW - 2 * DAY
OLD = NOW - 200 * DAY
H = "/Users/x"


def n(path: str, size: float, mtime: float = FRESH, children: Optional[List[dict]] = None,
      app: Optional[dict] = None, **extra: Any) -> Dict[str, Any]:
    node = {"path": path, "name": path.rstrip("/").rsplit("/", 1)[-1], "size": int(size),
            "files": 10, "mtime": mtime, "is_dir": True, "error": None,
            "children": children or []}
    if app is not None:
        node["app"] = app
    node.update(extra)
    return node


def syn(parent: str, size: float, mtime: float = OLD) -> Dict[str, Any]:
    return {"path": parent, "name": "(7 smaller items)", "size": int(size), "files": 7,
            "mtime": mtime, "is_dir": False, "error": None, "children": [], "synthetic": True}


def root(label: str, category: str, node: dict, nested_in: Optional[str] = None) -> dict:
    node.update({"label": label, "category": category, "nested_in": nested_in})
    return node


def app(name: str, installed: bool = True, running: bool = False, version: Optional[str] = None,
        installed_versions: Optional[List[str]] = None, kind: str = "app") -> dict:
    return {"name": name, "bundle_id": None, "app_path": None, "installed": installed,
            "running": running, "version": version,
            "installed_versions": installed_versions or [], "kind": kind, "source": "known"}


def idea(version: str, installed: bool) -> dict:
    return app("IntelliJ IDEA", installed=installed, running=True, version=version,
               installed_versions=["2027.1"], kind="app")


C = f"{H}/Library/Caches"
JB = f"{C}/JetBrains"
AS = f"{H}/Library/Application Support"
CT = f"{H}/Library/Containers"
VMS = f"{CT}/com.docker.docker/Data/vms"
BZ = "/private/var/tmp/_bazel_x"


def make_result() -> dict:
    caches = root("Caches", "cache", n(C, 165 * GB, children=[
        n(JB, 153 * GB, children=[
            n(f"{JB}/MonorepoBazel", 94 * GB),
            n(f"{JB}/IntelliJIdea2026.3", 21.5 * GB, app=idea("2026.3", False), children=[
                n(f"{JB}/IntelliJIdea2026.3/caches", 15 * GB),
                n(f"{JB}/IntelliJIdea2026.3/index", 6.5 * GB),
            ]),
            n(f"{JB}/IntelliJIdea2027.1", 18.9 * GB, app=idea("2027.1", True)),
            n(f"{JB}/monorepo-bazel-cache", 13.4 * GB, mtime=OLD),
            syn(JB, 5.2 * GB),
        ]),
        n(f"{C}/copilot", 6.2 * GB),
        n(f"{C}/Homebrew", 1.6 * GB, app=app("Homebrew", kind="cli")),
        syn(C, 4.2 * GB),
    ]))
    support = root("Application Support", "app_support", n(AS, 11 * GB, children=[
        n(f"{AS}/Slack", 2 * GB, app=app("Slack", running=True)),
        n(f"{AS}/com.apple.TextInput", 1 * GB, app=app("TextInput", kind="system")),
        n(f"{AS}/GoneApp", 3 * GB, mtime=OLD, app=app("GoneApp", installed=False)),
        n(f"{AS}/Unknown", 5 * GB),
    ]))
    containers = root("Containers", "container", n(CT, 13 * GB, children=[
        n(f"{CT}/com.docker.docker", 10 * GB, app=app("Docker"), children=[
            n(f"{CT}/com.docker.docker/Data", 10 * GB, children=[n(VMS, 9.5 * GB)]),
        ]),
        n(f"{CT}/com.gone.app", 1 * GB, app=app("Gone", installed=False)),
        n(f"{CT}/com.apple.Notes", 2 * GB, app=app("Notes", kind="system")),
    ]))
    docker = root("Docker", "docker", n(VMS, 9.5 * GB, children=[
        n(f"{VMS}/0", 9.5 * GB, children=[n(f"{VMS}/0/Data.raw", 9.5 * GB)]),
    ]), nested_in="Containers")
    gradle = root("Gradle", "dev_cache", n(f"{H}/.gradle", 5 * GB, children=[
        n(f"{H}/.gradle/caches", 4 * GB, mtime=OLD),
        n(f"{H}/.gradle/wrapper", 1 * GB),
    ]))
    bazel = root("Bazel", "bazel", n(BZ, 30 * GB, children=[
        n(f"{BZ}/abc123", 20 * GB, mtime=OLD),
        n(f"{BZ}/def456", 10 * GB),
    ]))
    return {"scanned_at": NOW, "duration_s": 1.0, "home": H,
            "roots": [caches, support, containers, docker, gradle, bazel]}


def walk(node: dict):
    yield node
    for c in node.get("children") or []:
        yield from walk(c)


def find(result: dict, path: str, root_label: Optional[str] = None) -> dict:
    for r in result["roots"]:
        if root_label and r["label"] != root_label:
            continue
        for x in walk(r):
            if x["path"] == path and not x.get("synthetic"):
                return x
    raise KeyError(path)


class AnnotateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.r = make_result()
        insights.annotate(self.r, stale_days=90, now=NOW)
        self.s = self.r["summary"]
        self.top = {c["path"]: c for c in self.s["top_candidates"]}

    def test_every_node_annotated(self) -> None:
        for r in self.r["roots"]:
            for x in walk(r):
                self.assertIn(x["safety"], insights.SAFETY_LEVELS)
                self.assertIsInstance(x["flags"], list)
                self.assertTrue(x["reason"])

    def test_old_intellij_version(self) -> None:
        old = find(self.r, f"{JB}/IntelliJIdea2026.3")
        self.assertEqual(old["flags"], ["regenerable", "orphaned", "old_version"])
        self.assertEqual(old["safety"], "safe")  # running 2027.1 does not affect 2026.3 caches
        self.assertIn("IntelliJ IDEA 2026.3, which is not installed (installed: 2027.1)",
                      old["reason"])
        self.assertIn("safe to delete", old["reason"])
        self.assertIn(old["path"], self.top)
        self.assertEqual(self.top[old["path"]]["app"], "IntelliJ IDEA")
        self.assertEqual(self.top[old["path"]]["size"], int(21.5 * GB))
        # children inherit the app but are not separate (non-maximal) candidates
        child = find(self.r, f"{JB}/IntelliJIdea2026.3/caches")
        self.assertIn("old_version", child["flags"])
        self.assertNotIn(child["path"], self.top)

    def test_current_intellij_version(self) -> None:
        cur = find(self.r, f"{JB}/IntelliJIdea2027.1")
        self.assertEqual(cur["flags"], ["regenerable"])
        self.assertEqual(cur["safety"], "caution")  # running -> bumped from safe
        self.assertIn("quit IntelliJ IDEA first", cur["reason"])
        self.assertNotIn(cur["path"], self.top)

    def test_running_app_bumps_safety(self) -> None:
        slack = find(self.r, f"{AS}/Slack")
        self.assertEqual(slack["safety"], "danger")  # app_support caution -> danger
        self.assertEqual(slack["reason"], "Slack data — app is running, quit Slack first")
        r = make_result()
        r["roots"][0]["children"][2]["app"]["running"] = True  # Homebrew cache
        insights.annotate(r, now=NOW)
        self.assertEqual(find(r, f"{C}/Homebrew")["safety"], "caution")

    def test_regenerable_reasons(self) -> None:
        self.assertEqual(find(self.r, f"{H}/.gradle/wrapper")["reason"],
                         "Gradle wrapper distributions — regenerated on next build")
        self.assertEqual(find(self.r, f"{C}/copilot")["reason"], "Cache — regenerated on demand")
        self.assertEqual(find(self.r, f"{C}/Homebrew")["reason"],
                         "Cache of Homebrew — regenerated on demand")

    def test_docker_danger(self) -> None:
        for x in walk(next(r for r in self.r["roots"] if r["label"] == "Docker")):
            self.assertEqual(x["safety"], "danger")
            self.assertNotIn(x["path"], self.top)
        disk = find(self.r, f"{VMS}/0/Data.raw")
        self.assertIn("docker system prune", disk["reason"])
        # reached through Containers, the VM dir takes the docker category too
        via = find(self.r, VMS, root_label="Containers")
        self.assertEqual(via["safety"], "danger")
        self.assertIn("docker system prune", via["reason"])

    def test_roots_danger(self) -> None:
        for r in self.r["roots"]:
            self.assertEqual(r["safety"], "danger")
            self.assertIn("top-level location — clean inside it", r["reason"])
            self.assertNotIn(r["path"], self.top)

    def test_system_and_orphaned_data(self) -> None:
        self.assertEqual(find(self.r, f"{AS}/com.apple.TextInput")["safety"], "danger")
        self.assertEqual(find(self.r, f"{CT}/com.apple.Notes")["safety"], "danger")
        self.assertEqual(find(self.r, f"{CT}/com.docker.docker")["safety"], "danger")
        gone = find(self.r, f"{CT}/com.gone.app")
        self.assertEqual(gone["flags"], ["orphaned"])
        self.assertEqual(gone["safety"], "caution")
        self.assertIn(gone["path"], self.top)
        gone_as = find(self.r, f"{AS}/GoneApp")
        self.assertEqual(gone_as["flags"], ["orphaned", "stale"])
        self.assertEqual(gone_as["safety"], "caution")
        self.assertNotEqual(find(self.r, f"{AS}/Unknown")["safety"], "safe")

    def test_stale_detection(self) -> None:
        stale = find(self.r, f"{JB}/monorepo-bazel-cache")
        self.assertEqual(stale["flags"], ["regenerable", "stale"])
        self.assertIn("untouched for 200 days", stale["reason"])
        self.assertIn(stale["path"], self.top)
        self.assertNotIn("stale", find(self.r, f"{JB}/MonorepoBazel")["flags"])
        # with a wider window nothing is stale
        r = make_result()
        insights.annotate(r, stale_days=365, now=NOW)
        self.assertNotIn("stale", find(r, f"{JB}/monorepo-bazel-cache")["flags"])
        self.assertNotIn(f"{JB}/monorepo-bazel-cache",
                         {c["path"] for c in r["summary"]["top_candidates"]})

    def test_error_nodes(self) -> None:
        r = make_result()
        node = r["roots"][0]["children"][0]["children"][3]  # monorepo-bazel-cache (old mtime)
        node["error"] = "permission"
        insights.annotate(r, now=NOW)
        self.assertEqual(node["safety"], "safe")
        self.assertNotIn("stale", node["flags"])
        self.assertIn("partially unreadable (grant Full Disk Access)", node["reason"])
        self.assertNotIn(node["path"], {c["path"] for c in r["summary"]["top_candidates"]})

    def test_synthetic_skipped(self) -> None:
        for r in self.r["roots"]:
            for x in walk(r):
                if x.get("synthetic"):
                    self.assertNotIn("stale", x["flags"])
        for c in self.s["top_candidates"]:
            self.assertFalse(c["name"].startswith("("))

    def test_summary_totals(self) -> None:
        # Docker (nested in Containers) excluded from total
        self.assertEqual(self.s["total"], int(165 * GB + 11 * GB + 13 * GB + 5 * GB + 30 * GB))
        self.assertEqual(sum(self.s["by_category"].values()), self.s["total"])
        self.assertEqual(self.s["by_category"]["docker"], int(9.5 * GB))
        self.assertEqual(self.s["by_category"]["container"], int(13 * GB - 9.5 * GB))
        self.assertEqual(self.s["regenerable_total"], int(165 * GB + 5 * GB + 30 * GB))

    def test_reclaimable(self) -> None:
        expected = [f"{JB}/IntelliJIdea2026.3", f"{JB}/monorepo-bazel-cache", f"{AS}/GoneApp",
                    f"{CT}/com.gone.app", f"{H}/.gradle/caches", f"{BZ}/abc123"]
        self.assertEqual(set(self.top), set(expected))
        sizes = {p: find(self.r, p)["size"] for p in expected}
        self.assertEqual(self.s["reclaimable"], sum(sizes.values()))
        self.assertEqual(self.s["candidate_count"], len(expected))
        # top list sorted by size desc
        got = [c["size"] for c in self.s["top_candidates"]]
        self.assertEqual(got, sorted(got, reverse=True))
        self.assertLessEqual(self.s["reclaimable"], self.s["total"])
        self.assertEqual(self.s["by_flag"]["old_version"], int(21.5 * GB))
        self.assertEqual(self.s["by_flag"]["orphaned"], int(21.5 * GB + 3 * GB + 1 * GB))
        for k in insights.FLAG_ORDER:
            self.assertIn(k, self.s["by_flag"])
            self.assertLessEqual(self.s["by_flag"][k], self.s["total"])

    def test_maximal_dedupe(self) -> None:
        r = make_result()
        jb = r["roots"][0]["children"][0]
        jb["mtime"] = OLD
        for c in walk(jb):
            c["mtime"] = OLD
        insights.annotate(r, now=NOW)
        s = r["summary"]
        paths = [c["path"] for c in s["top_candidates"]]
        self.assertIn(JB, paths)  # whole JetBrains dir is stale -> one candidate
        self.assertFalse(any(p.startswith(JB + "/") for p in paths))
        self.assertEqual(s["by_flag"]["stale"],
                         sum(find(r, p)["size"] for p in [JB, f"{AS}/GoneApp",
                                                          f"{H}/.gradle/caches", f"{BZ}/abc123"]))

    def test_nested_root_not_double_counted(self) -> None:
        # Homebrew is also scanned as its own root nested in Caches; its stale content would be
        # reached twice.
        r = make_result()
        hb = r["roots"][0]["children"][2]
        hb["mtime"] = OLD
        hb["children"] = [n(f"{C}/Homebrew/downloads", 1.5 * GB, mtime=OLD)]
        nested = root("Homebrew", "cache", n(f"{C}/Homebrew", 1.6 * GB, mtime=OLD, children=[
            n(f"{C}/Homebrew/downloads", 1.5 * GB, mtime=OLD)]), nested_in="Caches")
        r["roots"].append(nested)
        base = make_result()
        insights.annotate(base, now=NOW)
        insights.annotate(r, now=NOW)
        s, b = r["summary"], base["summary"]
        self.assertEqual(s["total"], b["total"])
        self.assertEqual(s["by_category"]["cache"], b["by_category"]["cache"])
        self.assertEqual(sum(s["by_category"].values()), s["total"])
        # a scan root can't be moved to the Trash, so its stale content is suggested instead, once
        self.assertEqual(s["reclaimable"], b["reclaimable"] + int(1.5 * GB))
        paths = [c["path"] for c in s["top_candidates"]]
        self.assertNotIn(f"{C}/Homebrew", paths)
        self.assertEqual(paths.count(f"{C}/Homebrew/downloads"), 1)

    def test_orphaned_docker_container_counted_once(self) -> None:
        r = make_result()
        r["roots"][2]["children"][0]["app"]["installed"] = False
        insights.annotate(r, now=NOW)
        s = r["summary"]
        self.assertEqual(find(r, f"{CT}/com.docker.docker")["safety"], "caution")
        paths = [c["path"] for c in s["top_candidates"]]
        self.assertIn(f"{CT}/com.docker.docker", paths)
        self.assertFalse(any(p.startswith(VMS) for p in paths))

    def test_pure_and_idempotent(self) -> None:
        r = make_result()
        insights.annotate(r, now=NOW)
        first = copy.deepcopy(r)
        insights.annotate(r, now=NOW)
        self.assertEqual(first, r)

    def test_empty_and_speed(self) -> None:
        empty: dict = {"roots": []}
        insights.annotate(empty, now=NOW)
        self.assertEqual(empty["summary"]["total"], 0)
        self.assertEqual(empty["summary"]["top_candidates"], [])
        # ~50k nodes
        kids = [n(f"{C}/d{i}", GB, mtime=OLD if i % 3 else FRESH,
                  children=[n(f"{C}/d{i}/s{j}", GB // 50) for j in range(49)])
                for i in range(1000)]
        big = {"roots": [root("Caches", "cache", n(C, 1000 * GB, children=kids))]}
        t = time.perf_counter()
        insights.annotate(big, now=NOW)
        self.assertLess(time.perf_counter() - t, 5.0)
        self.assertEqual(len(big["summary"]["top_candidates"]), insights.TOP_N)



# =========================================================================== iteration 2

G = 10 ** 9  # decimal GB, as shown in reasons ("177 GB")
U = f"{H}/IdeaProjects/ultimate"
OLDP = f"{H}/IdeaProjects/oldproj"
PAR = f"{H}/Parallels"
DOCS = f"{H}/Documents"
DL = f"{H}/Downloads"
LIB = f"{H}/Library"
PRUNABLE = [f"/tmp/wt{i}" for i in range(7)]


def stub(path: str, label: str) -> dict:
    return {"path": path, "name": path.rsplit("/", 1)[-1], "size": 0, "files": 0, "is_dir": True,
            "children": [], "link_root": label}


def git_actions(temp_frees: float = 177 * G) -> List[dict]:
    return [
        {"id": "git_delete_temp_packs", "path": f"{U}/.git", "label": "Delete 73 leftover fetch temp files",
         "frees": int(temp_frees), "safety": "safe", "blocked": None, "background": False, "confirm": "?"},
        {"id": "git_worktree_prune", "path": f"{U}/.git", "label": "Prune 7 missing worktrees",
         "frees": 0, "safety": "safe", "blocked": None, "background": False, "confirm": "?"},
        {"id": "git_gc", "path": f"{U}/.git", "label": "Run git gc (53 packs)", "frees": int(60 * G),
         "frees_estimated": True, "safety": "caution", "blocked": "needs 10 GB of free space",
         "background": True, "confirm": "?"},
    ]


def git_details(repo: str = "ultimate") -> dict:
    return {"repo": repo, "git_dir": f"{U}/.git", "worktree": U, "bare": False,
            "pack_size": int(209 * G), "packs": 53, "largest_pack": int(8.3 * G),
            "garbage_size": int(177 * G), "garbage_files": ["tmp_pack_%d" % i for i in range(73)],
            "prunable_worktrees": PRUNABLE, "worktrees": 9, "gc_running": False,
            "fetch_running": False, "last_commit": FRESH}


def make_v2(disk: bool = True) -> dict:
    ij = app("IntelliJ IDEA projects", running=True)
    ultimate = n(U, 418 * G, kind="project", details={"git_dir": f"{U}/.git", "last_commit": FRESH},
                 children=[
                     n(f"{U}/.git", 394 * G, kind="git_dir", details=git_details(), actions=git_actions(),
                       children=[n(f"{U}/.git/objects", 393 * G, children=[
                           n(f"{U}/.git/objects/pack", 392 * G)])]),
                     n(f"{U}/out", 23.6 * G, kind="build_output",
                       details={"project": U, "project_mtime": FRESH, "type": "out"}),
                     n(f"{U}/src", 0.4 * G),
                 ])
    oldproj = n(OLDP, 5 * G, mtime=OLD, kind="project", details={"last_commit": OLD}, children=[
        n(f"{OLDP}/build", 3 * G, mtime=OLD, kind="build_output",
          details={"project": OLDP, "project_mtime": OLD}),
        n(f"{OLDP}/node_modules", 1.5 * G, mtime=OLD, kind="build_output",
          details={"project": OLDP, "project_mtime": OLD}),
    ])
    parallels = n(PAR, 136 * G, app=app("Parallels Desktop"), children=[
        n(f"{PAR}/Windows 11.pvm", 116 * G, kind="vm",
          details={"vm_name": "Windows 11", "last_used": NOW - 10 * DAY, "snapshots": 2, "type": "parallels"},
          children=[n(f"{PAR}/Windows 11.pvm/disk.hdd", 115 * G)]),
        n(f"{PAR}/Old Ubuntu.pvm", 20 * G, mtime=OLD, kind="vm",
          details={"vm_name": "Old Ubuntu", "last_used": OLD, "snapshots": 0, "type": "parallels"}),
    ])
    sysapp = app("macOS user folder", kind="system")
    docs = n(DOCS, 90 * G, mtime=OLD, app=sysapp, kind="user_data", details={"label": "Documents"},
             children=[n(f"{DOCS}/PHOTO", 84 * G, mtime=OLD), syn(DOCS, 6 * G)])
    downloads = n(DL, 12 * G, app=sysapp, kind="downloads", details={"label": "Downloads"}, children=[
        n(f"{DL}/ubuntu.iso", 6 * G, mtime=OLD, kind="disk_image", details={"in_downloads": True}),
        n(f"{DL}/Installer.dmg", 2 * G, kind="disk_image", details={"in_downloads": True}),
        n(f"{DL}/old-stuff", 3 * G, mtime=OLD),
        syn(DL, 1 * G),
    ])
    home = root("Home", "home", n(H, 520.0 * G + 136 * G + 90 * G + 12 * G + 22 * G, children=[
        n(f"{H}/IdeaProjects", 423 * G, app=ij, children=[ultimate, oldproj]),
        parallels, docs, downloads,
        n(f"{H}/Projects", 15 * G),  # a deep VM below the scanned depth (see "special")
        n(f"{H}/.codex", 2 * G, mtime=OLD, app=app("Codex", installed=False)),
        n(f"{H}/.aws", 0.1 * G, mtime=OLD, app=app("AWS CLI", installed=False, kind="cli")),
        n(f"{H}/GoneTool", 1 * G, mtime=OLD, app=app("GoneTool", installed=False)),
        n(f"{H}/.Trash", 4 * G, app=app("Trash", kind="system")),
        stub(LIB, "Library (other)"), stub(f"{H}/.gradle", "Gradle"), stub(f"{H}/.cache", "~/.cache"),
    ]))
    home["exclude"] = [LIB, f"{H}/.gradle", f"{H}/.cache"]
    home["size"] = sum(c["size"] for c in home["children"]) + int(82 * G)  # + unscanned small stuff
    library = root("Library (other)", "library_other", n(LIB, 14 * G, children=[
        stub(C, "Caches"), stub(CT, "Containers"),
        n(f"{LIB}/Mobile Documents", 8 * G, kind="user_data", details={"label": "Library/Mobile Documents"},
          app=app("iCloud Drive", kind="system")),
        n(f"{LIB}/Developer", 5 * G, mtime=OLD, app=app("Xcode", installed=False)),
        n(f"{LIB}/Preferences", 0.1 * G, app=app("macOS", kind="system")),
    ]))
    cgit = f"{C}/somecache/repo/.git"
    caches = root("Caches", "cache", n(C, 20 * G, children=[
        n(f"{JB}/IntelliJIdea2026.3", 10 * G, app=idea("2026.3", False)),
        n(f"{C}/somecache", 4 * G, mtime=OLD, children=[
            n(f"{C}/somecache/repo", 4 * G, mtime=OLD, kind="project", children=[
                n(cgit, 4 * G, mtime=OLD, kind="git_dir", details={"repo": "repo"},
                  actions=[dict(git_actions()[0], path=cgit, frees=int(3 * G))])])]),
    ]))
    containers = root("Containers", "container", n(CT, 15 * G, children=[
        n(f"{CT}/com.docker.docker", 12 * G, app=app("Docker Desktop"))]))
    docker = root("Docker Desktop", "docker", n(f"{CT}/com.docker.docker", 12 * G, children=[
        n(f"{CT}/com.docker.docker/Data", 12 * G)]), nested_in="Containers")
    gradle = root("Gradle", "dev_cache", n(f"{H}/.gradle", 9 * G, children=[
        n(f"{H}/.gradle/caches", 8 * G, mtime=OLD)]))
    apps = root("Applications", "applications", n("/Applications", 2.5 * G, children=[
        n("/Applications/Slack.app", 0.5 * G, mtime=OLD, app=app("Slack", running=True)),
        n("/Applications/Docker.app", 2 * G, mtime=OLD, app=app("Docker")),
    ]))
    syslib = root("System Library", "system", n("/Library", 10 * G, children=[
        n("/Library/Developer", 5 * G, mtime=OLD, app=app("Apple developer tools", kind="system"), children=[
            n("/Library/Developer/CommandLineTools", 5 * G, mtime=OLD,
              app=app("Xcode Command Line Tools", kind="system"))]),
        n("/Library/Google", 1 * G, mtime=OLD, app=app("Google", installed=False)),
    ]))
    opt = root("/opt", "dev_cache", n("/opt", 6 * G, children=[
        n("/opt/homebrew", 6 * G, mtime=OLD, app=app("Homebrew", kind="cli"))]))
    swap = n("/private/var/vm", 4 * G, app=app("macOS swap & sleep image", kind="system"),
             children=[n("/private/var/vm/sleepimage", 4 * G, app=app("macOS swap & sleep image", kind="system"))])
    swap = root("Swap & sleepimage", "system", swap)
    tmp = root("/private/var/tmp", "other", n("/private/var/tmp", 1 * G, children=[
        stub(BZ, "Bazel output")]))
    bazel = root("Bazel output", "bazel", n(BZ, 30 * G, children=[n(f"{BZ}/abc", 30 * G, mtime=OLD)]))
    roots = [home, library, caches, containers, docker, gradle, apps, syslib, opt, swap, tmp, bazel]
    r = {"scanned_at": NOW, "duration_s": 1.0, "home": H, "roots": roots,
         "special": [{"path": f"{H}/Projects/vms/deep.utm", "kind": "vm", "size": int(10 * G),
                      "files": 3, "mtime": OLD, "root": "Home"},
                     {"path": f"{U}/.git", "kind": "git_dir", "size": int(394 * G), "root": "Home"}],
         "git_repos": [dict(git_details(), path=U, size=int(394 * G), actions=git_actions())]}
    if disk:
        total = sum(x["size"] for x in roots if not x.get("nested_in"))
        vols = [{"name": "Macintosh HD - Data", "role": "Data", "used": total + int(150 * G)},
                {"name": "Macintosh HD", "role": "System", "used": int(13.3 * G)},
                {"name": "Preboot", "role": "Preboot", "used": int(11.5 * G)},
                {"name": "Nix Store", "role": None, "used": int(3.8 * G)},
                {"name": "Recovery", "role": "Recovery", "used": int(1.9 * G)},
                {"name": "VM", "role": "VM", "used": 24576}]
        used = sum(v["used"] for v in vols)
        free = int(84 * G)
        r["disk"] = {"container": {"size": used + free, "used": used, "free": free},
                     "data_volume": {"name": vols[0]["name"], "used": vols[0]["used"]},
                     "volumes": vols,
                     "snapshots": [{"name": "com.apple.os.update-ABC", "kind": "os_update"}],
                     "purgeable": int(0.4 * G),
                     "users": [{"name": "x", "home": H, "readable": True},
                               {"name": "administrator", "home": "/Users/administrator", "readable": False}],
                     "collected_at": NOW}
    return r


class InsightsV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.r = make_v2()
        insights.annotate(self.r, stale_days=90, now=NOW)
        self.s = self.r["summary"]
        self.trash = {c["path"]: c for c in self.s["top_candidates"] if "action" not in c}
        self.acts = [c for c in self.s["top_candidates"] if "action" in c]

    def f(self, path: str, label: Optional[str] = None) -> dict:
        return find(self.r, path, label)

    def test_every_node_annotated(self) -> None:
        for r in self.r["roots"]:
            for x in walk(r):
                self.assertIn(x["safety"], insights.SAFETY_LEVELS)
                self.assertTrue(x["reason"])
                self.assertIn("in_use", x)

    def test_git_dir(self) -> None:
        g = self.f(f"{U}/.git")
        self.assertEqual(g["safety"], "danger")
        self.assertIn("Git data of ultimate", g["reason"])
        self.assertIn("177 GB of temp files from failed fetches", g["reason"])
        self.assertIn("7 prunable worktrees", g["reason"])
        self.assertIn("'Delete 73 leftover fetch temp files' frees 177 GB", g["reason"])
        self.assertNotIn(g["path"], self.trash)
        self.assertFalse(g["in_use"])  # IntelliJ running does not matter for git data
        part = self.f(f"{U}/.git/objects/pack")
        self.assertEqual(part["safety"], "danger")
        self.assertIn("Part of the Git data of ultimate", part["reason"])

    def test_git_actions_counted(self) -> None:
        # temp packs (177 GB) unblocked; prune frees 0; gc blocked -> only 177 GB of ultimate.
        # The cache repo's action lies inside a trash candidate -> not counted again.
        self.assertEqual(self.s["actions_reclaimable"], 177 * G)
        self.assertEqual(self.s["actions_reclaimable_estimated"], 0)
        self.assertEqual(self.s["actions_blocked_frees"], 60 * G)
        self.assertEqual(self.s["action_count"], 1)
        self.assertEqual([a["action"]["id"] for a in self.acts], ["git_delete_temp_packs", "git_gc"])
        gc = self.acts[1]
        self.assertEqual(gc["blocked"], "needs 10 GB of free space")
        self.assertEqual(gc["reason"], "Duplicate packs / loose objects in ultimate — 'Run git gc (53 packs)' "
                                       "frees up to ~60.0 GB (estimate) — blocked: needs 10 GB of free space")
        a = self.acts[0]
        self.assertEqual(a["action"]["id"], "git_delete_temp_packs")
        self.assertEqual(a["size"], 177 * G)
        self.assertEqual(a["path"], f"{U}/.git")
        self.assertEqual(a["reason"], "Git temp files from failed fetches in ultimate — 177 GB can be "
                                      "deleted with 'Delete 73 leftover fetch temp files'")
        self.assertEqual(self.s["top_candidates"][0]["path"], f"{U}/.git")  # the biggest item
        self.assertEqual(self.s["reclaimable"], self.s["trash_reclaimable"] + self.s["actions_reclaimable"])

    def test_actions_capped_and_unblocked(self) -> None:
        r = make_v2()
        node = r["roots"][0]["children"][0]["children"][0]["children"][0]
        node["actions"][2]["blocked"] = None  # gc now possible, but its 60 GB is only an estimate
        insights.annotate(r, now=NOW)
        s = r["summary"]
        self.assertEqual(s["actions_reclaimable"], 177 * G)
        self.assertEqual(s["actions_reclaimable_estimated"], 60 * G)
        self.assertEqual(s["actions_blocked_frees"], 0)
        self.assertEqual(s["action_count"], 2)
        self.assertEqual(s["reclaimable"], s["trash_reclaimable"] + 177 * G)
        node["actions"][0]["frees"] = int(1000 * G)  # never more than the .git folder itself
        insights.annotate(r, now=NOW)
        self.assertEqual(r["summary"]["actions_reclaimable"], 394 * G)
        node["actions"][0]["blocked"] = "git gc is running in this repository"
        insights.annotate(r, now=NOW)
        self.assertEqual(r["summary"]["actions_reclaimable"], 0)
        self.assertIn("up to ~60.0 GB (estimate)", node["reason"])  # best remaining action

    def test_git_repo_below_scanned_depth(self) -> None:
        r = make_v2()
        ultimate = r["roots"][0]["children"][0]["children"][0]
        ultimate["children"] = [c for c in ultimate["children"] if not c["path"].endswith(".git")]
        insights.annotate(r, now=NOW)
        self.assertEqual(r["summary"]["actions_reclaimable"], 177 * G)  # from result["git_repos"]

    def test_build_output_active_project(self) -> None:
        out = self.f(f"{U}/out")
        self.assertIn("regenerable", out["flags"])
        self.assertNotIn("stale", out["flags"])
        self.assertTrue(out["reason"].startswith(
            "Build output of project ultimate — project active (changed 2 days ago)"), out["reason"])
        self.assertTrue(out["in_use"])  # IntelliJ is running and the project is active
        self.assertTrue(out["reason"].endswith("app is running, quit IntelliJ IDEA first"), out["reason"])
        self.assertEqual(out["safety"], "caution")
        self.assertNotIn(out["path"], self.trash)

    def test_build_output_stale_project(self) -> None:
        b = self.f(f"{OLDP}/build")
        self.assertEqual(b["safety"], "safe")
        self.assertEqual(b["flags"], ["regenerable", "stale"])
        self.assertEqual(b["reason"], "Build output of project oldproj — project untouched for 200 days "
                                      "— regenerated on next build")
        self.assertFalse(b["in_use"])
        self.assertIn(b["path"], self.trash)
        self.assertIn(f"{OLDP}/node_modules", self.trash)
        self.assertNotIn(OLDP, self.trash)  # the project itself (source code) is never a candidate

    def test_projects(self) -> None:
        u = self.f(U)
        self.assertEqual(u["safety"], "caution")
        self.assertIn("Project ultimate", u["reason"])
        self.assertIn("source code", u["reason"])
        self.assertFalse(u["in_use"])
        self.assertIn("Part of project ultimate", self.f(f"{U}/src")["reason"])
        ideap = self.f(f"{H}/IdeaProjects")
        self.assertEqual(ideap["safety"], "caution")  # user content: IntelliJ running doesn't matter
        self.assertFalse(ideap["in_use"])
        self.assertTrue(ideap["reason"].startswith("IntelliJ IDEA projects — "), ideap["reason"])

    def test_vms(self) -> None:
        w = self.f(f"{PAR}/Windows 11.pvm")
        self.assertEqual(w["safety"], "caution")
        self.assertIn("Parallels VM 'Windows 11'", w["reason"])
        self.assertIn("last used 10 days ago", w["reason"])
        self.assertIn("2 snapshots", w["reason"])
        self.assertNotIn(w["path"], self.trash)
        self.assertNotIn(f"{PAR}/Windows 11.pvm/disk.hdd", self.trash)
        old = self.f(f"{PAR}/Old Ubuntu.pvm")
        self.assertIn("stale", old["flags"])
        self.assertIn("not used for 200 days", old["reason"])
        self.assertIn("Parallels Desktop", old["reason"])
        self.assertIn(old["path"], self.trash)

    def test_personal_data(self) -> None:
        photo = self.f(f"{DOCS}/PHOTO")
        self.assertEqual(photo["safety"], "caution")
        self.assertEqual(photo["reason"], "Personal photos — move to an external drive instead")
        self.assertEqual(photo["flags"], [])
        docs = self.f(DOCS)
        self.assertEqual(docs["safety"], "caution")  # kind wins over the "macOS user folder" app
        self.assertIn("Your Documents folder", docs["reason"])
        icloud = self.f(f"{LIB}/Mobile Documents")
        self.assertEqual(icloud["safety"], "caution")
        self.assertIn("iCloud Drive files", icloud["reason"])
        for p in (DOCS, f"{DOCS}/PHOTO", f"{LIB}/Mobile Documents"):
            self.assertNotIn(p, self.trash)

    def test_downloads(self) -> None:
        self.assertNotIn(DL, self.trash)
        self.assertIn("Downloads folder", self.f(DL)["reason"])
        iso = self.f(f"{DL}/ubuntu.iso")
        self.assertEqual(iso["safety"], "safe")
        self.assertIn(iso["path"], self.trash)
        self.assertIn("untouched for 200 days", iso["reason"])
        self.assertNotIn(f"{DL}/Installer.dmg", self.trash)  # recent
        self.assertIn(f"{DL}/old-stuff", self.trash)
        self.assertEqual(self.f(f"{DL}/old-stuff")["safety"], "caution")

    def test_home_rules(self) -> None:
        self.assertIn(f"{H}/.codex", self.trash)  # tool is gone, hidden folder
        self.assertIn("Codex data, which is not installed", self.trash[f"{H}/.codex"]["reason"])
        self.assertNotIn(f"{H}/.aws", self.trash)  # credentials
        self.assertNotIn(f"{H}/GoneTool", self.trash)  # visible folder = user content
        t = self.f(f"{H}/.Trash")
        self.assertEqual(t["safety"], "caution")
        self.assertIn("empty the Trash in Finder", t["reason"])
        self.assertNotIn(t["path"], self.trash)

    def test_link_stubs(self) -> None:
        s = self.f(LIB, "Home")
        self.assertTrue(s.get("link_root"))
        self.assertEqual(s["safety"], "danger")
        self.assertEqual(s["flags"], [])
        self.assertEqual(s["reason"], "Scanned separately → Library (other)")
        self.assertEqual(self.f(BZ, "/private/var/tmp")["reason"], "Scanned separately → Bazel output")
        stub_paths = {LIB, f"{H}/.gradle", f"{H}/.cache", C, CT, BZ}
        for c in self.s["top_candidates"]:
            self.assertNotIn(c["path"], stub_paths - {BZ})

    def test_library_other(self) -> None:
        self.assertIn(f"{LIB}/Developer", self.trash)  # Xcode is gone
        self.assertEqual(self.f(f"{LIB}/Preferences")["safety"], "danger")

    def test_kinds_inside_caches_stay_cache(self) -> None:
        g = self.f(f"{C}/somecache/repo/.git")
        self.assertEqual(g["safety"], "safe")
        self.assertIn(f"{C}/somecache", self.trash)  # stale cache, contains a repo clone
        self.assertNotIn(f"{C}/somecache/repo/.git", [a["path"] for a in self.acts])

    def test_git_dir_in_cache_is_never_a_trash_candidate(self) -> None:
        # Move to Trash refuses git internals, even where they are rated as plain cache: a stale Cargo
        # bare repo next to a fresh one must not be suggested (nor counted as reclaimable)
        db = f"{H}/.cargo/git/db"
        r = {"roots": [root("Cargo", "dev_cache", n(f"{H}/.cargo", 3 * G, children=[n(db, 3 * G, children=[
            n(f"{db}/foo-123", 2 * G, mtime=OLD, kind="git_dir", details={"repo": "foo", "bare": True},
              children=[n(f"{db}/foo-123/objects", 2 * G, mtime=OLD)]),
            n(f"{db}/bar-456", 1 * G, kind="git_dir", details={"repo": "bar", "bare": True}),
        ])]))]}
        insights.annotate(r, stale_days=90, now=NOW)
        self.assertEqual(find(r, f"{db}/foo-123")["safety"], "safe")  # still rated as cache
        self.assertEqual(r["summary"]["top_candidates"], [])
        self.assertEqual(r["summary"]["trash_reclaimable"], 0)

    def test_applications_and_system(self) -> None:
        slack = self.f("/Applications/Slack.app")
        self.assertEqual(slack["safety"], "danger")
        self.assertTrue(slack["in_use"])
        self.assertIn("drag it to the Trash in Finder", slack["reason"])
        self.assertNotIn("stale", self.f("/Applications/Docker.app")["flags"])
        for p in ("/Library/Developer", "/Library/Developer/CommandLineTools", "/Library/Google",
                  "/private/var/vm/sleepimage"):
            self.assertEqual(self.f(p)["safety"], "danger", p)
            self.assertNotIn(p, self.trash)
        self.assertIn("Xcode Command Line Tools — macOS system component",
                      self.f("/Library/Developer/CommandLineTools")["reason"])
        self.assertIn("shrinks after a restart", self.f("/private/var/vm/sleepimage")["reason"])

    def test_build_dirs_of_installed_software(self) -> None:
        # node_modules/dist that ship with installed software must not become "safe, regenerable"
        def bo(path: str) -> dict:
            return n(path, 1 * G, mtime=OLD, kind="build_output",
                     details={"project": path.rsplit("/", 1)[0], "project_mtime": OLD})
        ext = f"{H}/.vscode/extensions/ms-python.python-2025.1"
        ws = f"{H}/Applications/WebStorm.app/Contents/plugins/js"
        code = f"{AS}/Code/User/globalStorage/ext"
        site = f"{H}/work/site/node_modules"
        vscode = app("Visual Studio Code")
        r = {"home": H, "roots": [
            root("Home", "home", n(H, 6 * G, children=[
                n(f"{H}/.vscode", 2 * G, mtime=OLD, app=vscode,
                  children=[bo(f"{ext}/node_modules"), bo(f"{ext}/dist")]),
                n(f"{H}/Applications", 1 * G, mtime=OLD, children=[bo(f"{ws}/node_modules")]),
                n(f"{H}/work", 1 * G, mtime=OLD, children=[bo(site)]),  # a real project: still a candidate
            ])),
            root("/usr/local", "dev_cache", n("/usr/local", 1 * G, mtime=OLD, children=[
                n("/usr/local/lib", 1 * G, mtime=OLD, children=[bo("/usr/local/lib/node_modules")])])),
            root("/opt", "dev_cache", n("/opt", 1 * G, mtime=OLD, children=[
                n("/opt/homebrew", 1 * G, mtime=OLD, app=app("Homebrew", kind="cli"),
                  children=[bo("/opt/homebrew/lib/node_modules")])])),
            root("Application Support", "app_support", n(AS, 1 * G, mtime=OLD, children=[
                n(f"{AS}/Code", 1 * G, mtime=OLD, app=vscode, children=[bo(f"{code}/node_modules")])])),
        ]}
        insights.annotate(r, stale_days=90, now=NOW)
        cands = {c["path"] for c in r["summary"]["top_candidates"] if "action" not in c}
        self.assertEqual(cands, {site})
        self.assertEqual(find(r, site)["safety"], "safe")
        for p in (f"{ext}/node_modules", f"{ext}/dist", f"{ws}/node_modules", "/usr/local/lib/node_modules",
                  "/opt/homebrew/lib/node_modules", f"{code}/node_modules"):
            x = find(r, p)
            self.assertNotEqual(x["safety"], "safe", p)
            self.assertNotIn("regenerable", x["flags"], p)
            self.assertNotIn("Build output", x["reason"], p)

    def test_installed_software(self) -> None:
        hb = self.f("/opt/homebrew")
        self.assertEqual(hb["safety"], "caution")
        self.assertNotIn("regenerable", hb["flags"])
        self.assertNotIn(hb["path"], self.trash)
        self.assertIn("brew", hb["reason"])
        self.assertEqual(self.s["by_category"]["dev_cache"], int(9 * G + 6 * G))

    def test_trash_candidates_and_totals(self) -> None:
        expected = {f"{OLDP}/build", f"{OLDP}/node_modules", f"{PAR}/Old Ubuntu.pvm", f"{DL}/ubuntu.iso",
                    f"{DL}/old-stuff", f"{H}/.codex", f"{LIB}/Developer", f"{JB}/IntelliJIdea2026.3",
                    f"{C}/somecache", f"{H}/.gradle/caches", f"{BZ}/abc"}
        self.assertEqual(set(self.trash), expected)
        self.assertEqual(self.s["trash_reclaimable"], sum(self.f(p)["size"] for p in expected))
        roots = self.r["roots"]
        self.assertEqual(self.s["total"], sum(x["size"] for x in roots if not x.get("nested_in")))
        self.assertEqual(sum(self.s["by_category"].values()), self.s["total"])
        self.assertLessEqual(self.s["reclaimable"], self.s["total"])

    def test_by_kind(self) -> None:
        k = self.s["by_kind"]
        self.assertEqual(k["git_dir"], 394 * G + 4 * G)
        self.assertEqual(k["build_output"], int(23.6 * G) + 3 * G + int(1.5 * G))
        self.assertEqual(k["vm"], 136 * G + 10 * G)  # incl. the deep special
        self.assertEqual(k["user_data"], 98 * G)
        self.assertEqual(k["downloads"], 12 * G)
        self.assertEqual(k["disk_image"], 8 * G)
        self.assertEqual(k["project"], 418 * G + 5 * G + 4 * G)

    def test_unaccounted_and_segments(self) -> None:
        s, disk = self.s, self.r["disk"]
        self.assertEqual(s["unaccounted"], 150 * G)
        segs = {x["key"]: x["size"] for x in s["disk_segments"]}
        self.assertEqual([x["key"] for x in s["disk_segments"]],
                         [k for k, _ in insights.SEGMENTS])
        self.assertEqual(sum(segs.values()), disk["container"]["size"])
        self.assertEqual(segs["system_volumes"], int(13.3 * G) + int(11.5 * G) + int(3.8 * G)
                         + int(1.9 * G) + 24576)
        self.assertEqual(segs["vms"], 136 * G + 10 * G)
        self.assertEqual(segs["personal"], 90 * G + 12 * G + 8 * G)
        home = self.r["roots"][0]["size"]
        self.assertEqual(segs["home"], home - 146 * G - 102 * G)
        self.assertEqual(segs["docker"], 12 * G)
        self.assertEqual(segs["library"], 14 * G - 8 * G + 20 * G + 15 * G - 12 * G)
        self.assertEqual(segs["dev"], 9 * G + 6 * G + 30 * G)
        self.assertEqual(segs["apps_system"], int(2.5 * G) + 10 * G + 4 * G)
        self.assertEqual(segs["other"], 1 * G)
        self.assertEqual(segs["free"], 84 * G)
        notes = " | ".join(s["unaccounted_notes"])
        self.assertIn("administrator", notes)
        self.assertIn("1 local APFS snapshot (macOS update)", notes)

    def test_segments_scaled_when_overcounted(self) -> None:
        r = make_v2()
        r["disk"]["data_volume"]["used"] = int(500 * G)  # less than the scanned total
        r["disk"]["container"]["size"] = sum(v["used"] for v in r["disk"]["volumes"][1:]) + int(584 * G)
        insights.annotate(r, now=NOW)
        s = r["summary"]
        self.assertEqual(s["unaccounted"], 0)
        total = sum(x["size"] for x in s["disk_segments"])
        self.assertAlmostEqual(total / r["disk"]["container"]["size"], 1.0, places=6)
        self.assertTrue(all(x["size"] >= 0 for x in s["disk_segments"]))

    def test_without_disk(self) -> None:
        for disk in (None, {"error": "boom"}):
            r = make_v2(disk=False)
            if disk is not None:
                r["disk"] = disk
            insights.annotate(r, now=NOW)
            self.assertIsNone(r["summary"]["unaccounted"])
            self.assertEqual(r["summary"]["disk_segments"], [])

    def test_nested_and_stub_not_double_counted(self) -> None:
        # Home's stubs carry no size; Docker (nested in Containers) is excluded from the total.
        s = self.s
        roots = {x["label"]: x for x in self.r["roots"]}
        expected_total = sum(x["size"] for l, x in roots.items() if l != "Docker Desktop")
        self.assertEqual(s["total"], expected_total)
        self.assertEqual(s["by_category"]["docker"], 12 * G)
        self.assertEqual(s["by_category"]["container"], 3 * G)

    def test_pure_and_idempotent(self) -> None:
        r = make_v2()
        insights.annotate(r, now=NOW)
        first = copy.deepcopy(r)
        insights.annotate(r, now=NOW)
        self.assertEqual(first, r)

    def test_human(self) -> None:
        self.assertEqual(insights.human(177 * G), "177 GB")
        self.assertEqual(insights.human(23.6 * G), "23.6 GB")
        self.assertEqual(insights.human(512 * 10 ** 6), "512 MB")
        self.assertEqual(insights.human(0), "0 B")


if __name__ == "__main__":
    unittest.main()
