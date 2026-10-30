"""Tests for resolver.py against real directory names on this machine (no sizes computed)."""
from __future__ import annotations

import json
import os
import time
import unittest
from typing import Dict, List, Optional

from librarylens import resolver

HOME = os.path.expanduser("~")
USER = os.environ.get("USER") or os.path.basename(HOME)
ROOTS = [
    ("Caches", "~/Library/Caches", "cache"),
    ("Application Support", "~/Library/Application Support", "app_support"),
    ("Containers", "~/Library/Containers", "container"),
    ("Group Containers", "~/Library/Group Containers", "group_container"),
    ("Logs", "~/Library/Logs", "logs"),
    ("~/.cache", "~/.cache", "dev_cache"),
    ("Gradle", "~/.gradle", "dev_cache"),
    ("Maven", "~/.m2", "dev_cache"),
    ("npm", "~/.npm", "dev_cache"),
    ("Kotlin/Native", "~/.konan", "dev_cache"),
    ("NuGet", "~/.nuget", "dev_cache"),
    ("Go modules", "~/go/pkg", "dev_cache"),
    ("Bazel output", "/private/var/tmp/_bazel_" + USER, "bazel"),
    ("Docker Desktop", "~/Library/Containers/com.docker.docker", "docker"),
    ("~/.docker", "~/.docker", "docker"),
]
VENDORS = {"jetbrains", "google", "microsoft", "mozilla", "adobe", ".cache"}
INTELLIJ = [os.path.join(HOME, "Applications/IntelliJ IDEA.app"), "/Applications/IntelliJ IDEA.app"]


def _listdir(path: str) -> List[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _node(path: str, depth: int) -> dict:
    """Fake Node with real child names; depth-2 children only for vendor dirs."""
    name = os.path.basename(path)
    node = {"path": path, "name": name, "size": 0, "files": 0, "mtime": 0.0, "is_dir": True,
            "error": None, "children": []}
    if depth == 0 or (depth == 1 and name.lower() in VENDORS):
        node["children"] = [_node(os.path.join(path, n), depth + 1) for n in _listdir(path)
                            if os.path.isdir(os.path.join(path, n)) or depth == 0]
    return node


def build_result() -> dict:
    roots = []
    for label, tmpl, cat in ROOTS:
        path = os.path.expanduser(tmpl)
        if not os.path.isdir(path):
            continue
        r = _node(path, 0)
        r["children"].append({"name": "(3 smaller items)", "path": path, "is_dir": False,
                              "synthetic": True, "size": 0, "children": []})
        r.update(label=label, category=cat, nested_in=None)
        roots.append(r)
    return {"scanned_at": time.time(), "duration_s": 0.0, "home": HOME, "roots": roots}


def _installed_intellij_version() -> Optional[str]:
    for app in INTELLIJ:
        try:
            with open(os.path.join(app, "Contents/Resources/product-info.json")) as f:
                dd = json.load(f).get("dataDirectoryName", "")
            return dd[len("IntelliJIdea"):] if dd.startswith("IntelliJIdea") else None
        except OSError:
            continue
    return None


class ResolverRealDataTest(unittest.TestCase):
    result: dict
    elapsed: float

    @classmethod
    def setUpClass(cls) -> None:
        resolver.reset()
        cls.result = build_result()
        t0 = time.time()
        resolver.annotate(cls.result)
        cls.elapsed = time.time() - t0
        cls.by_path: Dict[str, dict] = {}

        def walk(n: dict) -> None:
            if not n.get("synthetic"):
                cls.by_path[n["path"]] = n
            for c in n.get("children") or []:
                walk(c)
        for r in cls.result["roots"]:
            walk(r)
        cls._print_table()

    @classmethod
    def _print_table(cls) -> None:
        print("\nannotate() took %.2fs (cold); warm: %.3fs" % (cls.elapsed, cls._warm()))
        for r in cls.result["roots"]:
            kids = [c for c in r["children"] if not c.get("synthetic")]
            known = sum(1 for c in kids if c.get("app"))
            ra = r.get("app")
            print("\n== %s [%s] root-> %s   resolved %d/%d" % (
                r["path"], r["category"], ra["name"] if ra else None, known, len(kids)))
            for c in kids:
                for n in [c] + [g for g in c.get("children") or [] if not g.get("synthetic")]:
                    a = n.get("app")
                    rel = os.path.relpath(n["path"], r["path"])
                    if a:
                        print("  %-58s -> %-40s inst=%-5s run=%-5s ver=%-8s iv=%s src=%s" % (
                            rel[:58], a["name"][:40], a["installed"], a["running"], a["version"],
                            ",".join(a["installed_versions"]), a["source"]))
                    else:
                        print("  %-58s -> ?" % rel[:58])

    @classmethod
    def _warm(cls) -> float:
        t0 = time.time()
        resolver.annotate(build_result())
        return time.time() - t0

    def app(self, path: str) -> Optional[dict]:
        node = self.by_path.get(os.path.expanduser(path))
        if node is None:
            self.skipTest("%s not present on this machine" % path)
        return node.get("app")

    def test_fast(self) -> None:
        self.assertLess(self.elapsed, 5.0)

    def test_appinfo_shape(self) -> None:
        keys = {"name", "bundle_id", "app_path", "installed", "running", "version",
                "installed_versions", "kind", "source"}
        for n in self.by_path.values():
            a = n.get("app")
            if a:
                self.assertEqual(set(a), keys, n["path"])
                self.assertIn(a["kind"], ("app", "cli", "system"))
                self.assertIn(a["source"], ("bundle_id", "container_plist", "name_match", "known", "jetbrains"))
                json.dumps(a)

    def test_synthetic_skipped(self) -> None:
        for r in self.result["roots"]:
            for c in r["children"]:
                if c.get("synthetic"):
                    self.assertNotIn("app", c)

    def test_old_intellij_not_installed(self) -> None:
        ver = _installed_intellij_version()
        if not ver:
            self.skipTest("no IntelliJ IDEA with product-info.json")
        for old in ("~/Library/Caches/JetBrains/IntelliJIdea2026.3",
                    "~/Library/Application Support/JetBrains/IntelliJIdea2026.3"):
            a = self.app(old)
            self.assertEqual(a["name"], "IntelliJ IDEA")
            self.assertEqual(a["version"], "2026.3")
            self.assertEqual(a["source"], "jetbrains")
            if ver != "2026.3":
                self.assertFalse(a["installed"])
            self.assertIn(ver, a["installed_versions"])
        cur = self.app("~/Library/Caches/JetBrains/IntelliJIdea" + ver)
        self.assertTrue(cur["installed"])
        self.assertTrue(cur["app_path"].endswith("IntelliJ IDEA.app"))

    def test_jetbrains_misc(self) -> None:
        a = self.app("~/Library/Caches/JetBrains/MonorepoBazel")
        self.assertIn("Bazel", a["name"])
        self.assertTrue(a["installed"])
        self.assertEqual(self.app("~/Library/Caches/JetBrains/Toolbox")["name"], "JetBrains Toolbox")
        self.assertEqual(self.app("~/Library/Caches/JetBrains")["name"], "JetBrains")

    def test_slack_shipit(self) -> None:
        a = self.app("~/Library/Caches/com.tinyspeck.slackmacgap.ShipIt")
        self.assertEqual(a["name"], "Slack")
        self.assertTrue(a["installed"])
        self.assertEqual(a["app_path"], "/Applications/Slack.app")

    def test_code_is_vscode(self) -> None:
        a = self.app("~/Library/Application Support/Code")
        self.assertEqual(a["name"], "Visual Studio Code")
        self.assertEqual(a["bundle_id"], "com.microsoft.VSCode")
        installed = any(os.path.isdir(p) for p in ("/Applications/Visual Studio Code.app",
                                                   os.path.join(HOME, "Applications/Visual Studio Code.app")))
        if installed:
            self.assertTrue(a["installed"])

    def test_homebrew(self) -> None:
        a = self.app("~/Library/Caches/Homebrew")
        self.assertEqual(a["name"], "Homebrew")
        self.assertEqual(a["kind"], "cli")

    def test_roots(self) -> None:
        self.assertIsNone(self.app("~/Library/Caches"))
        self.assertEqual(self.app("~/.gradle")["name"], "Gradle")
        self.assertEqual(self.app("~/go/pkg")["name"], "Go module cache")
        a = self.app("~/Library/Containers/com.docker.docker")
        self.assertEqual(a["name"], "Docker Desktop")
        self.assertEqual(self.app("/private/var/tmp/_bazel_" + USER)["name"], "Bazel")

    def test_bazel_children_inherit(self) -> None:
        root = self.by_path.get("/private/var/tmp/_bazel_" + USER)
        if not root:
            self.skipTest("no bazel output root")
        for c in root["children"]:
            if not c.get("synthetic"):
                self.assertEqual(c["app"]["name"], "Bazel", c["path"])

    def test_apple_is_system(self) -> None:
        a = self.app("~/Library/Caches/com.apple.Safari")
        self.assertEqual(a["kind"], "system")
        self.assertTrue(a["installed"])

    def test_group_containers(self) -> None:
        self.assertEqual(self.app("~/Library/Group Containers/2BUA8C4S2C.com.1password")["name"], "1Password")
        self.assertEqual(self.app("~/Library/Group Containers/group.com.docker")["name"], "Docker Desktop")

    def test_telegram_alias(self) -> None:
        a = self.app("~/Library/Application Support/Telegram Desktop")
        self.assertEqual(a["name"], "Telegram")
        self.assertTrue(a["installed"])


class ResolverUnitTest(unittest.TestCase):
    def test_strip_group(self) -> None:
        self.assertEqual(resolver._strip_group("group.2BUA8C4S2C.com.1password"), ("com.1password", "2BUA8C4S2C"))
        self.assertEqual(resolver._strip_group("243LU875E5.groups.com.apple.podcasts")[0], "com.apple.podcasts")
        self.assertEqual(resolver._strip_group("group.com.docker"), ("com.docker", None))

    def test_bundle_detection(self) -> None:
        self.assertTrue(resolver._looks_like_bundle("com.tinyspeck.slackmacgap.ShipIt"))
        self.assertTrue(resolver._looks_like_bundle("us.zoom.xos"))
        self.assertFalse(resolver._looks_like_bundle("main.kts.compiled.cache"))
        self.assertFalse(resolver._looks_like_bundle("Telegram Desktop"))

    def test_unknown_plain_name(self) -> None:
        self.assertIsNone(resolver.resolve("/nonexistent/Caches/zz-not-a-real-thing", "cache"))



def _n(path: str, children: Optional[List[dict]] = None, **extra) -> dict:
    node = {"path": path, "name": os.path.basename(path), "size": 0, "files": 0, "mtime": 0.0,
            "is_dir": True, "error": None, "children": children or []}
    node.update(extra)
    return node


def _r(label: str, path: str, category: str, children: List[dict]) -> dict:
    return dict(_n(path, children), label=label, category=category, nested_in=None)


class ResolverV2RootsTest(unittest.TestCase):
    """Attribution of depth-1 children of the iteration-2 roots (paths need not exist)."""

    @classmethod
    def setUpClass(cls) -> None:
        resolver.reset()
        h = HOME
        cls.result = {"roots": [
            _r("Home", h, "home", [
                _n(h + "/IdeaProjects"), _n(h + "/DataGripProjects"), _n(h + "/PycharmProjects"),
                _n(h + "/Parallels"), _n(h + "/.pluginVerifier"), _n(h + "/.claude"), _n(h + "/.codex"),
                _n(h + "/.jbcontext"), _n(h + "/.rbenv"), _n(h + "/.bun"), _n(h + "/.duckdb"),
                _n(h + "/.copilot"), _n(h + "/.config"), _n(h + "/.local"), _n(h + "/Documents"),
                _n(h + "/Downloads"), _n(h + "/.Trash"),
                _n(h + "/Library", link_root="Library (other)"), _n(h + "/zz-unknown-folder")]),
            _r("Library (other)", h + "/Library", "library_other", [
                _n(h + "/Library/Developer", [_n(h + "/Library/Developer/CoreSimulator")]),
                _n(h + "/Library/Mail"), _n(h + "/Library/Messages"), _n(h + "/Library/Mobile Documents"),
                _n(h + "/Library/Photos"), _n(h + "/Library/Preferences"),
                _n(h + "/Library/Caches", link_root="Caches")]),
            _r("Applications", "/Applications", "applications", [
                _n("/Applications/Safari.app"),
                _n("/Applications/Utilities", [_n("/Applications/Utilities/Terminal.app")])]),
            _r("System Library", "/Library", "system", [
                _n("/Library/Developer", [_n("/Library/Developer/CommandLineTools")]),
                _n("/Library/Application Support")]),
            _r("/opt", "/opt", "dev_cache", [_n("/opt/homebrew")]),
            _r("Swap & sleepimage", "/private/var/vm", "system", [_n("/private/var/vm/sleepimage")]),
        ]}
        t = time.time()
        resolver.annotate(cls.result)
        cls.elapsed = time.time() - t
        cls.nodes: Dict[str, dict] = {}

        def walk(n: dict) -> None:
            cls.nodes[n["path"]] = n
            for c in n["children"]:
                walk(c)
        for r in cls.result["roots"]:
            for c in r["children"]:
                walk(c)
            cls.nodes["ROOT:" + r["label"]] = r

    def name(self, path: str) -> Optional[str]:
        a = self.nodes[os.path.expanduser(path)].get("app")
        return a["name"] if a else None

    def test_fast(self) -> None:
        self.assertLess(self.elapsed, 5.0)

    def test_container_roots_not_attributed(self) -> None:
        for label in ("Home", "Library (other)", "Applications", "System Library", "/opt"):
            self.assertNotIn("app", self.nodes["ROOT:" + label], label)
        self.assertIsNone(self.name("~/zz-unknown-folder"))

    def test_link_stubs_skipped(self) -> None:
        self.assertNotIn("app", self.nodes[HOME + "/Library"])
        self.assertNotIn("app", self.nodes[HOME + "/Library/Caches"])

    def test_home_children(self) -> None:
        expect = {"~/IdeaProjects": "IntelliJ IDEA projects", "~/DataGripProjects": "DataGrip projects",
                  "~/PycharmProjects": "PyCharm projects", "~/Parallels": "Parallels Desktop",
                  "~/.pluginVerifier": "IntelliJ Plugin Verifier", "~/.claude": "Claude Code",
                  "~/.codex": "Codex", "~/.rbenv": "rbenv", "~/.bun": "Bun", "~/.copilot": "GitHub Copilot",
                  "~/.Trash": "Trash", "~/Documents": "macOS user folder", "~/Downloads": "macOS user folder"}
        for p, name in expect.items():
            self.assertEqual(self.name(p), name, p)
        for p in ("~/.jbcontext", "~/.duckdb", "~/.config", "~/.local"):
            self.assertTrue(self.name(p), p)
        self.assertEqual(self.nodes[HOME + "/Documents"]["app"]["kind"], "system")
        ij = self.nodes[HOME + "/IdeaProjects"]["app"]
        self.assertIn(ij["installed"], (True, False))
        self.assertNotEqual(ij["installed"], False, "any JetBrains IDE counts as installed")

    def test_category_restricted_entries(self) -> None:
        # "Documents" is a macOS user folder only directly in the home root
        self.assertIsNone(resolver.resolve(HOME + "/Library/Caches/Documents", "cache"))
        self.assertEqual(self.name("~/Library/Developer"), "Xcode")
        self.assertEqual(self.name("/Library/Developer"), "Apple developer tools")

    def test_library_other(self) -> None:
        self.assertEqual(self.name("~/Library/Mobile Documents"), "iCloud Drive")
        self.assertEqual(self.name("~/Library/Mail"), "Apple Mail")
        self.assertEqual(self.name("~/Library/Messages"), "Messages")
        self.assertEqual(self.name("~/Library/Photos"), "Photos")
        self.assertEqual(self.nodes[HOME + "/Library/Preferences"]["app"]["kind"], "system")
        self.assertEqual(self.name("~/Library/Developer/CoreSimulator"), "Xcode Simulator")

    def test_system_and_opt(self) -> None:
        self.assertEqual(self.name("/Library/Developer/CommandLineTools"), "Xcode Command Line Tools")
        self.assertEqual(self.name("/opt/homebrew"), "Homebrew")
        self.assertEqual(self.nodes["ROOT:Swap & sleepimage"]["app"]["name"], "macOS swap & sleep image")
        self.assertEqual(self.name("/private/var/vm/sleepimage"), "macOS swap & sleep image")

    def test_applications_are_the_apps(self) -> None:
        if not os.path.isdir("/Applications/Safari.app"):
            self.skipTest("no Safari.app")
        a = self.nodes["/Applications/Safari.app"]["app"]
        self.assertEqual(a["name"], "Safari")
        self.assertEqual(a["app_path"], "/Applications/Safari.app")
        self.assertTrue(a["installed"])
        self.assertEqual(a["kind"], "system")
        if os.path.isdir("/Applications/Utilities/Terminal.app"):
            self.assertEqual(self.name("/Applications/Utilities/Terminal.app"), "Terminal")
        slack = "/Applications/Slack.app"
        if os.path.isdir(slack):
            s = resolver.resolve(slack, "applications")
            self.assertEqual((s["name"], s["installed"], s["app_path"]), ("Slack", True, slack))
            self.assertIsInstance(s["running"], bool)


if __name__ == "__main__":
    unittest.main()
