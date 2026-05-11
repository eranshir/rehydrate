"""
test_snapshot.py — unit tests for scripts/snapshot.py

Coverage:
- Regular files are stored in objects/ with correct content and hash
- Symlinks are stored as link-target-string bytes
- Manifest validates against schemas/manifest.schema.json
- Manifest source_machine matches probe-output
- Manifest file entries have correct object_hash
- Idempotency: second run deduplicates objects (mtime unchanged)
- Idempotency: second run on same snapshot-id exits non-zero (no overwrite)
- Refuses non-existent --drive
- Refuses malformed walk-output
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure the repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_home(tmpdir: str) -> tuple[str, dict[str, str]]:
    """
    Create a small home dir with:
    - .zshrc (regular file)
    - .gitconfig (regular file)
    - .local/bin/python3 (symlink → /usr/bin/python3)

    Returns (home_path, {name: abs_path}).
    """
    home = os.path.join(tmpdir, "home")
    os.makedirs(home)

    zshrc = os.path.join(home, ".zshrc")
    with open(zshrc, "w", encoding="utf-8") as fh:
        fh.write("# zsh config\nexport EDITOR=vim\n")

    gitconfig = os.path.join(home, ".gitconfig")
    with open(gitconfig, "w", encoding="utf-8") as fh:
        fh.write("[user]\n\tname = Tester\n\temail = test@example.com\n")

    local_bin = os.path.join(home, ".local", "bin")
    os.makedirs(local_bin)
    py_link = os.path.join(local_bin, "python3")
    os.symlink("/usr/bin/python3", py_link)

    return home, {
        "zshrc": zshrc,
        "gitconfig": gitconfig,
        "py_link": py_link,
    }


def _make_walk_output(home: str, tmpdir: str) -> str:
    """Build a walk-output JSON describing the home fixture and write it to a temp file."""

    files = []
    for rel, is_symlink, symlink_target in [
        (".zshrc", False, None),
        (".gitconfig", False, None),
        (".local/bin/python3", True, "/usr/bin/python3"),
    ]:
        abs_path = os.path.join(home, rel)
        st = os.lstat(abs_path)
        size = 0 if is_symlink else st.st_size
        from datetime import datetime, timezone
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        mode = f"{st.st_mode & 0o7777:04o}"
        files.append(
            {
                "path": rel,
                "size": size,
                "mtime": mtime,
                "mode": mode,
                "is_symlink": is_symlink,
                "symlink_target": symlink_target,
            }
        )

    walk_data = {
        "category": "dotfiles",
        "files": files,
        "coverage": {"globs": {}, "skipped": [], "large_files_warned": []},
    }

    out_path = os.path.join(tmpdir, "walk-dotfiles.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(walk_data, fh, indent=2)
    return out_path


def _make_probe_output(tmpdir: str) -> str:
    """Build a minimal probe-output JSON and write it to a temp file."""
    data = {
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
    out_path = os.path.join(tmpdir, "probe.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return out_path


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_path(drive: str, digest: str) -> str:
    aa = digest[:2]
    bb = digest[2:4]
    return os.path.join(drive, "objects", aa, bb, digest)


def _run_snapshot(
    walk_output: str,
    category: str,
    probe_output: str,
    drive: str,
    snapshot_id: str,
    home: str,
    parent: str | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        "-m",
        "scripts.snapshot",
        "--walk-output", walk_output,
        "--category", category,
        "--probe-output", probe_output,
        "--drive", drive,
        "--snapshot-id", snapshot_id,
        "--home", home,
    ]
    if parent is not None:
        cmd += ["--parent", parent]
    if extra_args:
        cmd += extra_args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def _load_manifest_schema() -> dict:
    schema_path = _REPO_ROOT / "schemas" / "manifest.schema.json"
    with schema_path.open(encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestSnapshotObjects(unittest.TestCase):
    """Objects are stored correctly, with correct hashes and content."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.home, self.paths = _make_home(self.tmpdir)
        self.walk_output = _make_walk_output(self.home, self.tmpdir)
        self.probe_output = _make_probe_output(self.tmpdir)
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)
        self.snapshot_id = "test-snapshot-20260511T000000Z"

        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=self.snapshot_id,
            home=self.home,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"snapshot.py exited non-zero.\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_regular_file_object_exists(self):
        for name in (".zshrc", ".gitconfig"):
            abs_src = os.path.join(self.home, name)
            digest = _sha256_file(abs_src)
            obj_path = _object_path(self.drive, digest)
            self.assertTrue(
                os.path.exists(obj_path),
                f"Object for {name!r} not found at {obj_path}",
            )

    def test_regular_file_object_content_matches_source(self):
        for name in (".zshrc", ".gitconfig"):
            abs_src = os.path.join(self.home, name)
            digest = _sha256_file(abs_src)
            obj_path = _object_path(self.drive, digest)
            with open(abs_src, "rb") as fh:
                src_bytes = fh.read()
            with open(obj_path, "rb") as fh:
                obj_bytes = fh.read()
            self.assertEqual(
                src_bytes, obj_bytes,
                f"Object content for {name!r} does not match source",
            )

    def test_symlink_object_exists(self):
        target_bytes = b"/usr/bin/python3"
        digest = _sha256_bytes(target_bytes)
        obj_path = _object_path(self.drive, digest)
        self.assertTrue(
            os.path.exists(obj_path),
            f"Object for symlink not found at {obj_path}",
        )

    def test_symlink_object_content_is_target_string_bytes(self):
        target_bytes = b"/usr/bin/python3"
        digest = _sha256_bytes(target_bytes)
        obj_path = _object_path(self.drive, digest)
        with open(obj_path, "rb") as fh:
            stored = fh.read()
        self.assertEqual(
            stored, target_bytes,
            "Symlink object must contain the link-target string as bytes",
        )

    def test_symlink_object_is_regular_file_not_symlink(self):
        target_bytes = b"/usr/bin/python3"
        digest = _sha256_bytes(target_bytes)
        obj_path = _object_path(self.drive, digest)
        self.assertFalse(
            os.path.islink(obj_path),
            "Symlink target should be stored as a regular file in the object store",
        )

    def test_object_hashes_match_manifest(self):
        manifest_path = os.path.join(
            self.drive, "snapshots", self.snapshot_id, "manifest.json"
        )
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)

        for entry in manifest["categories"]["dotfiles"]["files"]:
            digest = entry["object_hash"]
            obj_path = _object_path(self.drive, digest)
            self.assertTrue(
                os.path.exists(obj_path),
                f"Object for {entry['path']!r} (hash {digest[:16]}…) not found",
            )
            # Verify stored hash is self-consistent
            self.assertEqual(
                len(digest), 64,
                "object_hash must be 64 hex chars",
            )
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_manifest_object_hash_matches_hashlib(self):
        """object_hash in manifest must match hashlib.sha256 of the stored object bytes."""
        manifest_path = os.path.join(
            self.drive, "snapshots", self.snapshot_id, "manifest.json"
        )
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)

        for entry in manifest["categories"]["dotfiles"]["files"]:
            claimed_digest = entry["object_hash"]
            obj_path = _object_path(self.drive, claimed_digest)
            actual_digest = _sha256_file(obj_path)
            self.assertEqual(
                actual_digest, claimed_digest,
                f"Object at {obj_path} has hash mismatch: "
                f"claimed={claimed_digest[:16]}… actual={actual_digest[:16]}…",
            )


class TestSnapshotManifest(unittest.TestCase):
    """Manifest structure and schema validation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.home, _ = _make_home(self.tmpdir)
        self.walk_output = _make_walk_output(self.home, self.tmpdir)
        self.probe_output = _make_probe_output(self.tmpdir)
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)
        self.snapshot_id = "test-manifest-20260511T000000Z"

        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=self.snapshot_id,
            home=self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        manifest_path = os.path.join(
            self.drive, "snapshots", self.snapshot_id, "manifest.json"
        )
        with open(manifest_path, encoding="utf-8") as fh:
            self.manifest = json.load(fh)

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipIf(jsonschema is None, "jsonschema not installed")
    def test_manifest_validates_against_schema(self):
        schema = _load_manifest_schema()
        # Should not raise
        jsonschema.validate(instance=self.manifest, schema=schema)

    def test_manifest_source_machine_matches_probe(self):
        with open(self.probe_output, encoding="utf-8") as fh:
            probe_data = json.load(fh)
        self.assertEqual(self.manifest["source_machine"], probe_data)

    def test_manifest_snapshot_id_matches_arg(self):
        self.assertEqual(self.manifest["snapshot_id"], self.snapshot_id)

    def test_manifest_parent_snapshot_is_null_by_default(self):
        self.assertIsNone(self.manifest["parent_snapshot"])

    def test_manifest_parent_snapshot_set_when_provided(self):
        parent_id = "parent-snap-20260510T000000Z"
        drive2 = os.path.join(self.tmpdir, "drive2")
        os.makedirs(drive2)
        # First create the parent snapshot so validation passes
        proc_parent = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=drive2,
            snapshot_id=parent_id,
            home=self.home,
        )
        self.assertEqual(proc_parent.returncode, 0, proc_parent.stderr)
        # Now create child referencing the parent
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=drive2,
            snapshot_id="child-snap-20260511T000000Z",
            home=self.home,
            parent=parent_id,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        mf_path = os.path.join(drive2, "snapshots", "child-snap-20260511T000000Z", "manifest.json")
        with open(mf_path, encoding="utf-8") as fh:
            mf = json.load(fh)
        self.assertEqual(mf["parent_snapshot"], parent_id)

    def test_manifest_schema_version(self):
        self.assertEqual(self.manifest["schema_version"], "0.1.0")

    def test_manifest_categories_has_dotfiles(self):
        self.assertIn("dotfiles", self.manifest["categories"])

    def test_manifest_dotfiles_strategy(self):
        self.assertEqual(
            self.manifest["categories"]["dotfiles"]["strategy"], "file-list"
        )

    def test_manifest_file_entry_fields(self):
        files = self.manifest["categories"]["dotfiles"]["files"]
        self.assertGreater(len(files), 0)
        for entry in files:
            for field in ("path", "object_hash", "mode", "mtime", "size", "is_symlink", "symlink_target"):
                self.assertIn(field, entry, f"Missing field {field!r} in entry {entry['path']!r}")

    def test_parent_txt_contains_none(self):
        parent_txt = os.path.join(
            self.drive, "snapshots", self.snapshot_id, "parent.txt"
        )
        with open(parent_txt, encoding="utf-8") as fh:
            content = fh.read().strip()
        self.assertEqual(content, "none")


class TestSnapshotIdempotency(unittest.TestCase):
    """Re-running with the same inputs deduplicates objects and refuses snapshot overwrite."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.home, self.paths = _make_home(self.tmpdir)
        self.walk_output = _make_walk_output(self.home, self.tmpdir)
        self.probe_output = _make_probe_output(self.tmpdir)
        self.drive = os.path.join(self.tmpdir, "drive")
        os.makedirs(self.drive)
        self.snapshot_id = "idempotent-snap-20260511T000000Z"

        # First run
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=self.snapshot_id,
            home=self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def tearDown(self):
        self._tmp.cleanup()

    def test_objects_not_rewritten_on_second_run(self):
        """Second run with a different snapshot-id must not rewrite existing objects."""
        # Capture mtimes of all objects after first run
        objects_root = os.path.join(self.drive, "objects")
        mtime_before: dict[str, float] = {}
        for dirpath, _, filenames in os.walk(objects_root):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                mtime_before[fp] = os.stat(fp).st_mtime

        # Sleep just enough to detect a mtime change if any write occurs
        time.sleep(0.05)

        # Second run with a different snapshot-id (so dir doesn't conflict)
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id="idempotent-snap-run2-20260511T000001Z",
            home=self.home,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # All original objects must have unchanged mtimes
        for fp, mtime_orig in mtime_before.items():
            current_mtime = os.stat(fp).st_mtime
            self.assertEqual(
                current_mtime,
                mtime_orig,
                f"Object {fp} was unexpectedly rewritten (mtime changed)",
            )

    def test_second_run_same_snapshot_id_exits_nonzero(self):
        """Attempting to overwrite an existing snapshot dir must fail."""
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=self.snapshot_id,  # same id — must fail
            home=self.home,
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "Second run with same snapshot-id must exit non-zero",
        )

    def test_snapshot_dir_not_clobbered(self):
        """Snapshot dir content is unchanged after a failed duplicate-id run."""
        manifest_before = os.path.join(
            self.drive, "snapshots", self.snapshot_id, "manifest.json"
        )
        mtime_before = os.stat(manifest_before).st_mtime

        _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id=self.snapshot_id,
            home=self.home,
        )

        mtime_after = os.stat(manifest_before).st_mtime
        self.assertEqual(mtime_before, mtime_after, "manifest.json must not be overwritten")


class TestSnapshotValidation(unittest.TestCase):
    """Input validation edge cases."""

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

    def test_refuses_nonexistent_drive(self):
        proc = _run_snapshot(
            walk_output=self.walk_output,
            category="dotfiles",
            probe_output=self.probe_output,
            drive="/nonexistent/path/that/does/not/exist",
            snapshot_id="test-noexist-20260511T000000Z",
            home=self.home,
        )
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero for missing drive")

    def test_refuses_malformed_walk_output(self):
        bad_walk = os.path.join(self.tmpdir, "bad-walk.json")
        with open(bad_walk, "w", encoding="utf-8") as fh:
            # Missing required 'files' field
            json.dump({"category": "dotfiles", "coverage": {}}, fh)

        proc = _run_snapshot(
            walk_output=bad_walk,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id="test-badwalk-20260511T000000Z",
            home=self.home,
        )
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero for malformed walk output")

    def test_refuses_invalid_json_walk_output(self):
        bad_walk = os.path.join(self.tmpdir, "invalid.json")
        with open(bad_walk, "w", encoding="utf-8") as fh:
            fh.write("not valid json {{{{")

        proc = _run_snapshot(
            walk_output=bad_walk,
            category="dotfiles",
            probe_output=self.probe_output,
            drive=self.drive,
            snapshot_id="test-invalidjson-20260511T000000Z",
            home=self.home,
        )
        self.assertNotEqual(proc.returncode, 0, "Must exit non-zero for invalid JSON walk output")

    def test_mismatched_walk_category_count_exits_nonzero(self):
        """--walk-output and --category must be provided in matched pairs."""
        cmd = [
            sys.executable,
            "-m",
            "scripts.snapshot",
            "--walk-output", self.walk_output,
            # No --category provided → argparse will error (required)
            "--probe-output", self.probe_output,
            "--drive", self.drive,
            "--snapshot-id", "test-mismatch-20260511T000000Z",
            "--home", self.home,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
