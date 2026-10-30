"""CLI entry point: `python3 -m librarylens serve` or `python3 -m librarylens scan`."""

from __future__ import annotations

import argparse
import sys
import time


def human(n: int) -> str:
    """Decimal units, like Finder."""
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def full_scan(max_depth: int, quiet: bool) -> dict:
    """Run the whole pipeline (scan, disk overview, apps, detectors, insights) and save the cache."""
    from . import pipeline

    last = [0.0]
    phase = ["scan"]

    def progress(msg: str) -> None:
        if msg.startswith("phase:"):
            phase[0] = msg[6:]
            msg = ""
        now = time.time()
        if quiet or now - last[0] < 0.5:
            return
        last[0] = now
        sys.stderr.write(f"\r\033[K  [{phase[0]}] " + msg[-100:])
        sys.stderr.flush()

    result = pipeline.run(max_depth=max_depth, progress=progress)
    if not quiet:
        sys.stderr.write("\r\033[K")
    return result


def print_report(result: dict, top: int) -> None:
    s = result.get("summary", {})
    print(f"Scanned in {result.get('duration_s', 0):.0f}s   total {human(s.get('total', 0))}"
          f"   likely reclaimable {human(s.get('reclaimable', 0))}"
          f"   unaccounted {human(s.get('unaccounted', 0))}\n")

    segments = s.get("disk_segments") or []
    if segments:
        print("Disk:")
        for seg in segments:
            print(f"  {human(seg['size']):>10}  {seg['label']}")
        print()

    print("Roots:")
    for r in sorted(result["roots"], key=lambda r: -r["size"]):
        nested = f"  (inside {r['nested_in']})" if r.get("nested_in") else ""
        print(f"  {human(r['size']):>10}  {r['label']:<28} {r['path']}{nested}")

    print(f"\nTop {top} cleanup candidates:")
    for c in s.get("top_candidates", [])[:top]:
        app = c.get("app") or "?"
        print(f"  {human(c['size']):>10}  [{c['safety']:<7}] {app:<24} {c['path']}")
        print(f"  {'':>10}  {c['reason']}")

    repos = result.get("git_repos") or []
    if repos:
        print("\nGit repositories:")
        for g in repos[:10]:
            print(f"  {human(g.get('size', 0)):>10}  {g.get('path')}   packs {g.get('packs', '?')}, "
                  f"garbage {human(g.get('garbage_size', 0))}, "
                  f"prunable worktrees {len(g.get('prunable_worktrees') or [])}")

    big = result.get("big_files") or []
    if big:
        print("\nBiggest files:")
        for f in big[:10]:
            print(f"  {human(f['size']):>10}  {f['path']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="librarylens", description=__doc__)
    sub = p.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="start the browser UI (default)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sc = sub.add_parser("scan", help="scan and print a report in the terminal")
    sc.add_argument("--top", type=int, default=30)
    sc.add_argument("--depth", type=int, default=3)
    sc.add_argument("--cached", action="store_true", help="use the last scan instead of rescanning")
    sc.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "scan":
        if args.cached:
            from . import scanner
            result = scanner.load_cache()
            if result is None:
                print("No cached scan yet; run without --cached.", file=sys.stderr)
                return 1
        else:
            result = full_scan(args.depth, args.quiet)
        print_report(result, args.top)
        return 0

    from . import server
    server.serve(port=getattr(args, "port", 8765), open_browser=not getattr(args, "no_browser", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
