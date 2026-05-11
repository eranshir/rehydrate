"""
no_pii_log — rehydrate shared logging utility.

NO-PII POLICY
=============
This module is the single logging surface for all rehydrate scripts.  Its
public API is intentionally constrained so that file contents can never be
logged — even by mistake:

  * No function accepts ``bytes``, ``bytearray``, or file-like objects.
  * ``log_path`` / ``log_error`` accept filesystem *paths* (str or Path),
    not the contents of those files.
  * ``log_hash`` accepts a hex-digest string, not raw binary.
  * ``log_info``, ``log_warn``, and ``log_debug`` accept ``str`` only and
    raise ``TypeError`` if anything else is passed.

The structural guarantee is enforced by an introspection test in
``scripts/tests/test_no_pii_log.py`` that scans every public function's
signature and asserts that none of the parameter annotations include
``bytes``, ``bytearray``, ``io.IOBase``, or unbound ``object``.

Environment variables
---------------------
REHYDRATE_LOG_LEVEL : str, optional
    One of ``debug``, ``info``, ``warn``, ``error``.  Messages below this
    level are silently dropped.  Default: ``info``.
REHYDRATE_LOG_FILE : str, optional
    If set, messages are *also* appended to this file (in addition to
    stderr).
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Level ordering
# ---------------------------------------------------------------------------

_LEVELS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "error": 40,
}

_LEVEL_LABELS: dict[str, str] = {
    "debug": "DEBUG",
    "info": "INFO",
    "warn": "WARN",
    "error": "ERROR",
}


def _active_level() -> int:
    """Return the numeric threshold for the current log level."""
    raw = os.environ.get("REHYDRATE_LOG_LEVEL", "info").lower().strip()
    return _LEVELS.get(raw, _LEVELS["info"])


def _now_utc() -> str:
    """Return the current UTC time formatted as YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(level: str, message: str) -> None:
    """Format and emit one log line, respecting level threshold and log file."""
    numeric = _LEVELS.get(level, _LEVELS["info"])
    if numeric < _active_level():
        return

    label = _LEVEL_LABELS.get(level, level.upper())
    line = f"[{_now_utc()}] [{label}] {message}"

    print(line, file=sys.stderr)

    log_file = os.environ.get("REHYDRATE_LOG_FILE")
    if log_file:
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Path normalisation helper
# ---------------------------------------------------------------------------

def _normalize_path(path: str | Path) -> str:
    """Replace the user home directory prefix with ``~`` to avoid leaking it."""
    path_str = str(path)
    home = os.path.expanduser("~")
    if path_str.startswith(home):
        path_str = "~" + path_str[len(home):]
    return path_str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_path(path: str | Path, *, level: str = "info") -> None:
    """Log a filesystem path (home-dir prefix replaced with ``~``)."""
    _emit(level, f"path: {_normalize_path(path)}")


def log_hash(hash_hex: str, *, level: str = "info") -> None:
    """Log a content-hash hex digest."""
    _emit(level, f"hash: {hash_hex}")


def log_count(label: str, n: int, *, level: str = "info") -> None:
    """Log a labelled count (e.g. number of files matched)."""
    _emit(level, f"count[{label}]: {n}")


def log_error(msg: str, *, path: str | Path | None = None) -> None:
    """Log an error message, optionally annotating a related path."""
    if path is not None:
        _emit("error", f"{msg} (path: {_normalize_path(path)})")
    else:
        _emit("error", msg)


def log_info(msg: str) -> None:
    """Log an informational message.  Raises ``TypeError`` if *msg* is not ``str``."""
    if not isinstance(msg, str):
        raise TypeError(
            f"log_info requires a str, got {type(msg).__name__!r}"
        )
    _emit("info", msg)


def log_warn(msg: str) -> None:
    """Log a warning message.  Raises ``TypeError`` if *msg* is not ``str``."""
    if not isinstance(msg, str):
        raise TypeError(
            f"log_warn requires a str, got {type(msg).__name__!r}"
        )
    _emit("warn", msg)


def log_debug(msg: str) -> None:
    """Log a debug message.  Raises ``TypeError`` if *msg* is not ``str``."""
    if not isinstance(msg, str):
        raise TypeError(
            f"log_debug requires a str, got {type(msg).__name__!r}"
        )
    _emit("debug", msg)
