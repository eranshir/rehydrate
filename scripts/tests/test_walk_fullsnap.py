"""
test_walk_fullsnap.py — unit tests for scripts/walk-fullsnap.py

Builds fixture trees via real git init operations against tempdirs.
No mocking of git — git must be available in the test environment.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the module under test via importlib (walk-fullsnap.py is not a valid
# Python identifier as a module name).
import importlib.util as _ilu

_WALKER_PATH = _REPO_ROOT / "scripts" / "walk-fullsnap.py"
_spec = _ilu.spec_from_file_location("walk_fullsnap", _WALKER_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

walk_fullsnap = _mod.walk_fullsnap
PROJECTS_SUBDIR = _mod.PROJECTS_SUBDIR
INVENTORY_FILENAME = _mod.INVENTORY_FILENAME
CATEGORY_NAME = _mod.CATEGORY_NAME
FILE_COUNT_CAP = _mod.FILE_COUNT_CAP


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str) -> None:
    """Run a git command; raise if non-zero."""
    subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        check=True,
    )


def _make_proj_no_git(parent: Path, name: str) -> Path:
    """Create a plain directory (no .git/) with files."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (d / "README.md").write_text("# Project\n", encoding="utf-8")
    # node_modules dir — should be excluded
    nm = d / "node_modules"
    nm.mkdir()
    (nm / "foo.js").write_text("module.exports = {};\n", encoding="utf-8")
    return d


def _make_proj_git_no_remote(parent: Path, name: str) -> Path:
    """Create a git repo with no remote configured."""
    d = parent / name
    d.mkdir(parents=True)
    _git(["init"], cwd=str(d))
    _git(["config", "user.email", "test@example.com"], cwd=str(d))
    _git(["config", "user.name", "Test"], cwd=str(d))
    (d / "main.py").write_text("# local only\n", encoding="utf-8")
    _git(["add", "."], cwd=str(d))
    try:
        _git(["commit", "-m", "init"], cwd=str(d))
    except subprocess.CalledProcessError:
        pass
    return d


def _make_proj_git_with_remote(parent: Path, name: str) -> Path:
    """Create a git repo WITH a remote — should be skipped by walk-fullsnap."""
    d = parent / name
    d.mkdir(parents=True)
    _git(["init"], cwd=str(d))
    _git(["config", "user.email", "test@example.com"], cwd=str(d))
    _git(["config", "user.name", "Test"], cwd=str(d))
    _git(["config", "remote.origin.url", "https://example.com/proj.git"], cwd=str(d))
    (d / "main.py").write_text("# has remote\n", encoding="utf-8")
    _git(["add", "."], cwd=str(d))
    try:
        _git(["commit", "-m", "init"], cwd=str(d))
    except subprocess.CalledProcessError:
        pass
    return d


# ---------------------------------------------------------------------------
# TestBasicFixture
# ---------------------------------------------------------------------------

class TestBasicFixture(unittest.TestCase):
    """
    Core fixture:
      proj-no-git/      — no .git/ → included (no-git)
      proj-git-no-remote/ — .git/ but no remote → included (git-no-remote)
      proj-git-with-remote/ — .git/ + remote → SKIPPED
      not-a-dir.txt     — plain file at root level → SKIPPED
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-fullsnap-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))

        _make_proj_no_git(self.root, "proj-no-git")
        _make_proj_git_no_remote(self.root, "proj-git-no-remote")
        _make_proj_git_with_remote(self.root, "proj-git-with-remote")
        # plain file at root level
        (self.root / "not-a-dir.txt").write_text("plain file\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _run(self, extra_excludes: list[str] | None = None) -> dict:
        return walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
            exclude_patterns=extra_excludes or [],
        )

    def _load_inventory(self) -> dict:
        return json.loads(
            (self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME).read_text()
        )

    # --- proj-no-git included ---

    def test_proj_no_git_in_inventory(self) -> None:
        self._run()
        inv = self._load_inventory()
        names = [p["name"] for p in inv["projects"]]
        self.assertIn("proj-no-git", names)

    def test_proj_no_git_reason_no_git(self) -> None:
        self._run()
        inv = self._load_inventory()
        proj = next(p for p in inv["projects"] if p["name"] == "proj-no-git")
        self.assertEqual(proj["included_reason"], "no-git")

    def test_proj_no_git_main_py_in_output(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/proj-no-git/main.py", paths)

    def test_proj_no_git_readme_in_output(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/proj-no-git/README.md", paths)

    # --- proj-git-no-remote included ---

    def test_proj_git_no_remote_in_inventory(self) -> None:
        self._run()
        inv = self._load_inventory()
        names = [p["name"] for p in inv["projects"]]
        self.assertIn("proj-git-no-remote", names)

    def test_proj_git_no_remote_reason(self) -> None:
        self._run()
        inv = self._load_inventory()
        proj = next(p for p in inv["projects"] if p["name"] == "proj-git-no-remote")
        self.assertEqual(proj["included_reason"], "git-no-remote")

    def test_proj_git_no_remote_main_py_in_output(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/proj-git-no-remote/main.py", paths)

    # --- proj-git-with-remote skipped ---

    def test_proj_git_with_remote_not_in_inventory(self) -> None:
        self._run()
        inv = self._load_inventory()
        names = [p["name"] for p in inv["projects"]]
        self.assertNotIn("proj-git-with-remote", names)

    def test_proj_git_with_remote_not_in_files(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(
            any("proj-git-with-remote" in p for p in paths),
            "proj-git-with-remote files should not appear in output",
        )

    def test_proj_git_with_remote_in_skipped(self) -> None:
        result = self._run()
        reasons = {s["reason"] for s in result["coverage"]["skipped"]}
        self.assertIn("has-git-remote", reasons)

    # --- node_modules excluded ---

    def test_node_modules_file_not_in_output(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(
            any("node_modules" in p for p in paths),
            "node_modules files should be excluded",
        )

    # --- plain file at root skipped ---

    def test_plain_file_at_root_not_in_inventory(self) -> None:
        self._run()
        inv = self._load_inventory()
        names = [p["name"] for p in inv["projects"]]
        self.assertNotIn("not-a-dir.txt", names)

    # --- inventory contains exactly 2 projects ---

    def test_inventory_has_two_projects(self) -> None:
        self._run()
        inv = self._load_inventory()
        self.assertEqual(len(inv["projects"]), 2)

    def test_inventory_sorted_by_name(self) -> None:
        self._run()
        inv = self._load_inventory()
        names = [p["name"] for p in inv["projects"]]
        self.assertEqual(names, sorted(names, key=str.lower))

    # --- bytes survive into workdir ---

    def test_no_git_main_py_bytes_round_trip(self) -> None:
        self._run()
        dest = self.workdir / PROJECTS_SUBDIR / "proj-no-git" / "main.py"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(encoding="utf-8"), "print('hello')\n")

    def test_git_no_remote_main_py_bytes_round_trip(self) -> None:
        self._run()
        dest = self.workdir / PROJECTS_SUBDIR / "proj-git-no-remote" / "main.py"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(encoding="utf-8"), "# local only\n")

    # --- output shape ---

    def test_top_level_keys(self) -> None:
        result = self._run()
        for key in ("category", "workdir", "files", "coverage"):
            self.assertIn(key, result)

    def test_category_value(self) -> None:
        result = self._run()
        self.assertEqual(result["category"], CATEGORY_NAME)

    def test_workdir_value(self) -> None:
        result = self._run()
        self.assertEqual(result["workdir"], str(self.workdir))

    def test_inventory_file_in_files(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/{INVENTORY_FILENAME}", paths)

    def test_file_entry_shape(self) -> None:
        result = self._run()
        entry = result["files"][0]
        for key in ("path", "size", "mtime", "mode", "is_symlink", "symlink_target"):
            self.assertIn(key, entry)

    def test_coverage_sub_keys(self) -> None:
        result = self._run()
        for key in ("globs", "skipped", "large_files_warned"):
            self.assertIn(key, result["coverage"])

    def test_is_json_serialisable(self) -> None:
        result = self._run()
        self.assertIsInstance(json.dumps(result), str)


# ---------------------------------------------------------------------------
# TestExcludeGlobs
# ---------------------------------------------------------------------------

class TestExcludeGlobs(unittest.TestCase):
    """Category exclude: glob patterns are applied at file level."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-fullsnap-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))

        proj = self.root / "myproj"
        proj.mkdir()
        (proj / "main.py").write_text("# main\n", encoding="utf-8")
        (proj / "scratch.tmp").write_text("temp file\n", encoding="utf-8")
        (proj / "notes.txt").write_text("some notes\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_tmp_file_excluded_by_glob(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
            exclude_patterns=["**/*.tmp"],
        )
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(
            any("scratch.tmp" in p for p in paths),
            "*.tmp file should be excluded by glob pattern",
        )

    def test_non_excluded_files_still_present(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
            exclude_patterns=["**/*.tmp"],
        )
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/myproj/main.py", paths)
        self.assertIn(f"{PROJECTS_SUBDIR}/myproj/notes.txt", paths)


# ---------------------------------------------------------------------------
# TestSymlinkHandling
# ---------------------------------------------------------------------------

class TestSymlinkHandling(unittest.TestCase):
    """Symlinks inside a project appear as symlinks in the workdir."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-fullsnap-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))

        proj = self.root / "linkproj"
        proj.mkdir()
        (proj / "real.txt").write_text("real content\n", encoding="utf-8")
        # Symlink pointing to real.txt (relative target)
        os.symlink("real.txt", str(proj / "link.txt"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_symlink_in_output_files(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/linkproj/link.txt", paths)

    def test_symlink_entry_is_symlink_true(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        link_entry = next(
            (f for f in result["files"] if "link.txt" in f["path"]),
            None,
        )
        self.assertIsNotNone(link_entry)
        self.assertTrue(link_entry["is_symlink"])

    def test_symlink_target_preserved(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        link_entry = next(
            (f for f in result["files"] if "link.txt" in f["path"]),
            None,
        )
        self.assertIsNotNone(link_entry)
        self.assertEqual(link_entry["symlink_target"], "real.txt")

    def test_symlink_in_workdir_is_symlink(self) -> None:
        walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        dest_link = self.workdir / PROJECTS_SUBDIR / "linkproj" / "link.txt"
        self.assertTrue(os.path.islink(str(dest_link)),
                        "workdir copy of symlink should itself be a symlink")

    def test_symlink_not_followed_during_walk(self) -> None:
        """walk-fullsnap must not follow symlinks (followlinks=False)."""
        # Create a symlink to a directory — its contents should NOT appear
        proj = self.root / "symlinkproj"
        proj.mkdir()
        target_dir = Path(tempfile.mkdtemp(prefix="rh-symlink-target-"))
        (target_dir / "hidden.txt").write_text("should not appear\n", encoding="utf-8")
        os.symlink(str(target_dir), str(proj / "linked_dir"))
        (proj / "visible.txt").write_text("should appear\n", encoding="utf-8")

        try:
            result = walk_fullsnap(
                roots=[str(self.root)],
                home=str(self.root),
                workdir=str(self.workdir),
            )
            paths = [f["path"] for f in result["files"]]
            # hidden.txt inside the symlinked dir should NOT appear
            self.assertFalse(
                any("hidden.txt" in p for p in paths),
                "files inside symlinked directories should not be walked",
            )
            self.assertIn(f"{PROJECTS_SUBDIR}/symlinkproj/visible.txt", paths)
        finally:
            shutil.rmtree(target_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# TestMissingRoot
# ---------------------------------------------------------------------------

class TestMissingRoot(unittest.TestCase):
    """A root that does not exist is gracefully skipped."""

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_missing_root_skipped(self) -> None:
        result = walk_fullsnap(
            roots=["/nonexistent/path/that/does/not/exist"],
            home="/nonexistent",
            workdir=str(self.workdir),
        )
        skipped_reasons = {s["reason"] for s in result["coverage"]["skipped"]}
        self.assertIn("root-not-found", skipped_reasons)

    def test_missing_root_inventory_still_written(self) -> None:
        walk_fullsnap(
            roots=["/nonexistent/path/that/does/not/exist"],
            home="/nonexistent",
            workdir=str(self.workdir),
        )
        inv_path = self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME
        self.assertTrue(inv_path.exists())
        inv = json.loads(inv_path.read_text())
        self.assertEqual(inv["projects"], [])


# ---------------------------------------------------------------------------
# TestInventoryStructure
# ---------------------------------------------------------------------------

class TestInventoryStructure(unittest.TestCase):
    """Verify the inventory.json internal structure."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-fullsnap-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))
        _make_proj_no_git(self.root, "structproj")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _load_inventory(self) -> dict:
        walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        return json.loads(
            (self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME).read_text()
        )

    def test_schema_version(self) -> None:
        inv = self._load_inventory()
        self.assertEqual(inv["schema_version"], "0.1.0")

    def test_scanned_at_ends_with_z(self) -> None:
        inv = self._load_inventory()
        self.assertTrue(inv["scanned_at"].endswith("Z"))

    def test_projects_is_list(self) -> None:
        inv = self._load_inventory()
        self.assertIsInstance(inv["projects"], list)

    def test_project_entry_keys(self) -> None:
        inv = self._load_inventory()
        proj = inv["projects"][0]
        for key in ("name", "original_path", "file_count", "total_bytes", "included_reason"):
            self.assertIn(key, proj)

    def test_project_file_count_positive(self) -> None:
        inv = self._load_inventory()
        proj = inv["projects"][0]
        self.assertGreater(proj["file_count"], 0)

    def test_project_total_bytes_positive(self) -> None:
        inv = self._load_inventory()
        proj = inv["projects"][0]
        self.assertGreater(proj["total_bytes"], 0)


# ---------------------------------------------------------------------------
# TestNoContentsInLog (no-PII)
# ---------------------------------------------------------------------------

class TestNoContentsInLog(unittest.TestCase):
    """File contents must never appear in stderr log output."""

    SENTINEL = "ULTRA_SECRET_LOCAL_TOKEN_DO_NOT_LOG_9876"

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-fullsnap-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))

        proj = self.root / "secretproj"
        proj.mkdir()
        (proj / "secrets.txt").write_text(self.SENTINEL + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _capture_stderr_run(self) -> str:
        buf = io.StringIO()
        original_stderr = sys.stderr
        try:
            sys.stderr = buf
            walk_fullsnap(
                roots=[str(self.root)],
                home=str(self.root),
                workdir=str(self.workdir),
            )
        finally:
            sys.stderr = original_stderr
        return buf.getvalue()

    def test_secret_content_not_in_stderr(self) -> None:
        captured = self._capture_stderr_run()
        self.assertNotIn(self.SENTINEL, captured)

    def test_do_not_log_not_in_stderr(self) -> None:
        captured = self._capture_stderr_run()
        self.assertNotIn("DO_NOT_LOG", captured)


# ---------------------------------------------------------------------------
# TestDefaultDirExcludes
# ---------------------------------------------------------------------------

class TestDefaultDirExcludes(unittest.TestCase):
    """Default excluded directory basenames are always pruned."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-fullsnap-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-fullsnap-work-"))

        proj = self.root / "buildproj"
        proj.mkdir()
        (proj / "src.py").write_text("# source\n", encoding="utf-8")

        for excluded_dir in [".venv", "__pycache__", "dist", "build", ".next"]:
            d = proj / excluded_dir
            d.mkdir()
            (d / "file.bin").write_bytes(b"\x00\x01\x02")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_venv_excluded(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(any(".venv" in p for p in paths))

    def test_pycache_excluded(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(any("__pycache__" in p for p in paths))

    def test_dist_excluded(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(any("/dist/" in p for p in paths))

    def test_build_excluded(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        paths = [f["path"] for f in result["files"]]
        self.assertFalse(any("/build/" in p for p in paths))

    def test_source_file_still_present(self) -> None:
        result = walk_fullsnap(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/buildproj/src.py", paths)


if __name__ == "__main__":
    unittest.main()
