from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import unittest
from unittest import mock

from librarylens import trash

HOME = os.path.expanduser("~")
TRASH = os.path.join(HOME, ".Trash")


class TrashTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tag = secrets.token_hex(6)
        self.root = os.path.join(HOME, "Library", "Caches", "librarylens-selftest-" + self.tag)
        os.makedirs(self.root)
        self.outside = tempfile.mkdtemp(prefix="librarylens-selftest-outside-" + self.tag)
        self.log = os.path.join(self.outside, "actions.log")
        p = mock.patch.object(trash, "ACTIONS_LOG", self.log)
        p.start()
        self.addCleanup(p.stop)
        self.trashed = []  # paths in ~/.Trash created by this test

    def tearDown(self) -> None:
        for t in self.trashed:
            # only ever delete items carrying our unique random tag, and only inside ~/.Trash
            if t and self.tag in os.path.basename(t) and os.path.dirname(t) == TRASH and os.path.lexists(t):
                if os.path.isdir(t) and not os.path.islink(t):
                    shutil.rmtree(t)
                else:
                    os.remove(t)
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def _make_item(self, name: str = "item") -> str:
        d = os.path.join(self.root, "%s-%s" % (name, self.tag))
        os.makedirs(os.path.join(d, "sub"))
        with open(os.path.join(d, "sub", "f.txt"), "w") as f:
            f.write("hello")
        return d

    def _find_in_trash(self, res: dict, name: str) -> str:
        dest = res.get("trashed_to")
        if dest and os.path.lexists(dest):
            return dest
        cand = os.path.join(TRASH, name)  # Finder didn't report a path; check the obvious place
        self.assertTrue(os.path.lexists(cand), "item not found in ~/.Trash: %r" % res)
        return cand

    def _log_entries(self) -> list:
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_move_to_trash_finder_or_fallback(self) -> None:
        item = self._make_item()
        res = trash.move_to_trash(item, [self.root], size=4096)
        self.assertTrue(res["ok"], res)
        self.assertIn(res["method"], ("finder", "rename"))
        self.assertFalse(os.path.lexists(item))
        dest = self._find_in_trash(res, os.path.basename(item))
        self.trashed.append(dest)
        self.assertTrue(os.path.basename(dest).startswith(os.path.basename(item)))
        self.assertTrue(os.path.exists(os.path.join(dest, "sub", "f.txt")))
        entries = self._log_entries()
        self.assertTrue(entries[-1]["ok"])
        self.assertEqual(entries[-1]["size"], 4096)
        self.assertEqual(entries[-1]["path"], os.path.realpath(item))

    def test_fallback_rename_when_finder_fails(self) -> None:
        item = self._make_item("fallback")
        with mock.patch.object(trash, "_finder_delete", return_value=(False, None, "execution error: -1743")):
            res = trash.move_to_trash(item, [self.root])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["method"], "rename")
        self.assertFalse(os.path.lexists(item))
        self.trashed.append(res["trashed_to"])
        self.assertEqual(os.path.dirname(res["trashed_to"]), TRASH)
        self.assertTrue(os.path.isdir(res["trashed_to"]))
        methods = [e["method"] for e in self._log_entries()]
        self.assertEqual(methods[-2:], ["finder", "rename"])

    def test_unique_name_in_trash(self) -> None:
        name = "dup-%s.txt" % self.tag
        with mock.patch.object(trash, "TRASH_DIR", self.outside):
            open(os.path.join(self.outside, name), "w").close()
            u = trash._unique_trash_name(name)
        self.assertNotEqual(u, name)
        self.assertTrue(u.startswith("dup-%s " % self.tag) and u.endswith(".txt"), u)

    def test_rejects_root_itself_and_protected(self) -> None:
        for p in (self.root, self.root + "/", "/", HOME, os.path.join(HOME, "Library")):
            res = trash.move_to_trash(p, [self.root])
            self.assertFalse(res["ok"], p)
        self.assertTrue(os.path.isdir(self.root))
        # even if someone passes very broad roots
        for p in ("/", HOME, os.path.join(HOME, "Library")):
            res = trash.move_to_trash(p, ["/", HOME])
            self.assertFalse(res["ok"], p)
        entries = self._log_entries()
        self.assertTrue(all(not e["ok"] for e in entries))

    def test_rejects_outside_and_relative_and_missing(self) -> None:
        outside_item = os.path.join(self.outside, "victim-" + self.tag)
        os.makedirs(outside_item)
        self.assertFalse(trash.move_to_trash(outside_item, [self.root])["ok"])
        self.assertTrue(os.path.isdir(outside_item))
        # path traversal
        trav = os.path.join(self.root, "..", os.path.basename(self.root) + "x")
        self.assertFalse(trash.move_to_trash(trav, [self.root])["ok"])
        self.assertFalse(trash.move_to_trash(self.root + "/../../Caches", [self.root])["ok"])
        self.assertFalse(trash.move_to_trash("relative/path", [self.root])["ok"])
        self.assertFalse(trash.move_to_trash(os.path.join(self.root, "missing"), [self.root])["ok"])
        self.assertFalse(trash.move_to_trash(os.path.join(self.root, "x"), [])["ok"])

    def test_rejects_git_internals(self) -> None:
        repo = os.path.join(self.root, "repo-" + self.tag)
        pack = os.path.join(repo, ".git", "objects", "pack")
        os.makedirs(pack)
        tmp = os.path.join(pack, "tmp_pack_" + self.tag)
        open(tmp, "w").close()
        for p in (os.path.join(repo, ".git"), tmp):
            res = trash.move_to_trash(p, [self.root])
            self.assertFalse(res["ok"], p)
            self.assertIn("git cleanup actions", res["error"])
        self.assertTrue(os.path.exists(tmp))

    def test_rejects_symlink_escape(self) -> None:
        victim = os.path.join(self.outside, "victim-" + self.tag)
        os.makedirs(os.path.join(victim, "inner"))
        link = os.path.join(self.root, "link-" + self.tag)
        os.symlink(victim, link)
        res = trash.move_to_trash(link, [self.root])
        self.assertFalse(res["ok"])
        self.assertIn("symlink", res["error"])
        # going *through* the symlink resolves outside the root
        res = trash.move_to_trash(os.path.join(link, "inner"), [self.root])
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.isdir(os.path.join(victim, "inner")))
        self.assertTrue(os.path.islink(link))

    def test_symlink_inside_moves_link_not_target(self) -> None:
        target = self._make_item("target")
        link = os.path.join(self.root, "inlink-" + self.tag)
        os.symlink(target, link)
        res = trash.move_to_trash(link, [self.root])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["method"], "rename")
        self.trashed.append(res["trashed_to"])
        self.assertTrue(os.path.islink(res["trashed_to"]))
        self.assertTrue(os.path.isdir(target))

    def test_reveal_uses_argv(self) -> None:
        with mock.patch.object(trash.subprocess, "run") as run:
            trash.reveal_in_finder("/tmp/a b; rm -rf ~")
        args = run.call_args[0][0]
        self.assertEqual(args, ["open", "-R", "/tmp/a b; rm -rf ~"])


if __name__ == "__main__":
    unittest.main()
