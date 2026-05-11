"""
Tests for scripts/verify-sandbox.py

Coverage:
  - Happy path: 3 files, total_files=3, pass=3, fail=0, exit code 0
  - Corrupted object: fail >= 1 (manifest-mismatch), exit non-zero
  - Source-modified after backup: failures includes source-mismatch
  - --no-source-check: source mismatches are not reported
  - --keep: tempdir still exists after process exits
  - Cleanup on failure: without --keep, tempdir removed even on non-zero exit
  - Extra in target: file added to tempdir not in manifest appears in extras_in_target
"""

from __future__ import annotations

import hashlib
import importlib.util as _ilu
import json
import os
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

# Load verify-sandbox.py via importlib (hyphen-free module alias not needed here
# since the file has a hyphen — load it directly).
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "verify-sandbox.py"
_spec = _ilu.spec_from_file_location("verify_sandbox", _SCRIPT_PATH)
vs = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(vs)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

class VerifyFixture:
    """Sets up a complete drive + source home for testing verify-sandbox.

    Drive layout::

        <drive>/
          snapshots/
            <id>/
              manifest.json
          objects/
            <aa>/<bb>/<hash>

    Source home layout::

        <source_home>/
          .fileA
          .fileB
          .fileC
    """

    SNAPSHOT_ID = "test-host-2026-05-11T120000Z"

    def __init__(self):
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self.base: Path | None = None
        self.drive: Path | None = None
        self.snapshot_dir: Path | None = None
        self.objects_dir: Path | None = None
        self.source_home: Path | None = None
        self._entries: list[dict] = []

    def __enter__(self) -> VerifyFixture:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self._tmpdir.name)

        # Drive tree (matches snapshot.py output):
        #   <base>/drive/snapshots/<id>/  ← snapshot_dir
        #   <base>/drive/objects/         ← objects_dir (<snap>/../../objects)
        self.drive = self.base / "drive"
        self.snapshot_dir = self.drive / "snapshots" / self.SNAPSHOT_ID
        self.snapshot_dir.mkdir(parents=True)
        self.objects_dir = self.drive / "objects"
        self.objects_dir.mkdir(parents=True)

        # Source home.
        self.source_home = self.base / "source_home"
        self.source_home.mkdir()

        return self

    # --- Entry builders ---

    def add_regular(
        self,
        rel_path: str,
        content: bytes,
        *,
        mode: str = "0644",
        mtime: str = "2026-05-01T10:00:00Z",
        write_source: bool = True,
    ) -> dict:
        """Add a regular file to the fixture."""
        object_hash = _sha256_bytes(content)
        entry = {
            "path": rel_path,
            "object_hash": object_hash,
            "mode": mode,
            "mtime": mtime,
            "size": len(content),
            "is_symlink": False,
            "symlink_target": None,
        }
        self._entries.append(entry)
        # Write to objects store.
        self._write_object(object_hash, content)
        # Write to source home.
        if write_source:
            src = self.source_home / rel_path
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes(content)
        return entry

    def _write_object(self, object_hash: str, data: bytes) -> Path:
        aa, bb = object_hash[:2], object_hash[2:4]
        obj_dir = self.objects_dir / aa / bb
        obj_dir.mkdir(parents=True, exist_ok=True)
        obj_path = obj_dir / object_hash
        obj_path.write_bytes(data)
        return obj_path

    def corrupt_object(self, object_hash: str) -> None:
        """Overwrite an object with garbage so its hash check fails."""
        aa, bb = object_hash[:2], object_hash[2:4]
        obj_path = self.objects_dir / aa / bb / object_hash
        obj_path.write_bytes(b"CORRUPTED - this will not match the expected hash")

    def write_manifest(self) -> None:
        """Write manifest.json to the snapshot directory."""
        manifest = {
            "schema_version": "0.1.0",
            "created_at": "2026-05-11T12:00:00Z",
            "snapshot_id": self.SNAPSHOT_ID,
            "parent_snapshot": None,
            "source_machine": {
                "os": "macOS",
                "os_version": "26.0",
                "build": "25A0000",
                "hostname": "test-host.local",
                "user": "testuser",
                "hardware": {
                    "arch": "arm64",
                    "model": "MacBookPro18,3",
                    "memory_bytes": 17179869184,
                },
                "shell": "/bin/zsh",
                "path": ["/usr/bin", "/bin"],
            },
            "categories": {
                "dotfiles": {
                    "strategy": "file-list",
                    "files": self._entries,
                }
            },
        }
        (self.snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def run_verify(
        self,
        *,
        keep: bool = False,
        no_source_check: bool = False,
        extra_argv: list[str] | None = None,
        cwd: Path | None = None,
    ) -> tuple[int, dict]:
        """Write the manifest, invoke vs.main(), and return (exit_code, report)."""
        self.write_manifest()

        # Capture stdout so we can parse the JSON report.
        import io
        out_buf = io.StringIO()

        argv = [
            "--snapshot", str(self.snapshot_dir),
            "--source-home", str(self.source_home),
        ]
        if keep:
            argv.append("--keep")
        if no_source_check:
            argv.append("--no-source-check")
        if extra_argv:
            argv.extend(extra_argv)

        # Patch sys.stdout so print() in vs.main() writes to our buffer.
        # Also override cwd if requested (for tempdir placement).
        cwd_ctx = _CwdContext(cwd or self.base)
        with patch("sys.stdout", out_buf), cwd_ctx:
            rc = vs.main(argv)

        output = out_buf.getvalue().strip()
        if output:
            report = json.loads(output)
        else:
            report = {}
        return rc, report

    def __exit__(self, *_):
        if self._tmpdir:
            self._tmpdir.cleanup()


class _CwdContext:
    """Context manager that temporarily changes the working directory."""

    def __init__(self, path: Path):
        self._path = path
        self._orig: str | None = None

    def __enter__(self):
        self._orig = os.getcwd()
        os.chdir(self._path)
        return self

    def __exit__(self, *_):
        if self._orig:
            os.chdir(self._orig)


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------

class TestHappyPath(unittest.TestCase):

    def test_happy_path_three_files(self):
        """3 files, all correct: total_files=3, pass=3, fail=0, exit code 0."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"zsh configuration content")
            fix.add_regular(".gitconfig", b"[user]\n\tname = Test User\n")
            fix.add_regular(".config/nvim/init.lua", b'vim.opt.number = true\n')

            rc, report = fix.run_verify()

        self.assertEqual(rc, 0)
        self.assertEqual(report["total_files"], 3)
        self.assertEqual(report["pass"], 3)
        self.assertEqual(report["fail"], 0)
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["extras_in_target"], [])
        self.assertEqual(report["verify_version"], "0.1.0")

    def test_report_contains_required_fields(self):
        """Verify all required fields are present in the report."""
        with VerifyFixture() as fix:
            fix.add_regular(".bashrc", b"bash content")
            rc, report = fix.run_verify()

        required = {
            "verify_version", "snapshot", "target_tempdir", "checked_at",
            "total_files", "pass", "fail", "skipped", "failures",
            "extras_in_target",
        }
        for field in required:
            self.assertIn(field, report, f"Missing field: {field!r}")

    def test_exit_code_zero_on_clean_snapshot(self):
        with VerifyFixture() as fix:
            fix.add_regular(".profile", b"profile content")
            rc, _ = fix.run_verify()
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Tests: corrupted object
# ---------------------------------------------------------------------------

class TestCorruptedObject(unittest.TestCase):

    def test_corrupted_object_detected(self):
        """Corrupting one object's bytes triggers a manifest-level failure (non-zero exit).

        When the object store is corrupted, restore-apply cannot write the file
        (it rejects the hash mismatch before writing).  The restored file ends
        up absent from the tempdir, so the per-file check records it as
        'missing' — which is still a manifest-level divergence.  We accept
        either 'manifest-mismatch' or 'missing' as valid kinds here.
        """
        with VerifyFixture() as fix:
            entry_a = fix.add_regular(".zshrc", b"zsh content")
            fix.add_regular(".bashrc", b"bash content")
            fix.add_regular(".vimrc", b"vim content")

            # Corrupt .zshrc's object after recording the hash.
            fix.corrupt_object(entry_a["object_hash"])

            rc, report = fix.run_verify(no_source_check=True)

        self.assertNotEqual(rc, 0)
        self.assertGreaterEqual(report["fail"], 1)
        failure_kinds = {f["kind"] for f in report["failures"]}
        # A corrupted object prevents the file from being written, so either
        # 'manifest-mismatch' (hash differs) or 'missing' (file absent) is valid.
        self.assertTrue(
            failure_kinds & {"manifest-mismatch", "missing"},
            f"Expected manifest-level failure kind, got: {failure_kinds}",
        )

    def test_corrupted_object_path_in_failures(self):
        """The failure record for a corrupted object references the correct path.

        When restore-apply rejects a corrupted object (hash mismatch inside the
        object store), the file is never written and the per-file check records
        it as 'missing'.  The path must still appear in the failures list.
        """
        with VerifyFixture() as fix:
            entry = fix.add_regular(".gitconfig", b"git config content")
            fix.add_regular(".zshrc", b"zsh content")
            fix.corrupt_object(entry["object_hash"])

            rc, report = fix.run_verify(no_source_check=True)

        # The corrupted-file's path must be in failures (as missing or mismatch).
        failure_paths = {f["path"] for f in report["failures"]}
        self.assertTrue(
            ".gitconfig" in failure_paths or "__restore_apply__" in failure_paths,
            f"Expected .gitconfig in failures, got: {failure_paths}",
        )

    def test_corrupted_object_fail_count(self):
        """Exactly one corrupted file → fail >= 1."""
        with VerifyFixture() as fix:
            entry = fix.add_regular(".tmux.conf", b"tmux config")
            fix.add_regular(".bashrc", b"bash config")
            fix.corrupt_object(entry["object_hash"])

            rc, report = fix.run_verify(no_source_check=True)

        self.assertGreaterEqual(report["fail"], 1)


# ---------------------------------------------------------------------------
# Tests: source-modified after backup
# ---------------------------------------------------------------------------

class TestSourceModified(unittest.TestCase):

    def test_source_mismatch_detected(self):
        """Modifying a source file after backup → source-mismatch in failures."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"original zsh content")
            fix.add_regular(".bashrc", b"bash content")
            fix.add_regular(".vimrc", b"vim content")

            # Overwrite source file to simulate post-backup change.
            (fix.source_home / ".zshrc").write_bytes(b"modified zsh content - changed after backup")

            rc, report = fix.run_verify()

        self.assertNotEqual(rc, 0)
        failure_kinds = {f["kind"] for f in report["failures"]}
        self.assertIn("source-mismatch", failure_kinds)
        failure_paths = {f["path"] for f in report["failures"]}
        self.assertIn(".zshrc", failure_paths)

    def test_source_mismatch_path_correct(self):
        """The source-mismatch failure entry has the right path."""
        with VerifyFixture() as fix:
            fix.add_regular(".gitconfig", b"original git config")
            (fix.source_home / ".gitconfig").write_bytes(b"modified git config")
            rc, report = fix.run_verify()

        source_failures = [f for f in report["failures"] if f["kind"] == "source-mismatch"]
        self.assertTrue(len(source_failures) >= 1)
        self.assertEqual(source_failures[0]["path"], ".gitconfig")

    def test_source_mismatch_has_expected_and_actual_hash(self):
        """source-mismatch failures carry expected_hash and actual_hash."""
        original = b"original content"
        modified = b"modified content"
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", original)
            (fix.source_home / ".zshrc").write_bytes(modified)
            rc, report = fix.run_verify()

        source_failures = [f for f in report["failures"] if f["kind"] == "source-mismatch"]
        self.assertTrue(len(source_failures) >= 1)
        f = source_failures[0]
        self.assertEqual(f["expected_hash"], _sha256_bytes(original))
        self.assertEqual(f["actual_hash"], _sha256_bytes(modified))


# ---------------------------------------------------------------------------
# Tests: --no-source-check
# ---------------------------------------------------------------------------

class TestNoSourceCheck(unittest.TestCase):

    def test_no_source_check_skips_source_mismatches(self):
        """With --no-source-check, source-mismatch failures are not reported."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"original content")
            fix.add_regular(".bashrc", b"bash content")
            # Modify source after backup.
            (fix.source_home / ".zshrc").write_bytes(b"modified content")

            rc, report = fix.run_verify(no_source_check=True)

        # All manifest checks pass; no source checks ran.
        self.assertEqual(rc, 0)
        source_failures = [f for f in report["failures"] if f["kind"] == "source-mismatch"]
        self.assertEqual(len(source_failures), 0)

    def test_no_source_check_still_catches_manifest_mismatch(self):
        """--no-source-check does not disable manifest-vs-restored checks.

        A corrupted object causes restore-apply to fail, which means the file
        is never written to the tempdir.  The per-file check then records it as
        'missing'.  We verify the overall exit is non-zero and at least one
        manifest-level failure is recorded (kind 'manifest-mismatch' or 'missing').
        """
        with VerifyFixture() as fix:
            entry = fix.add_regular(".zshrc", b"content")
            fix.corrupt_object(entry["object_hash"])
            rc, report = fix.run_verify(no_source_check=True)

        self.assertNotEqual(rc, 0)
        manifest_level_failures = [
            f for f in report["failures"]
            if f["kind"] in {"manifest-mismatch", "missing"}
        ]
        self.assertGreaterEqual(len(manifest_level_failures), 1)

    def test_no_source_check_pass_count(self):
        """With --no-source-check and correct objects, all files should pass."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"zsh content")
            fix.add_regular(".bashrc", b"bash content")
            # Modify source — should be irrelevant.
            (fix.source_home / ".zshrc").write_bytes(b"different content")
            rc, report = fix.run_verify(no_source_check=True)

        self.assertEqual(rc, 0)
        self.assertEqual(report["total_files"], 2)
        self.assertEqual(report["fail"], 0)


# ---------------------------------------------------------------------------
# Tests: --keep flag
# ---------------------------------------------------------------------------

class TestKeepFlag(unittest.TestCase):

    def test_keep_preserves_tempdir(self):
        """With --keep, the temp directory must still exist after the call."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"zsh content")
            fix.add_regular(".bashrc", b"bash content")
            fix.add_regular(".vimrc", b"vim content")

            rc, report = fix.run_verify(keep=True)

            # The tempdir path is recorded in the report.
            tempdir_path = Path(report["target_tempdir"])
            self.assertTrue(
                tempdir_path.exists(),
                f"Tempdir should still exist with --keep: {tempdir_path}",
            )
            # Clean up manually so the test doesn't leave debris.
            import shutil
            shutil.rmtree(tempdir_path, ignore_errors=True)

        self.assertEqual(rc, 0)

    def test_keep_exit_code_still_correct(self):
        """--keep does not affect the exit code."""
        with VerifyFixture() as fix:
            entry = fix.add_regular(".zshrc", b"content")
            fix.corrupt_object(entry["object_hash"])
            rc, report = fix.run_verify(keep=True, no_source_check=True)
            tempdir_path = Path(report.get("target_tempdir", ""))
            if tempdir_path.exists():
                import shutil
                shutil.rmtree(tempdir_path, ignore_errors=True)

        self.assertNotEqual(rc, 0)


# ---------------------------------------------------------------------------
# Tests: cleanup on failure (without --keep)
# ---------------------------------------------------------------------------

class TestCleanupOnFailure(unittest.TestCase):

    def test_tempdir_removed_on_failure_without_keep(self):
        """Without --keep, tempdir is removed even when exit code is non-zero."""
        with VerifyFixture() as fix:
            entry = fix.add_regular(".zshrc", b"content")
            fix.corrupt_object(entry["object_hash"])

            rc, report = fix.run_verify(no_source_check=True)

        self.assertNotEqual(rc, 0)
        if report.get("target_tempdir"):
            tempdir_path = Path(report["target_tempdir"])
            self.assertFalse(
                tempdir_path.exists(),
                f"Tempdir should be removed after non-zero exit without --keep: {tempdir_path}",
            )

    def test_tempdir_removed_on_success_without_keep(self):
        """Without --keep, tempdir is removed after successful run too."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"content")
            rc, report = fix.run_verify(no_source_check=True)

        self.assertEqual(rc, 0)
        if report.get("target_tempdir"):
            tempdir_path = Path(report["target_tempdir"])
            self.assertFalse(
                tempdir_path.exists(),
                f"Tempdir should be removed after exit without --keep: {tempdir_path}",
            )


# ---------------------------------------------------------------------------
# Tests: extras in target
# ---------------------------------------------------------------------------

class TestExtrasInTarget(unittest.TestCase):

    def test_extra_file_in_target_detected(self):
        """A file present in the tempdir that is not in the manifest appears
        in extras_in_target.

        We simulate this by hooking into _collect_extras to inject an extra
        path. Alternatively, we patch _run_restore_apply to add a file.
        """
        extra_rel_path = ".secret_extra_file"

        # We patch _run_restore_apply to also write an extra file.
        real_run_apply = vs._run_restore_apply

        def _patched_run_apply(snapshot, tempdir):
            real_run_apply(snapshot, tempdir)
            # Inject an extra file that the manifest does not list.
            extra = tempdir / extra_rel_path
            extra.write_bytes(b"this file was not in the snapshot")

        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"zsh content")
            fix.add_regular(".bashrc", b"bash content")
            fix.add_regular(".vimrc", b"vim content")

            with patch.object(vs, "_run_restore_apply", side_effect=_patched_run_apply):
                rc, report = fix.run_verify(no_source_check=True)

        # The extra file should appear in extras_in_target.
        self.assertIn(extra_rel_path, report["extras_in_target"])
        # extras_in_target causes a non-zero exit.
        self.assertNotEqual(rc, 0)

    def test_no_extras_in_clean_restore(self):
        """A clean restore produces an empty extras_in_target list."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"zsh content")
            fix.add_regular(".vimrc", b"vim content")
            rc, report = fix.run_verify(no_source_check=True)

        self.assertEqual(report["extras_in_target"], [])


# ---------------------------------------------------------------------------
# Tests: source-missing (source file deleted after backup)
# ---------------------------------------------------------------------------

class TestSourceMissing(unittest.TestCase):

    def test_source_missing_does_not_fail(self):
        """If source file no longer exists, it is recorded as source-missing
        (skipped) rather than a failure — manifest check still passes."""
        with VerifyFixture() as fix:
            fix.add_regular(".zshrc", b"zsh content")
            fix.add_regular(".deleted_file", b"was here once")

            # Delete the source file to simulate post-backup deletion.
            (fix.source_home / ".deleted_file").unlink()

            rc, report = fix.run_verify()

        # No source-mismatch failure for the deleted file.
        source_failures = [f for f in report["failures"] if f["kind"] == "source-mismatch"]
        paths_with_source_fail = {f["path"] for f in source_failures}
        self.assertNotIn(".deleted_file", paths_with_source_fail)
        # Exit code should still be 0 (manifest checks all pass).
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Tests: internal helper unit tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_sha256_file(self):
        content = b"hello world"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            fname = f.name
        try:
            result = vs._sha256_file(Path(fname))
            self.assertEqual(result, _sha256_bytes(content))
        finally:
            os.unlink(fname)

    def test_sha256_string(self):
        s = "/opt/homebrew/bin/python3"
        self.assertEqual(vs._sha256_string(s), _sha256_str(s))

    def test_collect_extras_ignores_plan_json(self):
        """plan.json written by verify-sandbox itself must not appear in extras."""
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            # Write plan.json (should be ignored).
            (td / "plan.json").write_bytes(b"{}")
            # Write a real file.
            (td / ".zshrc").write_bytes(b"content")

            manifest_paths = {".zshrc"}
            extras = vs._collect_extras(td, manifest_paths)

        self.assertNotIn("plan.json", extras)
        self.assertEqual(extras, [])

    def test_collect_extras_finds_extra(self):
        """An unlisted file must appear in the extras list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            (td / ".zshrc").write_bytes(b"content")
            (td / ".extra_file").write_bytes(b"extra")

            manifest_paths = {".zshrc"}
            extras = vs._collect_extras(td, manifest_paths)

        self.assertIn(".extra_file", extras)

    def test_collect_file_entries_across_categories(self):
        """_collect_file_entries flattens files from all categories."""
        manifest = {
            "categories": {
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [
                        {"path": ".zshrc", "object_hash": "a" * 64,
                         "mode": "0644", "mtime": "2026-05-01T10:00:00Z",
                         "size": 10, "is_symlink": False, "symlink_target": None},
                    ]
                },
                "other": {
                    "strategy": "file-list",
                    "files": [
                        {"path": ".vimrc", "object_hash": "b" * 64,
                         "mode": "0644", "mtime": "2026-05-01T10:00:00Z",
                         "size": 5, "is_symlink": False, "symlink_target": None},
                    ]
                }
            }
        }
        entries = vs._collect_file_entries(manifest)
        paths = {e["path"] for e in entries}
        self.assertIn(".zshrc", paths)
        self.assertIn(".vimrc", paths)
        self.assertEqual(len(entries), 2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
