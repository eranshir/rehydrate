"""
test_snapshot_diff.py — unit tests for scripts/snapshot-diff.py

Coverage:
- File added in child appears in added list
- File modified (different hash) appears in modified list with both hashes
- File removed in child appears in removed list
- File unchanged appears only in summary.unchanged, not in any category list
- Different categories (new category in child → all paths added)
- --against <explicit> overrides parent_snapshot
- Refusal: malformed manifest (schema validation failure)
- Refusal: missing snapshot directory
- Refusal: no parent and no --against
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import importlib.util

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_snapshot_diff_module():
    """Load scripts/snapshot-diff.py as a module (hyphen in filename needs importlib)."""
    spec = importlib.util.spec_from_file_location(
        "snapshot_diff",
        str(_REPO_ROOT / "scripts" / "snapshot-diff.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_DUMMY_HASH_A = "a" * 64
_DUMMY_HASH_B = "b" * 64
_DUMMY_HASH_C = "c" * 64
_DUMMY_HASH_D = "d" * 64

_PROBE_DATA = {
    "os": "macOS",
    "os_version": "26.0",
    "build": "25A0000",
    "hostname": "test-host.local",
    "user": "tester",
    "hardware": {
        "arch": "arm64",
        "model": "MacBookPro18,3",
        "memory_bytes": 17179869184,
    },
    "shell": "/bin/zsh",
    "path": ["/usr/local/bin", "/usr/bin", "/bin"],
}


def _file_entry(
    path: str,
    object_hash: str,
    is_symlink: bool = False,
    symlink_target: str | None = None,
    size: int = 100,
) -> dict:
    return {
        "path": path,
        "object_hash": object_hash,
        "mode": "0644",
        "mtime": "2026-05-11T00:00:00Z",
        "size": 0 if is_symlink else size,
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
    }


def _make_manifest(
    snapshot_id: str,
    parent_snapshot: str | None,
    categories: dict,
    created_at: str = "2026-05-11T00:00:00Z",
) -> dict:
    return {
        "schema_version": "0.1.0",
        "created_at": created_at,
        "snapshot_id": snapshot_id,
        "source_machine": _PROBE_DATA,
        "parent_snapshot": parent_snapshot,
        "categories": categories,
    }


def _write_manifest(drive: str, snapshot_id: str, manifest: dict) -> str:
    snap_dir = os.path.join(drive, "snapshots", snapshot_id)
    os.makedirs(snap_dir, exist_ok=True)
    manifest_path = os.path.join(snap_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest_path


def _run_diff(
    drive: str,
    snapshot_id: str,
    against: str | None = None,
    out_path: str | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "scripts.snapshot-diff",
        "--drive", drive,
        "--snapshot", snapshot_id,
    ]
    if against is not None:
        cmd += ["--against", against]
    if out_path is not None:
        cmd += ["--out", out_path]
    if extra_args:
        cmd += extra_args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# Tests: basic diff output
# ---------------------------------------------------------------------------


class TestSnapshotDiffBasic(unittest.TestCase):
    """
    Two snapshots on a temp drive; parent has baseline files, child differs.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)

        # Parent snapshot: unchanged.txt, modified.txt, removed.txt
        parent_categories = {
            "dotfiles": {
                "strategy": "file-list",
                "files": [
                    _file_entry("unchanged.txt", _DUMMY_HASH_A),
                    _file_entry("modified.txt", _DUMMY_HASH_B),
                    _file_entry("removed.txt", _DUMMY_HASH_C),
                ],
            }
        }
        self.parent_id = "parent-20260510T000000Z"
        parent_manifest = _make_manifest(
            self.parent_id, None, parent_categories, "2026-05-10T00:00:00Z"
        )
        _write_manifest(self.drive, self.parent_id, parent_manifest)

        # Child snapshot: unchanged.txt (same), modified.txt (new hash), added.txt (new)
        # removed.txt is gone from child
        child_categories = {
            "dotfiles": {
                "strategy": "file-list",
                "files": [
                    _file_entry("unchanged.txt", _DUMMY_HASH_A),
                    _file_entry("modified.txt", _DUMMY_HASH_D),  # changed
                    _file_entry("added.txt", _DUMMY_HASH_B),    # new
                ],
            }
        }
        self.child_id = "child-20260511T000000Z"
        child_manifest = _make_manifest(
            self.child_id, self.parent_id, child_categories, "2026-05-11T00:00:00Z"
        )
        _write_manifest(self.drive, self.child_id, child_manifest)

        # Run diff (uses implicit parent from manifest)
        proc = _run_diff(self.drive, self.child_id)
        self.assertEqual(
            proc.returncode, 0,
            f"snapshot-diff.py exited non-zero.\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.diff = json.loads(proc.stdout)

    def tearDown(self):
        self._tmp.cleanup()

    def test_child_and_parent_ids_in_output(self):
        self.assertEqual(self.diff["child"], self.child_id)
        self.assertEqual(self.diff["parent"], self.parent_id)

    def test_created_at_fields_present(self):
        self.assertEqual(self.diff["created_at_child"], "2026-05-11T00:00:00Z")
        self.assertEqual(self.diff["created_at_parent"], "2026-05-10T00:00:00Z")

    def test_added_file_in_added_list(self):
        added = self.diff["categories"]["dotfiles"]["added"]
        paths = [e["path"] for e in added]
        self.assertIn("added.txt", paths)
        entry = next(e for e in added if e["path"] == "added.txt")
        self.assertEqual(entry["object_hash"], _DUMMY_HASH_B)

    def test_modified_file_in_modified_list(self):
        modified = self.diff["categories"]["dotfiles"]["modified"]
        paths = [e["path"] for e in modified]
        self.assertIn("modified.txt", paths)
        entry = next(e for e in modified if e["path"] == "modified.txt")
        self.assertEqual(entry["old_hash"], _DUMMY_HASH_B)
        self.assertEqual(entry["new_hash"], _DUMMY_HASH_D)

    def test_removed_file_in_removed_list(self):
        removed = self.diff["categories"]["dotfiles"]["removed"]
        paths = [e["path"] for e in removed]
        self.assertIn("removed.txt", paths)
        entry = next(e for e in removed if e["path"] == "removed.txt")
        self.assertEqual(entry["old_hash"], _DUMMY_HASH_C)

    def test_unchanged_file_not_in_any_list(self):
        cat = self.diff["categories"]["dotfiles"]
        all_listed_paths = (
            [e["path"] for e in cat["added"]]
            + [e["path"] for e in cat["modified"]]
            + [e["path"] for e in cat["removed"]]
        )
        self.assertNotIn("unchanged.txt", all_listed_paths)

    def test_summary_counts(self):
        s = self.diff["summary"]
        self.assertEqual(s["added"], 1)
        self.assertEqual(s["modified"], 1)
        self.assertEqual(s["removed"], 1)
        self.assertEqual(s["unchanged"], 1)


# ---------------------------------------------------------------------------
# Tests: cross-category diffs
# ---------------------------------------------------------------------------


class TestSnapshotDiffCrossCategory(unittest.TestCase):
    """New category in child → all its paths are added."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)

        self.parent_id = "parent-crosscat-20260510T000000Z"
        parent_manifest = _make_manifest(
            self.parent_id,
            None,
            {
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [_file_entry("dot.txt", _DUMMY_HASH_A)],
                }
            },
        )
        _write_manifest(self.drive, self.parent_id, parent_manifest)

        self.child_id = "child-crosscat-20260511T000000Z"
        child_manifest = _make_manifest(
            self.child_id,
            self.parent_id,
            {
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [_file_entry("dot.txt", _DUMMY_HASH_A)],
                },
                "packages": {
                    "strategy": "file-list",
                    "files": [
                        _file_entry("brew.txt", _DUMMY_HASH_B),
                        _file_entry("pip.txt", _DUMMY_HASH_C),
                    ],
                },
            },
        )
        _write_manifest(self.drive, self.child_id, child_manifest)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_category_all_paths_added(self):
        proc = _run_diff(self.drive, self.child_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        diff = json.loads(proc.stdout)
        pkg = diff["categories"]["packages"]
        added_paths = [e["path"] for e in pkg["added"]]
        self.assertIn("brew.txt", added_paths)
        self.assertIn("pip.txt", added_paths)
        self.assertEqual(pkg["modified"], [])
        self.assertEqual(pkg["removed"], [])

    def test_existing_category_unchanged(self):
        proc = _run_diff(self.drive, self.child_id)
        diff = json.loads(proc.stdout)
        dot = diff["categories"]["dotfiles"]
        self.assertEqual(dot["added"], [])
        self.assertEqual(dot["modified"], [])
        self.assertEqual(dot["removed"], [])


# ---------------------------------------------------------------------------
# Tests: --against explicit override
# ---------------------------------------------------------------------------


class TestSnapshotDiffAgainstOverride(unittest.TestCase):
    """--against <explicit> uses that snapshot even if child has a parent_snapshot."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)

        # Implicit parent (what child.parent_snapshot points to)
        self.implicit_parent_id = "implicit-parent-20260509T000000Z"
        _write_manifest(
            self.drive,
            self.implicit_parent_id,
            _make_manifest(
                self.implicit_parent_id,
                None,
                {
                    "dotfiles": {
                        "strategy": "file-list",
                        "files": [_file_entry("file.txt", _DUMMY_HASH_A)],
                    }
                },
            ),
        )

        # Explicit override parent
        self.explicit_parent_id = "explicit-parent-20260510T000000Z"
        _write_manifest(
            self.drive,
            self.explicit_parent_id,
            _make_manifest(
                self.explicit_parent_id,
                None,
                {
                    "dotfiles": {
                        "strategy": "file-list",
                        "files": [_file_entry("file.txt", _DUMMY_HASH_B)],
                    }
                },
            ),
        )

        self.child_id = "child-override-20260511T000000Z"
        _write_manifest(
            self.drive,
            self.child_id,
            _make_manifest(
                self.child_id,
                self.implicit_parent_id,  # child's own parent pointer
                {
                    "dotfiles": {
                        "strategy": "file-list",
                        "files": [_file_entry("file.txt", _DUMMY_HASH_C)],
                    }
                },
            ),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_against_uses_explicit_snapshot(self):
        proc = _run_diff(
            self.drive, self.child_id, against=self.explicit_parent_id
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        diff = json.loads(proc.stdout)
        self.assertEqual(diff["parent"], self.explicit_parent_id)
        # file.txt changed from HASH_B → HASH_C (not HASH_A → HASH_C)
        modified = diff["categories"]["dotfiles"]["modified"]
        self.assertEqual(len(modified), 1)
        self.assertEqual(modified[0]["old_hash"], _DUMMY_HASH_B)
        self.assertEqual(modified[0]["new_hash"], _DUMMY_HASH_C)

    def test_without_against_uses_implicit_parent(self):
        proc = _run_diff(self.drive, self.child_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        diff = json.loads(proc.stdout)
        self.assertEqual(diff["parent"], self.implicit_parent_id)
        # file.txt changed from HASH_A → HASH_C
        modified = diff["categories"]["dotfiles"]["modified"]
        self.assertEqual(modified[0]["old_hash"], _DUMMY_HASH_A)
        self.assertEqual(modified[0]["new_hash"], _DUMMY_HASH_C)


# ---------------------------------------------------------------------------
# Tests: refusals
# ---------------------------------------------------------------------------


class TestSnapshotDiffRefusals(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_snapshot_exits_nonzero(self):
        proc = _run_diff(self.drive, "nonexistent-snap-id")
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero for missing snapshot")

    def test_no_parent_no_against_exits_nonzero(self):
        """Child has parent_snapshot=null and no --against → must error."""
        snap_id = "orphan-snap-20260511T000000Z"
        _write_manifest(
            self.drive,
            snap_id,
            _make_manifest(
                snap_id,
                None,  # no parent
                {
                    "dotfiles": {
                        "strategy": "file-list",
                        "files": [_file_entry("file.txt", _DUMMY_HASH_A)],
                    }
                },
            ),
        )
        proc = _run_diff(self.drive, snap_id)
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero when no parent and no --against")

    def test_malformed_manifest_exits_nonzero(self):
        """Schema validation failure must cause non-zero exit."""
        snap_id = "bad-manifest-snap-20260511T000000Z"
        snap_dir = os.path.join(self.drive, "snapshots", snap_id)
        os.makedirs(snap_dir, exist_ok=True)
        # Write a manifest that's missing required fields
        bad_manifest = {"schema_version": "0.1.0", "snapshot_id": snap_id}
        with open(os.path.join(snap_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(bad_manifest, fh)
        proc = _run_diff(self.drive, snap_id)
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero for malformed manifest")

    def test_invalid_json_manifest_exits_nonzero(self):
        """Non-JSON manifest file must cause non-zero exit."""
        snap_id = "invalid-json-snap-20260511T000000Z"
        snap_dir = os.path.join(self.drive, "snapshots", snap_id)
        os.makedirs(snap_dir, exist_ok=True)
        with open(os.path.join(snap_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write("not valid json {{{")
        proc = _run_diff(self.drive, snap_id)
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero for invalid JSON manifest")


# ---------------------------------------------------------------------------
# Tests: direct function call (compute_diff)
# ---------------------------------------------------------------------------


class TestComputeDiffDirect(unittest.TestCase):
    """Test the compute_diff function directly (no subprocess)."""

    def test_empty_categories_equal(self):
        mod = _load_snapshot_diff_module()
        child = _make_manifest("c", "p", {})
        parent = _make_manifest("p", None, {})
        diff = mod.compute_diff(child, parent)
        self.assertEqual(diff["summary"], {"added": 0, "modified": 0, "removed": 0, "unchanged": 0})
        self.assertEqual(diff["categories"], {})

    def test_all_paths_added_when_parent_empty(self):
        mod = _load_snapshot_diff_module()
        child = _make_manifest(
            "c",
            "p",
            {
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [
                        _file_entry("a.txt", _DUMMY_HASH_A),
                        _file_entry("b.txt", _DUMMY_HASH_B),
                    ],
                }
            },
        )
        parent = _make_manifest("p", None, {})
        diff = mod.compute_diff(child, parent)
        self.assertEqual(diff["summary"]["added"], 2)
        self.assertEqual(diff["summary"]["removed"], 0)
        self.assertEqual(diff["summary"]["unchanged"], 0)

    def test_all_paths_removed_when_child_empty(self):
        mod = _load_snapshot_diff_module()
        child = _make_manifest("c", "p", {})
        parent = _make_manifest(
            "p",
            None,
            {
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [_file_entry("a.txt", _DUMMY_HASH_A)],
                }
            },
        )
        diff = mod.compute_diff(child, parent)
        self.assertEqual(diff["summary"]["removed"], 1)
        self.assertEqual(diff["summary"]["added"], 0)


if __name__ == "__main__":
    unittest.main()
