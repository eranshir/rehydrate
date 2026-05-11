"""
snapshot.py — rehydrate Phase 2.4

Given one or more walk outputs (one per category) and a probe output,
copy each file into content-addressed ``objects/`` storage on the backup
drive and emit a validated snapshot manifest.

Usage:
    python3 scripts/snapshot.py \\
        --walk-output /tmp/walk-dotfiles.json --category dotfiles \\
        --probe-output /tmp/probe.json \\
        --drive /Volumes/PortableSSD/llm-backup \\
        --snapshot-id "$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)"

The ``--walk-output`` / ``--category`` flags must appear in pairs and the
same number of times.  Use ``action='append'`` behaviour — repeat the pair
for each category.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Make ``scripts`` package importable regardless of working directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.no_pii_log import (  # noqa: E402
    log_count,
    log_debug,
    log_error,
    log_hash,
    log_info,
    log_path,
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

SCHEMA_VERSION = "0.1.0"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST_SCHEMA_PATH = _REPO_ROOT / "schemas" / "manifest.schema.json"

_CHUNK_SIZE = 64 * 1024  # 64 KB streaming chunks for hashing large files

# Snapshot-id may contain alphanumerics plus: - _ . : T Z
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9\-_.:T]+$")


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _sha256_file(path: str) -> str:
    """Compute SHA-256 hex digest of a regular file, streaming in 64 KB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    """Compute SHA-256 hex digest of a byte string."""
    return hashlib.sha256(data).hexdigest()


def _object_path(drive: str, digest: str) -> str:
    """Return the content-addressed path for a given 64-char hex digest."""
    aa = digest[:2]
    bb = digest[2:4]
    return os.path.join(drive, "objects", aa, bb, digest)


# ---------------------------------------------------------------------------
# Object store writer
# ---------------------------------------------------------------------------


def _store_object_from_file(drive: str, digest: str, src_path: str) -> bool:
    """
    Copy *src_path* bytes into the object store at the path derived from
    *digest*.  Uses a .tmp file + atomic os.replace to avoid corrupt
    partial objects on crash.

    Returns True if the object was newly written, False if it already existed
    (dedup).
    """
    dest = _object_path(drive, digest)
    if os.path.exists(dest):
        log_debug(f"dedup: object already exists ({digest[:16]}…)")
        return False

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp_path = dest + ".tmp"
    try:
        shutil.copy2(src_path, tmp_path)
        os.replace(tmp_path, dest)
    except Exception:
        # Clean up partial .tmp on any error
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return True


def _store_object_from_bytes(drive: str, digest: str, data: bytes) -> bool:
    """
    Write *data* bytes into the object store at the path derived from
    *digest*.  Uses a .tmp file + atomic os.replace.

    Returns True if the object was newly written, False if it already existed.
    """
    dest = _object_path(drive, digest)
    if os.path.exists(dest):
        log_debug(f"dedup: object already exists ({digest[:16]}…)")
        return False

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp_path = dest + ".tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, dest)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return True


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def _load_manifest_schema() -> dict[str, Any]:
    if not _MANIFEST_SCHEMA_PATH.exists():
        log_error("manifest schema not found", path=_MANIFEST_SCHEMA_PATH)
        sys.exit(1)
    with _MANIFEST_SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _validate_manifest(manifest: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate *manifest* against *schema*; exit non-zero on failure."""
    try:
        jsonschema.validate(instance=manifest, schema=schema)
    except jsonschema.ValidationError as exc:
        log_error(f"manifest validation failed: {exc.message}")
        sys.exit(1)
    except jsonschema.SchemaError as exc:
        log_error(f"manifest schema itself is invalid: {exc.message}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Atomic manifest / parent write
# ---------------------------------------------------------------------------


def _atomic_write(dest: str, text: str) -> None:
    """Write *text* to *dest* atomically via a sibling .tmp file."""
    tmp_path = dest + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, dest)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------


def _load_walk_output(path: str) -> dict[str, Any]:
    """Load and sanity-check a walk-output JSON file; exit on error."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log_error(f"failed to load walk output: {exc}", path=path)
        sys.exit(1)

    # Minimal shape check
    for field in ("category", "files", "coverage"):
        if field not in data:
            log_error(
                f"walk output missing required field '{field}'",
                path=path,
            )
            sys.exit(1)

    if not isinstance(data["files"], list):
        log_error("walk output 'files' must be a list", path=path)
        sys.exit(1)

    return data


def _load_probe_output(path: str) -> dict[str, Any]:
    """Load probe output JSON; exit on error."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log_error(f"failed to load probe output: {exc}", path=path)
        sys.exit(1)
    return data


# ---------------------------------------------------------------------------
# Core snapshot logic
# ---------------------------------------------------------------------------


def run_snapshot(
    walk_pairs: list[tuple[str, str]],  # list of (walk_output_path, category_name)
    probe_output_path: str,
    drive: str,
    snapshot_id: str,
    parent_id: str | None,
    home: str,
) -> None:
    """
    Main snapshot logic.  All validation and I/O happens here so the CLI
    wrapper stays thin.
    """
    # ------------------------------------------------------------------
    # 1. Validate snapshot-id format
    # ------------------------------------------------------------------
    if not _SNAPSHOT_ID_RE.match(snapshot_id):
        log_error(
            f"snapshot-id contains disallowed characters: {snapshot_id!r}. "
            "Only alphanumerics and - _ . : T Z are permitted."
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Validate drive
    # ------------------------------------------------------------------
    if not os.path.isdir(drive):
        log_error("--drive does not exist or is not a directory", path=drive)
        sys.exit(1)
    if not os.access(drive, os.W_OK):
        log_error("--drive is not writable", path=drive)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Load inputs
    # ------------------------------------------------------------------
    probe_data = _load_probe_output(probe_output_path)

    walk_outputs: list[tuple[dict[str, Any], str]] = []
    for walk_path, category_name in walk_pairs:
        walk_data = _load_walk_output(walk_path)
        walk_outputs.append((walk_data, category_name))

    # ------------------------------------------------------------------
    # 4. Ensure objects/ exists and create snapshot dir (refuse to overwrite)
    # ------------------------------------------------------------------
    objects_dir = os.path.join(drive, "objects")
    os.makedirs(objects_dir, exist_ok=True)

    snapshot_dir = os.path.join(drive, "snapshots", snapshot_id)
    if os.path.exists(snapshot_dir):
        log_error(
            f"snapshot directory already exists — refusing to overwrite: {snapshot_id}",
            path=snapshot_dir,
        )
        sys.exit(1)
    os.makedirs(snapshot_dir)
    log_path(snapshot_dir, level="info")

    # ------------------------------------------------------------------
    # 5. Process each category
    # ------------------------------------------------------------------
    manifest_schema = _load_manifest_schema()
    categories_payload: dict[str, Any] = {}
    total_files = 0
    total_deduped = 0

    for walk_data, category_name in walk_outputs:
        files_in: list[dict[str, Any]] = walk_data["files"]
        manifest_entries: list[dict[str, Any]] = []
        cat_deduped = 0
        cat_new = 0

        for file_entry in files_in:
            rel_path: str = file_entry["path"]
            abs_src = os.path.join(home, rel_path)
            is_symlink: bool = file_entry.get("is_symlink", False)
            symlink_target: str | None = file_entry.get("symlink_target")

            if is_symlink:
                # Hash the UTF-8 encoded link-target string
                if symlink_target is None:
                    log_warn(
                        f"is_symlink=true but symlink_target is null for {rel_path!r}; skipping"
                    )
                    continue
                target_bytes = symlink_target.encode("utf-8")
                digest = _sha256_bytes(target_bytes)
                is_new = _store_object_from_bytes(drive, digest, target_bytes)
            else:
                # Regular file
                if not os.path.exists(abs_src):
                    log_warn(f"source file not found (skipping): {rel_path!r}")
                    continue
                try:
                    digest = _sha256_file(abs_src)
                except OSError as exc:
                    log_warn(f"could not read source file ({exc}): {rel_path!r}")
                    continue
                is_new = _store_object_from_file(drive, digest, abs_src)

            if is_new:
                cat_new += 1
            else:
                cat_deduped += 1

            manifest_entries.append({
                "path": rel_path,
                "object_hash": digest,
                "mode": file_entry["mode"],
                "mtime": file_entry["mtime"],
                "size": file_entry["size"],
                "is_symlink": is_symlink,
                "symlink_target": symlink_target,
            })

        log_count(f"{category_name}:files_captured", len(manifest_entries))
        log_count(f"{category_name}:objects_new", cat_new)
        log_count(f"{category_name}:objects_deduped", cat_deduped)

        total_files += len(manifest_entries)
        total_deduped += cat_deduped

        categories_payload[category_name] = {
            "strategy": "file-list",
            "files": manifest_entries,
        }

    log_count("total:files", total_files)
    log_count("total:deduped", total_deduped)

    # ------------------------------------------------------------------
    # 6. Build manifest
    # ------------------------------------------------------------------
    created_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "snapshot_id": snapshot_id,
        "source_machine": probe_data,
        "parent_snapshot": parent_id,
        "categories": categories_payload,
    }

    # ------------------------------------------------------------------
    # 7. Validate manifest before writing
    # ------------------------------------------------------------------
    _validate_manifest(manifest, manifest_schema)
    log_info("manifest validated against schema")

    # ------------------------------------------------------------------
    # 8. Write manifest + parent.txt atomically
    # ------------------------------------------------------------------
    manifest_path = os.path.join(snapshot_dir, "manifest.json")
    _atomic_write(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    log_path(manifest_path, level="info")

    parent_txt_path = os.path.join(snapshot_dir, "parent.txt")
    parent_text = parent_id if parent_id is not None else "none"
    _atomic_write(parent_txt_path, parent_text + "\n")

    log_info(f"snapshot complete: id={snapshot_id}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Copy walk output files into content-addressed objects/ storage "
            "on the backup drive and emit a validated snapshot manifest."
        ),
    )
    parser.add_argument(
        "--walk-output",
        dest="walk_outputs",
        action="append",
        metavar="PATH",
        required=True,
        help="JSON file produced by walk.py (may be repeated, paired with --category)",
    )
    parser.add_argument(
        "--category",
        dest="categories",
        action="append",
        metavar="NAME",
        required=True,
        help="Category name to accompany each --walk-output (same order)",
    )
    parser.add_argument(
        "--probe-output",
        required=True,
        metavar="PATH",
        help="JSON file produced by probe.py",
    )
    parser.add_argument(
        "--drive",
        required=True,
        metavar="PATH",
        help="Backup drive root directory (must exist and be writable)",
    )
    parser.add_argument(
        "--snapshot-id",
        required=True,
        metavar="ID",
        help="Unique snapshot identifier, e.g. <hostname>-<UTC-iso-no-colons>",
    )
    parser.add_argument(
        "--parent",
        default=None,
        metavar="ID",
        help="Parent snapshot ID for incremental chains (optional; becomes null in manifest)",
    )
    parser.add_argument(
        "--home",
        default=os.path.expanduser("~"),
        metavar="PATH",
        help="Home directory that walk-output paths are relative to (default: $HOME)",
    )

    args = parser.parse_args(argv)

    # Validate that --walk-output and --category counts match
    if len(args.walk_outputs) != len(args.categories):
        parser.error(
            f"--walk-output and --category must appear the same number of times "
            f"(got {len(args.walk_outputs)} walk-outputs, {len(args.categories)} categories)"
        )

    walk_pairs = list(zip(args.walk_outputs, args.categories))

    run_snapshot(
        walk_pairs=walk_pairs,
        probe_output_path=args.probe_output,
        drive=args.drive,
        snapshot_id=args.snapshot_id,
        parent_id=args.parent,
        home=args.home,
    )


if __name__ == "__main__":
    main()
