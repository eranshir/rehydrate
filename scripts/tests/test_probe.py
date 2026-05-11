"""
Unit tests for scripts/probe.py.

Coverage:
- JSON output matches expected structure (mocked subprocesses).
- Output validates against the ``source_machine`` schema block.
- ``--out PATH`` writes to file; stdout is empty.
- Subprocess timeout → non-zero exit.
- os.environ is NOT fully dumped (secret-looking var absent from output).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.probe as probe_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fixed fake values returned by mocked subprocesses.
_FAKE_OS_VERSION = "15.4.1"
_FAKE_BUILD = "24E263"
_FAKE_HW_MODEL = "MacBookPro18,3"
_FAKE_HW_MEMSIZE = "34359738368"

_FAKE_ENVIRON = {
    "USER": "testuser",
    "SHELL": "/bin/zsh",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "TOP_SECRET_TOKEN": "s3cr3t-should-never-appear",
}


def _make_completed_process(stdout: str) -> MagicMock:
    """Return a mock resembling subprocess.CompletedProcess with given stdout."""
    cp = MagicMock()
    cp.stdout = stdout
    cp.returncode = 0
    return cp


def _sw_vers_side_effect(args, **kwargs):
    """Dispatch mock output based on sw_vers argument."""
    if "-productVersion" in args:
        return _make_completed_process(_FAKE_OS_VERSION)
    if "-buildVersion" in args:
        return _make_completed_process(_FAKE_BUILD)
    return _make_completed_process("")


def _sysctl_side_effect(args, **kwargs):
    """Dispatch mock output based on sysctl argument."""
    if "hw.model" in args:
        return _make_completed_process(_FAKE_HW_MODEL)
    if "hw.memsize" in args:
        return _make_completed_process(_FAKE_HW_MEMSIZE)
    return _make_completed_process("")


def _subprocess_side_effect(args, **kwargs):
    """Combined dispatcher for both sw_vers and sysctl."""
    if args[0] == "sw_vers":
        return _sw_vers_side_effect(args, **kwargs)
    if args[0] == "sysctl":
        return _sysctl_side_effect(args, **kwargs)
    return _make_completed_process("")


# ---------------------------------------------------------------------------
# Test: JSON structure
# ---------------------------------------------------------------------------

class TestProbeStructure(unittest.TestCase):
    """Probe returns a dict with the correct keys and value types."""

    def _run_probe(self):
        with (
            patch("subprocess.run", side_effect=_subprocess_side_effect),
            patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
        ):
            return probe_mod.probe()

    def test_top_level_keys(self):
        data = self._run_probe()
        expected = {"os", "os_version", "build", "hostname", "user", "hardware", "shell", "path"}
        self.assertEqual(set(data.keys()), expected)

    def test_os_is_macos(self):
        data = self._run_probe()
        self.assertEqual(data["os"], "macOS")

    def test_os_version(self):
        data = self._run_probe()
        self.assertEqual(data["os_version"], _FAKE_OS_VERSION)

    def test_build(self):
        data = self._run_probe()
        self.assertEqual(data["build"], _FAKE_BUILD)

    def test_hostname(self):
        data = self._run_probe()
        self.assertEqual(data["hostname"], "test-host.local")

    def test_user(self):
        data = self._run_probe()
        self.assertEqual(data["user"], "testuser")

    def test_hardware_keys(self):
        data = self._run_probe()
        expected = {"arch", "model", "memory_bytes"}
        self.assertEqual(set(data["hardware"].keys()), expected)

    def test_hardware_arch(self):
        data = self._run_probe()
        self.assertEqual(data["hardware"]["arch"], "arm64")

    def test_hardware_model(self):
        data = self._run_probe()
        self.assertEqual(data["hardware"]["model"], _FAKE_HW_MODEL)

    def test_hardware_memory_bytes(self):
        data = self._run_probe()
        self.assertEqual(data["hardware"]["memory_bytes"], int(_FAKE_HW_MEMSIZE))
        self.assertIsInstance(data["hardware"]["memory_bytes"], int)

    def test_shell(self):
        data = self._run_probe()
        self.assertEqual(data["shell"], "/bin/zsh")

    def test_path_is_list(self):
        data = self._run_probe()
        self.assertIsInstance(data["path"], list)

    def test_path_entries(self):
        data = self._run_probe()
        self.assertEqual(data["path"], ["/usr/local/bin", "/usr/bin", "/bin"])


# ---------------------------------------------------------------------------
# Test: Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation(unittest.TestCase):
    """Output validates against the source_machine block of manifest.schema.json."""

    @classmethod
    def setUpClass(cls):
        try:
            import jsonschema
            cls.jsonschema = jsonschema
        except ImportError:
            cls.jsonschema = None

        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "manifest.schema.json"
        with open(schema_path, encoding="utf-8") as fh:
            manifest_schema = json.load(fh)
        cls.source_machine_schema = manifest_schema["properties"]["source_machine"]

    def _run_probe(self):
        with (
            patch("subprocess.run", side_effect=_subprocess_side_effect),
            patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
        ):
            return probe_mod.probe()

    def test_validates_against_schema(self):
        if self.jsonschema is None:
            self.skipTest("jsonschema not installed")
        data = self._run_probe()
        # Should not raise
        self.jsonschema.validate(data, self.source_machine_schema)

    def test_memory_bytes_is_integer(self):
        """memory_bytes must be an integer (schema minimum: 0)."""
        data = self._run_probe()
        self.assertIsInstance(data["hardware"]["memory_bytes"], int)
        self.assertGreaterEqual(data["hardware"]["memory_bytes"], 0)


# ---------------------------------------------------------------------------
# Test: --out PATH
# ---------------------------------------------------------------------------

class TestOutFlag(unittest.TestCase):
    """When --out PATH is given, file is written and stdout is empty."""

    def test_out_flag_writes_file_and_no_stdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "machine.json")

            captured_stdout = io.StringIO()

            with (
                patch("subprocess.run", side_effect=_subprocess_side_effect),
                patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
                patch("socket.gethostname", return_value="test-host.local"),
                patch("platform.machine", return_value="arm64"),
                patch("sys.stdout", captured_stdout),
                patch("sys.argv", ["probe.py", "--out", out_path]),
            ):
                probe_mod.main()

            # stdout should be empty
            self.assertEqual(captured_stdout.getvalue(), "")

            # file should exist and contain valid JSON
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as fh:
                data = json.load(fh)

            self.assertEqual(data["os"], "macOS")
            self.assertIn("hardware", data)

    def test_default_stdout_has_content(self):
        """Without --out, valid JSON is written to stdout."""
        captured_stdout = io.StringIO()

        with (
            patch("subprocess.run", side_effect=_subprocess_side_effect),
            patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
            patch("sys.stdout", captured_stdout),
            patch("sys.argv", ["probe.py"]),
        ):
            probe_mod.main()

        output = captured_stdout.getvalue()
        self.assertGreater(len(output.strip()), 0)
        data = json.loads(output)
        self.assertEqual(data["os"], "macOS")


# ---------------------------------------------------------------------------
# Test: subprocess timeout → non-zero exit
# ---------------------------------------------------------------------------

class TestSubprocessTimeout(unittest.TestCase):
    """A subprocess timeout causes sys.exit with a non-zero code."""

    def _timeout_side_effect(self, args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=5)

    def test_timeout_sw_vers_exits_nonzero(self):
        with (
            patch("subprocess.run", side_effect=self._timeout_side_effect),
            patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                probe_mod.probe()
            self.assertNotEqual(ctx.exception.code, 0)

    def test_timeout_sysctl_model_exits_nonzero(self):
        """Timeout on sysctl hw.model also exits non-zero."""

        call_count = [0]

        def partial_timeout(args, **kwargs):
            call_count[0] += 1
            # sw_vers calls succeed; sysctl raises timeout
            if args[0] == "sysctl":
                raise subprocess.TimeoutExpired(cmd=args, timeout=5)
            return _subprocess_side_effect(args, **kwargs)

        with (
            patch("subprocess.run", side_effect=partial_timeout),
            patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                probe_mod.probe()
            self.assertNotEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# Test: No full os.environ dump
# ---------------------------------------------------------------------------

class TestNoPIIEnvDump(unittest.TestCase):
    """
    os.environ is NOT fully serialised — a secret-looking env var must not
    appear anywhere in the probe output.
    """

    def test_secret_env_var_absent_from_output(self):
        secret_env = dict(_FAKE_ENVIRON)
        secret_env["AWS_SECRET_ACCESS_KEY"] = "AKIASUPERSECRETVALUE"
        secret_env["ANOTHER_SECRET"] = "password=hunter2"

        with (
            patch("subprocess.run", side_effect=_subprocess_side_effect),
            patch.dict(os.environ, secret_env, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
        ):
            data = probe_mod.probe()

        serialised = json.dumps(data)
        self.assertNotIn("AKIASUPERSECRETVALUE", serialised)
        self.assertNotIn("hunter2", serialised)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", serialised)
        self.assertNotIn("ANOTHER_SECRET", serialised)
        # The known-safe vars should still be present
        self.assertIn("testuser", serialised)
        self.assertIn("/bin/zsh", serialised)

    def test_only_named_env_vars_in_output(self):
        """Only USER, SHELL, and PATH contents appear — not arbitrary keys."""
        with (
            patch("subprocess.run", side_effect=_subprocess_side_effect),
            patch.dict(os.environ, _FAKE_ENVIRON, clear=True),
            patch("socket.gethostname", return_value="test-host.local"),
            patch("platform.machine", return_value="arm64"),
        ):
            data = probe_mod.probe()

        serialised = json.dumps(data)
        # The canary secret set in _FAKE_ENVIRON must not appear.
        self.assertNotIn("TOP_SECRET_TOKEN", serialised)
        self.assertNotIn("s3cr3t-should-never-appear", serialised)


if __name__ == "__main__":
    unittest.main()
