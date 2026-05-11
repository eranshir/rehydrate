"""
test_walk_packages.py — unit tests for scripts/walk-packages.py

Tests use unittest.mock.patch on subprocess.run and shutil.which so that
no real package managers need to be installed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the module under test.  It lives at scripts/walk-packages.py but is
# not a valid Python identifier as a module name; import it via importlib.
import importlib.util as _ilu

_WALKER_PATH = _REPO_ROOT / "scripts" / "walk-packages.py"
_spec = _ilu.spec_from_file_location("walk_packages", _WALKER_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

walk_packages = _mod.walk_packages
_run_manager = _mod._run_manager
_go_bin_listing = _mod._go_bin_listing
_MANAGER_META = _mod._MANAGER_META
PACKAGES_SUBDIR = _mod.PACKAGES_SUBDIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(stdout: bytes, returncode: int = 0) -> MagicMock:
    p = MagicMock()
    p.stdout = stdout
    p.returncode = returncode
    return p


def _all_which_found(binary: str) -> str | None:
    """Side-effect for shutil.which: all binaries found."""
    return f"/usr/bin/{binary}"


def _canned_output(manager: str) -> bytes:
    """Return a recognisable but innocuous canned byte string per manager."""
    return f"# canned output for {manager}\n".encode()


# ---------------------------------------------------------------------------
# TestEachManagerCaptured
# ---------------------------------------------------------------------------

class TestEachManagerCaptured(unittest.TestCase):
    """Each of the 6 managers with mocked subprocess → one file_entry each."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _run(self, managers: list[str]) -> dict:
        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", side_effect=self._fake_run):
            return walk_packages(managers=managers, home=self.workdir, workdir=self.workdir)

    def _fake_run(self, cmd, **kwargs):
        # Identify the manager from the command list
        for mgr in _MANAGER_META:
            binary, _ = _MANAGER_META[mgr]
            if binary in cmd[0] or any(binary in c for c in cmd):
                return _make_proc(_canned_output(mgr))
        return _make_proc(b"")

    def test_all_six_managers_emit_file_entries(self) -> None:
        managers = list(_MANAGER_META.keys())
        # go uses directory listing, not subprocess — create fake ~/go/bin
        go_bin = Path(self.workdir) / "go" / "bin"
        go_bin.mkdir(parents=True, exist_ok=True)
        (go_bin / "example-tool").touch()

        result = self._run(managers)
        # go does not go through subprocess.run, so we have it covered by dir
        file_paths = {f["path"] for f in result["files"]}

        for mgr, (_, filename) in _MANAGER_META.items():
            expected = f"{PACKAGES_SUBDIR}/{filename}"
            self.assertIn(expected, file_paths, f"Missing entry for manager '{mgr}'")

    def test_brew_entry_has_correct_path(self) -> None:
        result = self._run(["brew"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["path"], f"{PACKAGES_SUBDIR}/Brewfile")

    def test_npm_entry_has_correct_path(self) -> None:
        result = self._run(["npm"])
        self.assertEqual(result["files"][0]["path"], f"{PACKAGES_SUBDIR}/npm-globals.json")

    def test_pip_entry_has_correct_path(self) -> None:
        result = self._run(["pip"])
        self.assertEqual(result["files"][0]["path"], f"{PACKAGES_SUBDIR}/pip-requirements.txt")

    def test_cargo_entry_has_correct_path(self) -> None:
        result = self._run(["cargo"])
        self.assertEqual(result["files"][0]["path"], f"{PACKAGES_SUBDIR}/cargo-installed.txt")

    def test_gem_entry_has_correct_path(self) -> None:
        result = self._run(["gem"])
        self.assertEqual(result["files"][0]["path"], f"{PACKAGES_SUBDIR}/gem-list.txt")

    def test_file_is_written_to_workdir(self) -> None:
        result = self._run(["brew"])
        written = Path(self.workdir) / result["files"][0]["path"]
        self.assertTrue(written.exists(), "Expected file not written to workdir")

    def test_size_matches_bytes_written(self) -> None:
        result = self._run(["brew"])
        entry = result["files"][0]
        written = Path(self.workdir) / entry["path"]
        self.assertEqual(entry["size"], written.stat().st_size)

    def test_go_entry_using_directory_listing(self) -> None:
        go_bin = Path(self.workdir) / "go" / "bin"
        go_bin.mkdir(parents=True, exist_ok=True)
        (go_bin / "mytool").touch()
        (go_bin / "othertool").touch()

        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", side_effect=self._fake_run):
            result = walk_packages(managers=["go"], home=self.workdir, workdir=self.workdir)

        self.assertEqual(len(result["files"]), 1)
        entry = result["files"][0]
        self.assertEqual(entry["path"], f"{PACKAGES_SUBDIR}/go-bin.txt")
        written = Path(self.workdir) / entry["path"]
        content = written.read_text()
        self.assertIn("mytool", content)
        self.assertIn("othertool", content)


# ---------------------------------------------------------------------------
# TestManagerMissingSkipped
# ---------------------------------------------------------------------------

class TestManagerMissingSkipped(unittest.TestCase):
    """When shutil.which returns None, the manager appears in coverage.skipped."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_missing_brew_in_skipped(self) -> None:
        def which(binary: str) -> str | None:
            return None if binary == "brew" else f"/usr/bin/{binary}"

        with patch("shutil.which", side_effect=which), \
             patch("subprocess.run", return_value=_make_proc(b"")):
            result = walk_packages(managers=["brew"], home=self.workdir, workdir=self.workdir)

        self.assertEqual(len(result["files"]), 0)
        self.assertEqual(len(result["coverage"]["skipped"]), 1)
        skip = result["coverage"]["skipped"][0]
        self.assertEqual(skip["reason"], "manager-not-installed")
        self.assertIn("Brewfile", skip["path"])

    def test_missing_manager_not_in_files(self) -> None:
        with patch("shutil.which", return_value=None), \
             patch("subprocess.run", return_value=_make_proc(b"")):
            result = walk_packages(managers=["npm"], home=self.workdir, workdir=self.workdir)

        self.assertEqual(result["files"], [])

    def test_go_missing_when_no_bin_dir(self) -> None:
        # ~/go/bin does not exist → manager-not-installed
        with patch("shutil.which", return_value=None):
            result = walk_packages(managers=["go"], home=self.workdir, workdir=self.workdir)

        self.assertEqual(len(result["files"]), 0)
        self.assertEqual(result["coverage"]["skipped"][0]["reason"], "manager-not-installed")


# ---------------------------------------------------------------------------
# TestManagerFailureSkipped
# ---------------------------------------------------------------------------

class TestManagerFailureSkipped(unittest.TestCase):
    """Non-zero exit → coverage.skipped entry with reason manager-failed."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_nonzero_exit_produces_skipped_entry(self) -> None:
        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", return_value=_make_proc(b"error output", returncode=1)):
            result = walk_packages(managers=["pip"], home=self.workdir, workdir=self.workdir)

        self.assertEqual(result["files"], [])
        self.assertEqual(len(result["coverage"]["skipped"]), 1)
        skip = result["coverage"]["skipped"][0]
        self.assertEqual(skip["reason"], "manager-failed")
        self.assertEqual(skip["exit_code"], 1)

    def test_failed_manager_path_contains_filename(self) -> None:
        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", return_value=_make_proc(b"", returncode=2)):
            result = walk_packages(managers=["cargo"], home=self.workdir, workdir=self.workdir)

        skip = result["coverage"]["skipped"][0]
        self.assertIn("cargo-installed.txt", skip["path"])
        self.assertEqual(skip["exit_code"], 2)


# ---------------------------------------------------------------------------
# TestTimeoutHandled
# ---------------------------------------------------------------------------

class TestTimeoutHandled(unittest.TestCase):
    """subprocess.TimeoutExpired → coverage.skipped with reason manager-timeout."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_timeout_produces_skipped_entry(self) -> None:
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["brew"], timeout=30)

        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", side_effect=raise_timeout):
            result = walk_packages(managers=["brew"], home=self.workdir, workdir=self.workdir)

        self.assertEqual(result["files"], [])
        self.assertEqual(len(result["coverage"]["skipped"]), 1)
        skip = result["coverage"]["skipped"][0]
        self.assertEqual(skip["reason"], "manager-timeout")

    def test_timeout_does_not_propagate_exception(self) -> None:
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=["gem"], timeout=30)

        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", side_effect=raise_timeout):
            # Should not raise
            result = walk_packages(managers=["gem"], home=self.workdir, workdir=self.workdir)

        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# TestOutputShape
# ---------------------------------------------------------------------------

class TestOutputShape(unittest.TestCase):
    """JSON output must match the documented shape."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _run_with_brew(self) -> dict:
        with patch("shutil.which", side_effect=_all_which_found), \
             patch("subprocess.run", return_value=_make_proc(b"tap 'homebrew/core'\n")):
            return walk_packages(managers=["brew"], home=self.workdir, workdir=self.workdir)

    def test_top_level_keys(self) -> None:
        result = self._run_with_brew()
        self.assertIn("category", result)
        self.assertIn("workdir", result)
        self.assertIn("files", result)
        self.assertIn("coverage", result)

    def test_category_value(self) -> None:
        result = self._run_with_brew()
        self.assertEqual(result["category"], "package-managers")

    def test_workdir_value(self) -> None:
        result = self._run_with_brew()
        self.assertEqual(result["workdir"], self.workdir)

    def test_files_is_list(self) -> None:
        result = self._run_with_brew()
        self.assertIsInstance(result["files"], list)

    def test_coverage_sub_keys(self) -> None:
        result = self._run_with_brew()
        cov = result["coverage"]
        self.assertIn("globs", cov)
        self.assertIn("skipped", cov)
        self.assertIn("large_files_warned", cov)

    def test_coverage_globs_is_dict(self) -> None:
        result = self._run_with_brew()
        self.assertIsInstance(result["coverage"]["globs"], dict)

    def test_file_entry_shape(self) -> None:
        result = self._run_with_brew()
        entry = result["files"][0]
        self.assertIn("path", entry)
        self.assertIn("size", entry)
        self.assertIn("mtime", entry)
        self.assertIn("mode", entry)
        self.assertIn("is_symlink", entry)
        self.assertIn("symlink_target", entry)

    def test_file_entry_no_object_hash(self) -> None:
        """object_hash must NOT be included — snapshot.py computes it."""
        result = self._run_with_brew()
        entry = result["files"][0]
        self.assertNotIn("object_hash", entry)

    def test_file_entry_mode_is_0644(self) -> None:
        result = self._run_with_brew()
        self.assertEqual(result["files"][0]["mode"], "0644")

    def test_file_entry_is_symlink_false(self) -> None:
        result = self._run_with_brew()
        self.assertFalse(result["files"][0]["is_symlink"])

    def test_file_entry_symlink_target_null(self) -> None:
        result = self._run_with_brew()
        self.assertIsNone(result["files"][0]["symlink_target"])

    def test_is_json_serialisable(self) -> None:
        result = self._run_with_brew()
        # Must not raise
        serialised = json.dumps(result)
        self.assertIsInstance(serialised, str)


# ---------------------------------------------------------------------------
# TestNoBytesLogged
# ---------------------------------------------------------------------------

class TestNoBytesLogged(unittest.TestCase):
    """
    Captured package output must never appear in stderr log lines.

    We use a deliberately secret-looking string as the canned output and verify
    that it does not appear in the captured stderr produced by the walker.
    """

    SECRET_OUTPUT = b"SECRET-API-KEY=abc123XYZ-npm WARN deprecated foo: <key>"

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _capture_stderr(self, managers: list[str]) -> str:
        """Run walk_packages with mocked subprocess; return captured stderr."""
        import io as _io
        buf = _io.StringIO()
        original_stderr = sys.stderr
        try:
            sys.stderr = buf
            with patch("shutil.which", side_effect=_all_which_found), \
                 patch("subprocess.run", return_value=_make_proc(self.SECRET_OUTPUT)):
                walk_packages(managers=managers, home=self.workdir, workdir=self.workdir)
        finally:
            sys.stderr = original_stderr
        return buf.getvalue()

    def test_secret_npm_output_not_in_stderr(self) -> None:
        captured = self._capture_stderr(["npm"])
        secret_str = self.SECRET_OUTPUT.decode()
        self.assertNotIn(secret_str, captured)

    def test_secret_brew_output_not_in_stderr(self) -> None:
        captured = self._capture_stderr(["brew"])
        self.assertNotIn("SECRET-API-KEY", captured)

    def test_secret_pip_output_not_in_stderr(self) -> None:
        captured = self._capture_stderr(["pip"])
        self.assertNotIn("abc123XYZ", captured)

    def test_no_raw_bytes_in_stderr(self) -> None:
        """General check: captured stderr should contain no raw bytes repr."""
        captured = self._capture_stderr(["cargo"])
        # If someone accidentally logs b'...' repr, it would contain "b'"
        self.assertNotIn("SECRET", captured)


# ---------------------------------------------------------------------------
# TestGoManagerDirectoryListing
# ---------------------------------------------------------------------------

class TestGoManagerDirectoryListing(unittest.TestCase):
    """Detailed tests for the go manager's directory-listing logic."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_go_bin_absent_skipped(self) -> None:
        # No ~/go/bin in self.workdir
        with patch("shutil.which", return_value=None):
            result = walk_packages(managers=["go"], home=self.workdir, workdir=self.workdir)
        self.assertEqual(result["files"], [])
        self.assertEqual(result["coverage"]["skipped"][0]["reason"], "manager-not-installed")

    def test_go_bin_present_emits_file(self) -> None:
        go_bin = Path(self.workdir) / "go" / "bin"
        go_bin.mkdir(parents=True)
        (go_bin / "awesometool").touch()

        result = walk_packages(managers=["go"], home=self.workdir, workdir=self.workdir)
        self.assertEqual(len(result["files"]), 1)
        written = Path(self.workdir) / result["files"][0]["path"]
        self.assertIn("awesometool", written.read_text())

    def test_go_bin_empty_emits_empty_file(self) -> None:
        go_bin = Path(self.workdir) / "go" / "bin"
        go_bin.mkdir(parents=True)

        result = walk_packages(managers=["go"], home=self.workdir, workdir=self.workdir)
        # An empty directory produces an empty go-bin.txt — still a file entry
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["size"], 0)


# ---------------------------------------------------------------------------
# TestMultipleManagersMixed
# ---------------------------------------------------------------------------

class TestMultipleManagersMixed(unittest.TestCase):
    """Mix of present/missing managers → correct split between files and skipped."""

    def setUp(self) -> None:
        self.workdir = tempfile.mkdtemp(prefix="rh-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_two_present_one_missing(self) -> None:
        def which(binary: str) -> str | None:
            # only brew and gem are "installed"
            return f"/usr/bin/{binary}" if binary in ("brew", "gem") else None

        with patch("shutil.which", side_effect=which), \
             patch("subprocess.run", return_value=_make_proc(b"some-pkg\n")):
            result = walk_packages(
                managers=["brew", "pip", "gem"],
                home=self.workdir,
                workdir=self.workdir,
            )

        self.assertEqual(len(result["files"]), 2)
        self.assertEqual(len(result["coverage"]["skipped"]), 1)
        self.assertEqual(result["coverage"]["skipped"][0]["reason"], "manager-not-installed")


if __name__ == "__main__":
    unittest.main()
