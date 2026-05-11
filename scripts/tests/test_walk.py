"""
test_walk.py — unit tests for scripts/walk.py

Exercises: regular files, symlinks, broken symlinks, excluded files,
nested directories, directory-only glob matches, enabled/disabled categories.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# Ensure the repo root is on sys.path so `scripts.walk` can be imported
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.walk import walk_category, _rel_path, _matches_any_exclude


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write(path: str, content: str = "data") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_fixture(tmpdir: str) -> dict[str, str]:
    """
    Create a controlled fixture directory tree and return a dict of
    meaningful path labels → absolute paths.
    """
    paths: dict[str, str] = {}

    # Regular dotfile at root
    zshrc = os.path.join(tmpdir, ".zshrc")
    _write(zshrc, "# zsh config\nexport FOO=bar\n")
    paths["zshrc"] = zshrc

    # Nested file inside .config/git/
    git_config = os.path.join(tmpdir, ".config", "git", "config")
    _write(git_config, "[user]\n\tname = Test\n")
    paths["git_config"] = git_config

    # Another file in .config/zsh/
    zsh_extra = os.path.join(tmpdir, ".config", "zsh", "aliases.zsh")
    _write(zsh_extra, "alias ll='ls -la'\n")
    paths["zsh_extra"] = zsh_extra

    # A .DS_Store file that should be excluded
    ds_store = os.path.join(tmpdir, ".DS_Store")
    _write(ds_store, "junk")
    paths["ds_store"] = ds_store

    # A symlink pointing to an existing file inside the fixture
    symlink_target = zshrc
    symlink_path = os.path.join(tmpdir, ".zshrc_link")
    os.symlink(symlink_target, symlink_path)
    paths["symlink"] = symlink_path

    # A broken symlink (points to a path that does not exist)
    broken_link = os.path.join(tmpdir, ".broken_link")
    os.symlink(os.path.join(tmpdir, "nonexistent_file"), broken_link)
    paths["broken_link"] = broken_link

    return paths


def _make_categories_yaml(tmpdir: str, extra_yaml: str = "") -> str:
    """Write a minimal categories.yaml inside tmpdir and return its path."""
    content = textwrap.dedent(f"""\
        schema_version: "0.1.0"
        categories:
          - name: dotfiles
            enabled: true
            strategy: file-list
            description: Test dotfiles category
            globs:
              - ~/.zshrc
              - ~/.zshrc_link
              - ~/.broken_link
              - ~/.config/git/**
              - ~/.config/zsh/**
            exclude:
              - "**/.DS_Store"
{extra_yaml}
          - name: disabled-cat
            enabled: false
            strategy: file-list
            description: A disabled category
            globs:
              - ~/.bashrc
    """)
    cats_path = os.path.join(tmpdir, "categories.yaml")
    with open(cats_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return cats_path


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestWalkCategory(unittest.TestCase):

    def setUp(self):
        self._tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_obj.name
        self.paths = _make_fixture(self.tmpdir)
        self.cats_path = _make_categories_yaml(self.tmpdir)

        import yaml
        with open(self.cats_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        self.category = next(c for c in data["categories"] if c["name"] == "dotfiles")

    def tearDown(self):
        self._tmpdir_obj.cleanup()

    # ------------------------------------------------------------------
    # Regular files are present with correct relative paths
    # ------------------------------------------------------------------

    def test_zshrc_present(self):
        result = walk_category(self.category, home=self.tmpdir)
        rel_paths = {f["path"] for f in result["files"]}
        self.assertIn(".zshrc", rel_paths, "Expected .zshrc in files")

    def test_nested_git_config_present(self):
        result = walk_category(self.category, home=self.tmpdir)
        rel_paths = {f["path"] for f in result["files"]}
        self.assertIn(".config/git/config", rel_paths)

    def test_nested_zsh_aliases_present(self):
        result = walk_category(self.category, home=self.tmpdir)
        rel_paths = {f["path"] for f in result["files"]}
        self.assertIn(".config/zsh/aliases.zsh", rel_paths)

    # ------------------------------------------------------------------
    # .DS_Store is excluded
    # ------------------------------------------------------------------

    def test_ds_store_excluded(self):
        result = walk_category(self.category, home=self.tmpdir)
        rel_paths = {f["path"] for f in result["files"]}
        self.assertNotIn(".DS_Store", rel_paths, ".DS_Store must be excluded")

    # ------------------------------------------------------------------
    # Symlink: target is recorded; symlink is NOT followed (no traversal)
    # ------------------------------------------------------------------

    def test_symlink_present_with_target(self):
        result = walk_category(self.category, home=self.tmpdir)
        symlink_entries = [f for f in result["files"] if f["path"] == ".zshrc_link"]
        self.assertEqual(len(symlink_entries), 1, "Symlink should appear exactly once")
        entry = symlink_entries[0]
        self.assertTrue(entry["is_symlink"], "is_symlink must be True for symlink")
        self.assertIsNotNone(entry["symlink_target"], "symlink_target must be set")
        # The target string is what os.readlink returns — typically an absolute path here
        self.assertIn(".zshrc", entry["symlink_target"])

    def test_symlink_not_traversed_as_directory(self):
        """A symlink to a file must produce exactly one entry, not directory traversal."""
        result = walk_category(self.category, home=self.tmpdir)
        # All paths that start with .zshrc_link/ would indicate traversal
        traversal = [f["path"] for f in result["files"] if f["path"].startswith(".zshrc_link/")]
        self.assertEqual(traversal, [], "Symlink must not be traversed as a directory")

    # ------------------------------------------------------------------
    # Broken symlink appears in skipped with reason broken-symlink
    # ------------------------------------------------------------------

    def test_broken_symlink_in_skipped(self):
        result = walk_category(self.category, home=self.tmpdir)
        skipped_paths = {s["path"]: s["reason"] for s in result["coverage"]["skipped"]}
        self.assertIn(".broken_link", skipped_paths, "Broken symlink must appear in skipped")
        self.assertEqual(skipped_paths[".broken_link"], "broken-symlink")

    def test_broken_symlink_not_in_files(self):
        result = walk_category(self.category, home=self.tmpdir)
        rel_paths = {f["path"] for f in result["files"]}
        self.assertNotIn(".broken_link", rel_paths)

    # ------------------------------------------------------------------
    # Coverage glob counts
    # ------------------------------------------------------------------

    def test_coverage_glob_counts_accurate(self):
        result = walk_category(self.category, home=self.tmpdir)
        counts = result["coverage"]["globs"]
        # ~/.zshrc → 1 match
        self.assertEqual(counts.get("~/.zshrc", -1), 1, "~/.zshrc should match exactly 1 file")
        # ~/.config/git/** → at least 1 match (git_config)
        self.assertGreaterEqual(counts.get("~/.config/git/**", 0), 1)
        # ~/.config/zsh/** → at least 1 match (aliases.zsh)
        self.assertGreaterEqual(counts.get("~/.config/zsh/**", 0), 1)

    def test_coverage_output_structure(self):
        result = walk_category(self.category, home=self.tmpdir)
        cov = result["coverage"]
        self.assertIn("globs", cov)
        self.assertIn("skipped", cov)
        self.assertIn("large_files_warned", cov)

    # ------------------------------------------------------------------
    # File metadata fields
    # ------------------------------------------------------------------

    def test_file_entry_fields(self):
        result = walk_category(self.category, home=self.tmpdir)
        zshrc_entries = [f for f in result["files"] if f["path"] == ".zshrc"]
        self.assertEqual(len(zshrc_entries), 1)
        entry = zshrc_entries[0]
        self.assertIn("path", entry)
        self.assertIn("size", entry)
        self.assertIn("mtime", entry)
        self.assertIn("mode", entry)
        self.assertIn("is_symlink", entry)
        self.assertIn("symlink_target", entry)
        self.assertIsNone(entry["symlink_target"], "Regular file must have null symlink_target")
        self.assertFalse(entry["is_symlink"])
        # mode must be 4-digit octal string
        self.assertRegex(entry["mode"], r"^[0-7]{4}$")
        # mtime must be ISO 8601 UTC
        self.assertRegex(entry["mtime"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        # path must not start with /
        self.assertFalse(entry["path"].startswith("/"))

    def test_size_is_integer(self):
        result = walk_category(self.category, home=self.tmpdir)
        for entry in result["files"]:
            self.assertIsInstance(entry["size"], int)

    # ------------------------------------------------------------------
    # Output category field
    # ------------------------------------------------------------------

    def test_output_category_field(self):
        result = walk_category(self.category, home=self.tmpdir)
        self.assertEqual(result["category"], "dotfiles")

    # ------------------------------------------------------------------
    # No file contents in output
    # ------------------------------------------------------------------

    def test_no_file_contents_in_output(self):
        """File content ('# zsh config') must never appear in the JSON output."""
        result = walk_category(self.category, home=self.tmpdir)
        output_str = json.dumps(result)
        self.assertNotIn("# zsh config", output_str)
        self.assertNotIn("export FOO=bar", output_str)
        self.assertNotIn("[user]", output_str)


class TestWalkCLI(unittest.TestCase):
    """Tests that exercise the CLI entry point (subprocess)."""

    def setUp(self):
        self._tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_obj.name
        self.paths = _make_fixture(self.tmpdir)
        self.cats_path = _make_categories_yaml(self.tmpdir)

    def tearDown(self):
        self._tmpdir_obj.cleanup()

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, "-m", "scripts.walk",
            "--categories", self.cats_path,
            "--home", self.tmpdir,
            *extra_args,
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )

    def test_cli_success_exits_zero(self):
        proc = self._run("--category", "dotfiles")
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")

    def test_cli_output_is_valid_json(self):
        proc = self._run("--category", "dotfiles")
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertIn("files", data)
        self.assertIn("coverage", data)

    def test_cli_disabled_category_exits_nonzero(self):
        """--category disabled-cat must exit with non-zero status."""
        proc = self._run("--category", "disabled-cat")
        self.assertNotEqual(proc.returncode, 0, "Disabled category must exit non-zero")

    def test_cli_unknown_category_exits_nonzero(self):
        proc = self._run("--category", "nonexistent-category")
        self.assertNotEqual(proc.returncode, 0)

    def test_cli_out_file(self):
        out_path = os.path.join(self.tmpdir, "output.json")
        proc = self._run("--category", "dotfiles", "--out", out_path)
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertTrue(os.path.exists(out_path), "--out file must be created")
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("files", data)

    def test_cli_stdout_empty_when_out_used(self):
        """When --out is specified, stdout should not contain the JSON payload."""
        out_path = os.path.join(self.tmpdir, "output2.json")
        proc = self._run("--category", "dotfiles", "--out", out_path)
        self.assertEqual(proc.returncode, 0)
        # stdout should be empty (or contain only whitespace)
        self.assertEqual(proc.stdout.strip(), "")


class TestHelpers(unittest.TestCase):
    """Unit tests for internal helper functions."""

    def test_rel_path_strips_home(self):
        self.assertEqual(_rel_path("/home/user/.zshrc", "/home/user"), ".zshrc")
        self.assertEqual(_rel_path("/home/user/.config/git/config", "/home/user"), ".config/git/config")

    def test_rel_path_trailing_slash_home(self):
        self.assertEqual(_rel_path("/home/user/.zshrc", "/home/user/"), ".zshrc")

    def test_matches_any_exclude_ds_store(self):
        # Build a fake home and path
        tmpdir = tempfile.mkdtemp()
        abs_path = os.path.join(tmpdir, ".DS_Store")
        result = _matches_any_exclude(abs_path, ["**/.DS_Store"], tmpdir)
        self.assertTrue(result, ".DS_Store should be excluded by **/.DS_Store pattern")
        os.rmdir(tmpdir)

    def test_matches_any_exclude_no_match(self):
        tmpdir = tempfile.mkdtemp()
        abs_path = os.path.join(tmpdir, ".zshrc")
        result = _matches_any_exclude(abs_path, ["**/.DS_Store"], tmpdir)
        self.assertFalse(result, ".zshrc should not be excluded by **/.DS_Store")
        os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main()
