#!/usr/bin/env python3
"""
restore-apply.py — Execute a restore plan emitted by restore-plan.py.

Reads the plan JSON, locates objects in the snapshot store, and writes bytes
to the target directory.  Live $HOME restoration requires ``--live``; any
``overwrite-needs-confirm`` action requires ``--overwrite``.

Usage
-----
    python3 scripts/restore-apply.py \\
        --plan plan.json \\
        --snapshot /Volumes/PortableSSD/llm-backup/snapshots/<id> \\
        --target /Users/alice \\
        [--live] [--overwrite] [--dry-run]

If ``--target`` is omitted the script reads ``$REHYDRATE_TARGET``; if that is
also unset it exits with a non-zero status.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Project path setup — allow the script to be run from any working directory.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.no_pii_log import (  # noqa: E402
    log_count,
    log_debug,
    log_error,
    log_info,
    log_warn,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLAN_VERSION = "0.1.0"
MANIFEST_FILENAME = "manifest.json"
RESTORE_PLAN_SCHEMA_PATH = _REPO_ROOT / "schemas" / "restore-plan.schema.json"


# ---------------------------------------------------------------------------
# Internal exception for controlled exit
# ---------------------------------------------------------------------------

class _FatalError(Exception):
    """Raised by internal helpers to signal a fatal condition.

    Caught by ``main()`` which logs the error and returns exit code 1.
    """


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Load and return a JSON file; raise _FatalError on error."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        log_error("File not found", path=path)
        raise _FatalError(f"File not found: {path}")
    except json.JSONDecodeError as exc:
        log_error(f"JSON parse error: {exc}")
        raise _FatalError(f"JSON parse error in {path}: {exc}")


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _validate_plan(plan: dict) -> None:
    """Validate *plan* against the restore-plan JSON Schema.

    Uses jsonschema if available; falls back to a minimal structural check.
    """
    try:
        import jsonschema  # optional dep — test/dev only
        schema = _load_json(RESTORE_PLAN_SCHEMA_PATH)
        try:
            jsonschema.validate(plan, schema)
            log_info("Plan schema validation passed.")
        except jsonschema.ValidationError as exc:
            log_error(f"Plan schema validation failed: {exc.message}")
            raise _FatalError(f"Plan schema validation failed: {exc.message}")
    except ImportError:
        # Minimal structural check without jsonschema.
        required_top = {
            "plan_version", "snapshot_id", "target",
            "drift", "actions_by_category", "summary",
        }
        missing = required_top - plan.keys()
        if missing:
            log_error(f"Plan missing required keys: {sorted(missing)}")
            raise _FatalError(f"Plan missing required keys: {sorted(missing)}")
        log_debug("jsonschema not available; used minimal structural check.")


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of *path*'s contents (follows symlinks)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_string(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Object store lookup
# ---------------------------------------------------------------------------

def _object_path(objects_dir: Path, object_hash: str) -> Path:
    """Return the path to an object given its SHA-256 hex digest.

    Layout: ``<objects>/<aa>/<bb>/<full_hash>``
    where ``aa`` = first 2 chars, ``bb`` = chars 3-4.
    """
    aa = object_hash[:2]
    bb = object_hash[2:4]
    return objects_dir / aa / bb / object_hash


def _read_object(objects_dir: Path, object_hash: str) -> bytes:
    """Read and return the content of an object after verifying its hash.

    Raises _FatalError on missing file or hash mismatch.
    """
    obj_path = _object_path(objects_dir, object_hash)
    if not obj_path.exists():
        log_error(f"Object not found for hash prefix {object_hash[:8]}...", path=obj_path)
        raise _FatalError(f"Object not found: {obj_path}")

    with obj_path.open("rb") as fh:
        data = fh.read()

    actual = _sha256_bytes(data)
    if actual != object_hash:
        log_error(
            f"Object hash mismatch: expected {object_hash[:8]}... "
            f"got {actual[:8]}..."
        )
        raise _FatalError(
            f"Object hash mismatch for {obj_path}: "
            f"expected {object_hash}, got {actual}"
        )

    return data


# ---------------------------------------------------------------------------
# Manifest helper — recover is_symlink / symlink_target per path
# ---------------------------------------------------------------------------

def _build_manifest_index(manifest: dict) -> dict[str, dict]:
    """Return a mapping of relative path → file_entry dict from the manifest.

    Used to recover ``is_symlink`` / ``symlink_target`` for actions that
    don't carry those fields (which is the case for the current plan schema).
    """
    index: dict[str, dict] = {}
    for category_payload in manifest.get("categories", {}).values():
        for entry in category_payload.get("files", []):
            index[entry["path"]] = entry
    return index


# ---------------------------------------------------------------------------
# Safety refusal checks
# ---------------------------------------------------------------------------

def _resolve_home() -> Path:
    """Return the resolved live $HOME directory."""
    return Path(os.path.expanduser("~")).resolve()


def _check_target_safety(target: Path, live: bool) -> None:
    """Raise _FatalError if *target* fails any safety check.

    Checks (in order):
    1. Target must not be the filesystem root.
    2. Target must exist (caller must create it).
    3. Target must not be inside ~/Library (system-managed).
    4. Target must not be live $HOME (or its ancestors) unless --live is given.
    """
    # 1. Root refusal.
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target

    if resolved == Path("/"):
        log_error("Refusing to restore to filesystem root '/'.")
        raise _FatalError("Target '/' is not allowed.")

    # 2. Must exist.
    if not target.exists():
        log_error("Target directory does not exist.", path=target)
        raise _FatalError(f"Target does not exist: {target}")

    if not target.is_dir():
        log_error("Target path exists but is not a directory.", path=target)
        raise _FatalError(f"Target is not a directory: {target}")

    # 3. Inside ~/Library refusal.
    live_home = _resolve_home()
    library_dir = live_home / "Library"
    try:
        resolved.relative_to(library_dir)
        # If we get here, target is inside ~/Library.
        log_error(f"Refusing to restore into ~/Library (system-managed).")
        raise _FatalError(
            f"Target {target} is inside ~/Library which is system-managed."
        )
    except ValueError:
        pass  # Not inside Library — good.

    # 4. Live $HOME without --live.
    if not live:
        # Refuse if target IS $HOME or is any direct ancestor of $HOME
        # (e.g. /Users when $HOME=/Users/alice).
        try:
            live_home.relative_to(resolved)
            # If this succeeds, $HOME is resolved relative to target, meaning
            # target is an ancestor of (or equal to) $HOME.
            log_error(
                "Target resolves to live $HOME or its ancestor. "
                "Pass --live to confirm."
            )
            raise _FatalError(
                f"Target {target} covers live $HOME. "
                "Use --live to allow restoration to the live home directory."
            )
        except ValueError:
            pass  # Target is not an ancestor — good.


# ---------------------------------------------------------------------------
# Action application
# ---------------------------------------------------------------------------

def _apply_create(
    action: dict,
    objects_dir: Path,
    target: Path,
    manifest_index: dict[str, dict],
    dry_run: bool,
) -> None:
    """Apply a 'create' action."""
    rel_path: str = action["path"]
    object_hash: str = action["object_hash"]
    mode_str: str = action["mode"]

    # Determine if this is a symlink via the manifest index.
    entry = manifest_index.get(rel_path, {})
    is_symlink: bool = entry.get("is_symlink", False)
    symlink_target: str | None = entry.get("symlink_target")
    mtime_str: str | None = entry.get("mtime")

    dest = target / rel_path
    log_info(f"create: {rel_path}")

    if dry_run:
        log_info(f"[dry-run] would create: {rel_path}")
        return

    # Read and verify the object.
    data = _read_object(objects_dir, object_hash)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if is_symlink:
        _write_symlink(dest, data, symlink_target, mtime_str)
    else:
        _write_regular_file(dest, data, mode_str, mtime_str)


def _apply_overwrite(
    action: dict,
    objects_dir: Path,
    target: Path,
    manifest_index: dict[str, dict],
    dry_run: bool,
) -> None:
    """Apply an 'overwrite-needs-confirm' action."""
    rel_path: str = action["path"]
    object_hash: str = action["expected_hash"]
    mode_str: str = action["mode"]

    entry = manifest_index.get(rel_path, {})
    is_symlink: bool = entry.get("is_symlink", False)
    symlink_target: str | None = entry.get("symlink_target")
    mtime_str: str | None = entry.get("mtime")

    dest = target / rel_path
    log_info(f"overwrite: {rel_path}")

    if dry_run:
        log_info(f"[dry-run] would overwrite: {rel_path}")
        return

    # Read and verify the object.
    data = _read_object(objects_dir, object_hash)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Remove the existing path first (handles both files and symlinks).
    if dest.exists() or dest.is_symlink():
        dest.unlink()

    if is_symlink:
        _write_symlink(dest, data, symlink_target, mtime_str)
    else:
        _write_regular_file(dest, data, mode_str, mtime_str)


def _write_regular_file(
    dest: Path,
    data: bytes,
    mode_str: str,
    mtime_str: str | None,
) -> None:
    """Write *data* to *dest* atomically; set mode and mtime."""
    # Write to a .tmp file in the same parent dir, then atomically replace.
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        try:
            os.write(tmp_fd, data)
        finally:
            os.close(tmp_fd)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Apply mode.
    os.chmod(dest, int(mode_str, 8))

    # Apply mtime if available.
    if mtime_str:
        mtime_ts = _parse_mtime(mtime_str)
        if mtime_ts is not None:
            os.utime(dest, (mtime_ts, mtime_ts))


def _write_symlink(
    dest: Path,
    data: bytes,
    symlink_target: str | None,
    mtime_str: str | None,
) -> None:
    """Create a symlink at *dest*.

    The link target string is taken from *symlink_target* if provided;
    otherwise it is decoded from *data* (which is the UTF-8 target string
    stored in the object store for symlink entries).
    """
    if symlink_target is None:
        symlink_target = data.decode("utf-8").rstrip("\n")

    os.symlink(symlink_target, dest)

    # Apply mtime to the symlink itself (not the target) where supported.
    if mtime_str:
        mtime_ts = _parse_mtime(mtime_str)
        if mtime_ts is not None:
            try:
                os.utime(dest, (mtime_ts, mtime_ts), follow_symlinks=False)
            except (NotImplementedError, OSError):
                pass  # macOS may not support lutimes in all cases.


def _parse_mtime(mtime_str: str) -> float | None:
    """Parse an ISO 8601 UTC mtime string to a POSIX timestamp, or None."""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(mtime_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        log_warn(f"Could not parse mtime string, skipping mtime restore.")
        return None


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------

def _execute_plan(
    plan: dict,
    objects_dir: Path,
    target: Path,
    manifest_index: dict[str, dict],
    overwrite: bool,
    dry_run: bool,
) -> int:
    """Execute all actions in the plan.

    Returns 0 on full success, 1 if any action failed.
    """
    counts = {
        "created": 0,
        "skipped": 0,
        "overwritten": 0,
        "failed": 0,
    }

    for category_name, actions in plan["actions_by_category"].items():
        log_info(f"Processing category: {category_name}")
        for action in actions:
            action_type: str = action["type"]
            rel_path: str = action["path"]

            try:
                if action_type == "skip-identical":
                    log_debug(f"skip-identical: {rel_path}")
                    counts["skipped"] += 1

                elif action_type == "create":
                    _apply_create(
                        action, objects_dir, target,
                        manifest_index, dry_run,
                    )
                    counts["created"] += 1

                elif action_type == "overwrite-needs-confirm":
                    # This branch is only reached when --overwrite was given;
                    # the pre-flight check in main() already refused otherwise.
                    _apply_overwrite(
                        action, objects_dir, target,
                        manifest_index, dry_run,
                    )
                    counts["overwritten"] += 1

                else:
                    log_warn(f"Unknown action type {action_type!r}, skipping.")

            except _FatalError as exc:
                log_error(f"Action failed for path {rel_path!r}: {exc}")
                counts["failed"] += 1
                # Stop immediately; do NOT roll back.  Log partial state.
                log_warn(
                    "Stopping after first failure. "
                    "The target is in a partially-applied state. "
                    "Re-run restore-plan.py + restore-apply.py to resume safely."
                )
                _emit_summary(counts)
                return 1

    _emit_summary(counts)
    return 0


def _emit_summary(counts: dict[str, int]) -> None:
    """Emit per-category counts via log_count."""
    log_count("created", counts["created"])
    log_count("skipped", counts["skipped"])
    log_count("overwritten", counts["overwritten"])
    log_count("failed", counts["failed"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute a rehydrate restore plan: write files from the snapshot "
            "object store to the target directory."
        )
    )
    parser.add_argument(
        "--plan",
        required=True,
        metavar="PATH",
        help="Path to a restore plan JSON produced by restore-plan.py.",
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        metavar="PATH",
        help=(
            "Path to the snapshot directory containing manifest.json. "
            "The objects/ store is at <drive>/objects/ (two levels up)."
        ),
    )
    parser.add_argument(
        "--target",
        default=None,
        metavar="PATH",
        help=(
            "Restore target directory. Defaults to $REHYDRATE_TARGET if set; "
            "exits with an error if neither is provided."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help=(
            "Required when --target resolves to live $HOME or any ancestor. "
            "Protects against accidental live-home overwrite."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help=(
            "Required to perform any overwrite-needs-confirm actions. "
            "Without this flag the script refuses if any such action exists."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Log what would be done but write nothing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns an exit code."""
    args = _parse_args(argv)

    # Resolve target.
    target_str: str | None = args.target or os.environ.get("REHYDRATE_TARGET")
    if not target_str:
        log_error(
            "No restore target specified. "
            "Pass --target or set $REHYDRATE_TARGET."
        )
        return 1
    target = Path(target_str).expanduser().resolve()

    try:
        # ---------------------
        # Safety checks first.
        # ---------------------
        _check_target_safety(target, args.live)

        # ---------------------
        # Load + validate plan.
        # ---------------------
        plan_path = Path(args.plan).expanduser().resolve()
        log_info("Loading restore plan.")
        log_debug(f"Plan path: {plan_path}")
        plan = _load_json(plan_path)
        _validate_plan(plan)

        # ---------------------
        # Load manifest.
        # ---------------------
        snapshot_dir = Path(args.snapshot).expanduser().resolve()
        manifest_path = snapshot_dir / MANIFEST_FILENAME
        log_info("Loading snapshot manifest.")
        manifest = _load_json(manifest_path)
        manifest_index = _build_manifest_index(manifest)

        # ---------------------
        # Locate objects store.
        # ---------------------
        # Drive layout: <drive>/objects/ and <drive>/snapshots/<id>/, so the
        # objects dir is two levels up from a snapshot dir.
        objects_dir = (snapshot_dir / ".." / ".." / "objects").resolve()
        log_debug(f"Objects dir: {objects_dir}")
        if not objects_dir.is_dir():
            log_error("Objects directory not found.", path=objects_dir)
            raise _FatalError(f"Objects directory not found: {objects_dir}")

        # ---------------------
        # Pre-flight: overwrite gate.
        # ---------------------
        if not args.overwrite:
            needs_confirm = [
                (cat, a)
                for cat, actions in plan["actions_by_category"].items()
                for a in actions
                if a["type"] == "overwrite-needs-confirm"
            ]
            if needs_confirm:
                count = len(needs_confirm)
                log_error(
                    f"Plan contains {count} overwrite-needs-confirm action(s). "
                    "Pass --overwrite to allow them."
                )
                raise _FatalError(
                    f"{count} overwrite-needs-confirm action(s) require --overwrite."
                )

        # ---------------------
        # Log drift warnings.
        # ---------------------
        for drift_item in plan.get("drift", []):
            severity = drift_item.get("severity", "info")
            kind = drift_item.get("kind", "unknown")
            source = drift_item.get("source", "?")
            current = drift_item.get("current", "?")
            msg = f"Drift detected: {kind} source={source!r} current={current!r}"
            if severity == "warn":
                log_warn(msg)
            elif severity == "error":
                log_error(msg)
            else:
                log_info(msg)

        if args.dry_run:
            log_info("[dry-run] No changes will be written.")

        # ---------------------
        # Execute.
        # ---------------------
        log_info("Starting restore apply.")
        rc = _execute_plan(
            plan, objects_dir, target,
            manifest_index, args.overwrite, args.dry_run,
        )
        if rc == 0:
            log_info("Restore apply complete.")
        else:
            log_error("Restore apply finished with errors.")
        return rc

    except _FatalError:
        # Errors already logged by the raising helper.
        return 1


if __name__ == "__main__":
    sys.exit(main())
