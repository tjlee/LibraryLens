"""Full scan pipeline shared by the CLI and the server."""

from __future__ import annotations

import threading
from typing import Callable, Optional


def run(prev: Optional[dict] = None, only: Optional[list] = None, max_depth: int = 3,
        progress: Optional[Callable[[str], None]] = None,
        cancel: Optional[threading.Event] = None) -> dict:
    """Scan, describe the disk, attribute apps, detect special folders, compute insights, save.

    `only` rescans just those root labels and keeps the rest from `prev`.
    Raises scanner.Cancelled if `cancel` is set during the scan.
    """
    from . import detectors, disk, insights, resolver, scanner

    def phase(name: str) -> None:
        if progress:
            progress("phase:" + name)

    phase("scan")
    result = scanner.scan_all(max_depth=max_depth, progress=progress, only=only, prev=prev,
                              cancel=cancel)
    phase("disk")
    try:
        result["disk"] = disk.overview()
    except Exception as e:  # the overview is optional; never lose a scan over it
        result["disk"] = {"error": str(e)}
    phase("apps")
    resolver.annotate(result)
    phase("detect")
    detectors.annotate(result)
    phase("insights")
    insights.annotate(result)
    phase("save")
    scanner.save_cache(result)
    return result


def reannotate(result: dict) -> dict:
    """Recompute everything after the scan (e.g. after a trash/action), without rescanning."""
    from . import detectors, insights, resolver, scanner

    resolver.annotate(result)
    detectors.annotate(result)
    insights.annotate(result)
    scanner.save_cache(result)
    return result
