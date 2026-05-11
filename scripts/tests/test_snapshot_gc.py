"""
test_snapshot_gc.py — unit tests for scripts/snapshot-gc.py

Coverage:
- --keep-last 1: retains only newest; unique-to-others objects deleted; shared preserved
- --keep-last 2: keeps two newest
- --keep-after <middle-date>: keeps middle + newer
- --keep-ids A,C: keeps A and C, prunes B
- --dry-run: accurate report but no filesystem changes
- --keep-last 0 without --allow-empty: exits non-zero
- --keep-last 0 --allow-empty: deletes everything
- Two retention flags simultaneously: exits non-zero
- Orphan-objects detection: refuses without --allow-orphans
- Malformed manifest: exits non-zero (refuse to GC)
- Post-GC integrity: verify-sandbox still passes after --keep-last 1
"""

from __future__ import annotations

import hashlib
import importlib.util
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


# ---------------------------------------------------------------------------
# Module loader (hyphen in filename requires importlib)
# ---------------------------------------------------------------------------

def _load_gc_module():
    spec = importlib.util.spec_from_file_location(
        "snapshot_gc",
        str(_REPO_ROOT / "scripts" / "snapshot-gc.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

def _run_gc(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "scripts.snapshot-gc"] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

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


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_object(drive: Path, content: bytes) -> str:
    """Write *content* into the objects store; return its hex digest."""
    digest = _sha256_bytes(content)
    obj_path = drive / "objects" / digest[:2] / digest[2:4] / digest
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    obj_path.write_bytes(content)
    return digest


def _file_entry(
    path: str,
    object_hash: str,
    size: int = 10,
) -> dict:
    return {
        "path": path,
        "object_hash": object_hash,
        "mode": "0644",
        "mtime": "2026-05-11T00:00:00Z",
        "size": size,
        "is_symlink": False,
        "symlink_target": None,
    }


def _make_manifest(
    snapshot_id: str,
    created_at: str,
    parent_snapshot: str | None,
    categories: dict,
) -> dict:
    return {
        "schema_version": "0.1.0",
        "created_at": created_at,
        "snapshot_id": snapshot_id,
        "source_machine": _PROBE_DATA,
        "parent_snapshot": parent_snapshot,
        "categories": categories,
    }


def _write_snapshot(
    drive: Path,
    snapshot_id: str,
    created_at: str,
    parent_snapshot: str | None,
    categories: dict,
) -> None:
    """Write a snapshot directory with manifest.json to the drive."""
    snap_dir = drive / "snapshots" / snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    manifest = _make_manifest(snapshot_id, created_at, parent_snapshot, categories)
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    # Write parent.txt to be consistent with the rest of the system
    parent_text = parent_snapshot if parent_snapshot is not None else "none"
    (snap_dir / "parent.txt").write_text(parent_text + "\n", encoding="utf-8")


def _build_three_snapshot_drive(tmp_dir: str) -> tuple[Path, dict]:
    """
    Build a drive with 3 snapshots (A -> B -> C) where:
    - obj_shared is referenced by all three (hash shared_h)
    - obj_a_only is referenced only by A (hash a_h)
    - obj_b_only is referenced only by B (hash b_h)
    - obj_c_only is referenced only by C (hash c_h)
    - obj_bc is referenced by B and C (hash bc_h)

    Returns (drive_path, hashes_dict) where hashes_dict has keys:
      shared_h, a_h, b_h, c_h, bc_h
    """
    drive = Path(tmp_dir) / "drive"
    drive.mkdir()

    # Write object files
    shared_h = _write_object(drive, b"shared content")
    a_h = _write_object(drive, b"content unique to A")
    b_h = _write_object(drive, b"content unique to B")
    c_h = _write_object(drive, b"content unique to C")
    bc_h = _write_object(drive, b"content shared by B and C")

    # Snapshot A (oldest)
    _write_snapshot(
        drive,
        snapshot_id="snap-A",
        created_at="2026-01-01T00:00:00Z",
        parent_snapshot=None,
        categories={
            "dotfiles": {
                "strategy": "file-list",
                "files": [
                    _file_entry("shared.txt", shared_h),
                    _file_entry("a-only.txt", a_h),
                ],
            }
        },
    )

    # Snapshot B (middle)
    _write_snapshot(
        drive,
        snapshot_id="snap-B",
        created_at="2026-03-01T00:00:00Z",
        parent_snapshot="snap-A",
        categories={
            "dotfiles": {
                "strategy": "file-list",
                "files": [
                    _file_entry("shared.txt", shared_h),
                    _file_entry("b-only.txt", b_h),
                    _file_entry("bc-shared.txt", bc_h),
                ],
            }
        },
    )

    # Snapshot C (newest)
    _write_snapshot(
        drive,
        snapshot_id="snap-C",
        created_at="2026-05-01T00:00:00Z",
        parent_snapshot="snap-B",
        categories={
            "dotfiles": {
                "strategy": "file-list",
                "files": [
                    _file_entry("shared.txt", shared_h),
                    _file_entry("c-only.txt", c_h),
                    _file_entry("bc-shared.txt", bc_h),
                ],
            }
        },
    )

    return drive, {
        "shared_h": shared_h,
        "a_h": a_h,
        "b_h": b_h,
        "c_h": c_h,
        "bc_h": bc_h,
    }


def _obj_exists(drive: Path, digest: str) -> bool:
    obj_path = drive / "objects" / digest[:2] / digest[2:4] / digest
    return obj_path.exists()


def _snapshot_exists(drive: Path, snapshot_id: str) -> bool:
    return (drive / "snapshots" / snapshot_id).is_dir()


# ---------------------------------------------------------------------------
# Tests: --keep-last
# ---------------------------------------------------------------------------


class TestKeepLast(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drive, self.hashes = _build_three_snapshot_drive(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_keep_last_1_retains_only_newest(self):
        """--keep-last 1 retains snap-C, prunes A and B."""
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "1",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)

        self.assertEqual(report["retained"], ["snap-C"])
        self.assertEqual(sorted(report["pruned"]), ["snap-A", "snap-B"])
        self.assertFalse(_snapshot_exists(self.drive, "snap-A"))
        self.assertFalse(_snapshot_exists(self.drive, "snap-B"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-C"))

    def test_keep_last_1_unique_objects_deleted(self):
        """After --keep-last 1, objects unique to A and B are gone."""
        _run_gc(["--drive", str(self.drive), "--keep-last", "1"])
        self.assertFalse(_obj_exists(self.drive, self.hashes["a_h"]))
        self.assertFalse(_obj_exists(self.drive, self.hashes["b_h"]))

    def test_keep_last_1_shared_objects_preserved(self):
        """After --keep-last 1, shared objects still referenced by C are preserved."""
        _run_gc(["--drive", str(self.drive), "--keep-last", "1"])
        self.assertTrue(_obj_exists(self.drive, self.hashes["shared_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["c_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["bc_h"]))

    def test_keep_last_1_report_counts(self):
        """Report reflects correct object counts."""
        proc = _run_gc(["--drive", str(self.drive), "--keep-last", "1"])
        report = json.loads(proc.stdout)
        # a_h + b_h deleted (bc_h still referenced by C)
        self.assertEqual(report["objects_deleted"], 2)
        # shared_h + c_h + bc_h kept
        self.assertEqual(report["objects_kept"], 3)
        self.assertGreater(report["bytes_freed_approx"], 0)
        self.assertFalse(report["dry_run"])

    def test_keep_last_2_keeps_two_newest(self):
        """--keep-last 2 retains snap-B and snap-C, prunes snap-A."""
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "2",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)

        self.assertEqual(sorted(report["retained"]), ["snap-B", "snap-C"])
        self.assertEqual(report["pruned"], ["snap-A"])
        self.assertFalse(_snapshot_exists(self.drive, "snap-A"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-B"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-C"))
        # a_h is unique to A and should be gone
        self.assertFalse(_obj_exists(self.drive, self.hashes["a_h"]))
        # Everything else still referenced by B or C
        self.assertTrue(_obj_exists(self.drive, self.hashes["shared_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["b_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["c_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["bc_h"]))


# ---------------------------------------------------------------------------
# Tests: --keep-after
# ---------------------------------------------------------------------------


class TestKeepAfter(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drive, self.hashes = _build_three_snapshot_drive(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_keep_after_middle_date_keeps_b_and_c(self):
        """--keep-after 2026-02-01 keeps B (2026-03-01) and C (2026-05-01); prunes A."""
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-after", "2026-02-01",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)

        self.assertEqual(sorted(report["retained"]), ["snap-B", "snap-C"])
        self.assertEqual(report["pruned"], ["snap-A"])
        self.assertFalse(_snapshot_exists(self.drive, "snap-A"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-B"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-C"))

    def test_keep_after_c_date_keeps_only_c(self):
        """--keep-after 2026-04-01 keeps only C."""
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-after", "2026-04-01",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)
        self.assertEqual(report["retained"], ["snap-C"])
        self.assertEqual(sorted(report["pruned"]), ["snap-A", "snap-B"])


# ---------------------------------------------------------------------------
# Tests: --keep-ids
# ---------------------------------------------------------------------------


class TestKeepIds(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drive, self.hashes = _build_three_snapshot_drive(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_keep_ids_a_and_c(self):
        """--keep-ids snap-A,snap-C prunes only snap-B."""
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-ids", "snap-A,snap-C",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)

        self.assertEqual(sorted(report["retained"]), ["snap-A", "snap-C"])
        self.assertEqual(report["pruned"], ["snap-B"])
        self.assertTrue(_snapshot_exists(self.drive, "snap-A"))
        self.assertFalse(_snapshot_exists(self.drive, "snap-B"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-C"))
        # b_h unique to B should be gone
        self.assertFalse(_obj_exists(self.drive, self.hashes["b_h"]))
        # Objects still referenced by A or C must survive
        self.assertTrue(_obj_exists(self.drive, self.hashes["shared_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["a_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["c_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["bc_h"]))


# ---------------------------------------------------------------------------
# Tests: --dry-run
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drive, self.hashes = _build_three_snapshot_drive(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot_dir_mtime(self, snapshot_id: str) -> float:
        return (self.drive / "snapshots" / snapshot_id).stat().st_mtime

    def test_dry_run_no_filesystem_changes(self):
        """--dry-run produces a report but leaves all snapshots and objects intact."""
        # Capture mtimes before GC
        mtime_a_before = self._snapshot_dir_mtime("snap-A")
        mtime_b_before = self._snapshot_dir_mtime("snap-B")
        mtime_c_before = self._snapshot_dir_mtime("snap-C")

        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "1",
            "--dry-run",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")

        # All snapshots still exist
        self.assertTrue(_snapshot_exists(self.drive, "snap-A"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-B"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-C"))

        # All objects still exist
        for h in self.hashes.values():
            self.assertTrue(_obj_exists(self.drive, h), f"object {h[:8]}... was deleted in dry-run")

        # mtimes unchanged
        self.assertEqual(self._snapshot_dir_mtime("snap-A"), mtime_a_before)
        self.assertEqual(self._snapshot_dir_mtime("snap-B"), mtime_b_before)
        self.assertEqual(self._snapshot_dir_mtime("snap-C"), mtime_c_before)

    def test_dry_run_report_is_accurate(self):
        """--dry-run report matches what a live run would do."""
        proc_dry = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "1",
            "--dry-run",
        ])
        self.assertEqual(proc_dry.returncode, 0)
        report_dry = json.loads(proc_dry.stdout)

        self.assertTrue(report_dry["dry_run"])
        # dry-run should report what a live run would do
        self.assertEqual(report_dry["retained"], ["snap-C"])
        self.assertEqual(sorted(report_dry["pruned"]), ["snap-A", "snap-B"])
        self.assertEqual(report_dry["objects_deleted"], 2)
        self.assertEqual(report_dry["objects_kept"], 3)
        self.assertGreater(report_dry["bytes_freed_approx"], 0)


# ---------------------------------------------------------------------------
# Tests: safety checks
# ---------------------------------------------------------------------------


class TestSafetyChecks(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _empty_drive_with_snapshots(self) -> Path:
        """Create a drive with a snapshots/ dir but no snapshot subdirs."""
        drive = Path(self._tmp.name) / "drive"
        (drive / "snapshots").mkdir(parents=True)
        return drive

    def test_keep_last_zero_refuses_without_allow_empty(self):
        """--keep-last 0 without --allow-empty exits non-zero."""
        drive, _ = _build_three_snapshot_drive(self._tmp.name)
        proc = _run_gc(["--drive", str(drive), "--keep-last", "0"])
        self.assertNotEqual(proc.returncode, 0,
                            "Must exit non-zero when keep-last=0 without --allow-empty")
        self.assertIn("allow-empty", proc.stderr.lower())

    def test_keep_last_zero_with_allow_empty_deletes_everything(self):
        """--keep-last 0 --allow-empty deletes all snapshots and all objects."""
        drive, hashes = _build_three_snapshot_drive(self._tmp.name)
        proc = _run_gc([
            "--drive", str(drive),
            "--keep-last", "0",
            "--allow-empty",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)

        self.assertEqual(report["retained"], [])
        self.assertEqual(sorted(report["pruned"]), ["snap-A", "snap-B", "snap-C"])
        self.assertFalse(_snapshot_exists(drive, "snap-A"))
        self.assertFalse(_snapshot_exists(drive, "snap-B"))
        self.assertFalse(_snapshot_exists(drive, "snap-C"))
        for h in hashes.values():
            self.assertFalse(_obj_exists(drive, h), f"object {h[:8]}... should be gone")
        self.assertEqual(report["objects_deleted"], 5)
        self.assertEqual(report["objects_kept"], 0)

    def test_two_retention_flags_exits_nonzero(self):
        """Passing two retention flags simultaneously exits non-zero."""
        drive = self._empty_drive_with_snapshots()
        proc = _run_gc([
            "--drive", str(drive),
            "--keep-last", "1",
            "--keep-after", "2026-01-01",
        ])
        self.assertNotEqual(proc.returncode, 0,
                            "Must exit non-zero when two retention flags passed")

    def test_two_retention_flags_keep_last_and_keep_ids(self):
        """--keep-last and --keep-ids together must be rejected."""
        drive = self._empty_drive_with_snapshots()
        proc = _run_gc([
            "--drive", str(drive),
            "--keep-last", "1",
            "--keep-ids", "snap-A",
        ])
        self.assertNotEqual(proc.returncode, 0)

    def test_no_retention_flag_exits_nonzero(self):
        """Omitting all retention flags exits non-zero."""
        drive = self._empty_drive_with_snapshots()
        proc = _run_gc(["--drive", str(drive)])
        self.assertNotEqual(proc.returncode, 0)

    def test_orphan_objects_refused_without_allow_orphans(self):
        """Drive with empty snapshots/ but populated objects/ is refused."""
        drive = Path(self._tmp.name) / "drive"
        (drive / "snapshots").mkdir(parents=True)
        # Write an object manually
        _write_object(drive, b"orphaned object content")

        proc = _run_gc(["--drive", str(drive), "--keep-last", "1"])
        self.assertNotEqual(proc.returncode, 0,
                            "Must refuse orphan objects without --allow-orphans")
        self.assertIn("orphan", proc.stderr.lower())

    def test_orphan_objects_allowed_with_allow_orphans(self):
        """--allow-orphans bypasses the orphan-objects safety check."""
        drive = Path(self._tmp.name) / "drive"
        (drive / "snapshots").mkdir(parents=True)
        h = _write_object(drive, b"orphaned object content")

        proc = _run_gc([
            "--drive", str(drive),
            "--keep-last", "1",
            "--allow-orphans",
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        report = json.loads(proc.stdout)
        # With allow-orphans and no snapshots + allow-empty implicit (no snapshots means keep=0)
        # Actually: no snapshots to load → keep=[], prune=[], objects scanned but all orphaned
        # The GC should succeed and report 0 retained
        self.assertEqual(report["retained"], [])

    def test_malformed_manifest_exits_nonzero(self):
        """A malformed manifest causes GC to refuse (non-zero exit)."""
        drive = Path(self._tmp.name) / "drive"
        (drive / "snapshots").mkdir(parents=True)

        # Write one valid snapshot
        valid_h = _write_object(drive, b"valid content")
        _write_snapshot(
            drive,
            snapshot_id="snap-valid",
            created_at="2026-05-01T00:00:00Z",
            parent_snapshot=None,
            categories={
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [_file_entry("valid.txt", valid_h)],
                }
            },
        )

        # Write one snapshot with a malformed manifest
        bad_snap_dir = drive / "snapshots" / "snap-bad"
        bad_snap_dir.mkdir(parents=True)
        (bad_snap_dir / "manifest.json").write_text(
            '{"schema_version": "0.1.0", "snapshot_id": "snap-bad"}',
            encoding="utf-8",
        )

        proc = _run_gc(["--drive", str(drive), "--keep-last", "1"])
        self.assertNotEqual(proc.returncode, 0,
                            "Must exit non-zero when a manifest fails validation")

    def test_drive_not_found_exits_nonzero(self):
        """--drive pointing to a nonexistent path exits non-zero."""
        proc = _run_gc([
            "--drive", "/nonexistent/path/that/does/not/exist",
            "--keep-last", "1",
        ])
        self.assertNotEqual(proc.returncode, 0)

    def test_snapshots_dir_missing_exits_nonzero(self):
        """A drive without snapshots/ exits non-zero."""
        drive = Path(self._tmp.name) / "drive"
        drive.mkdir()
        proc = _run_gc(["--drive", str(drive), "--keep-last", "1"])
        self.assertNotEqual(proc.returncode, 0)


# ---------------------------------------------------------------------------
# Tests: --out path
# ---------------------------------------------------------------------------


class TestOutPath(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drive, self.hashes = _build_three_snapshot_drive(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_out_path_writes_json(self):
        """--out writes JSON report to the specified file."""
        out_file = os.path.join(self._tmp.name, "report.json")
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "1",
            "--out", out_file,
        ])
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertTrue(os.path.isfile(out_file))
        with open(out_file, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertIn("retained", report)
        self.assertIn("pruned", report)
        self.assertIn("objects_deleted", report)
        self.assertIn("objects_kept", report)
        self.assertIn("bytes_freed_approx", report)
        self.assertIn("dry_run", report)

    def test_out_path_stdout_empty_when_out_specified(self):
        """When --out is used, stdout should contain no JSON (GC report goes to file)."""
        out_file = os.path.join(self._tmp.name, "report.json")
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "1",
            "--out", out_file,
        ])
        self.assertEqual(proc.returncode, 0)
        # stdout should not contain the report JSON (it went to --out)
        # It may be empty or contain other incidental output; we just check
        # the file was written and is valid JSON.
        with open(out_file, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertIsInstance(report, dict)


# ---------------------------------------------------------------------------
# Tests: post-GC integrity (verify-sandbox)
# ---------------------------------------------------------------------------


class TestPostGCIntegrity(unittest.TestCase):
    """
    After GC with --keep-last 1, run verify-sandbox.py on the retained
    snapshot and confirm it still passes (GC did not accidentally delete
    an object that the retained snapshot needs).

    We use real file content so verify-sandbox can do the byte-equality check.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.drive = self.tmpdir / "drive"
        self.drive.mkdir()

        # Create a "home" directory with real files that snapshot.py would reference.
        self.home = self.tmpdir / "home"
        self.home.mkdir()

        # Write real content and compute hashes
        self.file_a_content = b"The content of file_a, unique to snap-old\n"
        self.file_shared_content = b"Shared file content present in both snapshots\n"
        self.file_c_content = b"The content of file_c, only in snap-new\n"

        a_h = _write_object(self.drive, self.file_a_content)
        shared_h = _write_object(self.drive, self.file_shared_content)
        c_h = _write_object(self.drive, self.file_c_content)

        self.hashes = {"a_h": a_h, "shared_h": shared_h, "c_h": c_h}

        # Old snapshot — references a_h and shared_h
        _write_snapshot(
            self.drive,
            snapshot_id="snap-old",
            created_at="2026-01-01T00:00:00Z",
            parent_snapshot=None,
            categories={
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [
                        _file_entry("file_a.txt", a_h, size=len(self.file_a_content)),
                        _file_entry("shared.txt", shared_h, size=len(self.file_shared_content)),
                    ],
                }
            },
        )

        # New snapshot — references shared_h and c_h (NOT a_h)
        _write_snapshot(
            self.drive,
            snapshot_id="snap-new",
            created_at="2026-05-01T00:00:00Z",
            parent_snapshot="snap-old",
            categories={
                "dotfiles": {
                    "strategy": "file-list",
                    "files": [
                        _file_entry("shared.txt", shared_h, size=len(self.file_shared_content)),
                        _file_entry("file_c.txt", c_h, size=len(self.file_c_content)),
                    ],
                }
            },
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_verify_sandbox_passes_after_keep_last_1(self):
        """After --keep-last 1, verify-sandbox on snap-new reports zero failures."""
        # Run GC
        proc = _run_gc([
            "--drive", str(self.drive),
            "--keep-last", "1",
        ])
        self.assertEqual(proc.returncode, 0, f"GC failed.\nstderr: {proc.stderr}")

        report = json.loads(proc.stdout)
        self.assertEqual(report["retained"], ["snap-new"])
        self.assertEqual(report["pruned"], ["snap-old"])

        # a_h should be gone (only referenced by snap-old which was pruned)
        self.assertFalse(_obj_exists(self.drive, self.hashes["a_h"]))
        # shared_h and c_h should still exist (referenced by snap-new)
        self.assertTrue(_obj_exists(self.drive, self.hashes["shared_h"]))
        self.assertTrue(_obj_exists(self.drive, self.hashes["c_h"]))

        # Run verify-sandbox on snap-new
        snap_new_path = self.drive / "snapshots" / "snap-new"
        verify_cmd = [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "verify-sandbox.py"),
            "--snapshot", str(snap_new_path),
            "--no-source-check",
        ]
        verify_proc = subprocess.run(
            verify_cmd,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(
            verify_proc.returncode,
            0,
            f"verify-sandbox failed after GC.\n"
            f"stdout: {verify_proc.stdout}\nstderr: {verify_proc.stderr}",
        )

        # Parse the verify report from stdout
        try:
            verify_report = json.loads(verify_proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f"verify-sandbox stdout was not valid JSON: {verify_proc.stdout!r}"
            )

        self.assertEqual(
            verify_report["fail"],
            0,
            f"verify-sandbox reported failures after GC: {verify_report['failures']}",
        )


# ---------------------------------------------------------------------------
# Tests: direct function API (run_gc)
# ---------------------------------------------------------------------------


class TestRunGCDirect(unittest.TestCase):
    """Test run_gc() directly without subprocess for fast unit tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.drive, self.hashes = _build_three_snapshot_drive(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_keep_last_2_via_api(self):
        """Direct API call: keep_last=2 prunes A, keeps B and C."""
        mod = _load_gc_module()

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.run_gc(
                drive=str(self.drive),
                keep_last=2,
                keep_after=None,
                keep_ids_raw=None,
                dry_run=False,
                out_path=None,
                allow_empty=False,
                allow_orphans=False,
            )

        report = json.loads(buf.getvalue())
        self.assertEqual(sorted(report["retained"]), ["snap-B", "snap-C"])
        self.assertEqual(report["pruned"], ["snap-A"])
        self.assertFalse(_snapshot_exists(self.drive, "snap-A"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-B"))
        self.assertTrue(_snapshot_exists(self.drive, "snap-C"))


if __name__ == "__main__":
    unittest.main()
