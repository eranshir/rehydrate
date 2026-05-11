"""
walk-apps.py — rehydrate Phase 3.4

Captures installed .app bundle metadata from /Applications/ and ~/Applications/.
Each bundle's Contents/Info.plist is read to extract name, bundle ID, version,
and build number. A source hint (appstore, cask, manual) is recorded.
The inventory is written as a single JSON file to:
  <workdir>/.rehydrate/apps/inventory.json

Usage:
    python3 scripts/walk-apps.py [--categories PATH] [--out PATH]
                                  [--home PATH] [--workdir PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Allow running as a script directly OR as scripts.walk_apps module
# ---------------------------------------------------------------------------
try:
    from scripts.no_pii_log import log_count, log_info
except ImportError:
    _here = Path(__file__).parent
    sys.path.insert(0, str(_here.parent))
    from scripts.no_pii_log import log_count, log_info

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_CATEGORIES = _REPO_ROOT / "categories.yaml"

CATEGORY_NAME = "app-inventory"
APPS_SUBDIR = ".rehydrate/apps"
INVENTORY_FILENAME = "inventory.json"
SCHEMA_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_file_entry(rel_path: str, size: int) -> dict[str, Any]:
    """Build a file_entry dict in the same shape walk.py emits."""
    return {
        "path": rel_path,
        "size": size,
        "mtime": _now_iso(),
        "mode": "0644",
        "is_symlink": False,
        "symlink_target": None,
    }


def _read_plist(plist_path: Path) -> dict[str, Any] | None:
    """Read and parse an Info.plist file. Returns None if unreadable."""
    try:
        with open(plist_path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:
        return None


def _determine_source(app_path: Path, bundle_id: str | None) -> str:
    """
    Determine the installation source heuristic for an .app bundle.

    - appstore  if Contents/_MASReceipt/receipt exists as a file
    - cask      if bundle_id is non-None (resolve to cask name at restore time)
    - manual    otherwise
    """
    receipt = app_path / "Contents" / "_MASReceipt" / "receipt"
    if receipt.is_file():
        return "appstore"
    if bundle_id is not None:
        return "cask"
    return "manual"


def _scan_apps(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Scan a single root directory (non-recursive) for .app bundles.

    Returns (apps_list, skipped_list).
    """
    apps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if not root.is_dir():
        return apps, skipped

    for entry in root.iterdir():
        if not entry.name.endswith(".app"):
            continue
        if not entry.is_dir():
            continue

        plist_path = entry / "Contents" / "Info.plist"
        plist_data = _read_plist(plist_path)

        if plist_data is None:
            skipped.append({
                "path": str(entry),
                "reason": "info-plist-unreadable",
            })
            continue

        # Fallback name: strip .app suffix from directory name
        fallback_name = entry.name[:-4]

        name = plist_data.get("CFBundleName") or fallback_name
        bundle_id = plist_data.get("CFBundleIdentifier") or None
        version = plist_data.get("CFBundleShortVersionString") or None
        build = plist_data.get("CFBundleVersion") or None

        source = _determine_source(entry, bundle_id)

        apps.append({
            "name": name,
            "bundle_id": bundle_id,
            "version": version,
            "build": build,
            "path": str(entry),
            "source": source,
        })

    return apps, skipped


# ---------------------------------------------------------------------------
# Core walk
# ---------------------------------------------------------------------------

def walk_apps(
    home: str,
    workdir: str,
    *,
    _app_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """
    Scan /Applications/ and <home>/Applications/ for .app bundles,
    write an inventory JSON to <workdir>/.rehydrate/apps/inventory.json,
    and return the walk output dict.

    *_app_roots* is an internal override used by tests to substitute a
    controlled fixture directory for the real /Applications root.
    """
    apps_dir = Path(workdir) / APPS_SUBDIR
    apps_dir.mkdir(parents=True, exist_ok=True)

    all_apps: list[dict[str, Any]] = []
    all_skipped: list[dict[str, Any]] = []

    if _app_roots is not None:
        roots = _app_roots
    else:
        roots = [
            Path("/Applications"),
            Path(home) / "Applications",
        ]

    for root in roots:
        apps, skipped = _scan_apps(root)
        all_apps.extend(apps)
        all_skipped.extend(skipped)

    # Sort: apps with bundle_id first (alphabetically by bundle_id),
    # then apps without bundle_id (alphabetically by name)
    def _sort_key(app: dict[str, Any]) -> tuple[int, str]:
        bid = app.get("bundle_id")
        if bid is not None:
            return (0, bid.lower())
        return (1, (app.get("name") or "").lower())

    all_apps.sort(key=_sort_key)

    inventory: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": _now_iso(),
        "apps": all_apps,
    }

    inventory_bytes = json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    inventory_path = apps_dir / INVENTORY_FILENAME
    inventory_path.write_bytes(inventory_bytes)

    size = len(inventory_bytes)
    rel_path = f"{APPS_SUBDIR}/{INVENTORY_FILENAME}"

    log_count("apps_found", len(all_apps))
    log_count("apps_skipped", len(all_skipped))
    log_info(f"walk-apps complete: files=1 skipped={len(all_skipped)}")

    return {
        "category": CATEGORY_NAME,
        "workdir": workdir,
        "files": [_make_file_entry(rel_path, size)],
        "coverage": {
            "globs": {},
            "skipped": all_skipped,
            "large_files_warned": [],
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Walk /Applications and ~/Applications for .app bundles and emit "
            "a JSON inventory + coverage report compatible with snapshot.py's "
            "--walk-output interface."
        ),
    )
    parser.add_argument(
        "--categories",
        default=str(_DEFAULT_CATEGORIES),
        help="Path to categories.yaml (default: categories.yaml at repo root)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for JSON (default: stdout)",
    )
    parser.add_argument(
        "--home",
        default=os.path.expanduser("~"),
        help="Home directory (used to resolve ~/Applications; default: $HOME)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Directory where virtual app files are written (default: fresh tempdir)",
    )
    args = parser.parse_args(argv)

    if args.workdir is None:
        workdir = tempfile.mkdtemp(prefix="rehydrate-apps-")
        log_info("created workdir")
    else:
        workdir = args.workdir
        Path(workdir).mkdir(parents=True, exist_ok=True)

    result = walk_apps(home=args.home, workdir=workdir)

    output = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        log_info("output written")
    else:
        print(output)


if __name__ == "__main__":
    main()
