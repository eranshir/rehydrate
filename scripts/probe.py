"""
probe.py — capture immutable machine state at backup time.

Outputs a JSON object conforming to the ``source_machine`` block of
``schemas/manifest.schema.json``.  The JSON is written to stdout by default;
use ``--out PATH`` to write to a file instead.

macOS only in v0.1.

NO-PII POLICY
=============
Only the named environment variables (USER, SHELL, PATH) are captured.
``os.environ`` is never serialised wholesale.  All diagnostic output goes
through ``scripts.no_pii_log``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import pwd
import socket
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make ``scripts`` package importable regardless of working directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.no_pii_log import log_error, log_info, log_path, log_debug  # noqa: E402

# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

_SUBPROCESS_TIMEOUT = 5  # seconds


def _run(args: list[str]) -> str:
    """
    Run a subprocess and return its stripped stdout.

    Raises ``subprocess.TimeoutExpired`` if the process exceeds the timeout,
    and ``subprocess.CalledProcessError`` if it exits non-zero.  Both are
    intentionally propagated to the caller.
    """
    log_debug(f"probe: running {args[0]}")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Individual probe functions
# ---------------------------------------------------------------------------

def _probe_os_version() -> str:
    return _run(["sw_vers", "-productVersion"])


def _probe_build() -> str:
    return _run(["sw_vers", "-buildVersion"])


def _probe_hw_model() -> str:
    return _run(["sysctl", "-n", "hw.model"])


def _probe_hw_memory() -> int:
    raw = _run(["sysctl", "-n", "hw.memsize"])
    return int(raw)


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------

def probe() -> dict:
    """
    Collect machine state and return a dict conforming to the ``source_machine``
    JSON schema block.

    Exits non-zero (via ``sys.exit``) if any mandatory subprocess call fails
    or times out.
    """
    # --- os_version ---
    try:
        os_version = _probe_os_version()
    except subprocess.TimeoutExpired:
        log_error("probe: sw_vers -productVersion timed out")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        log_error(f"probe: sw_vers -productVersion failed (exit {exc.returncode})")
        sys.exit(1)

    # --- build ---
    try:
        build = _probe_build()
    except subprocess.TimeoutExpired:
        log_error("probe: sw_vers -buildVersion timed out")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        log_error(f"probe: sw_vers -buildVersion failed (exit {exc.returncode})")
        sys.exit(1)

    # --- hardware.model ---
    try:
        hw_model = _probe_hw_model()
    except subprocess.TimeoutExpired:
        log_error("probe: sysctl -n hw.model timed out")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        log_error(f"probe: sysctl -n hw.model failed (exit {exc.returncode})")
        sys.exit(1)

    # --- hardware.memory_bytes ---
    try:
        memory_bytes = _probe_hw_memory()
    except subprocess.TimeoutExpired:
        log_error("probe: sysctl -n hw.memsize timed out")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        log_error(f"probe: sysctl -n hw.memsize failed (exit {exc.returncode})")
        sys.exit(1)
    except ValueError:
        log_error("probe: sysctl -n hw.memsize returned non-integer output")
        sys.exit(1)

    # --- user (env var with pwd fallback) ---
    user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name

    # --- hostname ---
    hostname = socket.gethostname()

    # --- arch ---
    arch = platform.machine()

    # --- shell ---
    shell = os.environ.get("SHELL", "/bin/sh")

    # --- path ---
    path_str = os.environ.get("PATH", "")
    path = [entry for entry in path_str.split(":") if entry]

    result = {
        "os": "macOS",
        "os_version": os_version,
        "build": build,
        "hostname": hostname,
        "user": user,
        "hardware": {
            "arch": arch,
            "model": hw_model,
            "memory_bytes": memory_bytes,
        },
        "shell": shell,
        "path": path,
    }

    log_info("probe: machine state captured")
    log_debug(f"probe: arch={arch} os_version={os_version}")

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture immutable machine state for the rehydrate manifest."
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="Write JSON output to PATH instead of stdout.",
    )
    args = parser.parse_args()

    data = probe()
    serialised = json.dumps(data, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(serialised + "\n", encoding="utf-8")
        log_path(args.out, level="info")
    else:
        sys.stdout.write(serialised + "\n")


if __name__ == "__main__":
    main()
