"""
test_walk_apps.py — unit tests for scripts/walk-apps.py

Tests use a fixture Applications/ tree under a tempdir with --home <tempdir>.
No real /Applications/ scanning is needed — the home-dir Applications/ is
sufficient to exercise all code paths.
"""

from __future__ import annotations

import io
import json
import plistlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the module under test via importlib (walk-apps.py is not a valid
# Python identifier as a module name).
import importlib.util as _ilu

_WALKER_PATH = _REPO_ROOT / "scripts" / "walk-apps.py"
_spec = _ilu.spec_from_file_location("walk_apps", _WALKER_PATH)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

walk_apps = _mod.walk_apps
APPS_SUBDIR = _mod.APPS_SUBDIR
INVENTORY_FILENAME = _mod.INVENTORY_FILENAME
CATEGORY_NAME = _mod.CATEGORY_NAME


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_plist(path: Path, data: dict) -> None:
    """Write a binary plist file at *path* from *data*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        plistlib.dump(data, fh)


def _make_app(
    apps_dir: Path,
    app_name: str,
    plist_data: dict | None,
    *,
    mas_receipt: bool = False,
) -> Path:
    """
    Create a minimal .app bundle under *apps_dir*.

    - *plist_data* is written to Contents/Info.plist (skipped if None)
    - *mas_receipt* creates Contents/_MASReceipt/receipt
    """
    app_path = apps_dir / f"{app_name}.app"
    contents = app_path / "Contents"
    contents.mkdir(parents=True, exist_ok=True)

    if plist_data is not None:
        _make_plist(contents / "Info.plist", plist_data)

    if mas_receipt:
        receipt_dir = contents / "_MASReceipt"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / "receipt").write_bytes(b"fake-receipt")

    return app_path


# ---------------------------------------------------------------------------
# TestSourceDetection
# ---------------------------------------------------------------------------

class TestSourceDetection(unittest.TestCase):
    """Verify source heuristic: appstore, cask, manual."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self) -> dict:
        return walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])

    def test_mas_receipt_gives_appstore_source(self) -> None:
        """An app with _MASReceipt/receipt should have source: appstore."""
        _make_app(
            self.apps_dir,
            "AppStoreApp",
            plist_data={
                "CFBundleName": "AppStoreApp",
                "CFBundleIdentifier": "com.example.appstore",
                "CFBundleShortVersionString": "3.0",
                "CFBundleVersion": "300",
            },
            mas_receipt=True,
        )
        result = self._run()
        apps = result["category"] and json.loads(
            (Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text()
        )["apps"]
        match = next((a for a in apps if a["bundle_id"] == "com.example.appstore"), None)
        self.assertIsNotNone(match, "App not found in inventory")
        self.assertEqual(match["source"], "appstore")

    def test_bundle_id_without_receipt_gives_cask_source(self) -> None:
        """An app with a bundle ID but no MAS receipt should have source: cask."""
        _make_app(
            self.apps_dir,
            "CaskApp",
            plist_data={
                "CFBundleName": "CaskApp",
                "CFBundleIdentifier": "com.example.cask",
                "CFBundleShortVersionString": "2.1",
                "CFBundleVersion": "210",
            },
        )
        result = self._run()
        inv_path = Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME
        apps = json.loads(inv_path.read_text())["apps"]
        match = next((a for a in apps if a["bundle_id"] == "com.example.cask"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["source"], "cask")

    def test_no_bundle_id_gives_manual_source(self) -> None:
        """An app with only CFBundleName and no bundle ID should have source: manual."""
        _make_app(
            self.apps_dir,
            "ManualApp",
            plist_data={
                "CFBundleName": "ManualApp",
                # No CFBundleIdentifier
            },
        )
        result = self._run()
        inv_path = Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME
        apps = json.loads(inv_path.read_text())["apps"]
        match = next((a for a in apps if a["name"] == "ManualApp"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match["source"], "manual")
        self.assertIsNone(match["bundle_id"])

    def test_missing_plist_goes_to_skipped(self) -> None:
        """An app with no Info.plist should appear in coverage.skipped."""
        _make_app(self.apps_dir, "NoPlsitApp", plist_data=None)
        result = self._run()
        skipped = result["coverage"]["skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "info-plist-unreadable")
        self.assertNotIn("NoPlsitApp", [a.get("name") for a in
            json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())["apps"]])


# ---------------------------------------------------------------------------
# TestInventoryFields
# ---------------------------------------------------------------------------

class TestInventoryFields(unittest.TestCase):
    """Verify the app entry fields are populated correctly."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_four_bundle_keys_extracted(self) -> None:
        """A fully-populated plist should produce all four fields."""
        _make_app(
            self.apps_dir,
            "FullApp",
            plist_data={
                "CFBundleName": "Full App",
                "CFBundleIdentifier": "com.example.full",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleVersion": "456",
            },
        )
        walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        inv = json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())
        app = inv["apps"][0]
        self.assertEqual(app["name"], "Full App")
        self.assertEqual(app["bundle_id"], "com.example.full")
        self.assertEqual(app["version"], "1.2.3")
        self.assertEqual(app["build"], "456")

    def test_partial_plist_fallbacks(self) -> None:
        """Only CFBundleName present; other fields should be None."""
        _make_app(
            self.apps_dir,
            "PartialApp",
            plist_data={
                "CFBundleName": "Partial App",
            },
        )
        walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        inv = json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())
        app = inv["apps"][0]
        self.assertEqual(app["name"], "Partial App")
        self.assertIsNone(app["bundle_id"])
        self.assertIsNone(app["version"])
        self.assertIsNone(app["build"])

    def test_fallback_name_is_dir_name_without_dot_app(self) -> None:
        """No CFBundleName in plist → fallback to directory name minus .app."""
        _make_app(
            self.apps_dir,
            "FallbackName",
            plist_data={
                "CFBundleIdentifier": "com.example.fallback",
            },
        )
        walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        inv = json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())
        app = next(a for a in inv["apps"] if a["bundle_id"] == "com.example.fallback")
        self.assertEqual(app["name"], "FallbackName")


# ---------------------------------------------------------------------------
# TestSorting
# ---------------------------------------------------------------------------

class TestSorting(unittest.TestCase):
    """Inventory must be sorted: bundle_id entries first, then None-id entries by name."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_and_load(self) -> list[dict]:
        walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        return json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())["apps"]

    def test_bundle_id_apps_before_no_bundle_id_apps(self) -> None:
        _make_app(self.apps_dir, "Zebra", plist_data={"CFBundleName": "Zebra"})  # no bundle_id
        _make_app(self.apps_dir, "Alpha", plist_data={
            "CFBundleName": "Alpha", "CFBundleIdentifier": "com.z.app",
        })
        apps = self._run_and_load()
        ids = [a["bundle_id"] for a in apps]
        # All non-None bundle_ids must come before any None
        saw_none = False
        for bid in ids:
            if bid is None:
                saw_none = True
            else:
                self.assertFalse(saw_none, "Non-None bundle_id appeared after a None bundle_id")

    def test_bundle_id_entries_sorted_alphabetically(self) -> None:
        for bid, name in [("com.z.app", "Zapp"), ("com.a.app", "Aapp"), ("com.m.app", "Mapp")]:
            _make_app(self.apps_dir, name, plist_data={
                "CFBundleName": name, "CFBundleIdentifier": bid,
            })
        apps = self._run_and_load()
        bundle_ids = [a["bundle_id"] for a in apps if a["bundle_id"]]
        self.assertEqual(bundle_ids, sorted(bundle_ids))

    def test_no_bundle_id_entries_sorted_by_name(self) -> None:
        for name in ["Zebra", "Apple", "Mango"]:
            _make_app(self.apps_dir, name, plist_data={"CFBundleName": name})
        apps = self._run_and_load()
        names = [a["name"] for a in apps if a["bundle_id"] is None]
        self.assertEqual(names, sorted(names, key=str.lower))


# ---------------------------------------------------------------------------
# TestOutputShape
# ---------------------------------------------------------------------------

class TestOutputShape(unittest.TestCase):
    """The walk_apps() return value must match the documented walk-output shape."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()
        _make_app(
            self.apps_dir,
            "TestApp",
            plist_data={
                "CFBundleName": "Test App",
                "CFBundleIdentifier": "com.example.test",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "100",
            },
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self) -> dict:
        return walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])

    def test_top_level_keys(self) -> None:
        result = self._run()
        for key in ("category", "workdir", "files", "coverage"):
            self.assertIn(key, result)

    def test_category_value(self) -> None:
        result = self._run()
        self.assertEqual(result["category"], CATEGORY_NAME)

    def test_workdir_value(self) -> None:
        result = self._run()
        self.assertEqual(result["workdir"], self.tmpdir)

    def test_files_has_exactly_one_entry(self) -> None:
        result = self._run()
        self.assertEqual(len(result["files"]), 1)

    def test_file_entry_path_is_inventory(self) -> None:
        result = self._run()
        self.assertEqual(
            result["files"][0]["path"],
            f"{APPS_SUBDIR}/{INVENTORY_FILENAME}",
        )

    def test_file_entry_path_exact(self) -> None:
        """Exact string check: .rehydrate/apps/inventory.json"""
        result = self._run()
        self.assertEqual(result["files"][0]["path"], ".rehydrate/apps/inventory.json")

    def test_file_entry_mode_is_0644(self) -> None:
        result = self._run()
        self.assertEqual(result["files"][0]["mode"], "0644")

    def test_file_entry_is_symlink_false(self) -> None:
        result = self._run()
        self.assertFalse(result["files"][0]["is_symlink"])

    def test_file_entry_symlink_target_null(self) -> None:
        result = self._run()
        self.assertIsNone(result["files"][0]["symlink_target"])

    def test_file_entry_no_object_hash(self) -> None:
        """object_hash must NOT be included — snapshot.py computes it."""
        result = self._run()
        self.assertNotIn("object_hash", result["files"][0])

    def test_coverage_sub_keys(self) -> None:
        result = self._run()
        cov = result["coverage"]
        for key in ("globs", "skipped", "large_files_warned"):
            self.assertIn(key, cov)

    def test_coverage_globs_is_empty_dict(self) -> None:
        result = self._run()
        self.assertIsInstance(result["coverage"]["globs"], dict)
        self.assertEqual(result["coverage"]["globs"], {})

    def test_is_json_serialisable(self) -> None:
        result = self._run()
        serialised = json.dumps(result)
        self.assertIsInstance(serialised, str)

    def test_inventory_file_written_to_workdir(self) -> None:
        self._run()
        inv_path = Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME
        self.assertTrue(inv_path.exists())

    def test_file_entry_size_matches_written_bytes(self) -> None:
        result = self._run()
        entry = result["files"][0]
        written = Path(self.tmpdir) / entry["path"]
        self.assertEqual(entry["size"], written.stat().st_size)


# ---------------------------------------------------------------------------
# TestInventoryStructure
# ---------------------------------------------------------------------------

class TestInventoryStructure(unittest.TestCase):
    """Verify the inventory.json internal structure."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()
        _make_app(
            self.apps_dir,
            "InventoryApp",
            plist_data={
                "CFBundleName": "Inventory App",
                "CFBundleIdentifier": "com.example.inv",
                "CFBundleShortVersionString": "5.0",
                "CFBundleVersion": "500",
            },
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _load_inventory(self) -> dict:
        walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        return json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())

    def test_inventory_has_schema_version(self) -> None:
        inv = self._load_inventory()
        self.assertIn("schema_version", inv)
        self.assertEqual(inv["schema_version"], "0.1.0")

    def test_inventory_has_scanned_at(self) -> None:
        inv = self._load_inventory()
        self.assertIn("scanned_at", inv)
        # Should look like an ISO 8601 UTC timestamp
        self.assertTrue(inv["scanned_at"].endswith("Z"))

    def test_inventory_has_apps_list(self) -> None:
        inv = self._load_inventory()
        self.assertIn("apps", inv)
        self.assertIsInstance(inv["apps"], list)

    def test_app_entry_has_required_keys(self) -> None:
        inv = self._load_inventory()
        app = inv["apps"][0]
        for key in ("name", "bundle_id", "version", "build", "path", "source"):
            self.assertIn(key, app)


# ---------------------------------------------------------------------------
# TestNoPiiLogged
# ---------------------------------------------------------------------------

class TestNoPiiLogged(unittest.TestCase):
    """
    App names and bundle IDs must never appear in the stderr log output.

    We use a unique sentinel string as the app name and bundle ID and verify
    it is absent from captured stderr.
    """

    SENTINEL_NAME = "SENTINEL_APP_XYZ_PRIVATE"
    SENTINEL_BID = "com.sentinel.xyz.private"

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()
        _make_app(
            self.apps_dir,
            self.SENTINEL_NAME,
            plist_data={
                "CFBundleName": self.SENTINEL_NAME,
                "CFBundleIdentifier": self.SENTINEL_BID,
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "100",
            },
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _capture_stderr(self) -> str:
        buf = io.StringIO()
        original_stderr = sys.stderr
        try:
            sys.stderr = buf
            walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        finally:
            sys.stderr = original_stderr
        return buf.getvalue()

    def test_app_name_not_in_stderr(self) -> None:
        captured = self._capture_stderr()
        self.assertNotIn(self.SENTINEL_NAME, captured)

    def test_bundle_id_not_in_stderr(self) -> None:
        captured = self._capture_stderr()
        self.assertNotIn(self.SENTINEL_BID, captured)

    def test_sentinel_not_in_stderr_at_all(self) -> None:
        captured = self._capture_stderr()
        self.assertNotIn("SENTINEL", captured)


# ---------------------------------------------------------------------------
# TestSkippedApps
# ---------------------------------------------------------------------------

class TestSkippedApps(unittest.TestCase):
    """Apps with unreadable or missing Info.plist appear in coverage.skipped."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_plist_in_skipped(self) -> None:
        _make_app(self.apps_dir, "BrokenApp", plist_data=None)
        result = walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        skipped = result["coverage"]["skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "info-plist-unreadable")

    def test_skipped_entry_has_path(self) -> None:
        _make_app(self.apps_dir, "BrokenApp2", plist_data=None)
        result = walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        skipped = result["coverage"]["skipped"]
        self.assertIn("path", skipped[0])

    def test_skipped_app_not_in_apps_list(self) -> None:
        _make_app(self.apps_dir, "GoodApp", plist_data={
            "CFBundleName": "Good App",
            "CFBundleIdentifier": "com.example.good",
        })
        _make_app(self.apps_dir, "BadApp", plist_data=None)
        result = walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        inv = json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())
        self.assertEqual(len(inv["apps"]), 1)
        self.assertEqual(len(result["coverage"]["skipped"]), 1)

    def test_corrupted_plist_in_skipped(self) -> None:
        """A file named Info.plist that is not valid binary plist should be skipped."""
        app_path = self.apps_dir / "CorruptApp.app" / "Contents"
        app_path.mkdir(parents=True)
        (app_path / "Info.plist").write_bytes(b"this is not a valid plist \x00\xff")
        result = walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        skipped = result["coverage"]["skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "info-plist-unreadable")


# ---------------------------------------------------------------------------
# TestMixedFixtures
# ---------------------------------------------------------------------------

class TestMixedFixtures(unittest.TestCase):
    """
    Integration-style test with all four fixture types together:
    - A valid app with all four bundle keys → source: cask
    - An app with MAS receipt → source: appstore
    - An app with no Info.plist → skipped
    - An app with only CFBundleName → source: manual, bundle_id: null
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="rh-apps-test-")
        self.apps_dir = Path(self.tmpdir) / "Applications"
        self.apps_dir.mkdir()

        # Fixture 1: valid full app — source: cask
        _make_app(
            self.apps_dir,
            "FullCask",
            plist_data={
                "CFBundleName": "Full Cask",
                "CFBundleIdentifier": "com.example.fullcask",
                "CFBundleShortVersionString": "4.0",
                "CFBundleVersion": "400",
            },
        )

        # Fixture 2: MAS receipt — source: appstore
        _make_app(
            self.apps_dir,
            "MASApp",
            plist_data={
                "CFBundleName": "MAS App",
                "CFBundleIdentifier": "com.example.masapp",
                "CFBundleShortVersionString": "2.0",
                "CFBundleVersion": "200",
            },
            mas_receipt=True,
        )

        # Fixture 3: no Info.plist → skipped
        _make_app(self.apps_dir, "NoPlist", plist_data=None)

        # Fixture 4: only CFBundleName → source: manual, bundle_id: null
        _make_app(
            self.apps_dir,
            "NameOnly",
            plist_data={"CFBundleName": "Name Only App"},
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self) -> tuple[dict, dict]:
        result = walk_apps(home=self.tmpdir, workdir=self.tmpdir, _app_roots=[self.apps_dir])
        inv = json.loads((Path(self.tmpdir) / APPS_SUBDIR / INVENTORY_FILENAME).read_text())
        return result, inv

    def test_three_apps_in_inventory(self) -> None:
        _, inv = self._run()
        self.assertEqual(len(inv["apps"]), 3)

    def test_one_entry_in_skipped(self) -> None:
        result, _ = self._run()
        self.assertEqual(len(result["coverage"]["skipped"]), 1)

    def test_full_cask_source_is_cask(self) -> None:
        _, inv = self._run()
        app = next(a for a in inv["apps"] if a.get("bundle_id") == "com.example.fullcask")
        self.assertEqual(app["source"], "cask")

    def test_mas_app_source_is_appstore(self) -> None:
        _, inv = self._run()
        app = next(a for a in inv["apps"] if a.get("bundle_id") == "com.example.masapp")
        self.assertEqual(app["source"], "appstore")

    def test_name_only_source_is_manual(self) -> None:
        _, inv = self._run()
        app = next(a for a in inv["apps"] if a["name"] == "Name Only App")
        self.assertEqual(app["source"], "manual")
        self.assertIsNone(app["bundle_id"])

    def test_inventory_sorted_bid_apps_before_none(self) -> None:
        _, inv = self._run()
        apps = inv["apps"]
        # First two should have bundle_ids, last should be None
        self.assertIsNotNone(apps[0]["bundle_id"])
        self.assertIsNotNone(apps[1]["bundle_id"])
        self.assertIsNone(apps[2]["bundle_id"])

    def test_file_entry_path_is_inventory(self) -> None:
        result, _ = self._run()
        self.assertEqual(result["files"][0]["path"], ".rehydrate/apps/inventory.json")


if __name__ == "__main__":
    unittest.main()
