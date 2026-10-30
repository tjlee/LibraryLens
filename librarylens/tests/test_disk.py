"""Tests for disk.py: parsers on real (trimmed) command output + a live smoke call."""
from __future__ import annotations

import json
import os
import plistlib
import time
import unittest
from unittest import mock

from librarylens import disk


def _vol(name, dev, used, roles):
    return {"CapacityInUse": used, "DeviceIdentifier": dev, "Name": name, "Roles": roles}


# Trimmed `diskutil apfs list -plist` from the dev machine (Apple silicon, macOS 14): the internal
# Recovery container (disk1) and the main container (disk3). Sizes are the real ones.
APFS_LIST = {"Containers": [
    {"APFSContainerUUID": "027E8F37-4CD0-4342-9E00-5CFA918999F9", "CapacityCeiling": 5368664064,
     "CapacityFree": 3102920704, "ContainerReference": "disk1", "DesignatedPhysicalStore": "disk0s3",
     "Fusion": False,
     "PhysicalStores": [{"DeviceIdentifier": "disk0s3", "Size": 5368664064}],
     "Volumes": [_vol("Recovery", "disk1s1", 2245967872, ["Recovery"]),
                 _vol("Update", "disk1s2", 32768, ["Update"])]},
    {"APFSContainerUUID": "1BD82FA1-D405-4754-BC8B-349BD4C22A89", "CapacityCeiling": 994662584320,
     "CapacityFree": 89355653120, "ContainerReference": "disk3", "DesignatedPhysicalStore": "disk0s2",
     "Fusion": False,
     "PhysicalStores": [{"DeviceIdentifier": "disk0s2", "Size": 994662584320}],
     "Volumes": [_vol("Macintosh HD - Data", "disk3s1", 873660022784, ["Data"]),
                 _vol("Update", "disk3s2", 953499648, ["Update"]),
                 _vol("Macintosh HD", "disk3s3", 13252554752, ["System"]),
                 _vol("Preboot", "disk3s4", 11528892416, ["Preboot"]),
                 _vol("Recovery", "disk3s5", 1873993728, ["Recovery"]),
                 _vol("VM", "disk3s6", 24576, ["VM"]),
                 _vol("Nix Store", "disk3s7", 3831246848, [])]},
]}
APFS_PLIST = plistlib.dumps(APFS_LIST)

TMUTIL_OUT = """Snapshots for volume group containing disk /:
com.apple.os.update-26E6372B5387D07F8D15BD012ED3A2ED6E03A612DC29D30F027BA027D9554B5B0875C287DFA14E550CCA05A212D74355
com.apple.os.update-C6C236054E23451B9FDAC2E9FA0969DAD53C1365B023068D492024B3BDE60770
com.apple.os.update-MSUPrepareUpdate
com.apple.TimeMachine.2026-10-30-101500.local
some.other.snapshot
"""


class ParseApfsTest(unittest.TestCase):
    def test_main_container(self) -> None:
        o = disk.parse_apfs_list(APFS_PLIST, "disk3s3s1")
        self.assertEqual(o["container"]["size"], 994662584320)
        self.assertEqual(o["container"]["free"], 89355653120)
        self.assertEqual(o["container"]["used"], 994662584320 - 89355653120)
        self.assertEqual(o["data_volume"], {"name": "Macintosh HD - Data", "used": 873660022784})
        names = [v["name"] for v in o["volumes"]]
        self.assertEqual(names[0], "Macintosh HD - Data")  # sorted by size desc
        self.assertEqual(len(names), 7)
        roles = {v["name"]: v["role"] for v in o["volumes"]}
        self.assertEqual(roles["Macintosh HD"], "System")
        self.assertIsNone(roles["Nix Store"])
        for v in o["volumes"]:
            self.assertEqual(set(v), {"name", "role", "used", "device"})
            self.assertIsInstance(v["used"], int)

    def test_container_choice_without_root_device(self) -> None:
        # falls back to the container that has a Data volume, not the first one
        self.assertEqual(disk.parse_apfs_list(APFS_PLIST, None)["container"]["device"], "disk3")
        self.assertEqual(disk.parse_apfs_list(APFS_PLIST, "disk1s1")["container"]["device"], "disk1")

    def test_garbage(self) -> None:
        self.assertEqual(disk.parse_apfs_list(b"not a plist"), {})
        self.assertEqual(disk.parse_apfs_list(plistlib.dumps({"Containers": []})), {})
        o = disk.parse_apfs_list(plistlib.dumps({"Containers": [{"ContainerReference": "disk9",
                                                                  "Volumes": [{"Name": "X"}]}]}))
        self.assertNotIn("container", o)
        self.assertNotIn("data_volume", o)
        self.assertEqual(o["volumes"][0]["used"], 0)


class SnapshotsTest(unittest.TestCase):
    def test_parse(self) -> None:
        snaps = disk.parse_snapshots(TMUTIL_OUT)
        self.assertEqual([s["kind"] for s in snaps],
                         ["os_update", "os_update", "os_update", "time_machine", "other"])
        self.assertTrue(snaps[2]["name"].endswith("MSUPrepareUpdate"))
        self.assertEqual(disk.parse_snapshots(""), [])


class OverviewMockedTest(unittest.TestCase):
    def test_overview_with_fake_commands(self) -> None:
        def fake_run(cmd, timeout=disk.TIMEOUT):
            self.assertLessEqual(timeout, 10)
            self.assertNotIn("sudo", cmd)
            exe = os.path.basename(cmd[0])
            if exe == "diskutil":
                return APFS_PLIST
            if exe == "df":
                return b"Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/disk3s3s1 1 1 1 1% /\n"
            if exe == "tmutil":
                return TMUTIL_OUT.encode()
            if exe == "osascript":
                return b"89797461560\n"
            return None

        with mock.patch.object(disk, "_run", side_effect=fake_run), \
                mock.patch.object(disk, "_free_bytes", return_value=89355632640):
            o = disk.overview()
        self.assertEqual(o["container"]["device"], "disk3")
        self.assertEqual(o["purgeable"], 89797461560 - 89355632640)
        self.assertEqual(len(o["snapshots"]), 5)
        json.dumps(o)

    def test_overview_when_everything_fails(self) -> None:
        with mock.patch.object(disk, "_run", return_value=None), \
                mock.patch.object(disk, "users", side_effect=OSError("boom")):
            o = disk.overview()
        self.assertIsNone(o["purgeable"])
        self.assertNotIn("container", o)
        self.assertIn("collected_at", o)

    def test_users_skip_shared(self) -> None:
        names = [u["name"] for u in disk.users()]
        self.assertNotIn("Shared", names)
        me = [u for u in disk.users() if u["current"]]
        if me:
            self.assertTrue(me[0]["readable"])


class OverviewLiveTest(unittest.TestCase):
    def test_live_types(self) -> None:
        t = time.time()
        o = disk.overview()
        self.assertLess(time.time() - t, 30)
        json.dumps(o)
        self.assertIsInstance(o["collected_at"], float)
        if "container" in o:
            c = o["container"]
            self.assertIsInstance(c["size"], int)
            self.assertIsInstance(c.get("free", 0), int)
            self.assertIsInstance(c.get("used", 0), int)
        if "data_volume" in o:
            self.assertIsInstance(o["data_volume"]["name"], str)
            self.assertIsInstance(o["data_volume"]["used"], int)
        for v in o.get("volumes") or []:
            self.assertIsInstance(v["name"], str)
            self.assertIsInstance(v["used"], int)
            self.assertTrue(v["role"] is None or isinstance(v["role"], str))
        for s in o.get("snapshots") or []:
            self.assertIn(s["kind"], ("os_update", "time_machine", "other"))
        self.assertTrue(o["purgeable"] is None or isinstance(o["purgeable"], int))
        for u in o.get("users") or []:
            self.assertIsInstance(u["name"], str)
            self.assertIsInstance(u["readable"], bool)
            self.assertTrue(u["home"].startswith("/"))


if __name__ == "__main__":
    unittest.main()
