"""
snapshot-gc.py — rehydrate Phase 4.2

Garbage-collect old snapshots and unreferenced objects from a backup drive.
Mirrors git's loose-object GC model: enumerate all snapshots, compute the
union of every object_hash referenced by retained snapshots, then delete
objects NOT in that set and delete the pruned snapshot directories.

Usage:
    python3 scripts/snapshot-gc.py \\
        --drive /Volumes/PortableSSD/llm-backup \\
        --keep-last 5 \\
        [--dry-run] \\
        [--out gc-report.json] \\
        [--allow-empty] \\
        [--allow-orphans]

Exactly ONE retention rule must be specified:
    --keep-last N         Keep the N most recent snapshots (by created_at).
    --keep-after DATE     Keep snapshots whose created_at >= DATE (ISO 8601).
    --keep-ids ID1,ID2    Keep only the listed snapshot IDs (comma-separated).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Make ``scripts`` package importable regardless of working directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.no_pii_log import (  # noqa: E402
    log_count,
    log_error,
    log_info,
    log_warn,
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
    manifest_path: Path,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Load and validate a manifest JSON file.

    Returns the parsed manifest dict.
    Raises ValueError with a descriptive message on any failure (caller handles exit).
    """
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read/parse manifest: {exc}") from exc

    try:
        jsonschema.validate(instance=manifest, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"manifest failed schema validation: {exc.message}") from exc
    except jsonschema.SchemaError as exc:
        raise ValueError(f"manifest schema itself is invalid: {exc.message}") from exc

    return manifest


# ---------------------------------------------------------------------------
# Retention helpers
# ---------------------------------------------------------------------------


def _parse_keep_after(date_str: str) -> datetime:
    """Parse an ISO 8601 date or datetime string into a timezone-aware datetime.

    Accepts:
      - YYYY-MM-DD
      - YYYY-MM-DDTHH:MM:SS
      - YYYY-MM-DDTHH:MM:SSZ
      - YYYY-MM-DDTHH:MM:SS+HH:MM  etc.
    Returns a UTC-aware datetime for comparison.
    """
    # Try a few common formats, falling back to fromisoformat.
    # Strip trailing Z and treat as UTC for compatibility with Python < 3.11.
    s = date_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Try bare date
        try:
            dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"cannot parse --keep-after value as ISO 8601: {date_str!r}"
            )
    if dt.tzinfo is None:
        # Treat naive datetimes as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_manifest_created_at(created_at_str: str) -> datetime:
    """Parse the manifest's created_at string into a UTC-aware datetime."""
    s = created_at_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"cannot parse manifest created_at: {created_at_str!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _apply_retention_rule(
    snapshots: list[dict[str, Any]],
    keep_last: int | None,
    keep_after: str | None,
    keep_ids_set: set[str] | None,
) -> tuple[list[str], list[str]]:
    """Compute KEEP and PRUNE snapshot id lists.

    *snapshots* is a list of loaded manifest dicts (already validated).
    Returns (keep_ids_sorted, prune_ids_sorted).
    """
    all_ids = [m["snapshot_id"] for m in snapshots]

    if keep_ids_set is not None:
        keep = [sid for sid in all_ids if sid in keep_ids_set]
        prune = [sid for sid in all_ids if sid not in keep_ids_set]
        return sorted(keep), sorted(prune)

    if keep_after is not None:
        threshold = _parse_keep_after(keep_after)
        keep = []
        prune = []
        for m in snapshots:
            try:
                created = _parse_manifest_created_at(m["created_at"])
            except ValueError as exc:
                log_warn(f"skipping unparseble created_at for {m['snapshot_id']!r}: {exc}")
                prune.append(m["snapshot_id"])
                continue
            if created >= threshold:
                keep.append(m["snapshot_id"])
            else:
                prune.append(m["snapshot_id"])
        return sorted(keep), sorted(prune)

    if keep_last is not None:
        # Sort by created_at descending; keep the first keep_last.
        def sort_key(m: dict[str, Any]) -> datetime:
            try:
                return _parse_manifest_created_at(m["created_at"])
            except ValueError:
                return datetime.min.replace(tzinfo=timezone.utc)

        sorted_newest_first = sorted(snapshots, key=sort_key, reverse=True)
        keep = [m["snapshot_id"] for m in sorted_newest_first[:keep_last]]
        prune = [m["snapshot_id"] for m in sorted_newest_first[keep_last:]]
        return sorted(keep), sorted(prune)

    raise RuntimeError("no retention rule provided (should have been caught earlier)")


# ---------------------------------------------------------------------------
# Object hash collection
# ---------------------------------------------------------------------------


def _collect_hashes_from_manifest(manifest: dict[str, Any]) -> set[str]:
    """Return the set of all object_hash values referenced by a manifest."""
    hashes: set[str] = set()
    for cat_payload in manifest.get("categories", {}).values():
        for entry in cat_payload.get("files", []):
            h = entry.get("object_hash")
            if h:
                hashes.add(h)
    return hashes


# ---------------------------------------------------------------------------
# Object store scanning
# ---------------------------------------------------------------------------


def _scan_objects(objects_dir: Path) -> list[Path]:
    """Recursively enumerate object files under *objects_dir*.

    Skips .tmp files (in-flight writes from snapshot.py).
    Expected layout: <objects_dir>/<aa>/<bb>/<hash>
    """
    results: list[Path] = []
    if not objects_dir.is_dir():
        return results
    for aa_dir in objects_dir.iterdir():
        if not aa_dir.is_dir():
            continue
        for bb_dir in aa_dir.iterdir():
            if not bb_dir.is_dir():
                continue
            for obj_file in bb_dir.iterdir():
                if obj_file.is_file() and not obj_file.name.endswith(".tmp"):
                    results.append(obj_file)
    return results


def _prune_empty_shard_dirs(objects_dir: Path) -> None:
    """Remove empty <aa>/<bb>/ and <aa>/ directories after object deletion."""
    if not objects_dir.is_dir():
        return
    for aa_dir in list(objects_dir.iterdir()):
        if not aa_dir.is_dir():
            continue
        for bb_dir in list(aa_dir.iterdir()):
            if not bb_dir.is_dir():
                continue
            try:
                bb_dir.rmdir()  # Only succeeds if empty
            except OSError:
                pass  # Not empty — leave it
        try:
            aa_dir.rmdir()  # Only succeeds if empty
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Core GC logic
# ---------------------------------------------------------------------------


def run_gc(
    drive: str,
    keep_last: int | None,
    keep_after: str | None,
    keep_ids_raw: str | None,
    dry_run: bool,
    out_path: str | None,
    allow_empty: bool,
    allow_orphans: bool,
) -> None:
    """Main GC logic. All validation and I/O live here; CLI wrapper stays thin."""

    drive_path = Path(drive)

    # ------------------------------------------------------------------
    # 1. Validate drive
    # ------------------------------------------------------------------
    if not drive_path.exists():
        log_error(f"--drive does not exist: {drive!r}", path=drive_path)
        sys.exit(1)
    if not drive_path.is_dir():
        log_error(f"--drive is not a directory: {drive!r}", path=drive_path)
        sys.exit(1)

    snapshots_dir = drive_path / "snapshots"
    objects_dir = drive_path / "objects"

    if not snapshots_dir.exists():
        log_error(
            f"snapshots directory not found under --drive. "
            f"Expected: {snapshots_dir}",
            path=snapshots_dir,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Orphan-objects safety check
    #    If snapshots/ is empty but objects/ has content, refuse unless
    #    --allow-orphans is set.
    # ------------------------------------------------------------------
    snapshot_subdirs = [
        d for d in snapshots_dir.iterdir() if d.is_dir()
    ] if snapshots_dir.exists() else []

    if not snapshot_subdirs and objects_dir.exists():
        all_objects = _scan_objects(objects_dir)
        if all_objects:
            if not allow_orphans:
                log_error(
                    "orphan-objects detected: snapshots/ is empty but objects/ contains "
                    "files. This may indicate snapshots were moved or deleted manually. "
                    "Pass --allow-orphans to proceed anyway."
                )
                sys.exit(1)
            else:
                log_warn(
                    "orphan-objects detected: proceeding because --allow-orphans was passed."
                )

    # ------------------------------------------------------------------
    # 3. Load manifest schema
    # ------------------------------------------------------------------
    schema = _load_manifest_schema()

    # ------------------------------------------------------------------
    # 4. Load and validate all manifests
    # ------------------------------------------------------------------
    manifests: list[dict[str, Any]] = []
    load_errors: list[str] = []

    for snap_dir in snapshot_subdirs:
        manifest_path = snap_dir / "manifest.json"
        if not manifest_path.is_file():
            log_warn(f"snapshot directory has no manifest.json, skipping: {snap_dir.name!r}")
            continue
        try:
            manifest = _load_and_validate_manifest(manifest_path, schema)
        except ValueError as exc:
            msg = f"snapshot {snap_dir.name!r}: {exc}"
            log_error(msg)
            load_errors.append(msg)
            continue
        manifests.append(manifest)

    if load_errors:
        log_error(
            f"{len(load_errors)} manifest(s) failed validation — refusing to GC to "
            "avoid accidental data loss. Fix or remove the invalid snapshots first."
        )
        sys.exit(1)

    log_info(f"loaded {len(manifests)} snapshot manifest(s)")

    # ------------------------------------------------------------------
    # 5. Parse keep_ids if provided
    # ------------------------------------------------------------------
    keep_ids_set: set[str] | None = None
    if keep_ids_raw is not None:
        keep_ids_set = {sid.strip() for sid in keep_ids_raw.split(",") if sid.strip()}

    # ------------------------------------------------------------------
    # 6. Apply retention rule
    # ------------------------------------------------------------------
    if not manifests:
        # No snapshots at all — nothing to do.
        log_info("no snapshots found; nothing to GC")
        report = {
            "retained": [],
            "pruned": [],
            "objects_deleted": 0,
            "objects_kept": 0,
            "bytes_freed_approx": 0,
            "dry_run": dry_run,
        }
        _emit_report(report, out_path)
        return

    keep_ids, prune_ids = _apply_retention_rule(
        manifests,
        keep_last=keep_last,
        keep_after=keep_after,
        keep_ids_set=keep_ids_set,
    )

    # ------------------------------------------------------------------
    # 7. Safety: refuse if KEEP is empty and --allow-empty not set
    # ------------------------------------------------------------------
    if not keep_ids and not allow_empty:
        log_error(
            "retention rule would delete ALL snapshots. Refusing to proceed. "
            "Pass --allow-empty to override this safety check."
        )
        sys.exit(1)

    log_count("snapshots:retained", len(keep_ids))
    log_count("snapshots:pruned", len(prune_ids))

    # ------------------------------------------------------------------
    # 8. Compute referenced_hashes_kept
    # ------------------------------------------------------------------
    id_to_manifest: dict[str, dict[str, Any]] = {
        m["snapshot_id"]: m for m in manifests
    }

    referenced_hashes_kept: set[str] = set()
    for sid in keep_ids:
        m = id_to_manifest.get(sid)
        if m:
            referenced_hashes_kept |= _collect_hashes_from_manifest(m)

    # ------------------------------------------------------------------
    # 9. Scan objects and determine which to delete
    # ------------------------------------------------------------------
    all_object_files = _scan_objects(objects_dir)

    objects_to_delete: list[Path] = []
    objects_to_keep: list[Path] = []

    for obj_path in all_object_files:
        obj_hash = obj_path.name
        if obj_hash in referenced_hashes_kept:
            objects_to_keep.append(obj_path)
        else:
            objects_to_delete.append(obj_path)

    bytes_freed: int = 0
    for obj_path in objects_to_delete:
        try:
            bytes_freed += obj_path.stat().st_size
        except OSError:
            pass

    log_count("objects:to_delete", len(objects_to_delete))
    log_count("objects:to_keep", len(objects_to_keep))

    # ------------------------------------------------------------------
    # 10. Apply or report under --dry-run
    # ------------------------------------------------------------------
    if dry_run:
        log_info(
            f"dry-run: would prune {len(prune_ids)} snapshot(s), "
            f"delete {len(objects_to_delete)} object(s) "
            f"(approx {bytes_freed} bytes)"
        )
    else:
        # Delete pruned snapshot directories
        for sid in prune_ids:
            snap_dir = snapshots_dir / sid
            if snap_dir.exists():
                try:
                    shutil.rmtree(snap_dir)
                    log_info(f"deleted snapshot: {sid!r}")
                except OSError as exc:
                    log_error(f"failed to delete snapshot {sid!r}: {exc}")
                    sys.exit(1)

        # Delete unreferenced object files
        for obj_path in objects_to_delete:
            try:
                obj_path.unlink()
            except OSError as exc:
                log_error(f"failed to delete object {obj_path.name!r}: {exc}")
                sys.exit(1)

        # Prune empty shard directories
        _prune_empty_shard_dirs(objects_dir)

        log_info(
            f"GC complete: pruned {len(prune_ids)} snapshot(s), "
            f"deleted {len(objects_to_delete)} object(s) "
            f"(approx {bytes_freed} bytes freed)"
        )

    # ------------------------------------------------------------------
    # 11. Emit report
    # ------------------------------------------------------------------
    report = {
        "retained": keep_ids,
        "pruned": prune_ids,
        "objects_deleted": len(objects_to_delete),
        "objects_kept": len(objects_to_keep),
        "bytes_freed_approx": bytes_freed,
        "dry_run": dry_run,
    }
    _emit_report(report, out_path)


def _emit_report(report: dict[str, Any], out_path: str | None) -> None:
    report_json = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if out_path is not None:
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(report_json)
            log_info(f"GC report written to {out_path!r}")
        except OSError as exc:
            log_error(f"failed to write GC report: {exc}", path=out_path)
            sys.exit(1)
    else:
        sys.stdout.write(report_json)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Garbage-collect old snapshots and unreferenced objects from a "
            "rehydrate backup drive. Exactly one retention rule must be specified."
        ),
    )
    parser.add_argument(
        "--drive",
        required=True,
        metavar="PATH",
        help="Backup drive root directory (must exist and contain a snapshots/ subdirectory)",
    )

    retention_group = parser.add_argument_group(
        "retention rules (exactly one required)"
    )
    retention_group.add_argument(
        "--keep-last",
        type=int,
        metavar="N",
        default=None,
        help="Keep the N most recent snapshots by created_at. Must be >= 0.",
    )
    retention_group.add_argument(
        "--keep-after",
        metavar="DATE",
        default=None,
        help=(
            "Keep snapshots whose created_at is >= DATE. "
            "Accepts ISO 8601 date (YYYY-MM-DD) or datetime."
        ),
    )
    retention_group.add_argument(
        "--keep-ids",
        metavar="ID1,ID2,...",
        default=None,
        help="Comma-separated list of snapshot IDs to retain. All others are pruned.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would be deleted without making any filesystem changes.",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write the GC report JSON to this file instead of stdout.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        default=False,
        help=(
            "Bypass the safety check that refuses to delete all snapshots. "
            "Use with extreme caution."
        ),
    )
    parser.add_argument(
        "--allow-orphans",
        action="store_true",
        default=False,
        help=(
            "Bypass the orphan-objects safety check (objects/ present but "
            "snapshots/ is empty). Use when snapshots were intentionally removed."
        ),
    )

    args = parser.parse_args(argv)

    # Enforce exactly-one retention rule
    retention_flags = [
        ("--keep-last", args.keep_last is not None),
        ("--keep-after", args.keep_after is not None),
        ("--keep-ids", args.keep_ids is not None),
    ]
    active = [name for name, present in retention_flags if present]

    if len(active) == 0:
        parser.error(
            "exactly one retention rule is required: "
            "--keep-last, --keep-after, or --keep-ids"
        )
    if len(active) > 1:
        parser.error(
            f"only one retention rule may be specified at a time; "
            f"got: {', '.join(active)}"
        )

    # Validate --keep-last range
    if args.keep_last is not None and args.keep_last < 0:
        parser.error("--keep-last must be >= 0")

    run_gc(
        drive=args.drive,
        keep_last=args.keep_last,
        keep_after=args.keep_after,
        keep_ids_raw=args.keep_ids,
        dry_run=args.dry_run,
        out_path=args.out,
        allow_empty=args.allow_empty,
        allow_orphans=args.allow_orphans,
    )


if __name__ == "__main__":
    main()
