#!/usr/bin/env python3
"""
verify-sandbox.py — Sandboxed test restore and byte-equality verifier.

Restores a snapshot into a fresh temp directory (never live $HOME), then
diffs every restored file against the manifest's recorded hash — and
optionally against the original source file — to prove the backup is
byte-for-byte correct.

Usage
-----
    python3 scripts/verify-sandbox.py \\
        --snapshot /Volumes/Backup/snapshots/<id> \\
        [--source-home $HOME] \\
        [--out report.json] \\
        [--keep] \\
        [--no-source-check]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import random
import shutil
import string
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project path setup — allow the script to be run from any working directory.
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
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

VERIFY_VERSION = "0.1.0"
MANIFEST_FILENAME = "manifest.json"

# ---------------------------------------------------------------------------
# Internal exception for controlled exit
# ---------------------------------------------------------------------------


class _FatalError(Exception):
    """Raised by internal helpers to signal a fatal condition."""


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
# Manifest loading
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


def _collect_file_entries(manifest: dict) -> list[dict]:
    """Collect all file_entry dicts across all categories in *manifest*."""
    entries: list[dict] = []
    for category_payload in manifest.get("categories", {}).values():
        for entry in category_payload.get("files", []):
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Temp directory creation
# ---------------------------------------------------------------------------


def _make_tempdir() -> Path:
    """Create and return a temp directory under cwd/tmp/.

    Name pattern: ``verify-<YYYY-MM-DDTHH-MM-SS>-<6-char-random>``
    """
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H-%M-%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    name = f"verify-{ts}-{rand}"
    tmp_root = Path.cwd() / "tmp"
    tmp_root.mkdir(exist_ok=True)
    tempdir = tmp_root / name
    tempdir.mkdir(parents=True, exist_ok=False)
    log_info("Created temp directory for sandbox restore.")
    log_debug(f"Tempdir: {tempdir}")
    return tempdir


# ---------------------------------------------------------------------------
# Child process invocation
# ---------------------------------------------------------------------------


def _run_restore_plan(snapshot: Path, tempdir: Path) -> None:
    """Invoke restore-plan.py; raise _FatalError on non-zero exit."""
    plan_out = tempdir / "plan.json"
    cmd = [
        sys.executable,
        str(_THIS_DIR / "restore-plan.py"),
        "--snapshot", str(snapshot),
        "--target", str(tempdir),
        "--out", str(plan_out),
    ]
    log_info("Running restore-plan.py for sandbox.")
    env = {**os.environ, "REHYDRATE_TARGET": str(tempdir)}
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        log_error(f"restore-plan.py exited with code {result.returncode}")
        raise _FatalError(
            f"restore-plan.py failed with exit code {result.returncode}"
        )
    log_info("restore-plan.py completed successfully.")


def _run_restore_apply(snapshot: Path, tempdir: Path) -> None:
    """Invoke restore-apply.py; raise _FatalError on non-zero exit."""
    plan_path = tempdir / "plan.json"
    cmd = [
        sys.executable,
        str(_THIS_DIR / "restore-apply.py"),
        "--plan", str(plan_path),
        "--snapshot", str(snapshot),
        "--target", str(tempdir),
        "--overwrite",
        # No --live — tempdir is never live $HOME.
    ]
    log_info("Running restore-apply.py for sandbox.")
    env = {**os.environ, "REHYDRATE_TARGET": str(tempdir)}
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        log_error(f"restore-apply.py exited with code {result.returncode}")
        raise _FatalError(
            f"restore-apply.py failed with exit code {result.returncode}"
        )
    log_info("restore-apply.py completed successfully.")


# ---------------------------------------------------------------------------
# Verification checks
# ---------------------------------------------------------------------------


def _check_entry_manifest_vs_restored(
    entry: dict,
    tempdir: Path,
    failures: list[dict],
) -> str:
    """Check restored file hash against the manifest hash.

    Returns one of: 'pass', 'fail', 'skipped'.
    """
    rel_path: str = entry["path"]
    expected_hash: str = entry["object_hash"]
    is_symlink: bool = entry.get("is_symlink", False)
    symlink_target: str | None = entry.get("symlink_target")

    restored_path = tempdir / rel_path

    # Missing from restored tree.
    if not restored_path.exists() and not restored_path.is_symlink():
        failures.append({
            "path": rel_path,
            "kind": "missing",
            "expected_hash": expected_hash,
            "actual_hash": None,
        })
        log_warn("Restored file missing.")
        return "fail"

    if is_symlink:
        if not restored_path.is_symlink():
            # Expected a symlink but got a regular file.
            try:
                actual_hash = _sha256_file(restored_path)
            except OSError:
                actual_hash = None
            failures.append({
                "path": rel_path,
                "kind": "symlink-mismatch",
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            })
            log_warn("Expected symlink but found regular file.")
            return "fail"

        # Compare hash of the link target string.
        actual_target = os.readlink(restored_path)
        actual_hash = _sha256_string(actual_target)
        if actual_hash != expected_hash:
            failures.append({
                "path": rel_path,
                "kind": "symlink-mismatch",
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            })
            log_warn("Symlink target hash mismatch for restored file.")
            return "fail"

        # Also confirm the recorded symlink_target string matches.
        if symlink_target is not None and actual_target != symlink_target:
            failures.append({
                "path": rel_path,
                "kind": "symlink-mismatch",
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            })
            log_warn("Symlink target string mismatch for restored file.")
            return "fail"

        return "pass"

    # Regular file.
    try:
        actual_hash = _sha256_file(restored_path)
    except OSError as exc:
        log_error(f"Cannot read restored file for hashing: {exc}")
        failures.append({
            "path": rel_path,
            "kind": "manifest-mismatch",
            "expected_hash": expected_hash,
            "actual_hash": None,
        })
        return "fail"

    if actual_hash != expected_hash:
        failures.append({
            "path": rel_path,
            "kind": "manifest-mismatch",
            "expected_hash": expected_hash,
            "actual_hash": actual_hash,
        })
        log_warn("Hash mismatch for restored file vs manifest.")
        return "fail"

    return "pass"


def _check_entry_source_vs_manifest(
    entry: dict,
    source_home: Path,
    failures: list[dict],
) -> str:
    """Check source file hash against the manifest hash.

    Returns one of: 'pass', 'fail', 'source-missing' (treated as skipped).
    """
    rel_path: str = entry["path"]
    expected_hash: str = entry["object_hash"]
    is_symlink: bool = entry.get("is_symlink", False)

    source_path = source_home / rel_path

    # Source file no longer exists — record as source-missing, not a failure.
    if not source_path.exists() and not source_path.is_symlink():
        log_debug("Source file not present; skipping source check.")
        return "source-missing"

    if is_symlink:
        if not source_path.is_symlink():
            # In source it's a regular file but manifest says symlink.
            try:
                actual_hash = _sha256_file(source_path)
            except OSError:
                actual_hash = None
            failures.append({
                "path": rel_path,
                "kind": "source-mismatch",
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            })
            log_warn("Source-vs-manifest: expected symlink but found regular file.")
            return "fail"

        actual_target = os.readlink(source_path)
        actual_hash = _sha256_string(actual_target)
        if actual_hash != expected_hash:
            failures.append({
                "path": rel_path,
                "kind": "source-mismatch",
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            })
            log_warn("Source-vs-manifest: symlink target hash mismatch.")
            return "fail"
        return "pass"

    # Regular file.
    try:
        actual_hash = _sha256_file(source_path)
    except OSError as exc:
        log_error(f"Cannot read source file for hashing: {exc}")
        return "source-missing"

    if actual_hash != expected_hash:
        failures.append({
            "path": rel_path,
            "kind": "source-mismatch",
            "expected_hash": expected_hash,
            "actual_hash": actual_hash,
        })
        log_warn("Source-vs-manifest: hash mismatch.")
        return "fail"

    return "pass"


def _collect_extras(tempdir: Path, manifest_paths: set[str]) -> list[str]:
    """Walk *tempdir* and return paths that are NOT in *manifest_paths*.

    Skips `plan.json` which is written by verify-sandbox.py itself.
    """
    extras: list[str] = []
    for root, dirs, files in os.walk(tempdir):
        # Also check symlinks (os.walk won't list them in files by default
        # when followlinks=False, but they appear as files when they're leaf nodes).
        root_path = Path(root)
        # Collect both regular files and symlinks.
        candidates = []
        for fname in files:
            candidates.append(root_path / fname)
        # Check for symlinks in dirs (os.walk with followlinks=False omits them
        # from files but lists dangling symlinks in files; cover both).
        for dname in list(dirs):
            d = root_path / dname
            if d.is_symlink():
                candidates.append(d)
                dirs.remove(dname)  # Don't descend into symlinked dirs.

        for fpath in candidates:
            try:
                rel = str(fpath.relative_to(tempdir))
            except ValueError:
                continue
            # Ignore the plan.json file written by verify-sandbox.py itself.
            if rel == "plan.json":
                continue
            if rel not in manifest_paths:
                extras.append(rel)
                log_warn("Extra file in restore target not in manifest.")

    return sorted(extras)


# ---------------------------------------------------------------------------
# Main verification logic
# ---------------------------------------------------------------------------


def _verify(
    snapshot: Path,
    source_home: Path,
    tempdir: Path,
    *,
    no_source_check: bool,
) -> dict:
    """Run the verification and return the report dict."""
    # Load the manifest.
    manifest_path = snapshot / MANIFEST_FILENAME
    log_info("Loading manifest.")
    manifest = _load_json(manifest_path)
    entries = _collect_file_entries(manifest)

    log_count("manifest-entries", len(entries))

    failures: list[dict] = []
    passed = 0
    failed_count = 0
    skipped = 0

    for entry in entries:
        log_debug("Checking entry.")

        # --- Manifest-vs-restored check (always runs) ---
        manifest_result = _check_entry_manifest_vs_restored(entry, tempdir, failures)

        if manifest_result == "fail":
            failed_count += 1
            # Still attempt source check if enabled, using the same failure
            # list (source-mismatch would be a separate entry).
        elif manifest_result == "pass":
            # --- Source-vs-manifest check (optional) ---
            if no_source_check:
                passed += 1
            else:
                source_result = _check_entry_source_vs_manifest(
                    entry, source_home, failures
                )
                if source_result == "fail":
                    failed_count += 1
                elif source_result == "source-missing":
                    skipped += 1
                    passed += 1  # Manifest check passed; source absence is noted
                else:
                    passed += 1
        # If manifest_result is 'missing' etc. already counted as failed above.

    # Extras check.
    manifest_paths = {e["path"] for e in entries}
    extras = _collect_extras(tempdir, manifest_paths)
    if extras:
        log_warn("Found extra files in restore target not listed in manifest.")

    total = len(entries)
    log_count("pass", passed)
    log_count("fail", failed_count)
    log_count("skipped", skipped)

    report = {
        "verify_version": VERIFY_VERSION,
        "snapshot": str(snapshot),
        "target_tempdir": str(tempdir),
        "checked_at": datetime.datetime.now(tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "total_files": total,
        "pass": passed,
        "fail": failed_count,
        "skipped": skipped,
        "failures": failures,
        "extras_in_target": extras,
    }
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Restore a snapshot into a temp directory and verify every "
            "restored file matches the manifest's recorded hash. "
            "Optionally compare against the original source files too."
        )
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        metavar="PATH",
        help="Path to a snapshot directory containing manifest.json.",
    )
    parser.add_argument(
        "--source-home",
        default=None,
        metavar="PATH",
        help=(
            "Directory the snapshot was originally captured from. "
            "Defaults to $HOME. Used for the source-vs-manifest byte check."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Write the verification report JSON to this file instead of stdout.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        default=False,
        help="Do NOT remove the temp directory after verification (useful for debugging).",
    )
    parser.add_argument(
        "--no-source-check",
        action="store_true",
        default=False,
        dest="no_source_check",
        help=(
            "Skip the source-vs-manifest byte check. "
            "Useful when verifying a snapshot on a different machine."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns an exit code."""
    args = _parse_args(argv)

    snapshot = Path(args.snapshot).expanduser().resolve()
    if not snapshot.is_dir():
        log_error("Snapshot directory not found.", path=snapshot)
        return 1

    if not (snapshot / MANIFEST_FILENAME).exists():
        log_error("manifest.json not found in snapshot directory.", path=snapshot)
        return 1

    source_home_str = args.source_home or os.environ.get("HOME", "")
    if not source_home_str:
        log_error("Cannot determine source home; pass --source-home.")
        return 1
    source_home = Path(source_home_str).expanduser().resolve()

    # Create the sandbox temp directory.
    try:
        tempdir = _make_tempdir()
    except OSError as exc:
        log_error(f"Failed to create temp directory: {exc}")
        return 1

    report: dict | None = None
    exit_code = 0

    try:
        # Step 1 — restore-plan.
        try:
            _run_restore_plan(snapshot, tempdir)
        except _FatalError as exc:
            log_error(f"restore-plan step failed: {exc}")
            exit_code = 1
            # Still produce a partial report below.
            report = {
                "verify_version": VERIFY_VERSION,
                "snapshot": str(snapshot),
                "target_tempdir": str(tempdir),
                "checked_at": datetime.datetime.now(
                    tz=datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "total_files": 0,
                "pass": 0,
                "fail": 1,
                "skipped": 0,
                "failures": [{"path": "__restore_plan__", "kind": "missing",
                               "expected_hash": None, "actual_hash": None}],
                "extras_in_target": [],
                "error": str(exc),
            }

        # Step 2 — restore-apply.
        # Even on partial failure we continue to verification so per-file
        # checks can produce accurate manifest-mismatch / missing records.
        if exit_code == 0:
            try:
                _run_restore_apply(snapshot, tempdir)
            except _FatalError as exc:
                log_error(f"restore-apply step failed: {exc}")
                log_warn(
                    "restore-apply exited non-zero; continuing to verification "
                    "to record per-file divergences."
                )
                exit_code = 1
                # Don't short-circuit — fall through to verification below.

        # Step 3 — verification checks (always run if restore-plan succeeded).
        if report is None:
            try:
                report = _verify(
                    snapshot,
                    source_home,
                    tempdir,
                    no_source_check=args.no_source_check,
                )
                if report["fail"] > 0 or report["extras_in_target"]:
                    exit_code = 1
            except _FatalError as exc:
                log_error(f"Verification step failed: {exc}")
                exit_code = 1
                report = {
                    "verify_version": VERIFY_VERSION,
                    "snapshot": str(snapshot),
                    "target_tempdir": str(tempdir),
                    "checked_at": datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "total_files": 0,
                    "pass": 0,
                    "fail": 1,
                    "skipped": 0,
                    "failures": [],
                    "extras_in_target": [],
                    "error": str(exc),
                }

        # Output the report.
        if report is not None:
            report_json = json.dumps(report, indent=2, ensure_ascii=False)
            if args.out:
                out_path = Path(args.out)
                out_path.write_text(report_json + "\n", encoding="utf-8")
                log_info("Verification report written.")
            else:
                print(report_json)

        if exit_code == 0:
            log_info("Verification passed.")
        else:
            log_warn("Verification finished with failures.")

        return exit_code

    finally:
        if not args.keep:
            try:
                shutil.rmtree(tempdir)
                log_info("Temp directory cleaned up.")
            except OSError as exc:
                log_warn(f"Could not clean up temp directory: {exc}")
        else:
            log_info("--keep set; temp directory preserved.")
            log_debug(f"Preserved temp directory: {tempdir}")


if __name__ == "__main__":
    sys.exit(main())
