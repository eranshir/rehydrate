"""
test_walk_repos.py — unit tests for scripts/walk-repos.py

Tests build fixture repo trees via real git init operations against tempdirs.
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

# Import the module under test via importlib (walk-repos.py is not a valid
# Python identifier as a module name).
import importlib.util as _ilu

_WALKER_PATH = _REPO_ROOT / "scripts" / "walk-repos.py"
_spec = _ilu.spec_from_file_location("walk_repos", _WALKER_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

walk_repos = _mod.walk_repos
PROJECTS_SUBDIR = _mod.PROJECTS_SUBDIR
INVENTORY_FILENAME = _mod.INVENTORY_FILENAME
CATEGORY_NAME = _mod.CATEGORY_NAME
DEFAULT_SECRET_PATTERNS = _mod.DEFAULT_SECRET_PATTERNS


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


def _make_repo_with_remote(
    parent: Path,
    name: str,
    remote_url: str,
    *,
    tracked_files: dict[str, bytes] | None = None,
    gitignored_files: dict[str, bytes] | None = None,
    gitignore_patterns: list[str] | None = None,
) -> Path:
    """
    Create a git repo at *parent*/*name* with *remote_url*.

    - *tracked_files*: files added to the index (committed)
    - *gitignored_files*: files created on disk but listed in .gitignore
    - *gitignore_patterns*: patterns written to .gitignore
    """
    repo = parent / name
    repo.mkdir(parents=True)

    _git(["init"], cwd=str(repo))
    _git(["config", "user.email", "test@example.com"], cwd=str(repo))
    _git(["config", "user.name", "Test"], cwd=str(repo))
    _git(["config", "remote.origin.url", remote_url], cwd=str(repo))

    # Write .gitignore
    patterns = gitignore_patterns or []
    if gitignored_files:
        # Make sure each gitignored file has a matching pattern
        for rel_path in gitignored_files:
            fname = Path(rel_path).name
            if fname not in patterns:
                patterns.append(fname)

    if patterns:
        gitignore_content = "\n".join(patterns) + "\n"
        (repo / ".gitignore").write_text(gitignore_content, encoding="utf-8")
        _git(["add", ".gitignore"], cwd=str(repo))

    # Write and stage tracked files
    if tracked_files:
        for rel_path, content in tracked_files.items():
            abs_path = repo / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(content)
            _git(["add", rel_path], cwd=str(repo))

    # Commit whatever is staged
    has_staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=str(repo),
        capture_output=True,
    ).returncode != 0

    if has_staged or not patterns:
        # Commit even an empty tree so HEAD exists
        try:
            _git(
                ["commit", "--allow-empty", "-m", "initial"],
                cwd=str(repo),
            )
        except subprocess.CalledProcessError:
            pass

    # Write gitignored files AFTER commit so they are not tracked
    if gitignored_files:
        for rel_path, content in gitignored_files.items():
            abs_path = repo / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(content)

    return repo


def _make_repo_no_remote(parent: Path, name: str) -> Path:
    """Create a git repo with no remote configured."""
    repo = parent / name
    repo.mkdir(parents=True)
    _git(["init"], cwd=str(repo))
    _git(["config", "user.email", "test@example.com"], cwd=str(repo))
    _git(["config", "user.name", "Test"], cwd=str(repo))
    (repo / "file.txt").write_text("hello\n")
    _git(["add", "."], cwd=str(repo))
    try:
        _git(["commit", "-m", "init"], cwd=str(repo))
    except subprocess.CalledProcessError:
        pass
    return repo


def _make_non_repo_dir(parent: Path, name: str) -> Path:
    """Create a plain directory with a file but no .git/."""
    d = parent / name
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("just a note\n")
    return d


# ---------------------------------------------------------------------------
# TestBasicFixture
# ---------------------------------------------------------------------------

class TestBasicFixture(unittest.TestCase):
    """
    Core fixture: repoA (remote + .env secret), repoB (no remote), notARepo.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-repos-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))

        self.env_content = b"SECRET_KEY=abc123\nAPI_TOKEN=xyz789\n"

        _make_repo_with_remote(
            self.root,
            "repoA",
            "https://example.com/a.git",
            tracked_files={"README.md": b"# repoA\n"},
            gitignored_files={".env": self.env_content},
            gitignore_patterns=[".env"],
        )
        _make_repo_no_remote(self.root, "repoB")
        _make_non_repo_dir(self.root, "notARepo")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _run(self) -> tuple[dict, dict]:
        result = walk_repos(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        inv = json.loads(
            (self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME).read_text()
        )
        return result, inv

    def test_repoA_in_inventory(self) -> None:
        _, inv = self._run()
        names = [r["name"] for r in inv["repos"]]
        self.assertIn("repoA", names)

    def test_repoA_remote_url(self) -> None:
        _, inv = self._run()
        repo = next(r for r in inv["repos"] if r["name"] == "repoA")
        self.assertEqual(repo["remote_url"], "https://example.com/a.git")

    def test_repoB_not_in_inventory(self) -> None:
        """repoB has no remote — should be skipped."""
        _, inv = self._run()
        names = [r["name"] for r in inv["repos"]]
        self.assertNotIn("repoB", names)

    def test_not_a_repo_not_in_inventory(self) -> None:
        _, inv = self._run()
        names = [r["name"] for r in inv["repos"]]
        self.assertNotIn("notARepo", names)

    def test_env_in_captured_secrets(self) -> None:
        _, inv = self._run()
        repo = next(r for r in inv["repos"] if r["name"] == "repoA")
        self.assertIn(".env", repo["captured_secrets"])

    def test_readme_not_in_captured_secrets(self) -> None:
        """README.md is tracked — must NOT appear in captured_secrets."""
        _, inv = self._run()
        repo = next(r for r in inv["repos"] if r["name"] == "repoA")
        self.assertNotIn("README.md", repo["captured_secrets"])

    def test_env_bytes_round_trip(self) -> None:
        """The captured .env content must match the original bytes."""
        self._run()
        captured = (
            self.workdir / PROJECTS_SUBDIR / "repoA" / ".env"
        ).read_bytes()
        self.assertEqual(captured, self.env_content)

    def test_repoB_in_skipped(self) -> None:
        result, _ = self._run()
        reasons = {s["reason"] for s in result["coverage"]["skipped"]}
        self.assertIn("no-remote", reasons)

    def test_inventory_sorted_by_name(self) -> None:
        # Add a second repo so we can verify ordering
        _make_repo_with_remote(
            self.root,
            "alphaRepo",
            "https://example.com/alpha.git",
        )
        _, inv = self._run()
        names = [r["name"] for r in inv["repos"]]
        self.assertEqual(names, sorted(names, key=str.lower))


# ---------------------------------------------------------------------------
# TestSubdirSecret
# ---------------------------------------------------------------------------

class TestSubdirSecret(unittest.TestCase):
    """A secret in a subdirectory retains its relative path."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-repos-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))

        self.local_content = b"LOCAL_DB_URL=postgres://localhost/mydb\n"

        _make_repo_with_remote(
            self.root,
            "repoA",
            "https://example.com/a.git",
            tracked_files={"README.md": b"# repoA\n"},
            gitignored_files={"subdir/.env.local": self.local_content},
            gitignore_patterns=[".env.local", ".env.*"],
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_subdir_env_local_captured(self) -> None:
        result = walk_repos(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        inv = json.loads(
            (self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME).read_text()
        )
        repo = next(r for r in inv["repos"] if r["name"] == "repoA")
        # The secret path uses OS separator; normalise to forward slash for comparison
        secret_paths = [s.replace(os.sep, "/") for s in repo["captured_secrets"]]
        self.assertIn("subdir/.env.local", secret_paths)

    def test_subdir_env_local_bytes_preserved(self) -> None:
        walk_repos(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        captured = (
            self.workdir / PROJECTS_SUBDIR / "repoA" / "subdir" / ".env.local"
        ).read_bytes()
        self.assertEqual(captured, self.local_content)


# ---------------------------------------------------------------------------
# TestMissingRoot
# ---------------------------------------------------------------------------

class TestMissingRoot(unittest.TestCase):
    """A root that does not exist is gracefully skipped."""

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_missing_root_skipped(self) -> None:
        result = walk_repos(
            roots=["/nonexistent/path/that/does/not/exist"],
            home="/nonexistent",
            workdir=str(self.workdir),
        )
        self.assertEqual(result["repos"] if "repos" in result else [], [])
        skipped_reasons = {s["reason"] for s in result["coverage"]["skipped"]}
        self.assertIn("root-not-found", skipped_reasons)

    def test_missing_root_inventory_still_written(self) -> None:
        walk_repos(
            roots=["/nonexistent/path/that/does/not/exist"],
            home="/nonexistent",
            workdir=str(self.workdir),
        )
        inv_path = self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME
        self.assertTrue(inv_path.exists())
        inv = json.loads(inv_path.read_text())
        self.assertEqual(inv["repos"], [])


# ---------------------------------------------------------------------------
# TestNoSecretContentsInLog
# ---------------------------------------------------------------------------

class TestNoSecretContentsInLog(unittest.TestCase):
    """
    Secret file contents must never appear in the stderr log output.
    Uses io.StringIO to capture stderr during the walk.
    """

    SENTINEL = b"ULTRA_SECRET_TOKEN=do_not_log_me_XYZ_9876"

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-repos-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))

        _make_repo_with_remote(
            self.root,
            "secretRepo",
            "https://example.com/secret.git",
            tracked_files={"README.md": b"readme\n"},
            gitignored_files={".env": self.SENTINEL},
            gitignore_patterns=[".env"],
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _capture_stderr_run(self) -> str:
        buf = io.StringIO()
        original_stderr = sys.stderr
        try:
            sys.stderr = buf
            walk_repos(
                roots=[str(self.root)],
                home=str(self.root),
                workdir=str(self.workdir),
            )
        finally:
            sys.stderr = original_stderr
        return buf.getvalue()

    def test_secret_content_not_in_stderr(self) -> None:
        captured = self._capture_stderr_run()
        sentinel_str = self.SENTINEL.decode()
        self.assertNotIn(sentinel_str, captured)

    def test_token_keyword_not_in_stderr(self) -> None:
        captured = self._capture_stderr_run()
        self.assertNotIn("ULTRA_SECRET_TOKEN", captured)

    def test_do_not_log_me_not_in_stderr(self) -> None:
        captured = self._capture_stderr_run()
        self.assertNotIn("do_not_log_me", captured)


# ---------------------------------------------------------------------------
# TestOutputShape
# ---------------------------------------------------------------------------

class TestOutputShape(unittest.TestCase):
    """The walk_repos() return value must match the documented walk-output shape."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-repos-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))
        _make_repo_with_remote(
            self.root,
            "shapeRepo",
            "https://example.com/shape.git",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _run(self) -> dict:
        return walk_repos(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )

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

    def test_files_is_list(self) -> None:
        result = self._run()
        self.assertIsInstance(result["files"], list)

    def test_inventory_file_in_files(self) -> None:
        result = self._run()
        paths = [f["path"] for f in result["files"]]
        self.assertIn(f"{PROJECTS_SUBDIR}/{INVENTORY_FILENAME}", paths)

    def test_coverage_sub_keys(self) -> None:
        result = self._run()
        for key in ("globs", "skipped", "large_files_warned"):
            self.assertIn(key, result["coverage"])

    def test_file_entry_shape(self) -> None:
        result = self._run()
        entry = result["files"][0]
        for key in ("path", "size", "mtime", "mode", "is_symlink", "symlink_target"):
            self.assertIn(key, entry)

    def test_file_entry_no_object_hash(self) -> None:
        result = self._run()
        self.assertNotIn("object_hash", result["files"][0])

    def test_file_entry_mode_0644(self) -> None:
        result = self._run()
        self.assertEqual(result["files"][0]["mode"], "0644")

    def test_file_entry_is_symlink_false(self) -> None:
        result = self._run()
        self.assertFalse(result["files"][0]["is_symlink"])

    def test_file_entry_symlink_target_null(self) -> None:
        result = self._run()
        self.assertIsNone(result["files"][0]["symlink_target"])

    def test_is_json_serialisable(self) -> None:
        result = self._run()
        serialised = json.dumps(result)
        self.assertIsInstance(serialised, str)


# ---------------------------------------------------------------------------
# TestInventoryStructure
# ---------------------------------------------------------------------------

class TestInventoryStructure(unittest.TestCase):
    """Verify the inventory.json internal structure."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-repos-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))
        _make_repo_with_remote(
            self.root,
            "structRepo",
            "https://example.com/struct.git",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _load_inventory(self) -> dict:
        walk_repos(
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

    def test_repos_is_list(self) -> None:
        inv = self._load_inventory()
        self.assertIsInstance(inv["repos"], list)

    def test_repo_entry_keys(self) -> None:
        inv = self._load_inventory()
        repo = inv["repos"][0]
        for key in ("name", "path", "remote_url", "branch", "head_sha", "captured_secrets"):
            self.assertIn(key, repo)

    def test_captured_secrets_is_list(self) -> None:
        inv = self._load_inventory()
        repo = inv["repos"][0]
        self.assertIsInstance(repo["captured_secrets"], list)


# ---------------------------------------------------------------------------
# TestTrackedFilesNotCaptured
# ---------------------------------------------------------------------------

class TestTrackedFilesNotCaptured(unittest.TestCase):
    """
    Files already tracked by git (committed) must not appear in captured_secrets,
    even if their name matches a secret pattern.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="rh-repos-root-"))
        self.workdir = Path(tempfile.mkdtemp(prefix="rh-repos-work-"))

        # Create a repo where credentials.json is tracked (committed)
        repo = self.root / "myRepo"
        repo.mkdir()
        _git(["init"], cwd=str(repo))
        _git(["config", "user.email", "test@example.com"], cwd=str(repo))
        _git(["config", "user.name", "Test"], cwd=str(repo))
        _git(["config", "remote.origin.url", "https://example.com/my.git"], cwd=str(repo))
        # credentials.json is tracked — NOT gitignored
        (repo / "credentials.json").write_bytes(b'{"key": "value"}')
        _git(["add", "credentials.json"], cwd=str(repo))
        try:
            _git(["commit", "-m", "add credentials"], cwd=str(repo))
        except subprocess.CalledProcessError:
            pass

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_tracked_credentials_not_captured(self) -> None:
        walk_repos(
            roots=[str(self.root)],
            home=str(self.root),
            workdir=str(self.workdir),
        )
        inv = json.loads(
            (self.workdir / PROJECTS_SUBDIR / INVENTORY_FILENAME).read_text()
        )
        repo = next((r for r in inv["repos"] if r["name"] == "myRepo"), None)
        self.assertIsNotNone(repo)
        self.assertNotIn("credentials.json", repo["captured_secrets"])


if __name__ == "__main__":
    unittest.main()
