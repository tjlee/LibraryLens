"""Tests for actions.py on throw-away git repositories in a temp dir (never touches real repos)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from librarylens import actions

OLD = time.time() - 3600


def git(*args: str, cwd: str = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    cp = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                         "-c", "init.defaultBranch=main"] + list(args),
                        cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if cp.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), cp.stderr))
    return cp.stdout


def make_repo(parent: str, name: str = "repo", packs: int = 0) -> str:
    """git init + one commit (+ ``packs`` extra commits each repacked into its own pack). Returns .git."""
    wt = os.path.join(parent, name)
    os.makedirs(wt)
    git("init", "-q", wt)
    with open(os.path.join(wt, "a.txt"), "w") as f:
        f.write("hello\n")
    git("-C", wt, "add", "a.txt")
    git("-C", wt, "commit", "-qm", "init")
    for i in range(packs):
        with open(os.path.join(wt, "f%d.txt" % i), "w") as f:
            f.write(os.urandom(2000).hex())
        git("-C", wt, "add", ".")
        git("-C", wt, "commit", "-qm", "c%d" % i)
        git("-C", wt, "repack", "-q")
    return os.path.join(wt, ".git")


def put(path: str, size: int = 50000, mtime: float = OLD) -> str:
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    os.chmod(path, 0o444)  # git leaves them read-only
    os.utime(path, (mtime, mtime))
    return path


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="librarylens-actions-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.root)
        os.makedirs(self.outside)
        self.log = os.path.join(self.tmp, "logdir", "actions.log")
        p = mock.patch.object(actions, "ACTIONS_LOG", self.log)
        p.start()
        self.addCleanup(p.stop)

    def log_entries(self):
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def hold_open(self, path: str) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c",
                              "import sys,time\nf=open(sys.argv[1],'rb')\nprint('ok',flush=True)\n"
                              "time.sleep(60)", path], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "ok")
        p.stdout.close()
        return p


class RegexTest(unittest.TestCase):
    def test_garbage_names(self) -> None:
        good = ["tmp_pack_0IrRtI", "tmp_idx_AbC123", "tmp_rev_zzzzzz",
                ".tmp-25929-pack-" + "a" * 40 + ".pack", ".tmp-1-pack-" + "b" * 40 + ".idx", ".tmp-25929-pack"]
        bad = ["tmp_pack_", "tmp_pack_ABC", "tmp_pack_ABCDEFG", "tmp_pack_ABC/EF", "pack-" + "a" * 40 + ".pack",
               "xtmp_pack_ABCDEF", "tmp_pack_ABCDEF.keep", ".tmp-x-pack", "tmp_pack_ABCDE\n", "multi-pack-index"]
        for n in good:
            self.assertTrue(actions.GARBAGE_RE.match(n), n)
        for n in bad:
            self.assertFalse(actions.GARBAGE_RE.match(n), n)

    def test_parse_git_command(self) -> None:
        p = actions._parse_git_command
        self.assertEqual(p("/usr/bin/git -c core.quotepath=false -C /x fetch origin")["sub"], "fetch")
        self.assertEqual(p("/usr/bin/git -c core.quotepath=false -C /x fetch origin")["C"], "/x")
        self.assertEqual(p("/Library/Developer/CommandLineTools/usr/libexec/git-core/git pack-objects --local")
                         ["sub"], "pack-objects")
        self.assertEqual(p("git-remote-https origin https://x")["sub"], "remote-https")
        self.assertIsNone(p("/bin/zsh -c echo git fetch"))
        self.assertIsNone(p("vim notes-about-git"))


class DeleteTempPacksTest(Base):
    def test_deletes_only_old_unused_strictly_named_files(self) -> None:
        gd = make_repo(self.root)
        pack = os.path.join(gd, "objects", "pack")
        old1 = put(os.path.join(pack, "tmp_pack_AAAAAA"), 200000)
        old2 = put(os.path.join(pack, "tmp_idx_BBBBBB"), 30000)
        old3 = put(os.path.join(pack, ".tmp-123-pack-" + "c" * 40 + ".pack"), 30000)
        fresh = put(os.path.join(pack, "tmp_pack_CCCCCC"), 30000, mtime=time.time() - 60)
        held = put(os.path.join(pack, "tmp_pack_DDDDDD"), 30000)
        odd = put(os.path.join(pack, "tmp_pack_TOOLONG1"), 30000)
        other = put(os.path.join(self.root, "tmp_pack_EEEEEE"), 30000)  # outside objects/pack
        target = put(os.path.join(self.outside, "victim"), 30000)
        link = os.path.join(pack, "tmp_pack_LINKED")
        os.symlink(target, link)
        os.utime(link, (OLD, OLD), follow_symlinks=False)
        self.hold_open(held)

        node = {"path": gd, "kind": "git_dir", "details": dict(actions.list_pack_dir(gd), git_dir=gd)}
        desc = [a for a in actions.available(node) if a["id"] == "git_delete_temp_packs"][0]
        self.assertIsNone(desc["blocked"])
        self.assertFalse(desc["background"])
        self.assertEqual(desc["safety"], "safe")
        self.assertIn("Permanently", desc["confirm"])

        out = []
        res = actions.run("git_delete_temp_packs", gd, [self.root], out.append)
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["details"]["deleted"]),
                         sorted(os.path.basename(p) for p in (old1, old2, old3)))
        for p in (old1, old2, old3):
            self.assertFalse(os.path.lexists(p))
        for p in (fresh, held, odd, other, target, link):
            self.assertTrue(os.path.lexists(p), p)
        reasons = {s["name"]: s["reason"] for s in res["details"]["skipped"]}
        self.assertIn("10 minutes", reasons["tmp_pack_CCCCCC"])
        self.assertIn("open", reasons["tmp_pack_DDDDDD"])
        self.assertIn("regular", reasons["tmp_pack_LINKED"])
        self.assertGreaterEqual(res["freed"], 200000 + 30000 + 30000)
        self.assertEqual(len(out), 3)
        # repo still healthy
        git("-C", os.path.dirname(gd), "fsck", "--no-progress")
        e = self.log_entries()[-1]
        self.assertEqual(e["action"], "git_delete_temp_packs")
        self.assertTrue(e["ok"])
        self.assertEqual(e["freed"], res["freed"])

    def test_accepts_work_tree_path_and_bare_repo(self) -> None:
        gd = make_repo(self.root)
        f = put(os.path.join(gd, "objects", "pack", "tmp_pack_AAAAAA"))
        res = actions.run("git_delete_temp_packs", os.path.dirname(gd), [self.root])
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(f))
        bare = os.path.join(self.root, "b.git")
        git("init", "-q", "--bare", bare)
        f = put(os.path.join(bare, "objects", "pack", "tmp_pack_BBBBBB"))
        res = actions.run("git_delete_temp_packs", bare, [self.root])
        self.assertTrue(res["ok"], res)
        self.assertFalse(os.path.exists(f))

    def test_refuses_outside_root(self) -> None:
        gd = make_repo(self.outside)
        f = put(os.path.join(gd, "objects", "pack", "tmp_pack_AAAAAA"))
        res = actions.run("git_delete_temp_packs", gd, [self.root])
        self.assertFalse(res["ok"])
        self.assertIn("outside", res["error"])
        self.assertTrue(os.path.exists(f))
        res = actions.run("git_delete_temp_packs", self.root + "/../outside/repo/.git", [self.root])
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(f))
        res = actions.run("git_delete_temp_packs", gd, [])
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(f))
        self.assertFalse(self.log_entries()[-1]["ok"])

    def test_refuses_symlink_escape(self) -> None:
        gd = make_repo(self.outside)
        f = put(os.path.join(gd, "objects", "pack", "tmp_pack_AAAAAA"))
        os.symlink(os.path.dirname(gd), os.path.join(self.root, "linkrepo"))
        os.symlink(gd, os.path.join(self.root, "linkgit"))
        for p in ("linkrepo", "linkgit", "linkrepo/.git"):
            res = actions.run("git_delete_temp_packs", os.path.join(self.root, p), [self.root])
            self.assertFalse(res["ok"], p)
        self.assertTrue(os.path.exists(f))

    def test_refuses_symlinked_pack_dir(self) -> None:
        gd = make_repo(self.root)
        real_pack = os.path.join(self.outside, "pack")
        os.makedirs(real_pack)
        f = put(os.path.join(real_pack, "tmp_pack_AAAAAA"))
        shutil.rmtree(os.path.join(gd, "objects", "pack"))
        os.symlink(real_pack, os.path.join(gd, "objects", "pack"))
        res = actions.run("git_delete_temp_packs", gd, [self.root])
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(f))

    def test_refuses_non_git_root_itself_and_linked_worktree(self) -> None:
        plain = os.path.join(self.root, "plain")
        os.makedirs(os.path.join(plain, "objects", "pack"))
        self.assertFalse(actions.run("git_delete_temp_packs", plain, [self.root])["ok"])
        gd = make_repo(self.root)
        self.assertFalse(actions.run("git_delete_temp_packs", gd, [gd])["ok"])  # root itself
        self.assertFalse(actions.run("git_delete_temp_packs", "relative/.git", [self.root])["ok"])
        wt = os.path.join(self.root, "wt")
        git("-C", os.path.dirname(gd), "worktree", "add", "-q", wt)
        res = actions.run("git_delete_temp_packs", wt, [self.root])
        self.assertFalse(res["ok"])
        self.assertIn("worktree", res["error"])
        self.assertEqual(actions.run("rm_rf", gd, [self.root])["error"], "unknown action")

    def test_blocked_while_gc_or_fetch_running(self) -> None:
        gd = make_repo(self.root)
        f = put(os.path.join(gd, "objects", "pack", "tmp_pack_AAAAAA"))
        with open(os.path.join(gd, "gc.pid"), "w") as fh:
            fh.write("%d host\n" % os.getpid())
        det = dict(actions.list_pack_dir(gd), git_dir=gd, **actions.repo_activity(gd, []))
        self.assertTrue(det["gc_running"])
        acts = {a["id"]: a for a in actions.available({"kind": "git_dir", "path": gd, "details": det})}
        self.assertIn("gc is running", acts["git_delete_temp_packs"]["blocked"])
        res = actions.run("git_delete_temp_packs", gd, [self.root])
        self.assertFalse(res["ok"])
        self.assertIn("gc", res["error"])
        self.assertTrue(os.path.exists(f))
        # stale gc.pid (dead pid) is ignored; a fresh lock blocks
        with open(os.path.join(gd, "gc.pid"), "w") as fh:
            fh.write("999999 host\n")
        open(os.path.join(gd, "packed-refs.lock"), "w").close()
        res = actions.run("git_delete_temp_packs", gd, [self.root])
        self.assertFalse(res["ok"])
        self.assertIn("fetch", res["error"])
        self.assertTrue(os.path.exists(f))
        os.unlink(os.path.join(gd, "packed-refs.lock"))
        self.assertTrue(actions.run("git_delete_temp_packs", gd, [self.root])["ok"])
        self.assertFalse(os.path.exists(f))

    def test_blocked_when_git_process_in_repo(self) -> None:
        gd = make_repo(self.root)
        procs = [{"pid": 1, "sub": "index-pack", "type": "fetch", "cmd": "git index-pack", "C": None,
                  "git_dir_arg": None, "cwd": os.path.dirname(gd)},
                 {"pid": 2, "sub": "fetch", "type": "fetch", "cmd": "git fetch", "C": None,
                  "git_dir_arg": None, "cwd": self.outside}]
        act = actions.repo_activity(gd, procs)
        self.assertTrue(act["fetch_running"])
        self.assertEqual(len(actions.processes_for_repo(gd, procs)), 1)
        self.assertFalse(actions.repo_activity(gd, procs[1:])["fetch_running"])
        with mock.patch.object(actions, "git_processes", return_value=procs):
            f = put(os.path.join(gd, "objects", "pack", "tmp_pack_AAAAAA"))
            self.assertFalse(actions.run("git_delete_temp_packs", gd, [self.root])["ok"])
            self.assertTrue(os.path.exists(f))


class WorktreePruneTest(Base):
    def test_prune_missing_worktree(self) -> None:
        gd = make_repo(self.root)
        repo = os.path.dirname(gd)
        for name in ("wt1", "wt2"):
            git("-C", repo, "worktree", "add", "-q", os.path.join(self.root, name))
        shutil.rmtree(os.path.join(self.root, "wt1"))
        wt = actions.worktrees(gd)
        self.assertEqual(wt["worktrees"], 2)
        self.assertEqual(wt["prunable_worktrees"], [os.path.join(self.root, "wt1")])
        det = dict(actions.list_pack_dir(gd), git_dir=gd, **wt)
        acts = {a["id"]: a for a in actions.available({"kind": "git_dir", "path": gd, "details": det})}
        self.assertIn("git_worktree_prune", acts)
        self.assertIsNone(acts["git_worktree_prune"]["blocked"])
        res = actions.run("git_worktree_prune", gd, [self.root])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["details"]["pruned"], [os.path.join(self.root, "wt1")])
        after = actions.worktrees(gd)
        self.assertEqual(after["worktrees"], 1)
        self.assertEqual(after["prunable_worktrees"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "wt2")))


class GcTest(Base):
    def test_gc_blocked_when_free_space_insufficient(self) -> None:
        gd = make_repo(self.root, packs=2)
        put(os.path.join(gd, "objects", "pack", "tmp_pack_AAAAAA"))
        det = dict(actions.list_pack_dir(gd), git_dir=gd, loose_count=0)
        self.assertGreaterEqual(det["packs"], 2)
        with mock.patch.object(actions, "free_bytes", return_value=10):
            acts = {a["id"]: a for a in actions.available({"kind": "git_dir", "path": gd, "details": det})}
            g = acts["git_gc"]
            self.assertTrue(g["background"])
            self.assertIn("needs at least", g["blocked"])
            self.assertIn("delete the leftover temp pack files first", g["blocked"])
            before = sorted(os.listdir(os.path.join(gd, "objects", "pack")))
            res = actions.run("git_gc", gd, [self.root])
            self.assertFalse(res["ok"])
            self.assertIn("not enough free space", res["error"])
            self.assertEqual(sorted(os.listdir(os.path.join(gd, "objects", "pack"))), before)

    def test_gc_success_streams_output(self) -> None:
        gd = make_repo(self.root, packs=2)
        self.assertGreaterEqual(actions.list_pack_dir(gd)["packs"], 2)
        lines = []
        res = actions.run("git_gc", gd, [self.root], lines.append)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["details"]["packs_after"], 1)
        self.assertTrue(lines and lines[0].startswith("$ git -C"))
        git("-C", os.path.dirname(gd), "fsck", "--no-progress")
        phases = [e.get("phase") for e in self.log_entries()]
        self.assertIn("start", phases)
        self.assertTrue(self.log_entries()[-1]["ok"])

    def test_gc_blocked_while_running(self) -> None:
        gd = make_repo(self.root, packs=2)
        with open(os.path.join(gd, "gc.pid"), "w") as fh:
            fh.write("%d host\n" % os.getpid())
        res = actions.run("git_gc", gd, [self.root])
        self.assertFalse(res["ok"])
        self.assertIn("gc is running", res["error"])


if __name__ == "__main__":
    unittest.main()
