"""
walk-packages.py — rehydrate Phase 3.3

Captures installed-package inventories from each configured package manager.
Each manager's output is written as a virtual file under a temp workdir rooted
at `<workdir>/.rehydrate/packages/<filename>`. snapshot.py ingests these via its
standard `--walk-output` / `--category` interface with `--home <workdir>`.

Supported managers and their commands:
  brew   → brew bundle dump --file=- --force  → Brewfile
  npm    → npm list -g --depth=0 --json        → npm-globals.json
  pip    → pip3 freeze                         → pip-requirements.txt
  cargo  → cargo install --list               → cargo-installed.txt
  go     → list ~/go/bin/* filenames           → go-bin.txt
  gem    → gem list                            → gem-list.txt

Usage:
    python3 scripts/walk-packages.py [--categories PATH] [--out PATH]
                                     [--home PATH] [--workdir PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Allow running as a script directly OR as scripts.walk_packages module
# ---------------------------------------------------------------------------
try:
    from scripts.no_pii_log import log_error, log_info, log_warn
except ImportError:
    _here = Path(__file__).parent
    sys.path.insert(0, str(_here.parent))
    from scripts.no_pii_log import log_error, log_info, log_warn

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_CATEGORIES = _REPO_ROOT / "categories.yaml"

CATEGORY_NAME = "package-managers"
PACKAGES_SUBDIR = ".rehydrate/packages"
SUBPROCESS_TIMEOUT = 30  # seconds

# Map manager name → (binary_to_check, output_filename)
# The command itself is assembled in run_manager().
_MANAGER_META: dict[str, tuple[str, str]] = {
    "brew":  ("brew",  "Brewfile"),
    "npm":   ("npm",   "npm-globals.json"),
    "pip":   ("pip3",  "pip-requirements.txt"),
    "cargo": ("cargo", "cargo-installed.txt"),
    "go":    ("go",    "go-bin.txt"),
    "gem":   ("gem",   "gem-list.txt"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_file_entry(rel_path: str, size: int) -> dict[str, Any]:
    """Build a file_entry dict in the same shape walk.py emits."""
    return {
        "path": rel_path,
        "size": size,
        "mtime": _now_iso(),
        "mode": "0644",
        "is_symlink": False,
        "symlink_target": None,
    }


def _go_bin_listing(home: str) -> bytes | None:
    """
    Return newline-joined filenames from ~/go/bin, or None if the dir is absent.
    This does not invoke a subprocess — it just lists the directory.
    """
    go_bin = Path(home) / "go" / "bin"
    if not go_bin.is_dir():
        return None
    names = sorted(p.name for p in go_bin.iterdir() if p.is_file())
    return "\n".join(names).encode()


def _run_manager(
    manager: str,
    home: str,
) -> tuple[bytes | None, str | None, int | None]:
    """
    Run the command for *manager*.

    Returns (stdout_bytes, failure_reason, exit_code):
      - On success: (bytes, None, None)
      - Binary missing: (None, "manager-not-installed", None)
      - Timeout: (None, "manager-timeout", None)
      - Non-zero exit: (None, "manager-failed", exit_code)
    """
    binary, _ = _MANAGER_META[manager]

    # ---- go: no subprocess; just list the directory ----
    # We only need ~/go/bin to exist; the `go` binary itself is optional
    # (tools may have been installed via `go install` on a previous machine).
    if manager == "go":
        result = _go_bin_listing(home)
        if result is None:
            return None, "manager-not-installed", None
        return result, None, None

    # ---- all other managers: check binary then run subprocess ----
    if shutil.which(binary) is None:
        return None, "manager-not-installed", None

    if manager == "brew":
        cmd = ["brew", "bundle", "dump", "--file=-", "--force"]
    elif manager == "npm":
        cmd = ["npm", "list", "-g", "--depth=0", "--json"]
    elif manager == "pip":
        cmd = ["pip3", "freeze"]
    elif manager == "cargo":
        cmd = ["cargo", "install", "--list"]
    elif manager == "gem":
        cmd = ["gem", "list"]
    else:
        return None, "manager-not-installed", None

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log_warn(f"manager timed out: {manager}")
        return None, "manager-timeout", None
    except FileNotFoundError:
        return None, "manager-not-installed", None

    if proc.returncode != 0:
        log_warn(f"manager exited {proc.returncode}: {manager}")
        return None, "manager-failed", proc.returncode

    return proc.stdout, None, None


# ---------------------------------------------------------------------------
# Core walk
# ---------------------------------------------------------------------------

def walk_packages(
    managers: list[str],
    home: str,
    workdir: str,
) -> dict[str, Any]:
    """
    Run each manager in *managers*, write output files under
    `<workdir>/.rehydrate/packages/`, and return the walk output dict.
    """
    pkg_dir = Path(workdir) / PACKAGES_SUBDIR
    pkg_dir.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for manager in managers:
        if manager not in _MANAGER_META:
            log_warn(f"unknown manager '{manager}' — skipping")
            continue

        _, filename = _MANAGER_META[manager]
        rel_path = f"{PACKAGES_SUBDIR}/{filename}"
        abs_path = Path(workdir) / rel_path

        stdout_bytes, reason, exit_code = _run_manager(manager, home)

        if reason is not None:
            skip_entry: dict[str, Any] = {"path": rel_path, "reason": reason}
            if exit_code is not None:
                skip_entry["exit_code"] = exit_code
            skipped.append(skip_entry)
            log_info(f"manager skipped: manager={manager} reason={reason}")
            continue

        # Write the captured output to the workdir
        data: bytes = stdout_bytes if stdout_bytes is not None else b""
        abs_path.write_bytes(data)

        size = len(data)
        files.append(_make_file_entry(rel_path, size))
        log_info(f"manager captured: manager={manager} bytes={size}")

    log_info(
        f"walk-packages complete: files={len(files)} skipped={len(skipped)}"
    )

    return {
        "category": CATEGORY_NAME,
        "workdir": workdir,
        "files": files,
        "coverage": {
            "globs": {},
            "skipped": skipped,
            "large_files_warned": [],
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_managers(categories_path: str) -> list[str]:
    """Load the managers list from the package-managers category."""
    path = Path(categories_path)
    if not path.exists():
        log_error("categories file not found", path=path)
        sys.exit(1)

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    for cat in data.get("categories", []):
        if cat.get("name") == CATEGORY_NAME:
            return cat.get("managers", [])

    log_error(f"category '{CATEGORY_NAME}' not found in {categories_path}")
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Walk package managers and emit a JSON file-list + coverage report "
            "compatible with snapshot.py's --walk-output interface."
        ),
    )
    parser.add_argument(
        "--categories",
        default=str(_DEFAULT_CATEGORIES),
        help="Path to categories.yaml (default: categories.yaml at repo root)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for JSON (default: stdout)",
    )
    parser.add_argument(
        "--home",
        default=os.path.expanduser("~"),
        help="Home directory (used to resolve ~/go/bin; default: $HOME)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Directory where virtual package files are written (default: fresh tempdir)",
    )
    args = parser.parse_args(argv)

    managers = _load_managers(args.categories)

    if args.workdir is None:
        workdir = tempfile.mkdtemp(prefix="rehydrate-packages-")
        log_info(f"created workdir")
    else:
        workdir = args.workdir
        Path(workdir).mkdir(parents=True, exist_ok=True)

    result = walk_packages(managers=managers, home=args.home, workdir=workdir)

    output = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        log_info(f"output written")
    else:
        print(output)


if __name__ == "__main__":
    main()
