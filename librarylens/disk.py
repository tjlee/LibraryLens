"""Whole-disk overview: APFS container/volumes, local snapshots, purgeable space, user homes.

Public API (see CONTRACT.md, "Disk overview"):
    overview() -> dict

Never needs admin, never raises, every subprocess has a timeout (<= 10 s). Fields that cannot be
obtained are simply missing (or None for "purgeable"). Imports nothing from the package.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import time
from typing import Any, Dict, List, Optional

TIMEOUT = 10.0
USERS_DIR = "/Users"
SKIP_USERS = frozenset({"Shared", "Guest", ".localized"})

# JXA snippet: the "important usage" free space includes purgeable space macOS would free on demand.
_JXA_IMPORTANT = (
    'ObjC.import("Foundation");'
    'var u = $.NSURL.fileURLWithPath("/");'
    'var r = u.resourceValuesForKeysError(["NSURLVolumeAvailableCapacityForImportantUsageKey"], null);'
    'var v = r ? r.objectForKey("NSURLVolumeAvailableCapacityForImportantUsageKey") : null;'
    'v ? String(v.longLongValue) : ""'
)


def _run(cmd: List[str], timeout: float = TIMEOUT) -> Optional[bytes]:
    """stdout of a command, or None on any failure / non-zero exit / timeout."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=min(timeout, TIMEOUT))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return p.stdout if p.returncode == 0 else None


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- APFS

def _root_device() -> Optional[str]:
    """BSD device of the volume mounted at "/" (e.g. "disk3s3s1"), via `df`."""
    out = _run(["/bin/df", "-P", "/"], timeout=5)
    if not out:
        return None
    lines = out.decode("utf-8", "replace").splitlines()
    if len(lines) < 2:
        return None
    dev = lines[1].split()[0]
    return dev[len("/dev/"):] if dev.startswith("/dev/") else dev


def _pick_container(containers: List[dict], root_dev: Optional[str]) -> Optional[dict]:
    """The container holding "/": matches the root device, else the one with a Data volume,
    else the largest."""
    cs = [c for c in containers if isinstance(c, dict)]
    if not cs:
        return None
    if root_dev:
        for c in cs:
            ref = c.get("ContainerReference") or ""
            if ref and (root_dev == ref or root_dev.startswith(ref + "s")):
                return c
    for c in cs:
        if any("Data" in (v.get("Roles") or []) for v in c.get("Volumes") or [] if isinstance(v, dict)):
            return c
    return max(cs, key=lambda c: _int(c.get("CapacityCeiling")) or 0)


def parse_apfs_list(data: bytes, root_dev: Optional[str] = None) -> Dict[str, Any]:
    """Parse `diskutil apfs list -plist` output into {"container", "volumes", "data_volume"}.

    Missing/garbled input gives {}."""
    try:
        pl = plistlib.loads(data)
    except Exception:
        return {}
    if not isinstance(pl, dict):
        return {}
    c = _pick_container(pl.get("Containers") or [], root_dev)
    if c is None:
        return {}
    out: Dict[str, Any] = {}
    size = _int(c.get("CapacityCeiling"))
    free = _int(c.get("CapacityFree"))
    if size is not None:
        container: Dict[str, Any] = {"size": size, "device": c.get("ContainerReference")}
        if free is not None:
            container["free"] = free
            container["used"] = max(0, size - free)
        out["container"] = container
    vols: List[Dict[str, Any]] = []
    for v in c.get("Volumes") or []:
        if not isinstance(v, dict):
            continue
        roles = [r for r in v.get("Roles") or [] if isinstance(r, str)]
        vols.append({"name": v.get("Name") or v.get("DeviceIdentifier") or "?",
                     "role": roles[0] if roles else None,
                     "used": _int(v.get("CapacityInUse")) or 0,
                     "device": v.get("DeviceIdentifier")})
    vols.sort(key=lambda v: v["used"], reverse=True)
    out["volumes"] = vols
    data_vol = next((v for v in vols if v["role"] == "Data"), None)
    if data_vol is not None:
        out["data_volume"] = {"name": data_vol["name"], "used": data_vol["used"]}
    return out


# --------------------------------------------------------------------------- snapshots

def classify_snapshot(name: str) -> str:
    if name.startswith("com.apple.os.update-"):
        return "os_update"
    if name.startswith("com.apple.TimeMachine."):
        return "time_machine"
    return "other"


def parse_snapshots(text: str) -> List[Dict[str, str]]:
    """Parse `tmutil listlocalsnapshots /` output (header line + one snapshot name per line)."""
    snaps = []
    for line in text.splitlines():
        name = line.strip()
        if not name or name.endswith(":") or " " in name:
            continue  # "Snapshots for volume group containing disk /:" header
        snaps.append({"name": name, "kind": classify_snapshot(name)})
    return snaps


def _snapshots() -> Optional[List[Dict[str, str]]]:
    out = _run(["/usr/bin/tmutil", "listlocalsnapshots", "/"])
    if out is None:
        return None
    return parse_snapshots(out.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- purgeable

def _free_bytes(path: str = "/") -> Optional[int]:
    try:
        s = os.statvfs(path)
    except OSError:
        return None
    return int(s.f_bavail) * int(s.f_frsize)


def _important_free() -> Optional[int]:
    out = _run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _JXA_IMPORTANT], timeout=8)
    if not out:
        return None
    return _int(out.decode("utf-8", "replace").strip())


def _purgeable() -> Optional[int]:
    important = _important_free()
    free = _free_bytes("/")
    if important is None or free is None:
        return None
    return max(0, important - free)


# --------------------------------------------------------------------------- users

def users(users_dir: str = USERS_DIR) -> List[Dict[str, Any]]:
    """Home folders under /Users (except Shared), with whether we can read them."""
    me = os.path.realpath(os.path.expanduser("~"))
    out = []
    try:
        names = sorted(os.listdir(users_dir))
    except OSError:
        return out
    for name in names:
        home = os.path.join(users_dir, name)
        if name.startswith(".") or name in SKIP_USERS or not os.path.isdir(home) or os.path.islink(home):
            continue
        current = os.path.realpath(home) == me
        out.append({"name": name, "home": home, "readable": current or _home_readable(home),
                    "current": current})
    return out


def _home_readable(home: str, limit: int = 64) -> bool:
    """True if the home and its top-level folders can be read, i.e. its size can be measured.

    Other users' homes are usually listable (drwxr-x---) but every folder inside is private."""
    if not os.access(home, os.R_OK | os.X_OK):
        return False
    try:
        names = os.listdir(home)
    except OSError:
        return False
    for name in names[:limit]:
        p = os.path.join(home, name)
        try:
            if os.path.isdir(p) and not os.path.islink(p) and not os.access(p, os.R_OK | os.X_OK):
                return False
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------- entry point

def overview() -> dict:
    """Snapshot of the disk: see module docstring. Never raises."""
    result: Dict[str, Any] = {"collected_at": time.time()}
    try:
        data = _run(["/usr/sbin/diskutil", "apfs", "list", "-plist"])
        if data:
            result.update(parse_apfs_list(data, _root_device()))
    except Exception:  # pragma: no cover - defensive, the parser already swallows errors
        pass
    try:
        snaps = _snapshots()
        if snaps is not None:
            result["snapshots"] = snaps
    except Exception:  # pragma: no cover
        pass
    try:
        result["purgeable"] = _purgeable()
    except Exception:  # pragma: no cover
        result["purgeable"] = None
    try:
        result["users"] = users()
    except Exception:  # pragma: no cover
        pass
    return result


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(overview(), indent=2))
