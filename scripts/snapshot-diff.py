"""
snapshot-diff.py — rehydrate Phase 4.1

Compare two snapshots on the same backup drive and emit a per-category diff
showing which files were added, modified, removed, or unchanged.

Usage:
    python3 scripts/snapshot-diff.py \\
        --drive /Volumes/PortableSSD/llm-backup \\
        --snapshot snap-2 \\
        [--against snap-1] \\
        [--out diff.json]

If ``--against`` is omitted the script reads the child manifest's
``parent_snapshot`` field and uses that as the parent.  If the child
has no parent (``parent_snapshot`` is null) and ``--against`` is also
not provided the script exits non-zero with an error.

Output JSON shape::

    {
      "child":              "<snapshot-id>",
      "parent":             "<snapshot-id>",
      "created_at_child":   "<iso-timestamp>",
      "created_at_parent":  "<iso-timestamp>",
      "categories": {
        "<name>": {
          "added":    [{"path": "...", "object_hash": "..."}],
          "modified": [{"path": "...", "old_hash": "...", "new_hash": "..."}],
          "removed":  [{"path": "...", "old_hash": "..."}]
        }
      },
      "summary": {"added": N, "modified": M, "removed": K, "unchanged": U}
    }

Unchanged files are counted in ``summary.unchanged`` but are not listed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Make ``scripts`` package importable regardless of working directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.no_pii_log import (  # noqa: E402
    log_error,
    log_info,
)

try:
    import jsonschema
except ImportError:
    print(
        "jsonschema is required: pip3 install jsonschema",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_SCHEMA_PATH = _REPO_ROOT / "schemas" / "manifest.schema.json"


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _load_manifest_schema() -> dict[str, Any]:
    if not _MANIFEST_SCHEMA_PATH.exists():
        log_error("manifest schema not found", path=_MANIFEST_SCHEMA_PATH)
        sys.exit(1)
    with _MANIFEST_SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_and_validate_manifest(
    drive: str,
    snapshot_id: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Load the manifest for *snapshot_id* from *drive* and validate it.

    Exits non-zero if the manifest file is missing, unreadable, invalid JSON,
    or fails schema validation.
    """
    manifest_path = Path(drive) / "snapshots" / snapshot_id / "manifest.json"
    if not manifest_path.is_file():
        log_error(
            f"manifest not found for snapshot {snapshot_id!r}",
            path=manifest_path,
        )
        sys.exit(1)

    try:
        with manifest_path.open(encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log_error(
            f"failed to load manifest for snapshot {snapshot_id!r}: {exc}",
            path=manifest_path,
        )
        sys.exit(1)

    try:
        jsonschema.validate(instance=manifest, schema=schema)
    except jsonschema.ValidationError as exc:
        log_error(
            f"manifest for snapshot {snapshot_id!r} failed schema validation: {exc.message}"
        )
        sys.exit(1)
    except jsonschema.SchemaError as exc:
        log_error(f"manifest schema itself is invalid: {exc.message}")
        sys.exit(1)

    return manifest


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------


def _path_hash_map(category_payload: dict[str, Any]) -> dict[str, str]:
    """Return a ``{path: object_hash}`` map for a category payload.

    Works for any category that uses the ``file-list`` strategy (files array).
    For other strategies (no ``files`` key) returns an empty map.
    """
    result: dict[str, str] = {}
    for entry in category_payload.get("files", []):
        result[entry["path"]] = entry["object_hash"]
    return result


def compute_diff(
    child_manifest: dict[str, Any],
    parent_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Return the diff structure comparing *child_manifest* against *parent_manifest*."""
    child_categories: dict[str, Any] = child_manifest.get("categories", {})
    parent_categories: dict[str, Any] = parent_manifest.get("categories", {})

    all_category_names = set(child_categories) | set(parent_categories)

    categories_out: dict[str, Any] = {}
    total_added = 0
    total_modified = 0
    total_removed = 0
    total_unchanged = 0

    for cat_name in sorted(all_category_names):
        child_cat = child_categories.get(cat_name, {})
        parent_cat = parent_categories.get(cat_name, {})

        child_map = _path_hash_map(child_cat)
        parent_map = _path_hash_map(parent_cat)

        added: list[dict[str, str]] = []
        modified: list[dict[str, str]] = []
        removed: list[dict[str, str]] = []
        unchanged_count = 0

        all_paths = set(child_map) | set(parent_map)
        for path in sorted(all_paths):
            in_child = path in child_map
            in_parent = path in parent_map

            if in_child and not in_parent:
                added.append({"path": path, "object_hash": child_map[path]})
            elif in_parent and not in_child:
                removed.append({"path": path, "old_hash": parent_map[path]})
            else:
                # Present in both
                if child_map[path] == parent_map[path]:
                    unchanged_count += 1
                else:
                    modified.append({
                        "path": path,
                        "old_hash": parent_map[path],
                        "new_hash": child_map[path],
                    })

        total_added += len(added)
        total_modified += len(modified)
        total_removed += len(removed)
        total_unchanged += unchanged_count

        categories_out[cat_name] = {
            "added": added,
            "modified": modified,
            "removed": removed,
        }

    return {
        "child": child_manifest["snapshot_id"],
        "parent": parent_manifest["snapshot_id"],
        "created_at_child": child_manifest["created_at"],
        "created_at_parent": parent_manifest["created_at"],
        "categories": categories_out,
        "summary": {
            "added": total_added,
            "modified": total_modified,
            "removed": total_removed,
            "unchanged": total_unchanged,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_diff(
    drive: str,
    snapshot_id: str,
    against_id: str | None,
    out_path: str | None,
) -> None:
    """Load manifests, compute diff, emit JSON."""
    schema = _load_manifest_schema()

    child_manifest = _load_and_validate_manifest(drive, snapshot_id, schema)

    # Resolve parent id
    if against_id is not None:
        parent_id: str = against_id
    else:
        parent_id = child_manifest.get("parent_snapshot")  # type: ignore[assignment]
        if not parent_id:
            log_error(
                f"snapshot {snapshot_id!r} has no parent_snapshot and --against was not provided. "
                "Use --against <parent-id> to specify the comparison target."
            )
            sys.exit(1)

    parent_manifest = _load_and_validate_manifest(drive, parent_id, schema)

    diff = compute_diff(child_manifest, parent_manifest)

    diff_json = json.dumps(diff, indent=2, ensure_ascii=False) + "\n"

    if out_path is not None:
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(diff_json)
            log_info(f"diff written to {out_path}")
        except OSError as exc:
            log_error(f"failed to write diff output: {exc}", path=out_path)
            sys.exit(1)
    else:
        sys.stdout.write(diff_json)

    summary = diff["summary"]
    log_info(
        f"diff complete: +{summary['added']} added, "
        f"~{summary['modified']} modified, "
        f"-{summary['removed']} removed, "
        f"={summary['unchanged']} unchanged"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare two snapshots on the same backup drive and emit a "
            "per-category JSON diff."
        ),
    )
    parser.add_argument(
        "--drive",
        required=True,
        metavar="PATH",
        help="Backup drive root directory containing snapshots/ and objects/",
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        metavar="ID",
        help="The 'child' snapshot to diff (the newer one)",
    )
    parser.add_argument(
        "--against",
        default=None,
        metavar="ID",
        help=(
            "The 'parent' snapshot to diff against. "
            "Defaults to the child's own parent_snapshot field. "
            "Required if the child has no parent_snapshot."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write JSON diff to this file instead of stdout",
    )

    args = parser.parse_args(argv)

    run_diff(
        drive=args.drive,
        snapshot_id=args.snapshot,
        against_id=args.against,
        out_path=args.out,
    )


if __name__ == "__main__":
    main()
