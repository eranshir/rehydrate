#!/usr/bin/env python3
"""
restore-plan.py — Read a rehydrate snapshot manifest and emit a per-action
restore plan as JSON.  This script is **read-only**: it never writes bytes to
the target directory; it only inspects it and describes what a restore would do.

Usage
-----
    python3 scripts/restore-plan.py \\
        --snapshot /Volumes/PortableSSD/llm-backup/snapshots/<id> \\
        --target /Users/alice \\
        [--out plan.json]

If ``--target`` is omitted the script reads ``$REHYDRATE_TARGET``; if that is
also unset it exits with a non-zero status (it does NOT fall back to $HOME).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project path setup — allow the script to be run from any working directory.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.no_pii_log import (  # noqa: E402
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
MANIFEST_SCHEMA_PATH = _REPO_ROOT / "schemas" / "manifest.schema.json"

# Drift severity levels
_SEVERITY_INFO = "info"
_SEVERITY_WARN = "warn"
_SEVERITY_ERROR = "error"


# ---------------------------------------------------------------------------
# Internal exception for controlled exit
# ---------------------------------------------------------------------------

class _FatalError(Exception):
    """Raised by internal helpers to signal a fatal condition.

    Caught by ``main()`` which logs the error and returns exit code 1.
    This avoids bare ``sys.exit()`` calls inside library-style functions,
    making the script callable from tests without raising SystemExit.
    """


# ---------------------------------------------------------------------------
# Manifest validation
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


def _validate_manifest(manifest: dict) -> None:
    """Validate *manifest* against the manifest JSON Schema.

    Uses jsonschema if available; falls back to a minimal structural check
    so that the script remains stdlib-only at runtime.
    """
    try:
        import jsonschema  # optional dep — test/dev only
        schema = _load_json(MANIFEST_SCHEMA_PATH)
        try:
            jsonschema.validate(manifest, schema)
            log_info("Manifest schema validation passed.")
        except jsonschema.ValidationError as exc:
            log_error(f"Manifest schema validation failed: {exc.message}")
            raise _FatalError(f"Manifest schema validation failed: {exc.message}")
    except ImportError:
        # Minimal structural check without jsonschema.
        required_top = {"schema_version", "created_at", "snapshot_id",
                        "source_machine", "categories"}
        missing = required_top - manifest.keys()
        if missing:
            log_error(f"Manifest missing required keys: {sorted(missing)}")
            raise _FatalError(f"Manifest missing required keys: {sorted(missing)}")
        log_debug("jsonschema not available; used minimal structural check.")


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of *path*'s contents."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_string(s: str) -> str:
    """Return the SHA-256 hex digest of a UTF-8 encoded string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def _detect_drift(source_machine: dict) -> list[dict]:
    """Compare *source_machine* (from manifest) to the current machine.

    Returns a list of structured drift-warning dicts.
    """
    drift: list[dict] = []

    # OS version
    mac_ver, _, _ = platform.mac_ver()
    current_os_version = mac_ver if mac_ver else platform.version()
    source_os_version = source_machine.get("os_version", "")
    if current_os_version != source_os_version:
        drift.append({
            "kind": "os_version_mismatch",
            "source": source_os_version,
            "current": current_os_version,
            "severity": _SEVERITY_INFO,
        })
        log_info(
            f"OS version drift: manifest={source_os_version!r} "
            f"current={current_os_version!r}"
        )

    # Hostname
    current_hostname = socket.gethostname()
    source_hostname = source_machine.get("hostname", "")
    if current_hostname != source_hostname:
        drift.append({
            "kind": "hostname_mismatch",
            "source": source_hostname,
            "current": current_hostname,
            "severity": _SEVERITY_INFO,
        })
        log_info(
            f"Hostname drift: manifest={source_hostname!r} "
            f"current={current_hostname!r}"
        )

    # User
    current_user = os.environ.get("USER", "")
    source_user = source_machine.get("user", "")
    if current_user != source_user:
        drift.append({
            "kind": "user_mismatch",
            "source": source_user,
            "current": current_user,
            "severity": _SEVERITY_WARN,
        })
        log_warn(
            f"User drift: manifest={source_user!r} "
            f"current={current_user!r}"
        )

    # Architecture
    current_arch = platform.machine()
    source_arch = source_machine.get("hardware", {}).get("arch", "")
    if current_arch != source_arch:
        drift.append({
            "kind": "arch_mismatch",
            "source": source_arch,
            "current": current_arch,
            "severity": _SEVERITY_WARN,
        })
        log_warn(
            f"Architecture drift: manifest={source_arch!r} "
            f"current={current_arch!r}"
        )

    return drift


# ---------------------------------------------------------------------------
# Action computation for a single file entry
# ---------------------------------------------------------------------------

def _compute_action(entry: dict, target_root: Path) -> dict:
    """Return a plan action dict for one file *entry*.

    The returned dict always contains at minimum ``path`` and ``type``.
    Additional fields depend on the action type.
    """
    rel_path: str = entry["path"]
    expected_hash: str = entry["object_hash"]
    mode: str = entry["mode"]
    is_symlink: bool = entry["is_symlink"]
    symlink_target: str | None = entry.get("symlink_target")

    target_path = target_root / rel_path

    if is_symlink:
        return _compute_symlink_action(rel_path, expected_hash, mode,
                                       symlink_target, target_path)
    return _compute_regular_action(rel_path, expected_hash, mode, target_path)


def _compute_regular_action(
    rel_path: str,
    expected_hash: str,
    mode: str,
    target_path: Path,
) -> dict:
    """Compute the action for a regular (non-symlink) file."""
    if not target_path.exists() and not target_path.is_symlink():
        # File is absent.
        return {
            "path": rel_path,
            "type": "create",
            "object_hash": expected_hash,
            "mode": mode,
        }

    if target_path.is_symlink():
        # The target is a symlink but the manifest expects a regular file.
        current_hash = _sha256_string(str(os.readlink(target_path)))
        return {
            "path": rel_path,
            "type": "overwrite-needs-confirm",
            "current_hash": current_hash,
            "expected_hash": expected_hash,
            "mode": mode,
        }

    # Regular file exists — compare hashes.
    try:
        current_hash = _sha256_file(target_path)
    except OSError as exc:
        log_error(f"Cannot read target file for hashing: {exc}")
        # Treat as needing confirmation to be safe.
        current_hash = "0" * 64

    if current_hash == expected_hash:
        return {
            "path": rel_path,
            "type": "skip-identical",
        }

    return {
        "path": rel_path,
        "type": "overwrite-needs-confirm",
        "current_hash": current_hash,
        "expected_hash": expected_hash,
        "mode": mode,
    }


def _compute_symlink_action(
    rel_path: str,
    expected_hash: str,
    mode: str,
    symlink_target: str | None,
    target_path: Path,
) -> dict:
    """Compute the action for a symlink entry."""
    if not target_path.exists() and not target_path.is_symlink():
        return {
            "path": rel_path,
            "type": "create",
            "object_hash": expected_hash,
            "mode": mode,
        }

    if target_path.is_symlink():
        # Compare the symlink target string.
        current_link_target = os.readlink(target_path)
        current_hash = _sha256_string(current_link_target)

        if current_hash == expected_hash:
            return {
                "path": rel_path,
                "type": "skip-identical",
            }

        return {
            "path": rel_path,
            "type": "overwrite-needs-confirm",
            "current_hash": current_hash,
            "expected_hash": expected_hash,
            "mode": mode,
        }

    # Something else (a regular file) occupies the path.
    try:
        current_hash = _sha256_file(target_path)
    except OSError as exc:
        log_error(f"Cannot read target path for hashing: {exc}")
        current_hash = "0" * 64

    return {
        "path": rel_path,
        "type": "overwrite-needs-confirm",
        "current_hash": current_hash,
        "expected_hash": expected_hash,
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------

def _build_plan(manifest: dict, target: Path) -> dict:
    """Assemble and return the full restore plan dict."""
    source_machine: dict = manifest["source_machine"]
    drift = _detect_drift(source_machine)

    actions_by_category: dict[str, list[dict]] = {}
    counts: dict[str, int] = {
        "create": 0,
        "skip-identical": 0,
        "overwrite-needs-confirm": 0,
    }

    categories: dict = manifest.get("categories", {})
    for category_name, category_payload in categories.items():
        files: list[dict] = category_payload.get("files", [])
        actions: list[dict] = []

        for entry in files:
            action = _compute_action(entry, target)
            actions.append(action)
            action_type = action["type"]
            if action_type in counts:
                counts[action_type] += 1
            log_debug(
                f"category={category_name!r} path={entry['path']!r} "
                f"action={action_type!r}"
            )

        actions_by_category[category_name] = actions
        log_info(f"Processed category {category_name!r}: {len(actions)} entries.")

    total = sum(counts.values())
    log_info(
        f"Plan summary: create={counts['create']} "
        f"skip-identical={counts['skip-identical']} "
        f"overwrite-needs-confirm={counts['overwrite-needs-confirm']} "
        f"total={total}"
    )

    return {
        "plan_version": PLAN_VERSION,
        "snapshot_id": manifest["snapshot_id"],
        "target": str(target),
        "drift": drift,
        "actions_by_category": actions_by_category,
        "summary": {
            "create": counts["create"],
            "skip-identical": counts["skip-identical"],
            "overwrite-needs-confirm": counts["overwrite-needs-confirm"],
            "total": total,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a per-action restore plan from a rehydrate snapshot "
            "manifest. This command is read-only: it never modifies the target."
        )
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        metavar="PATH",
        help="Path to a snapshot directory containing manifest.json.",
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
        "--out",
        default=None,
        metavar="PATH",
        help="Write the plan JSON to this file instead of stdout.",
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
        # Locate and load the manifest.
        snapshot_dir = Path(args.snapshot).expanduser().resolve()
        manifest_path = snapshot_dir / MANIFEST_FILENAME
        log_info("Loading manifest from snapshot directory.")
        log_debug(f"Snapshot dir: {snapshot_dir}")
        manifest = _load_json(manifest_path)

        # Validate.
        _validate_manifest(manifest)

        # Build the plan.
        log_info("Building restore plan for target.")
        plan = _build_plan(manifest, target)

    except _FatalError:
        # Errors already logged by the raising helper.
        return 1

    # Output.
    plan_json = json.dumps(plan, indent=2, ensure_ascii=False)
    if args.out:
        out_path = Path(args.out)
        out_path.write_text(plan_json + "\n", encoding="utf-8")
        log_info("Plan written.")
        log_debug(f"Plan path: {out_path}")
    else:
        print(plan_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
