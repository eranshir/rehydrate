"""
Tests for scripts/no_pii_log.py

Covers:
  - Output format for each public function
  - $HOME → ~ path normalisation in log_path and log_error
  - REHYDRATE_LOG_FILE redirects output to a file (via tempfile)
  - REHYDRATE_LOG_LEVEL=warn drops info and debug messages
  - Passing bytes to log_info / log_warn / log_debug raises TypeError
  - Introspection: no public function annotation includes bytes, bytearray,
    io.IOBase, or unbound object
"""

import inspect
import io
import os
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the project root importable when run from the repo root or directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.no_pii_log as _mod

# Timestamp pattern: [YYYY-MM-DDTHH:MM:SSZ]
_TS_RE = r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]"


def _capture_stderr(fn, *args, env_overrides=None, **kwargs):
    """Call *fn* and return the text written to stderr."""
    env = dict(os.environ)
    env.pop("REHYDRATE_LOG_FILE", None)
    env.pop("REHYDRATE_LOG_LEVEL", None)
    if env_overrides:
        env.update(env_overrides)
    buf = io.StringIO()
    with patch.dict(os.environ, env, clear=True), patch("sys.stderr", buf):
        fn(*args, **kwargs)
    return buf.getvalue()


class TestOutputFormat(unittest.TestCase):
    """Each public function produces output in the expected format."""

    def _assert_line(self, output: str, level_label: str, pattern: str):
        """Assert a single line matching [TS] [LEVEL] <pattern>."""
        full = rf"^{_TS_RE} \[{level_label}\] {pattern}$"
        self.assertRegex(output.strip(), full, msg=f"Output was: {output!r}")

    def test_log_path(self):
        out = _capture_stderr(_mod.log_path, "/some/path/to/file.txt")
        self._assert_line(out, "INFO", r"path: /some/path/to/file\.txt")

    def test_log_hash(self):
        hex_digest = "abc123def456"
        out = _capture_stderr(_mod.log_hash, hex_digest)
        self._assert_line(out, "INFO", f"hash: {hex_digest}")

    def test_log_count(self):
        out = _capture_stderr(_mod.log_count, "dotfiles", 42)
        self._assert_line(out, "INFO", r"count\[dotfiles\]: 42")

    def test_log_error_no_path(self):
        out = _capture_stderr(_mod.log_error, "something went wrong")
        self._assert_line(out, "ERROR", "something went wrong")

    def test_log_error_with_path(self):
        out = _capture_stderr(_mod.log_error, "missing file", path="/tmp/foo.txt")
        self._assert_line(out, "ERROR", r"missing file \(path: /tmp/foo\.txt\)")

    def test_log_info(self):
        out = _capture_stderr(_mod.log_info, "hello world")
        self._assert_line(out, "INFO", "hello world")

    def test_log_warn(self):
        out = _capture_stderr(_mod.log_warn, "watch out")
        self._assert_line(out, "WARN", "watch out")

    def test_log_debug(self):
        out = _capture_stderr(
            _mod.log_debug,
            "verbose detail",
            env_overrides={"REHYDRATE_LOG_LEVEL": "debug"},
        )
        self._assert_line(out, "DEBUG", "verbose detail")

    def test_log_path_level_kwarg(self):
        """log_path respects an explicit level keyword argument."""
        out = _capture_stderr(_mod.log_path, "/x", level="warn")
        self._assert_line(out, "WARN", "path: /x")


class TestPathNormalisation(unittest.TestCase):
    """log_path replaces $HOME with ~ before logging."""

    def test_home_replaced_in_log_path(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "Documents", "secret.txt")
        out = _capture_stderr(_mod.log_path, target)
        self.assertIn("~/Documents/secret.txt", out)
        self.assertNotIn(home + "/Documents", out)

    def test_home_replaced_as_path_object(self):
        home = Path.home()
        target = home / ".bashrc"
        out = _capture_stderr(_mod.log_path, target)
        self.assertIn("~/.bashrc", out)
        self.assertNotIn(str(home) + "/.bashrc", out)

    def test_non_home_path_unchanged(self):
        out = _capture_stderr(_mod.log_path, "/etc/hosts")
        self.assertIn("/etc/hosts", out)

    def test_home_replaced_in_log_error_path(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "missing.txt")
        out = _capture_stderr(_mod.log_error, "oops", path=target)
        self.assertIn("~/missing.txt", out)
        self.assertNotIn(home + "/missing.txt", out)


class TestLogFile(unittest.TestCase):
    """REHYDRATE_LOG_FILE redirects output to a file."""

    def test_log_file_receives_output(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as fh:
            log_path_str = fh.name
        try:
            buf = io.StringIO()
            env = dict(os.environ)
            env["REHYDRATE_LOG_FILE"] = log_path_str
            env.pop("REHYDRATE_LOG_LEVEL", None)
            with patch.dict(os.environ, env, clear=True), patch("sys.stderr", buf):
                _mod.log_info("file test message")

            with open(log_path_str, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("file test message", content)
            self.assertIn("[INFO]", content)
        finally:
            os.unlink(log_path_str)

    def test_stderr_still_receives_output_when_log_file_set(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as fh:
            log_path_str = fh.name
        try:
            buf = io.StringIO()
            env = dict(os.environ)
            env["REHYDRATE_LOG_FILE"] = log_path_str
            env.pop("REHYDRATE_LOG_LEVEL", None)
            with patch.dict(os.environ, env, clear=True), patch("sys.stderr", buf):
                _mod.log_info("dual output")
            self.assertIn("dual output", buf.getvalue())
        finally:
            os.unlink(log_path_str)

    def test_log_file_appends(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("existing line\n")
            log_path_str = fh.name
        try:
            buf = io.StringIO()
            env = dict(os.environ)
            env["REHYDRATE_LOG_FILE"] = log_path_str
            env.pop("REHYDRATE_LOG_LEVEL", None)
            with patch.dict(os.environ, env, clear=True), patch("sys.stderr", buf):
                _mod.log_info("new line")
            with open(log_path_str, encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(lines[0].strip(), "existing line")
            self.assertTrue(any("new line" in l for l in lines))
        finally:
            os.unlink(log_path_str)


class TestLevelFiltering(unittest.TestCase):
    """REHYDRATE_LOG_LEVEL=warn drops info and debug messages."""

    def test_warn_level_drops_info(self):
        out = _capture_stderr(
            _mod.log_info, "should be dropped",
            env_overrides={"REHYDRATE_LOG_LEVEL": "warn"},
        )
        self.assertEqual(out.strip(), "")

    def test_warn_level_drops_debug(self):
        out = _capture_stderr(
            _mod.log_debug, "also dropped",
            env_overrides={"REHYDRATE_LOG_LEVEL": "warn"},
        )
        self.assertEqual(out.strip(), "")

    def test_warn_level_passes_warn(self):
        out = _capture_stderr(
            _mod.log_warn, "this passes",
            env_overrides={"REHYDRATE_LOG_LEVEL": "warn"},
        )
        self.assertIn("this passes", out)

    def test_warn_level_passes_error(self):
        out = _capture_stderr(
            _mod.log_error, "this also passes",
            env_overrides={"REHYDRATE_LOG_LEVEL": "warn"},
        )
        self.assertIn("this also passes", out)

    def test_error_level_drops_warn(self):
        out = _capture_stderr(
            _mod.log_warn, "dropped at error level",
            env_overrides={"REHYDRATE_LOG_LEVEL": "error"},
        )
        self.assertEqual(out.strip(), "")

    def test_debug_level_passes_all(self):
        out = _capture_stderr(
            _mod.log_debug, "debug passes",
            env_overrides={"REHYDRATE_LOG_LEVEL": "debug"},
        )
        self.assertIn("debug passes", out)


class TestTypeEnforcement(unittest.TestCase):
    """Passing bytes to log_info / log_warn / log_debug raises TypeError."""

    def test_log_info_rejects_bytes(self):
        with self.assertRaises(TypeError):
            _mod.log_info(b"some bytes")

    def test_log_warn_rejects_bytes(self):
        with self.assertRaises(TypeError):
            _mod.log_warn(b"some bytes")

    def test_log_debug_rejects_bytes(self):
        with self.assertRaises(TypeError):
            _mod.log_debug(b"some bytes")

    def test_log_info_rejects_bytearray(self):
        with self.assertRaises(TypeError):
            _mod.log_info(bytearray(b"ba"))

    def test_log_info_rejects_int(self):
        with self.assertRaises(TypeError):
            _mod.log_info(42)  # type: ignore[arg-type]


# Forbidden annotation types for the introspection test.
_FORBIDDEN_TYPES = (bytes, bytearray, io.IOBase, object)


def _annotation_is_forbidden(annotation) -> bool:
    """
    Return True if *annotation* is, or directly contains, a forbidden type.

    Handles plain types and simple ``Union``/``Optional`` generics but does
    not recurse into nested generics — the policy is about top-level parameter
    types.
    """
    if annotation is inspect.Parameter.empty:
        return False
    # Unwrap Union / Optional (typing.Union[X, Y] → (X, Y))
    origin = getattr(annotation, "__origin__", None)
    if origin is typing.Union:
        args = typing.get_args(annotation)
        return any(_annotation_is_forbidden(a) for a in args)
    # Direct match against forbidden set
    if annotation in _FORBIDDEN_TYPES:
        return True
    return False


class TestIntrospection(unittest.TestCase):
    """
    Structural guarantee: no public function in no_pii_log accepts bytes,
    bytearray, io.IOBase, or unbound object as a parameter annotation.
    """

    def _public_functions(self):
        return [
            (name, obj)
            for name, obj in inspect.getmembers(_mod, inspect.isfunction)
            if not name.startswith("_")
        ]

    def test_has_public_functions(self):
        fns = self._public_functions()
        self.assertGreater(len(fns), 0, "Module exposes no public functions.")

    def test_no_forbidden_annotations(self):
        violations = []
        for fn_name, fn in self._public_functions():
            try:
                hints = typing.get_type_hints(fn)
            except Exception:
                hints = {}
            sig = inspect.signature(fn)
            for param_name, param in sig.parameters.items():
                # Prefer resolved hint from get_type_hints; fall back to annotation.
                annotation = hints.get(param_name, param.annotation)
                if _annotation_is_forbidden(annotation):
                    violations.append(
                        f"{fn_name}({param_name}): annotation {annotation!r} is forbidden"
                    )
        if violations:
            self.fail(
                "Public API contains forbidden parameter annotations:\n"
                + "\n".join(violations)
            )

    def test_expected_public_functions_present(self):
        """Confirm the documented API surface is complete."""
        expected = {
            "log_path",
            "log_hash",
            "log_count",
            "log_error",
            "log_info",
            "log_warn",
            "log_debug",
        }
        found = {name for name, _ in self._public_functions()}
        missing = expected - found
        self.assertFalse(
            missing,
            f"Expected public functions not found: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
