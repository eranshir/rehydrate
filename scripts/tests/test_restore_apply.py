"""
Tests for scripts/restore-apply.py

Coverage:
  - Happy path: create / skip-identical / overwrite-needs-confirm actions
  - Symlink creation; symlink-not-followed safety
  - All refusal rules: '/', $HOME without --live, ~/Library, non-existent target
  - overwrite-needs-confirm without --overwrite → non-zero, no writes
  - Object hash mismatch → non-zero, no writes
  - --dry-run writes nothing
  - Re-running after partial failure is safe
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

# restore-apply.py has a hyphen — load via importlib.
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "restore-apply.py"
_spec = _ilu.spec_from_file_location("restore_apply", _SCRIPT_PATH)
ra = _ilu.module_from_spec(_spec)   # type: ignore[arg-type]
_spec.loader.exec_module(ra)        # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Helpers — hashing
# ---------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers — manifest / plan construction
# ---------------------------------------------------------------------------

def _file_entry(
    path: str,
    content: bytes,
    *,
    mode: str = "0644",
    mtime: str = "2026-05-01T10:00:00Z",
    is_symlink: bool = False,
    symlink_target: str | None = None,
) -> dict:
    """Build a manifest file_entry dict."""
    if is_symlink and symlink_target is not None:
        object_hash = _sha256_str(symlink_target)
    else:
        object_hash = _sha256_bytes(content)
    return {
        "path": path,
        "object_hash": object_hash,
        "mode": mode,
        "mtime": mtime,
        "size": 0 if is_symlink else len(content),
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
    }


def _make_manifest(files: list[dict], *, snapshot_id: str = "test-snap-001") -> dict:
    return {
        "schema_version": "0.1.0",
        "created_at": "2026-05-11T12:00:00Z",
        "snapshot_id": snapshot_id,
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
                "files": files,
            }
        },
    }


def _make_plan(
    actions_by_category: dict,
    *,
    target: str = "/tmp/restore-target",
    snapshot_id: str = "test-snap-001",
) -> dict:
    all_actions = [a for actions in actions_by_category.values() for a in actions]
    return {
        "plan_version": "0.1.0",
        "snapshot_id": snapshot_id,
        "target": target,
        "drift": [],
        "actions_by_category": actions_by_category,
        "summary": {
            "create": sum(1 for a in all_actions if a["type"] == "create"),
            "skip-identical": sum(1 for a in all_actions if a["type"] == "skip-identical"),
            "overwrite-needs-confirm": sum(
                1 for a in all_actions if a["type"] == "overwrite-needs-confirm"
            ),
            "total": len(all_actions),
        },
    }


# ---------------------------------------------------------------------------
# Fixture class
# ---------------------------------------------------------------------------

class RestoreFixture:
    """Sets up a complete snapshot store + target directory for testing.

    Layout::

        <tmp>/
          snapshots/
            test-snap-001/
              manifest.json
          objects/
            <aa>/
              <bb>/
                <hash>
          target/
    """

    def __init__(self):
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self.base: Path | None = None
        self.snapshot_dir: Path | None = None
        self.objects_dir: Path | None = None
        self.target_dir: Path | None = None
        self._entries: list[dict] = []

    def __enter__(self) -> "RestoreFixture":
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self._tmpdir.name)
        # Layout (matches snapshot.py output):
        #   <base>/snapshots/test-snap-001/  ← snapshot_dir (contains manifest.json)
        #   <base>/objects/                  ← objects_dir  (<snapshot>/../../objects)
        #   <base>/target/                   ← target_dir
        self.snapshot_dir = self.base / "snapshots" / "test-snap-001"
        self.snapshot_dir.mkdir(parents=True)
        self.objects_dir = self.base / "objects"
        self.objects_dir.mkdir(parents=True)
        self.target_dir = self.base / "target"
        self.target_dir.mkdir()
        return self

    def add_regular(
        self,
        path: str,
        content: bytes,
        mode: str = "0644",
        mtime: str = "2026-05-01T10:00:00Z",
    ) -> dict:
        """Register a regular file entry and write its object."""
        entry = _file_entry(path, content, mode=mode, mtime=mtime)
        self._entries.append(entry)
        self._write_object(entry["object_hash"], content)
        return entry

    def add_symlink(
        self,
        path: str,
        link_target: str,
        mtime: str = "2026-05-01T10:00:00Z",
    ) -> dict:
        """Register a symlink entry and write its object (link target as UTF-8)."""
        entry = _file_entry(
            path, b"", mtime=mtime, is_symlink=True, symlink_target=link_target
        )
        self._entries.append(entry)
        # Object content for symlinks is the link target encoded as UTF-8.
        self._write_object(entry["object_hash"], link_target.encode("utf-8"))
        return entry

    def _write_object(self, object_hash: str, data: bytes) -> Path:
        aa, bb = object_hash[:2], object_hash[2:4]
        obj_dir = self.objects_dir / aa / bb
        obj_dir.mkdir(parents=True, exist_ok=True)
        obj_path = obj_dir / object_hash
        obj_path.write_bytes(data)
        return obj_path

    def corrupt_object(self, object_hash: str) -> None:
        """Overwrite an object with garbage so the hash check fails."""
        aa, bb = object_hash[:2], object_hash[2:4]
        obj_path = self.objects_dir / aa / bb / object_hash
        obj_path.write_bytes(b"CORRUPTED DATA - hash will not match")

    def write_manifest(self) -> None:
        manifest = _make_manifest(self._entries)
        (self.snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def write_target_file(self, rel_path: str, content: bytes) -> Path:
        dest = self.target_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return dest

    def write_target_symlink(self, rel_path: str, link_target: str) -> Path:
        dest = self.target_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        os.symlink(link_target, dest)
        return dest

    def build_plan(self, extra_actions: list[dict] | None = None) -> dict:
        """Build a plan dict from the registered entries."""
        actions: list[dict] = []
        for entry in self._entries:
            target_path = self.target_dir / entry["path"]
            if target_path.is_symlink():
                # Compare symlink target hash.
                current = _sha256_str(os.readlink(target_path))
                expected = entry["object_hash"]
                if current == expected:
                    actions.append({"path": entry["path"], "type": "skip-identical"})
                else:
                    actions.append({
                        "path": entry["path"],
                        "type": "overwrite-needs-confirm",
                        "current_hash": current,
                        "expected_hash": expected,
                        "mode": entry["mode"],
                    })
            elif target_path.exists():
                current = _sha256_bytes(target_path.read_bytes())
                expected = entry["object_hash"]
                if current == expected:
                    actions.append({"path": entry["path"], "type": "skip-identical"})
                else:
                    actions.append({
                        "path": entry["path"],
                        "type": "overwrite-needs-confirm",
                        "current_hash": current,
                        "expected_hash": expected,
                        "mode": entry["mode"],
                    })
            else:
                actions.append({
                    "path": entry["path"],
                    "type": "create",
                    "object_hash": entry["object_hash"],
                    "mode": entry["mode"],
                })
        if extra_actions:
            actions.extend(extra_actions)
        return _make_plan(
            {"dotfiles": actions},
            target=str(self.target_dir),
        )

    def run_apply(
        self,
        plan: dict,
        *,
        overwrite: bool = False,
        live: bool = False,
        dry_run: bool = False,
        target: Path | None = None,
        extra_env: dict | None = None,
    ) -> int:
        """Write plan to a temp file and invoke ra.main()."""
        self.write_manifest()
        if target is None:
            target = self.target_dir

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as plan_fh:
            json.dump(plan, plan_fh)
            plan_path = plan_fh.name

        try:
            argv = [
                "--plan", plan_path,
                "--snapshot", str(self.snapshot_dir),
                "--target", str(target),
            ]
            if overwrite:
                argv.append("--overwrite")
            if live:
                argv.append("--live")
            if dry_run:
                argv.append("--dry-run")

            env_overrides = {"REHYDRATE_TARGET": "", "USER": "testuser"}
            if extra_env:
                env_overrides.update(extra_env)

            with patch.dict(os.environ, env_overrides, clear=False):
                return ra.main(argv)
        finally:
            try:
                os.unlink(plan_path)
            except FileNotFoundError:
                pass

    def __exit__(self, *_):
        if self._tmpdir:
            self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Tests: happy path — create
# ---------------------------------------------------------------------------

class TestCreate(unittest.TestCase):

    def test_create_regular_file_bytes_correct(self):
        content = b"hello from the object store\n"
        with RestoreFixture() as fix:
            fix.add_regular(".zshrc", content, mode="0644")
            plan = fix.build_plan()
            rc = fix.run_apply(plan)
            dest = fix.target_dir / ".zshrc"
            self.assertEqual(rc, 0)
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), content)

    def test_create_file_mode_set(self):
        content = b"exec content"
        with RestoreFixture() as fix:
            fix.add_regular(".my_script", content, mode="0755")
            plan = fix.build_plan()
            rc = fix.run_apply(plan)
            dest = fix.target_dir / ".my_script"
            self.assertEqual(rc, 0)
            actual_mode = stat.S_IMODE(dest.stat().st_mode)
            self.assertEqual(actual_mode, 0o755)

    def test_create_file_mtime_set(self):
        from datetime import datetime, timezone
        content = b"timestamped content"
        mtime_str = "2026-04-15T08:30:00Z"
        with RestoreFixture() as fix:
            fix.add_regular(".profile", content, mtime=mtime_str)
            plan = fix.build_plan()
            rc = fix.run_apply(plan)
            dest = fix.target_dir / ".profile"
            self.assertEqual(rc, 0)
            expected_ts = datetime.fromisoformat(
                mtime_str.replace("Z", "+00:00")
            ).timestamp()
            actual_mtime = dest.stat().st_mtime
            self.assertAlmostEqual(actual_mtime, expected_ts, delta=2.0)

    def test_create_nested_path_parents_created(self):
        content = b"nested config"
        with RestoreFixture() as fix:
            fix.add_regular(".config/nvim/init.lua", content)
            plan = fix.build_plan()
            rc = fix.run_apply(plan)
            dest = fix.target_dir / ".config" / "nvim" / "init.lua"
            self.assertEqual(rc, 0)
            self.assertTrue(dest.exists())
            self.assertEqual(dest.read_bytes(), content)


# ---------------------------------------------------------------------------
# Tests: skip-identical
# ---------------------------------------------------------------------------

class TestSkipIdentical(unittest.TestCase):

    def test_skip_identical_does_not_modify_file(self):
        content = b"same content everywhere"
        with RestoreFixture() as fix:
            fix.add_regular(".vimrc", content)
            # Pre-populate the target with the identical content.
            dest = fix.write_target_file(".vimrc", content)
            mtime_before = dest.stat().st_mtime

            plan = fix.build_plan()  # Should produce skip-identical for .vimrc.

            # Verify the plan contains the expected action type.
            actions = {
                a["path"]: a
                for a in plan["actions_by_category"]["dotfiles"]
            }
            self.assertEqual(actions[".vimrc"]["type"], "skip-identical")

            rc = fix.run_apply(plan)
            mtime_after = dest.stat().st_mtime

            self.assertEqual(rc, 0)
            self.assertEqual(
                mtime_before, mtime_after,
                "skip-identical must not touch the file's mtime."
            )
            self.assertEqual(dest.read_bytes(), content)


# ---------------------------------------------------------------------------
# Tests: overwrite-needs-confirm
# ---------------------------------------------------------------------------

class TestOverwrite(unittest.TestCase):

    def _setup_overwrite_fixture(self):
        """Returns (fixture, expected_content, diverged_content)."""
        expected_content = b"the content from the snapshot"
        diverged_content = b"different content currently on disk"
        return expected_content, diverged_content

    def test_overwrite_without_flag_exits_nonzero(self):
        expected_content, diverged_content = self._setup_overwrite_fixture()
        with RestoreFixture() as fix:
            fix.add_regular(".tmux.conf", expected_content)
            fix.write_target_file(".tmux.conf", diverged_content)
            plan = fix.build_plan()  # overwrite-needs-confirm action

            # Verify plan contains overwrite-needs-confirm.
            actions = {
                a["path"]: a
                for a in plan["actions_by_category"]["dotfiles"]
            }
            self.assertEqual(
                actions[".tmux.conf"]["type"], "overwrite-needs-confirm"
            )

            rc = fix.run_apply(plan, overwrite=False)

        self.assertNotEqual(rc, 0)

    def test_overwrite_without_flag_writes_nothing(self):
        expected_content, diverged_content = self._setup_overwrite_fixture()
        with RestoreFixture() as fix:
            fix.add_regular(".tmux.conf", expected_content)
            dest = fix.write_target_file(".tmux.conf", diverged_content)
            mtime_before = dest.stat().st_mtime
            plan = fix.build_plan()

            fix.run_apply(plan, overwrite=False)

            # File must be untouched.
            self.assertEqual(dest.read_bytes(), diverged_content)
            self.assertEqual(dest.stat().st_mtime, mtime_before)

    def test_overwrite_with_flag_replaces_file(self):
        expected_content, diverged_content = self._setup_overwrite_fixture()
        with RestoreFixture() as fix:
            fix.add_regular(".tmux.conf", expected_content)
            fix.write_target_file(".tmux.conf", diverged_content)
            plan = fix.build_plan()

            rc = fix.run_apply(plan, overwrite=True)
            self.assertEqual(rc, 0)
            self.assertEqual(
                (fix.target_dir / ".tmux.conf").read_bytes(), expected_content
            )

    def test_overwrite_with_flag_bytes_correct(self):
        expected_content = b"correct content\x00binary\xff"
        with RestoreFixture() as fix:
            fix.add_regular(".gitconfig", expected_content)
            fix.write_target_file(".gitconfig", b"wrong")
            plan = fix.build_plan()

            rc = fix.run_apply(plan, overwrite=True)
            self.assertEqual(rc, 0)
            self.assertEqual(
                (fix.target_dir / ".gitconfig").read_bytes(), expected_content
            )


# ---------------------------------------------------------------------------
# Tests: symlink handling
# ---------------------------------------------------------------------------

class TestSymlinks(unittest.TestCase):
    LINK_TARGET = "/opt/homebrew/bin/python3"

    def test_symlink_created(self):
        with RestoreFixture() as fix:
            fix.add_symlink(".local/bin/python3", self.LINK_TARGET)
            plan = fix.build_plan()
            rc = fix.run_apply(plan)
            dest = fix.target_dir / ".local" / "bin" / "python3"
            self.assertEqual(rc, 0)
            self.assertTrue(dest.is_symlink())
            self.assertEqual(os.readlink(dest), self.LINK_TARGET)

    def test_symlink_target_string_matches_manifest(self):
        link_target = "/usr/local/bin/node"
        with RestoreFixture() as fix:
            fix.add_symlink(".local/bin/node", link_target)
            plan = fix.build_plan()
            fix.run_apply(plan)
            dest = fix.target_dir / ".local" / "bin" / "node"
            self.assertEqual(os.readlink(dest), link_target)

    def test_symlink_not_followed_during_apply(self):
        """A symlink in the target must be overwritten; the script must NOT
        follow it and write to the file the symlink points to."""
        # The symlink in target_dir points to a file OUTSIDE target_dir.
        # If the script follows symlinks it would write to that external file.
        expected_content = b"new content from snapshot"
        with RestoreFixture() as fix:
            fix.add_regular(".followed_test", expected_content)

            # Create an external file outside target.
            outside_file = fix.base / "outside_file.txt"
            outside_file.write_bytes(b"original outside content")

            # Place a symlink in target that points to the outside file.
            dest = fix.target_dir / ".followed_test"
            os.symlink(str(outside_file), dest)

            plan = fix.build_plan()  # overwrite-needs-confirm (symlink vs regular)

            # The action should be overwrite-needs-confirm since a symlink is
            # there but the manifest expects a regular file.
            actions = {
                a["path"]: a
                for a in plan["actions_by_category"]["dotfiles"]
            }
            self.assertEqual(
                actions[".followed_test"]["type"], "overwrite-needs-confirm"
            )

            rc = fix.run_apply(plan, overwrite=True)

            self.assertEqual(rc, 0)
            # The outside file must be untouched.
            self.assertEqual(outside_file.read_bytes(), b"original outside content")
            # The target must now be the restored regular file, not a symlink.
            dest = fix.target_dir / ".followed_test"
            self.assertFalse(dest.is_symlink())
            self.assertEqual(dest.read_bytes(), expected_content)


# ---------------------------------------------------------------------------
# Tests: refusal rules
# ---------------------------------------------------------------------------

class TestRefusalRules(unittest.TestCase):

    def _minimal_plan(self, target: str) -> dict:
        return _make_plan({"dotfiles": []}, target=target)

    def _make_minimal_snapshot(self, base: Path) -> Path:
        snap_dir = base / "snapshots" / "ref-snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        manifest = _make_manifest([])
        (snap_dir / "manifest.json").write_text(json.dumps(manifest))
        # objects/ is at <drive>/objects, i.e. <snapshot>/../../objects
        objects_dir = base / "objects"
        objects_dir.mkdir(exist_ok=True)
        return snap_dir

    def _run_with_target(
        self,
        target_str: str,
        *,
        live: bool = False,
        create_target: bool = True,
    ) -> int:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            snap_dir = self._make_minimal_snapshot(base)
            plan = self._minimal_plan(target_str)

            if create_target and target_str != "/" and not target_str.startswith("/dev"):
                try:
                    Path(target_str).mkdir(parents=True, exist_ok=True)
                except (PermissionError, OSError):
                    pass

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as plan_fh:
                json.dump(plan, plan_fh)
                plan_path = plan_fh.name

            try:
                argv = [
                    "--plan", plan_path,
                    "--snapshot", str(snap_dir),
                    "--target", target_str,
                ]
                if live:
                    argv.append("--live")

                with patch.dict(
                    os.environ,
                    {"REHYDRATE_TARGET": "", "USER": "testuser"},
                    clear=False,
                ):
                    return ra.main(argv)
            finally:
                try:
                    os.unlink(plan_path)
                except FileNotFoundError:
                    pass

    def test_refuses_target_root(self):
        rc = self._run_with_target("/", create_target=False)
        self.assertNotEqual(rc, 0)

    def test_refuses_live_home_without_live_flag(self):
        """Target is a tempdir that we pretend is $HOME — refuse without --live."""
        with tempfile.TemporaryDirectory() as fake_home:
            # Mock _resolve_home so that the fake_home IS the "live" home.
            with patch.object(ra, "_resolve_home", return_value=Path(fake_home).resolve()):
                rc = self._run_with_target(fake_home, live=False)
            self.assertNotEqual(rc, 0)

    def test_allows_live_home_with_live_flag(self):
        """Same setup but --live is given — should NOT refuse (may still fail for
        other reasons, but the home-refusal rule must pass)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            snap_dir = self._make_minimal_snapshot(base)
            fake_home = base / "fake_home"
            fake_home.mkdir()
            plan = self._minimal_plan(str(fake_home))

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as plan_fh:
                json.dump(plan, plan_fh)
                plan_path = plan_fh.name

            try:
                argv = [
                    "--plan", plan_path,
                    "--snapshot", str(snap_dir),
                    "--target", str(fake_home),
                    "--live",
                ]
                with patch.object(ra, "_resolve_home", return_value=fake_home.resolve()):
                    with patch.dict(
                        os.environ,
                        {"REHYDRATE_TARGET": "", "USER": "testuser"},
                        clear=False,
                    ):
                        rc = ra.main(argv)
            finally:
                try:
                    os.unlink(plan_path)
                except FileNotFoundError:
                    pass

        # rc == 0: empty plan, no errors.
        self.assertEqual(rc, 0)

    def test_refuses_inside_library(self):
        """Target inside ~/Library must be refused unconditionally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            snap_dir = self._make_minimal_snapshot(base)

            # Fake a ~/Library/Preferences-like path.
            fake_home = base / "fake_home"
            fake_library = fake_home / "Library" / "Preferences"
            fake_library.mkdir(parents=True)

            plan = self._minimal_plan(str(fake_library))

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as plan_fh:
                json.dump(plan, plan_fh)
                plan_path = plan_fh.name

            try:
                argv = [
                    "--plan", plan_path,
                    "--snapshot", str(snap_dir),
                    "--target", str(fake_library),
                ]
                # Make _resolve_home return our fake home so Library is relative to it.
                with patch.object(ra, "_resolve_home", return_value=fake_home.resolve()):
                    with patch.dict(
                        os.environ,
                        {"REHYDRATE_TARGET": "", "USER": "testuser"},
                        clear=False,
                    ):
                        rc = ra.main(argv)
            finally:
                try:
                    os.unlink(plan_path)
                except FileNotFoundError:
                    pass

        self.assertNotEqual(rc, 0)

    def test_refuses_nonexistent_target(self):
        """Target directory that does not exist → refuse."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            snap_dir = self._make_minimal_snapshot(base)
            nonexistent = base / "does_not_exist"
            plan = self._minimal_plan(str(nonexistent))

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as plan_fh:
                json.dump(plan, plan_fh)
                plan_path = plan_fh.name

            try:
                argv = [
                    "--plan", plan_path,
                    "--snapshot", str(snap_dir),
                    "--target", str(nonexistent),
                ]
                with patch.dict(
                    os.environ,
                    {"REHYDRATE_TARGET": "", "USER": "testuser"},
                    clear=False,
                ):
                    rc = ra.main(argv)
            finally:
                try:
                    os.unlink(plan_path)
                except FileNotFoundError:
                    pass

        self.assertNotEqual(rc, 0)

    def test_refuses_missing_target_env_and_flag(self):
        """Neither --target nor $REHYDRATE_TARGET → non-zero."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            snap_dir = self._make_minimal_snapshot(base)
            plan = self._minimal_plan(str(base / "target"))

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as plan_fh:
                json.dump(plan, plan_fh)
                plan_path = plan_fh.name

            try:
                argv = [
                    "--plan", plan_path,
                    "--snapshot", str(snap_dir),
                    # No --target.
                ]
                env = {k: v for k, v in os.environ.items()}
                env.pop("REHYDRATE_TARGET", None)

                with patch.dict(os.environ, env, clear=True):
                    rc = ra.main(argv)
            finally:
                try:
                    os.unlink(plan_path)
                except FileNotFoundError:
                    pass

        self.assertNotEqual(rc, 0)


# ---------------------------------------------------------------------------
# Tests: object hash mismatch
# ---------------------------------------------------------------------------

class TestObjectHashMismatch(unittest.TestCase):

    def test_hash_mismatch_exits_nonzero(self):
        content = b"legitimate content"
        with RestoreFixture() as fix:
            entry = fix.add_regular(".zshrc", content)
            # Corrupt the object after recording its hash.
            fix.corrupt_object(entry["object_hash"])
            plan = fix.build_plan()
            rc = fix.run_apply(plan)
            self.assertNotEqual(rc, 0)

    def test_hash_mismatch_writes_nothing(self):
        content = b"legitimate content"
        with RestoreFixture() as fix:
            entry = fix.add_regular(".zshrc", content)
            fix.corrupt_object(entry["object_hash"])
            plan = fix.build_plan()
            fix.run_apply(plan)
            # .zshrc must not exist in the target.
            dest = fix.target_dir / ".zshrc"
            self.assertFalse(dest.exists())


# ---------------------------------------------------------------------------
# Tests: --dry-run
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def test_dry_run_writes_nothing(self):
        content = b"would-be written content"
        with RestoreFixture() as fix:
            fix.add_regular(".zshrc", content)
            plan = fix.build_plan()
            rc = fix.run_apply(plan, dry_run=True)
            dest = fix.target_dir / ".zshrc"
            self.assertEqual(rc, 0)
            self.assertFalse(dest.exists(), "dry-run must not create files")

    def test_dry_run_returns_zero_on_valid_plan(self):
        with RestoreFixture() as fix:
            fix.add_regular(".profile", b"profile content")
            plan = fix.build_plan()
            rc = fix.run_apply(plan, dry_run=True)
            self.assertEqual(rc, 0)

    def test_dry_run_with_overwrite_writes_nothing(self):
        expected_content = b"expected"
        diverged_content = b"diverged"
        with RestoreFixture() as fix:
            fix.add_regular(".bashrc", expected_content)
            dest = fix.write_target_file(".bashrc", diverged_content)
            plan = fix.build_plan()
            rc = fix.run_apply(plan, overwrite=True, dry_run=True)
            self.assertEqual(rc, 0)
            self.assertEqual(dest.read_bytes(), diverged_content)


# ---------------------------------------------------------------------------
# Tests: re-run safety after partial failure
# ---------------------------------------------------------------------------

class TestRerunSafety(unittest.TestCase):

    def test_rerun_after_partial_failure_safe(self):
        """Simulates a partial apply: file A was created, file B failed.
        Re-running should create B and skip A (skip-identical) without error.

        We verify by: setting up two files, corrupting B's object,
        running apply (fails), fixing B's object, running apply again.
        On the second run, A is already present with the correct hash so the
        plan produced by restore-plan.py would mark it skip-identical.
        Here we build the plan manually to exercise the skip path.
        """
        content_a = b"file A content"
        content_b = b"file B content"

        with RestoreFixture() as fix:
            entry_a = fix.add_regular(".file_a", content_a)
            entry_b = fix.add_regular(".file_b", content_b)

            # Corrupt B before first run.
            fix.corrupt_object(entry_b["object_hash"])

            plan = fix.build_plan()
            rc1 = fix.run_apply(plan)
            self.assertNotEqual(rc1, 0)

            # Restore the correct object for B.
            fix._write_object(entry_b["object_hash"], content_b)

            # Build a new plan reflecting the current state of the target:
            # A was created, so it should be skip-identical; B is missing.
            plan2 = fix.build_plan()
            a_actions = {
                a["path"]: a
                for a in plan2["actions_by_category"]["dotfiles"]
            }
            self.assertEqual(a_actions[".file_a"]["type"], "skip-identical")
            self.assertEqual(a_actions[".file_b"]["type"], "create")

            rc2 = fix.run_apply(plan2)

            self.assertEqual(rc2, 0)
            self.assertEqual((fix.target_dir / ".file_a").read_bytes(), content_a)
            self.assertEqual((fix.target_dir / ".file_b").read_bytes(), content_b)


# ---------------------------------------------------------------------------
# Tests: plan schema validation gate
# ---------------------------------------------------------------------------

class TestPlanSchemaValidation(unittest.TestCase):

    def test_invalid_plan_exits_nonzero(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema not installed")

        with RestoreFixture() as fix:
            fix.write_manifest()
            bad_plan = {"not": "a valid plan"}

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as plan_fh:
                json.dump(bad_plan, plan_fh)
                plan_path = plan_fh.name

            try:
                argv = [
                    "--plan", plan_path,
                    "--snapshot", str(fix.snapshot_dir),
                    "--target", str(fix.target_dir),
                ]
                with patch.dict(
                    os.environ,
                    {"REHYDRATE_TARGET": "", "USER": "testuser"},
                    clear=False,
                ):
                    rc = ra.main(argv)
            finally:
                try:
                    os.unlink(plan_path)
                except FileNotFoundError:
                    pass

        self.assertNotEqual(rc, 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
