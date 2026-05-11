"""
walk-repos.py — rehydrate Phase 3.5

Captures git repo metadata (remote URL, current branch, HEAD commit) plus
gitignored secret files from each configured root under the dev-projects
category. Each captured secret is written as a virtual file under a temp
workdir. An inventory JSON file is written alongside the secrets.

Virtual files written:
  <workdir>/.rehydrate/projects/inventory.json
  <workdir>/.rehydrate/projects/<repo-name>/<relative-secret-path>

Usage:
    python3 scripts/walk-repos.py [--categories PATH] [--out PATH]
                                   [--home PATH] [--workdir PATH]
                                   [--secret-patterns PATH]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Allow running as a script directly OR as scripts.walk_repos module
# ---------------------------------------------------------------------------
try:
    from scripts.no_pii_log import log_count, log_error, log_info, log_path, log_warn
except ImportError:
    _here = Path(__file__).parent
    sys.path.insert(0, str(_here.parent))
    from scripts.no_pii_log import log_count, log_error, log_info, log_path, log_warn

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

CATEGORY_NAME = "dev-projects"
PROJECTS_SUBDIR = ".rehydrate/projects"
INVENTORY_FILENAME = "inventory.json"
SCHEMA_VERSION = "0.1.0"
GIT_TIMEOUT = 5  # seconds
MAX_SCAN_DEPTH = 5

DEFAULT_SECRET_PATTERNS: list[str] = [
    ".env",
    ".env.*",
    "*.p8",
    "credentials.json",
    "secrets.yaml",
    "tokens.js",
    "service-account*.json",
    "firebase-config.js",
    ".npmrc",
    ".netrc",
    "*.pem",
    "*.key",
    "APIKeys.swift",
    "apikeys.swift",
]


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


def _git_run(args: list[str], cwd: str) -> tuple[str, int]:
    """
    Run a git command in *cwd* with GIT_TIMEOUT.

    Returns (stdout_stripped, returncode).
    On timeout or FileNotFoundError returns ("", non-zero).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        return proc.stdout.decode("utf-8", errors="replace").strip(), proc.returncode
    except subprocess.TimeoutExpired:
        log_warn(f"git timed out: {' '.join(args)}")
        return "", 1
    except FileNotFoundError:
        log_warn("git not found on PATH")
        return "", 1


def _matches_secret_pattern(name: str, patterns: list[str]) -> bool:
    """Return True if *name* matches any of the secret glob patterns."""
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _is_gitignored(repo_path: str, rel_path: str) -> bool:
    """
    Return True if *rel_path* is gitignored in *repo_path*.
    Uses `git check-ignore --quiet <path>`.
    Exit 0 → gitignored. Exit 1 → not ignored (tracked or not matching).
    """
    _, rc = _git_run(["check-ignore", "--quiet", rel_path], cwd=repo_path)
    return rc == 0


def _scan_secrets(
    repo_path: str,
    patterns: list[str],
    max_depth: int = MAX_SCAN_DEPTH,
) -> list[str]:
    """
    Walk *repo_path* (excluding .git/) for files matching *patterns* that are
    also gitignored. Returns a sorted list of relative paths.
    """
    found: list[str] = []
    repo = Path(repo_path)

    for dirpath, dirnames, filenames in os.walk(repo_path):
        # Skip .git entirely
        dirnames[:] = [d for d in dirnames if d != ".git"]

        # Enforce max depth
        current_depth = len(Path(dirpath).relative_to(repo).parts)
        if current_depth >= max_depth:
            dirnames[:] = []

        for filename in filenames:
            if not _matches_secret_pattern(filename, patterns):
                continue
            abs_file = Path(dirpath) / filename
            try:
                rel = abs_file.relative_to(repo)
            except ValueError:
                continue
            rel_str = str(rel)
            if _is_gitignored(repo_path, rel_str):
                found.append(rel_str)

    return sorted(found)


# ---------------------------------------------------------------------------
# Per-repo capture
# ---------------------------------------------------------------------------

def _capture_repo(
    child: Path,
    patterns: list[str],
) -> dict[str, Any] | None:
    """
    Inspect a directory that contains .git/.  Return a repo dict or None if
    the repo should be skipped (no remote → local-only).
    """
    repo_path = str(child)

    # Remote URL
    remote_url, rc = _git_run(["config", "--get", "remote.origin.url"], cwd=repo_path)
    if rc != 0 or not remote_url:
        log_info(f"repo skipped (no remote): name={child.name}")
        return None

    # Branch
    branch, _ = _git_run(["branch", "--show-current"], cwd=repo_path)

    # HEAD sha
    head_sha, _ = _git_run(["rev-parse", "HEAD"], cwd=repo_path)

    # Secret scan
    captured_secrets = _scan_secrets(repo_path, patterns)
    log_count(f"secrets_found:{child.name}", len(captured_secrets))

    return {
        "name": child.name,
        "path": str(child),
        "remote_url": remote_url,
        "branch": branch,
        "head_sha": head_sha,
        "captured_secrets": captured_secrets,
    }


# ---------------------------------------------------------------------------
# Core walk
# ---------------------------------------------------------------------------

def walk_repos(
    roots: list[str],
    home: str,
    workdir: str,
    secret_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """
    Walk each root, find git repos with remotes, capture secrets, write
    inventory + secret files under <workdir>/.rehydrate/projects/, and return
    the walk output dict.
    """
    if secret_patterns is None:
        secret_patterns = DEFAULT_SECRET_PATTERNS

    projects_dir = Path(workdir) / PROJECTS_SUBDIR
    projects_dir.mkdir(parents=True, exist_ok=True)

    repos: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    for raw_root in roots:
        # Resolve ~/
        if raw_root.startswith("~/"):
            resolved_root = Path(home) / raw_root[2:]
        elif raw_root == "~":
            resolved_root = Path(home)
        else:
            resolved_root = Path(raw_root)

        if not resolved_root.is_dir():
            log_warn(f"root not found, skipping")
            log_path(resolved_root)
            skipped.append({"path": str(resolved_root), "reason": "root-not-found"})
            continue

        log_path(resolved_root)

        # List direct children
        try:
            children = sorted(resolved_root.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            log_warn("permission denied reading root")
            skipped.append({"path": str(resolved_root), "reason": "permission-denied"})
            continue

        for child in children:
            if not child.is_dir():
                continue

            git_dir = child / ".git"
            if not git_dir.exists():
                # Not a repo
                continue

            repo_data = _capture_repo(child, secret_patterns)
            if repo_data is None:
                skipped.append({"path": str(child), "reason": "no-remote"})
                continue

            repos.append(repo_data)

            # Write captured secret files
            for rel_secret in repo_data["captured_secrets"]:
                src = child / rel_secret
                dst = projects_dir / repo_data["name"] / rel_secret
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    data = src.read_bytes()
                    dst.write_bytes(data)
                    rel_out = f"{PROJECTS_SUBDIR}/{repo_data['name']}/{rel_secret}"
                    files.append(_make_file_entry(rel_out, len(data)))
                    log_path(dst)
                except OSError as exc:
                    log_warn(f"could not read secret file: {exc}")
                    skipped.append({
                        "path": str(src),
                        "reason": "secret-read-error",
                    })

    # Sort repos by name for stable hashing
    repos.sort(key=lambda r: r["name"].lower())

    # Build and write inventory
    inventory: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": _now_iso(),
        "repos": repos,
    }
    inventory_bytes = json.dumps(
        inventory, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    inventory_path = projects_dir / INVENTORY_FILENAME
    inventory_path.write_bytes(inventory_bytes)

    inv_rel = f"{PROJECTS_SUBDIR}/{INVENTORY_FILENAME}"
    files.insert(0, _make_file_entry(inv_rel, len(inventory_bytes)))

    log_count("repos_found", len(repos))
    log_count("repos_skipped", len(skipped))
    log_info(
        f"walk-repos complete: repos={len(repos)} files={len(files)} skipped={len(skipped)}"
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

def _load_roots(categories_path: str) -> list[str]:
    """Load the roots list from the dev-projects category."""
    path = Path(categories_path)
    if not path.exists():
        log_error("categories file not found", path=path)
        sys.exit(1)

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    for cat in data.get("categories", []):
        if cat.get("name") == CATEGORY_NAME:
            return cat.get("roots", [])

    log_error(f"category '{CATEGORY_NAME}' not found in {categories_path}")
    sys.exit(1)


def _load_secret_patterns(patterns_path: str) -> list[str]:
    """Load secret patterns from a file (one glob per line, # comments ignored)."""
    lines = Path(patterns_path).read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            result.append(stripped)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Walk git repos under configured roots, capture remote URLs + "
            "gitignored secrets, and emit a JSON file-list + coverage report "
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
        help="Home directory (used to resolve ~/...; default: $HOME)",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Directory where virtual project files are written (default: fresh tempdir)",
    )
    parser.add_argument(
        "--secret-patterns",
        default=None,
        dest="secret_patterns",
        help="Path to a file with custom secret glob patterns (one per line). "
             "If omitted, the built-in default list is used.",
    )
    args = parser.parse_args(argv)

    roots = _load_roots(args.categories)

    if args.workdir is None:
        workdir = tempfile.mkdtemp(prefix="rehydrate-repos-")
        log_info("created workdir")
    else:
        workdir = args.workdir
        Path(workdir).mkdir(parents=True, exist_ok=True)

    secret_patterns: list[str] | None = None
    if args.secret_patterns:
        secret_patterns = _load_secret_patterns(args.secret_patterns)

    result = walk_repos(
        roots=roots,
        home=args.home,
        workdir=workdir,
        secret_patterns=secret_patterns,
    )

    output = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        log_info("output written")
    else:
        print(output)


if __name__ == "__main__":
    main()
