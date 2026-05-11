"""
test_snapshot_parent_validation.py — tests for parent-snapshot validation in snapshot.py

Coverage:
- snapshot.py --parent <nonexistent-id> exits non-zero, no snapshot dir created
- snapshot.py --parent <existing-id> succeeds, manifest parent_snapshot == that id
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Re-use the fixture helpers from test_snapshot to keep things DRY.
from scripts.tests.test_snapshot import (  # noqa: E402
    _make_home,
    _make_probe_output,
    _make_walk_output,
    _run_snapshot,
)


class TestSnapshotParentValidation(unittest.TestCase):
    """snapshot.py must validate that the parent snapshot exists before writing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.home, _ = _make_home(self.tmpdir)
        self.walk_output = _make_walk_output(self.home, self.tmpdir)
        self.probe_output = _make_probe_output(self.tmpdir)
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)

    def tearDown(self):
        self._tmp.cleanup()

    def test_nonexistent_parent_exits_nonzero(self):
        """--parent pointing to a snapshot that does not exist must exit non-zero."""
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id="child-snap-20260511T000000Z",
            home=self.home,
            parent="this-parent-does-not-exist",
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            f"Expected non-zero exit for nonexistent parent.\nstderr: {proc.stderr}",
        )

    def test_nonexistent_parent_no_snapshot_dir_created(self):
        """No snapshot directory must be created when the parent validation fails."""
        child_id = "child-nodir-20260511T000000Z"
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=child_id,
            home=self.home,
            parent="nonexistent-parent-id",
        )
        self.assertNotEqual(proc.returncode, 0)
        snapshot_dir = os.path.join(self.drive, "snapshots", child_id)
        self.assertFalse(
            os.path.exists(snapshot_dir),
            f"Snapshot dir must NOT be created when parent validation fails: {snapshot_dir}",
        )

    def test_valid_parent_succeeds(self):
        """--parent pointing to an existing snapshot must succeed."""
        parent_id = "real-parent-20260510T000000Z"

        # Create the parent snapshot first
        proc_parent = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=parent_id,
            home=self.home,
        )
        self.assertEqual(
            proc_parent.returncode,
            0,
            f"Parent snapshot creation failed.\nstderr: {proc_parent.stderr}",
        )

        # Now create the child referencing the parent
        child_id = "real-child-20260511T000000Z"
        proc_child = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=child_id,
            home=self.home,
            parent=parent_id,
        )
        self.assertEqual(
            proc_child.returncode,
            0,
            f"Child snapshot creation failed.\nstderr: {proc_child.stderr}",
        )

    def test_valid_parent_manifest_records_parent_snapshot(self):
        """The child manifest's parent_snapshot field must equal the --parent value."""
        parent_id = "manifest-parent-20260510T000000Z"
        _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=parent_id,
            home=self.home,
        )

        child_id = "manifest-child-20260511T000000Z"
        _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=child_id,
            home=self.home,
            parent=parent_id,
        )

        manifest_path = os.path.join(self.drive, "snapshots", child_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(
            manifest["parent_snapshot"],
            parent_id,
            f"manifest.parent_snapshot should be {parent_id!r}, got {manifest['parent_snapshot']!r}",
        )

    def test_no_parent_manifest_has_null_parent_snapshot(self):
        """When --parent is not given, manifest.parent_snapshot must be null."""
        snap_id = "no-parent-snap-20260511T000000Z"
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=snap_id,
            home=self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        manifest_path = os.path.join(self.drive, "snapshots", snap_id, "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertIsNone(
            manifest["parent_snapshot"],
            "parent_snapshot must be null when --parent is not provided",
        )


if __name__ == "__main__":
    unittest.main()
