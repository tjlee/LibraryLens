"""Tests for detectors.annotate on a fake home with real (temporary) git repos and fixtures."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from typing import List, Optional
from unittest import mock

from librarylens import actions, detectors
from librarylens.tests.test_actions import OLD, git, make_repo, put

GIT_KEYS = {"repo", "pack_size", "packs", "largest_pack", "loose_size", "loose_count", "garbage_size",
            "garbage_files", "prunable_worktrees", "worktrees", "gc_running", "fetch_running", "last_commit"}


def node(path: str, size: int = 0, children: Optional[List[dict]] = None, **extra) -> dict:
    n = {"path": path, "name": os.path.basename(path), "size": size, "files": 1, "mtime": time.time(),
         "is_dir": True, "error": None, "children": children or []}
    n.update(extra)
    return n


class DetectorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="librarylens-detectors-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        h = self.home = os.path.join(self.tmp, "home")
        for d in ("Documents", "Downloads", "Library/Mail", "IdeaProjects", "Parallels", "deep/a/b"):
            os.makedirs(os.path.join(h, d))
        # big repo with garbage, two packs and a prunable worktree
        self.proj = os.path.join(h, "IdeaProjects", "ultimate")
        self.gd = make_repo(os.path.join(h, "IdeaProjects"), "ultimate", packs=2)
        put(os.path.join(self.gd, "objects", "pack", "tmp_pack_0IrRtI"), 300000)
        put(os.path.join(self.gd, "objects", "pack", "tmp_pack_FRESH1"), 1000, mtime=time.time())
        git("-C", self.proj, "worktree", "add", "-q", os.path.join(h, "IdeaProjects", "ultimate251"))
        shutil.rmtree(os.path.join(h, "IdeaProjects", "ultimate251"))
        os.makedirs(os.path.join(self.proj, "out"))
        os.makedirs(os.path.join(self.proj, "node_modules"))
        open(os.path.join(self.proj, "package.json"), "w").close()
        old = time.time() - 400 * 86400
        os.utime(os.path.join(self.proj, "package.json"), (old, old))
        # bare repo + deep repo not present in the tree
        self.bare = os.path.join(h, "IdeaProjects", "mirror.git")
        git("init", "-q", "--bare", self.bare)
        self.deep_gd = make_repo(os.path.join(h, "deep", "a", "b"), "small")
        # Parallels VM bundle
        self.vm = os.path.join(h, "Parallels", "Windows 11.pvm")
        os.makedirs(os.path.join(self.vm, "harddisk.hdd"))
        with open(os.path.join(self.vm, "config.pvs"), "w") as f:
            f.write("<ParallelsVirtualMachine><Identification><VmName>Win Test</VmName>"
                    "</Identification></ParallelsVirtualMachine>")
        with open(os.path.join(self.vm, "harddisk.hdd", "DiskDescriptor.xml"), "w") as f:
            f.write("<Snapshots><Shot><GUID>a</GUID></Shot><Shot><GUID>b</GUID></Shot></Snapshots>")
        put(os.path.join(self.vm, "x.mem"), 1000, mtime=OLD)
        self.result = self.build_result()

    def build_result(self) -> dict:
        h = self.home
        ip = os.path.join(h, "IdeaProjects")
        tree = node(h, 10 ** 9, [
            node(os.path.join(h, "Documents"), 100),
            node(os.path.join(h, "Downloads"), 100),
            node(os.path.join(h, "Library"), 0, link_root="Library (other)"),
            node(ip, 900, [
                node(self.proj, 800, [node(self.gd, 700), node(os.path.join(self.proj, "out"), 50),
                                      node(os.path.join(self.proj, "node_modules"), 40)]),
                node(self.bare, 5),
            ]),
            node(os.path.join(h, "Parallels"), 300, [node(self.vm, 300)]),
            node(os.path.join(h, "deep"), 1),
            {"path": h, "name": "(3 smaller items)", "size": 1, "synthetic": True, "children": [],
             "is_dir": False},
        ], label="Home", category="home", nested_in=None)
        mail = node(os.path.join(h, "Library", "Mail"), 10, label="Mail", category="other", nested_in=None)
        special = [
            {"path": self.gd, "kind": "git_dir", "size": 700, "files": 10, "mtime": 0, "root": "Home"},
            {"path": self.bare, "kind": "git_dir", "size": 5, "files": 10, "mtime": 0, "root": "Home"},
            {"path": self.deep_gd, "kind": "git_dir", "size": 3, "files": 10, "mtime": 0, "root": "Home"},
            {"path": os.path.join(self.proj, "out"), "kind": "build_output", "size": 50, "files": 1,
             "mtime": 0, "root": "Home"},
            {"path": os.path.join(self.proj, "node_modules"), "kind": "build_output", "size": 40, "files": 1,
             "mtime": 0, "root": "Home"},
            {"path": self.vm, "kind": "vm", "size": 300, "files": 3, "mtime": 0, "root": "Home"},
        ]
        return {"home": h, "roots": [tree, mail], "special": special}

    def find(self, path: str) -> dict:
        stack = list(self.result["roots"])
        while stack:
            n = stack.pop()
            if n.get("path") == path and not n.get("synthetic") and not n.get("link_root"):
                return n
            stack.extend(n.get("children") or [])
        raise KeyError(path)

    def test_annotate_shape(self) -> None:
        t0 = time.time()
        detectors.annotate(self.result)
        self.assertLess(time.time() - t0, 5)
        h = self.home
        self.assertEqual(self.find(os.path.join(h, "Documents"))["kind"], "user_data")
        self.assertEqual(self.find(os.path.join(h, "Library", "Mail"))["kind"], "user_data")
        self.assertEqual(self.find(os.path.join(h, "Downloads"))["kind"], "downloads")
        self.assertNotIn("kind", self.find(h))

        g = self.find(self.gd)
        self.assertEqual(g["kind"], "git_dir")
        d = g["details"]
        self.assertTrue(GIT_KEYS <= set(d), GIT_KEYS - set(d))
        self.assertEqual(d["repo"], "ultimate")
        self.assertGreaterEqual(d["packs"], 2)
        self.assertGreater(d["largest_pack"], 0)
        self.assertEqual(sorted(d["garbage_files"]), ["tmp_pack_0IrRtI", "tmp_pack_FRESH1"])
        self.assertEqual(d["garbage_old_files"], 1)
        self.assertGreaterEqual(d["garbage_size"], 300000)
        self.assertEqual(d["prunable_worktrees"], [os.path.join(h, "IdeaProjects", "ultimate251")])
        self.assertEqual(d["worktrees"], 1)
        self.assertFalse(d["gc_running"])
        self.assertFalse(d["fetch_running"])
        self.assertIsInstance(d["last_commit"], float)
        ids = {a["id"]: a for a in g["actions"]}
        self.assertEqual(set(ids), {"git_delete_temp_packs", "git_worktree_prune", "git_gc"})
        self.assertEqual(ids["git_delete_temp_packs"]["label"], "Delete 1 leftover fetch temp file")
        self.assertEqual(ids["git_delete_temp_packs"]["frees"], d["garbage_old_size"])
        for a in g["actions"]:
            self.assertTrue({"id", "label", "frees", "safety", "blocked", "background", "confirm"} <= set(a))

        p = self.find(self.proj)
        self.assertEqual(p["kind"], "project")
        self.assertEqual(p["details"]["git_dir"], self.gd)
        out = self.find(os.path.join(self.proj, "out"))
        self.assertEqual(out["kind"], "build_output")
        self.assertEqual(out["details"]["project"], self.proj)
        self.assertIsInstance(out["details"]["project_mtime"], float)

        vm = self.find(self.vm)
        self.assertEqual(vm["kind"], "vm")
        self.assertEqual(vm["details"]["vm_name"], "Win Test")
        self.assertEqual(vm["details"]["snapshots"], 1)
        self.assertTrue(vm["details"]["suspended"])
        self.assertIsNotNone(vm["details"]["last_used"])

        b = self.find(self.bare)
        self.assertEqual(b["kind"], "git_dir")
        self.assertTrue(b["details"]["bare"])
        self.assertNotIn("actions", b)  # empty bare repo: nothing to do

        repos = self.result["git_repos"]
        self.assertEqual([r["git_dir"] for r in repos], [self.gd, self.bare, self.deep_gd])
        self.assertEqual(repos[0]["path"], self.proj)
        self.assertEqual(repos[0]["size"], 700)
        self.assertEqual(repos[1]["path"], self.bare)
        self.assertEqual(repos[2]["path"], os.path.dirname(self.deep_gd))  # deeper than the tree
        self.assertEqual(repos[2]["repo"], "small")
        self.assertTrue(all(isinstance(r["actions"], list) for r in repos))

        projects = {p["path"]: p for p in self.result["projects"]}
        up = projects[self.proj]
        self.assertEqual(up["git_size"], 700)
        self.assertEqual(up["build_size"], 90)
        self.assertEqual(up["size"], 790)
        self.assertEqual([b["name"] for b in up["build_outputs"]], ["out", "node_modules"])
        self.assertIn(os.path.dirname(self.deep_gd), projects)
        self.assertNotIn(self.bare, projects)

    def test_reannotate_clears_stale_keys_and_reflects_changes(self) -> None:
        detectors.annotate(self.result)
        self.assertEqual(len(self.find(self.gd)["details"]["garbage_files"]), 2)
        for n in os.listdir(os.path.join(self.gd, "objects", "pack")):
            if n.startswith("tmp_pack_"):
                os.unlink(os.path.join(self.gd, "objects", "pack", n))
        self.result["special"] = [s for s in self.result["special"] if s["kind"] != "vm"]
        self.find(self.vm)["name"] = "renamed"  # no fallback re-detection by name
        detectors.annotate(self.result)
        g = self.find(self.gd)
        self.assertEqual(g["details"]["garbage_files"], [])
        self.assertNotIn("git_delete_temp_packs", {a["id"] for a in g["actions"]})
        self.assertNotIn("kind", self.find(self.vm))

    def test_gc_running_blocks_actions(self) -> None:
        with open(os.path.join(self.gd, "gc.pid"), "w") as f:
            f.write("%d host\n" % os.getpid())
        detectors.annotate(self.result)
        g = self.find(self.gd)
        self.assertTrue(g["details"]["gc_running"])
        ids = {a["id"]: a for a in g["actions"]}
        self.assertIn("gc is running", ids["git_delete_temp_packs"]["blocked"])
        self.assertIn("gc is running", ids["git_gc"]["blocked"])

    def test_fallback_without_special(self) -> None:
        del self.result["special"]
        detectors.annotate(self.result)
        self.assertEqual(self.find(self.gd)["kind"], "git_dir")
        self.assertEqual(self.find(self.vm)["kind"], "vm")
        self.assertEqual([r["git_dir"] for r in self.result["git_repos"]], [self.gd])

    def test_loose_objects_sampling(self) -> None:
        objs = os.path.join(self.tmp, "fake", "objects")
        for i in range(256):
            d = os.path.join(objs, "%02x" % i)
            os.makedirs(d)
            for j in range(3):
                open(os.path.join(d, "%038x" % j), "w").close()
        with mock.patch.object(detectors, "LOOSE_EXACT_MAX", 100):
            est = detectors.loose_objects(os.path.join(self.tmp, "fake"))
        self.assertTrue(est["loose_estimated"])
        self.assertEqual(est["loose_count"], 768)
        exact = detectors.loose_objects(os.path.join(self.tmp, "fake"))
        self.assertFalse(exact["loose_estimated"])
        self.assertEqual(exact["loose_count"], 768)

    def test_light_mode_after_budget(self) -> None:
        with mock.patch.object(detectors, "TIME_BUDGET_S", -1):
            detectors.annotate(self.result)
        d = self.find(self.gd)["details"]
        self.assertIsNone(d["loose_count"])
        self.assertEqual(len(d["garbage_files"]), 2)

    def test_details_never_mutate_repo(self) -> None:
        def snapshot():
            out = {}
            for dp, dn, fn in os.walk(self.gd):
                for n in fn:
                    st = os.lstat(os.path.join(dp, n))
                    out[os.path.join(dp, n)] = (st.st_size, st.st_mtime)
            return out
        before = snapshot()
        detectors.annotate(self.result)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
