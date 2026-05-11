"""
Tests for scripts/restore-plan.py

Coverage:
  - Action types: create, skip-identical, overwrite-needs-confirm
  - Drift detection: OS version, hostname, user, architecture mismatches
  - Read-only contract: running the plan must not modify the target directory
  - Schema: emitted plan must validate against schemas/restore-plan.schema.json
  - Refusal: missing --target (and no $REHYDRATE_TARGET) → non-zero exit
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

import importlib.util as _ilu  # noqa: E402

# restore-plan.py has a hyphen in its name, which is not a valid Python
# identifier, so we load it explicitly rather than via the normal import system.
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "restore-plan.py"
_spec = _ilu.spec_from_file_location("restore_plan", _SCRIPT_PATH)
rp = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(rp)  # type: ignore[union-attr]

RESTORE_PLAN_SCHEMA = _REPO_ROOT / "schemas" / "restore-plan.schema.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_manifest(
    files: list[dict],
    *,
    snapshot_id: str = "test-snapshot-001",
    os_version: str = "26.0",
    hostname: str = "test-host.local",
    user: str = "testuser",
    arch: str = "arm64",
) -> dict:
    """Build a minimal valid manifest dict."""
    return {
        "schema_version": "0.1.0",
        "created_at": "2026-05-11T12:00:00Z",
        "snapshot_id": snapshot_id,
        "parent_snapshot": None,
        "source_machine": {
            "os": "macOS",
            "os_version": os_version,
            "build": "25A0000",
            "hostname": hostname,
            "user": user,
            "hardware": {
                "arch": arch,
                "model": "MacBookPro18,3",
                "memory_bytes": 17179869184,
            },
            "shell": "/bin/zsh",
            "path": ["/usr/bin", "/bin"],
        },
        "categories": {
            "dotfiles": {
                "strategy": "file-list",
                "files": files,
            }
        },
    }


def _file_entry(
    path: str,
    content: bytes,
    *,
    mode: str = "0644",
    is_symlink: bool = False,
    symlink_target: str | None = None,
) -> dict:
    """Build a manifest file_entry dict from raw content bytes."""
    object_hash = (
        _sha256_str(symlink_target) if is_symlink and symlink_target
        else _sha256_bytes(content)
    )
    return {
        "path": path,
        "object_hash": object_hash,
        "mode": mode,
        "mtime": "2026-05-01T10:00:00Z",
        "size": len(content),
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
    }


# ---------------------------------------------------------------------------
# Snapshot directory fixture builder
# ---------------------------------------------------------------------------

class SnapshotFixture:
    """Context manager that creates a temp snapshot dir and target dir."""

    def __init__(self, manifest: dict):
        self.manifest = manifest
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self.snapshot_dir: Path | None = None
        self.target_dir: Path | None = None

    def __enter__(self) -> "SnapshotFixture":
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.snapshot_dir = base / "snapshot"
        self.snapshot_dir.mkdir()
        (self.snapshot_dir / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        self.target_dir = base / "target"
        self.target_dir.mkdir()
        return self

    def write_target_file(self, rel_path: str, content: bytes) -> Path:
        """Write a regular file to the target directory."""
        full = self.target_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)
        return full

    def write_target_symlink(self, rel_path: str, link_target: str) -> Path:
        """Create a symlink in the target directory."""
        full = self.target_dir / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        if full.is_symlink() or full.exists():
            full.unlink()
        os.symlink(link_target, full)
        return full

    def run_plan(self, extra_env: dict | None = None) -> dict:
        """Run restore-plan main() and return the parsed plan."""
        env_overrides = {
            "REHYDRATE_TARGET": "",
            "USER": "testuser",
        }
        if extra_env:
            env_overrides.update(extra_env)

        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        ) as out_fh:
            out_path = out_fh.name

        try:
            with patch.dict(os.environ, env_overrides, clear=False):
                rc = rp.main([
                    "--snapshot", str(self.snapshot_dir),
                    "--target", str(self.target_dir),
                    "--out", out_path,
                ])
            assert rc == 0, f"restore-plan exited with {rc}"
            return json.loads(Path(out_path).read_text(encoding="utf-8"))
        finally:
            try:
                os.unlink(out_path)
            except FileNotFoundError:
                pass

    def __exit__(self, *_):
        if self._tmpdir:
            self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Tests: action types
# ---------------------------------------------------------------------------

class TestActionTypes(unittest.TestCase):
    """Three-file fixture: create, skip-identical, overwrite-needs-confirm."""

    CREATE_CONTENT = b"this file will be created"
    IDENTICAL_CONTENT = b"this file is already correct"
    EXPECTED_CONTENT = b"this is the expected content"
    DIVERGED_CONTENT = b"this is the wrong content on disk"

    def _build_manifest_and_fixture(self):
        create_entry = _file_entry(".newfile", self.CREATE_CONTENT)
        identical_entry = _file_entry(".samedata", self.IDENTICAL_CONTENT)
        diverged_entry = _file_entry(".changed", self.EXPECTED_CONTENT)
        manifest = _make_manifest([create_entry, identical_entry, diverged_entry])
        return manifest, create_entry, identical_entry, diverged_entry

    def test_create_action(self):
        manifest, _, _, _ = self._build_manifest_and_fixture()
        with SnapshotFixture(manifest) as fix:
            # Do NOT write .newfile to target — it should be missing.
            fix.write_target_file(".samedata", self.IDENTICAL_CONTENT)
            fix.write_target_file(".changed", self.DIVERGED_CONTENT)
            plan = fix.run_plan()

        actions = {a["path"]: a for a in plan["actions_by_category"]["dotfiles"]}
        self.assertEqual(actions[".newfile"]["type"], "create")
        self.assertIn("object_hash", actions[".newfile"])
        self.assertEqual(
            actions[".newfile"]["object_hash"],
            _sha256_bytes(self.CREATE_CONTENT),
        )

    def test_skip_identical_action(self):
        manifest, _, _, _ = self._build_manifest_and_fixture()
        with SnapshotFixture(manifest) as fix:
            fix.write_target_file(".samedata", self.IDENTICAL_CONTENT)
            fix.write_target_file(".changed", self.DIVERGED_CONTENT)
            plan = fix.run_plan()

        actions = {a["path"]: a for a in plan["actions_by_category"]["dotfiles"]}
        self.assertEqual(actions[".samedata"]["type"], "skip-identical")

    def test_overwrite_needs_confirm_action(self):
        manifest, _, _, _ = self._build_manifest_and_fixture()
        with SnapshotFixture(manifest) as fix:
            fix.write_target_file(".samedata", self.IDENTICAL_CONTENT)
            fix.write_target_file(".changed", self.DIVERGED_CONTENT)
            plan = fix.run_plan()

        actions = {a["path"]: a for a in plan["actions_by_category"]["dotfiles"]}
        entry = actions[".changed"]
        self.assertEqual(entry["type"], "overwrite-needs-confirm")
        self.assertEqual(entry["current_hash"], _sha256_bytes(self.DIVERGED_CONTENT))
        self.assertEqual(entry["expected_hash"], _sha256_bytes(self.EXPECTED_CONTENT))

    def test_summary_counts(self):
        manifest, _, _, _ = self._build_manifest_and_fixture()
        with SnapshotFixture(manifest) as fix:
            fix.write_target_file(".samedata", self.IDENTICAL_CONTENT)
            fix.write_target_file(".changed", self.DIVERGED_CONTENT)
            plan = fix.run_plan()

        summary = plan["summary"]
        self.assertEqual(summary["create"], 1)
        self.assertEqual(summary["skip-identical"], 1)
        self.assertEqual(summary["overwrite-needs-confirm"], 1)
        self.assertEqual(summary["total"], 3)


# ---------------------------------------------------------------------------
# Tests: symlink handling
# ---------------------------------------------------------------------------

class TestSymlinkActions(unittest.TestCase):
    LINK_TARGET = "/opt/homebrew/bin/python3"

    def _symlink_entry(self) -> dict:
        return _file_entry(
            ".local/bin/python3",
            b"",
            mode="0777",
            is_symlink=True,
            symlink_target=self.LINK_TARGET,
        )

    def test_symlink_missing_is_create(self):
        manifest = _make_manifest([self._symlink_entry()])
        with SnapshotFixture(manifest) as fix:
            plan = fix.run_plan()
        actions = plan["actions_by_category"]["dotfiles"]
        self.assertEqual(actions[0]["type"], "create")

    def test_symlink_identical_is_skip(self):
        manifest = _make_manifest([self._symlink_entry()])
        with SnapshotFixture(manifest) as fix:
            fix.write_target_symlink(".local/bin/python3", self.LINK_TARGET)
            plan = fix.run_plan()
        actions = plan["actions_by_category"]["dotfiles"]
        self.assertEqual(actions[0]["type"], "skip-identical")

    def test_symlink_different_target_is_overwrite(self):
        manifest = _make_manifest([self._symlink_entry()])
        different_target = "/usr/bin/python3"
        with SnapshotFixture(manifest) as fix:
            fix.write_target_symlink(".local/bin/python3", different_target)
            plan = fix.run_plan()
        actions = plan["actions_by_category"]["dotfiles"]
        entry = actions[0]
        self.assertEqual(entry["type"], "overwrite-needs-confirm")
        self.assertEqual(entry["current_hash"], _sha256_str(different_target))
        self.assertEqual(entry["expected_hash"], _sha256_str(self.LINK_TARGET))

    def test_regular_file_where_symlink_expected_is_overwrite(self):
        manifest = _make_manifest([self._symlink_entry()])
        with SnapshotFixture(manifest) as fix:
            fix.write_target_file(".local/bin/python3", b"#!/usr/bin/env python3\n")
            plan = fix.run_plan()
        actions = plan["actions_by_category"]["dotfiles"]
        self.assertEqual(actions[0]["type"], "overwrite-needs-confirm")


# ---------------------------------------------------------------------------
# Tests: drift detection
# ---------------------------------------------------------------------------

class TestDriftDetection(unittest.TestCase):

    def _run_with_machine_overrides(
        self,
        manifest: dict,
        *,
        mac_ver: str = "26.0",
        hostname: str = "test-host.local",
        user: str = "testuser",
        arch: str = "arm64",
    ) -> list[dict]:
        # Patch attributes on the module directly since the module is loaded
        # via importlib (hyphenated filename) and is not in sys.modules under a
        # dotted package path.
        with SnapshotFixture(manifest) as fix:
            with (
                patch.object(rp.platform, "mac_ver",
                             return_value=(mac_ver, "", "")),
                patch.object(rp.socket, "gethostname",
                             return_value=hostname),
                patch.object(rp.platform, "machine",
                             return_value=arch),
            ):
                plan = fix.run_plan(extra_env={"USER": user})
        return plan["drift"]

    def test_no_drift_when_machines_match(self):
        manifest = _make_manifest(
            [],
            os_version="26.0",
            hostname="test-host.local",
            user="testuser",
            arch="arm64",
        )
        drift = self._run_with_machine_overrides(
            manifest,
            mac_ver="26.0",
            hostname="test-host.local",
            user="testuser",
            arch="arm64",
        )
        self.assertEqual(drift, [])

    def test_os_version_mismatch_detected(self):
        manifest = _make_manifest([], os_version="26.0")
        drift = self._run_with_machine_overrides(manifest, mac_ver="26.1")
        kinds = [d["kind"] for d in drift]
        self.assertIn("os_version_mismatch", kinds)
        item = next(d for d in drift if d["kind"] == "os_version_mismatch")
        self.assertEqual(item["source"], "26.0")
        self.assertEqual(item["current"], "26.1")

    def test_hostname_mismatch_detected(self):
        manifest = _make_manifest([], hostname="source-host.local")
        drift = self._run_with_machine_overrides(
            manifest, hostname="restore-host.local"
        )
        kinds = [d["kind"] for d in drift]
        self.assertIn("hostname_mismatch", kinds)

    def test_user_mismatch_detected(self):
        manifest = _make_manifest([], user="original-user")
        drift = self._run_with_machine_overrides(manifest, user="new-user")
        kinds = [d["kind"] for d in drift]
        self.assertIn("user_mismatch", kinds)
        item = next(d for d in drift if d["kind"] == "user_mismatch")
        self.assertEqual(item["source"], "original-user")
        self.assertEqual(item["current"], "new-user")

    def test_arch_mismatch_detected(self):
        manifest = _make_manifest([], arch="arm64")
        drift = self._run_with_machine_overrides(manifest, arch="x86_64")
        kinds = [d["kind"] for d in drift]
        self.assertIn("arch_mismatch", kinds)
        item = next(d for d in drift if d["kind"] == "arch_mismatch")
        self.assertEqual(item["source"], "arm64")
        self.assertEqual(item["current"], "x86_64")

    def test_multiple_drifts_all_reported(self):
        manifest = _make_manifest(
            [],
            os_version="26.0",
            hostname="source-host.local",
            user="source-user",
            arch="arm64",
        )
        drift = self._run_with_machine_overrides(
            manifest,
            mac_ver="26.1",
            hostname="restore-host.local",
            user="restore-user",
            arch="x86_64",
        )
        kinds = {d["kind"] for d in drift}
        self.assertIn("os_version_mismatch", kinds)
        self.assertIn("hostname_mismatch", kinds)
        self.assertIn("user_mismatch", kinds)
        self.assertIn("arch_mismatch", kinds)

    def test_drift_severity_values_are_valid(self):
        manifest = _make_manifest(
            [],
            os_version="26.0",
            hostname="src.local",
            user="src-user",
            arch="arm64",
        )
        drift = self._run_with_machine_overrides(
            manifest,
            mac_ver="26.1",
            hostname="dst.local",
            user="dst-user",
            arch="x86_64",
        )
        valid_severities = {"info", "warn", "error"}
        for item in drift:
            self.assertIn(item["severity"], valid_severities)


# ---------------------------------------------------------------------------
# Tests: read-only contract
# ---------------------------------------------------------------------------

class TestReadOnlyContract(unittest.TestCase):
    """Running the plan must not modify the target directory tree."""

    def _snapshot_tree(self, root: Path) -> dict[str, tuple[float, bytes | str]]:
        """Return {relative_path: (mtime, content_or_link_target)} for every
        path under *root*, following no symlinks."""
        result: dict[str, tuple[float, bytes | str]] = {}
        for item in sorted(root.rglob("*")):
            rel = str(item.relative_to(root))
            if item.is_symlink():
                result[rel] = (item.lstat().st_mtime, os.readlink(item))
            elif item.is_file():
                result[rel] = (item.stat().st_mtime, item.read_bytes())
        return result

    def test_plan_does_not_modify_target(self):
        content = b"untouched content"
        manifest = _make_manifest([
            _file_entry(".untouched", content),
            _file_entry(".different", b"expected"),
        ])
        with SnapshotFixture(manifest) as fix:
            # Write a file that would be "overwrite-needs-confirm".
            fix.write_target_file(".different", b"current")
            # Snapshot tree state before.
            before = self._snapshot_tree(fix.target_dir)
            fix.run_plan()
            # Snapshot tree state after.
            after = self._snapshot_tree(fix.target_dir)

        self.assertEqual(
            before, after,
            "restore-plan.py modified the target directory — "
            "read-only contract violated."
        )


# ---------------------------------------------------------------------------
# Tests: schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation(unittest.TestCase):
    """Emitted plan must validate against schemas/restore-plan.schema.json."""

    def test_plan_validates_against_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")

        schema = json.loads(RESTORE_PLAN_SCHEMA.read_text(encoding="utf-8"))
        manifest = _make_manifest([
            _file_entry(".zshrc", b"# zsh config"),
            _file_entry(".gitconfig", b"[user]\n  name = test"),
        ])

        with SnapshotFixture(manifest) as fix:
            fix.write_target_file(".gitconfig", b"[user]\n  name = test")
            plan = fix.run_plan()

        jsonschema.validate(plan, schema)  # Raises if invalid.

    def test_sample_plan_validates_against_schema(self):
        """The checked-in sample-restore-plan.json must also be valid."""
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")

        schema = json.loads(RESTORE_PLAN_SCHEMA.read_text(encoding="utf-8"))
        sample = json.loads(
            (_REPO_ROOT / "examples" / "sample-restore-plan.json")
            .read_text(encoding="utf-8")
        )
        jsonschema.validate(sample, schema)


# ---------------------------------------------------------------------------
# Tests: CLI refusal
# ---------------------------------------------------------------------------

class TestCLIRefusal(unittest.TestCase):
    """Missing --target with no $REHYDRATE_TARGET → non-zero exit."""

    def test_missing_target_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal snapshot dir.
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            manifest = _make_manifest([])
            (snap_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            env = dict(os.environ)
            env.pop("REHYDRATE_TARGET", None)

            with patch.dict(os.environ, env, clear=True):
                rc = rp.main(["--snapshot", str(snap_dir)])

        self.assertNotEqual(rc, 0)

    def test_explicit_target_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            target_dir = Path(tmpdir) / "target"
            target_dir.mkdir()
            manifest = _make_manifest([])
            (snap_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            env = dict(os.environ)
            env.pop("REHYDRATE_TARGET", None)
            env["USER"] = "testuser"

            with patch.dict(os.environ, env, clear=True):
                rc = rp.main([
                    "--snapshot", str(snap_dir),
                    "--target", str(target_dir),
                ])

        self.assertEqual(rc, 0)

    def test_rehydrate_target_env_var_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            target_dir = Path(tmpdir) / "target"
            target_dir.mkdir()
            manifest = _make_manifest([])
            (snap_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            env = dict(os.environ)
            env["REHYDRATE_TARGET"] = str(target_dir)
            env["USER"] = "testuser"

            with patch.dict(os.environ, env, clear=True):
                rc = rp.main(["--snapshot", str(snap_dir)])

        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Tests: manifest validation refusal
# ---------------------------------------------------------------------------

class TestManifestValidation(unittest.TestCase):
    """Invalid manifests must be rejected with a non-zero exit."""

    def test_invalid_manifest_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            target_dir = Path(tmpdir) / "target"
            target_dir.mkdir()
            # Deliberately broken manifest.
            (snap_dir / "manifest.json").write_text(
                json.dumps({"not": "a valid manifest"}), encoding="utf-8"
            )

            rc = rp.main([
                "--snapshot", str(snap_dir),
                "--target", str(target_dir),
            ])

        self.assertNotEqual(rc, 0)

    def test_missing_manifest_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            target_dir = Path(tmpdir) / "target"
            target_dir.mkdir()
            # No manifest.json written.

            rc = rp.main([
                "--snapshot", str(snap_dir),
                "--target", str(target_dir),
            ])

        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
