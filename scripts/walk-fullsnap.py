"""
walk-fullsnap.py — rehydrate Phase 3.6

Captures the full file tree of every project directory under configured roots
that is NOT covered by repo-list: either has no .git/ at all, or has .git/
but no remote.origin.url.  These are "local-only projects" — they lose all
content if not backed up.

Files captured are physically copied into the workdir under:
  <workdir>/.rehydrate/projects-local/<project-name>/<relpath>

An inventory JSON file is also written:
  <workdir>/.rehydrate/projects-local/inventory.json

Default excluded directory names (hard-coded, applied by basename):
  node_modules  .venv  venv  env  __pycache__  .next  .nuxt
  dist  build  .tox  .pytest_cache  .mypy_cache  .ruff_cache  .git

Usage:
    python3 scripts/walk-fullsnap.py [--categories PATH] [--out PATH]
                                      [--home PATH] [--workdir PATH]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Allow running as a script directly OR as scripts.walk_fullsnap module
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

CATEGORY_NAME = "local-only-projects"
PROJECTS_SUBDIR = ".rehydrate/projects-local"
INVENTORY_FILENAME = "inventory.json"
SCHEMA_VERSION = "0.1.0"
GIT_TIMEOUT = 5  # seconds
FILE_COUNT_CAP = 50_000  # per-project cap

# Directory basenames that are always pruned from recursive walk
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".next",
    ".nuxt",
    "dist",
    "build",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime_iso(st) -> str:
    dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mode_octal(st) -> str:
    return f"{st.st_mode & 0o7777:04o}"


def _make_file_entry(rel_path: str, size: int, mtime: str, mode: str,
                     is_symlink: bool, symlink_target: str | None) -> dict[str, Any]:
    """Build a file_entry dict in the same shape walk.py emits."""
    return {
        "path": rel_path,
        "size": size,
        "mtime": mtime,
        "mode": mode,
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
    }


def _git_remote_url(child: Path) -> str:
    """
    Return remote.origin.url for the git repo at *child*, or "" if not set or
    git fails.  Applies GIT_TIMEOUT.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(child), "config", "--get", "remote.origin.url"],
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8", errors="replace").strip()
        return ""
    except subprocess.TimeoutExpired:
        log_warn("git config --get remote.origin.url timed out")
        return ""
    except FileNotFoundError:
        log_warn("git not found on PATH")
        return ""


def _matches_exclude_glob(rel_path: str, exclude_patterns: list[str]) -> bool:
    """
    Return True if *rel_path* (relative to the project root, using forward
    slashes) matches any of the category's exclude glob patterns.

    Patterns like ``**/*.tmp`` are matched by testing the path against the
    suffix pattern (e.g. ``*.tmp``) at every depth level, so that a file at
    the project root also matches.  This mirrors standard gitignore/shell
    ``**`` semantics where ``**`` can match zero or more path components.
    """
    def _to_regex(pat: str) -> str:
        parts = re.split(r"(\*\*|\*|\?)", pat)
        result = []
        for part in parts:
            if part == "**":
                result.append(".*")
            elif part == "*":
                result.append("[^/]*")
            elif part == "?":
                result.append("[^/]")
            else:
                result.append(re.escape(part))
        return "^" + "".join(result) + "$"

    for pattern in exclude_patterns:
        pat = pattern.lstrip("/")
        if "**" in pat:
            # Strategy: try the full pattern first, then the suffix after **/ prefix
            # to handle files at the root level (zero path components before the
            # non-** tail).
            regex_full = _to_regex(pat)
            if re.match(regex_full, rel_path):
                return True
            # Also strip leading **/ and try the tail as a plain glob match
            # This makes **/*.tmp match "scratch.tmp" (root level) and
            # "subdir/scratch.tmp" (nested).
            tail = re.sub(r"^\*\*/", "", pat)
            if tail and fnmatch.fnmatch(os.path.basename(rel_path), tail):
                return True
            # Finally, try the tail as a regex against the full rel_path
            if tail:
                regex_tail = _to_regex(tail)
                if re.match(regex_tail, rel_path):
                    return True
        else:
            if fnmatch.fnmatch(rel_path, pat):
                return True
            if fnmatch.fnmatch(os.path.basename(rel_path), os.path.basename(pat)):
                return True
    return False


# ---------------------------------------------------------------------------
# Per-project capture
# ---------------------------------------------------------------------------

def _capture_project(
    child: Path,
    workdir_projects: Path,
    exclude_patterns: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """
    Recursively walk *child* (a local-only project directory), copy each
    captured file to *workdir_projects*/<child.name>/..., and return
    (file_entries, project_summary, skipped_entries).

    Skips files matching excluded dir basenames and exclude_patterns.
    Caps at FILE_COUNT_CAP files per project.
    """
    project_name = child.name
    dest_root = workdir_projects / project_name
    file_entries: list[dict[str, Any]] = []
    skipped_entries: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0
    cap_hit = False

    for dirpath, dirnames, filenames in os.walk(str(child), followlinks=False, topdown=True):
        # Prune excluded dir names in-place (modifies the walk)
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIR_NAMES
        ]

        for filename in filenames:
            if cap_hit:
                break

            abs_file = Path(dirpath) / filename

            # Relative path from project root (forward slashes)
            try:
                relpath = abs_file.relative_to(child)
            except ValueError:
                continue
            relpath_str = str(relpath)

            # Apply category exclude patterns
            if _matches_exclude_glob(relpath_str, exclude_patterns):
                skipped_entries.append({
                    "path": relpath_str,
                    "project": project_name,
                    "reason": "excluded-glob",
                })
                continue

            # Stat via lstat (don't follow symlinks)
            try:
                st = os.lstat(str(abs_file))
            except PermissionError:
                log_warn("permission denied reading file in project")
                skipped_entries.append({
                    "path": relpath_str,
                    "project": project_name,
                    "reason": "permission-denied",
                })
                continue
            except OSError:
                is_link = os.path.islink(str(abs_file))
                reason = "broken-symlink" if is_link else "os-error"
                skipped_entries.append({
                    "path": relpath_str,
                    "project": project_name,
                    "reason": reason,
                })
                continue

            is_link = stat_module.S_ISLNK(st.st_mode)
            is_reg = stat_module.S_ISREG(st.st_mode)

            if not is_reg and not is_link:
                # Skip devices, sockets, etc.
                continue

            # Destination path inside workdir
            dest_file = dest_root / relpath

            # The path emitted in walk output is relative to workdir root
            out_rel = f"{PROJECTS_SUBDIR}/{project_name}/{relpath_str}"

            if is_link:
                link_target = os.readlink(str(abs_file))
                # Create parent dirs and symlink in workdir
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if dest_file.exists() or dest_file.is_symlink():
                        dest_file.unlink()
                    os.symlink(link_target, str(dest_file))
                except OSError as exc:
                    log_warn(f"could not create symlink in workdir: {exc}")
                    skipped_entries.append({
                        "path": relpath_str,
                        "project": project_name,
                        "reason": "symlink-create-error",
                    })
                    continue
                entry = _make_file_entry(
                    out_rel,
                    size=st.st_size,
                    mtime=_mtime_iso(st),
                    mode=_mode_octal(st),
                    is_symlink=True,
                    symlink_target=link_target,
                )
            else:
                # Regular file — copy bytes into workdir
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(str(abs_file), str(dest_file))
                except PermissionError:
                    log_warn("permission denied copying file")
                    skipped_entries.append({
                        "path": relpath_str,
                        "project": project_name,
                        "reason": "permission-denied",
                    })
                    continue
                except OSError as exc:
                    log_warn(f"could not copy file: {exc}")
                    skipped_entries.append({
                        "path": relpath_str,
                        "project": project_name,
                        "reason": "copy-error",
                    })
                    continue
                entry = _make_file_entry(
                    out_rel,
                    size=st.st_size,
                    mtime=_mtime_iso(st),
                    mode=_mode_octal(st),
                    is_symlink=False,
                    symlink_target=None,
                )
                total_bytes += st.st_size

            file_entries.append(entry)
            file_count += 1

            if file_count >= FILE_COUNT_CAP:
                log_warn(
                    f"project file-count cap ({FILE_COUNT_CAP}) reached for "
                    f"project: name={project_name}"
                )
                cap_hit = True
                break

        if cap_hit:
            break

    summary = {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "cap_hit": cap_hit,
    }
    return file_entries, summary, skipped_entries


# ---------------------------------------------------------------------------
# Core walk
# ---------------------------------------------------------------------------

def walk_fullsnap(
    roots: list[str],
    home: str,
    workdir: str,
    exclude_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """
    Walk each root, identify local-only project directories, copy their full
    file trees into the workdir, and return the walk output dict.
    """
    if exclude_patterns is None:
        exclude_patterns = []

    workdir_path = Path(workdir)
    projects_dir = workdir_path / PROJECTS_SUBDIR
    projects_dir.mkdir(parents=True, exist_ok=True)

    projects: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    all_skipped: list[dict[str, Any]] = []
    excluded_file_count = 0

    for raw_root in roots:
        # Resolve ~/
        if raw_root.startswith("~/"):
            resolved_root = Path(home) / raw_root[2:]
        elif raw_root == "~":
            resolved_root = Path(home)
        else:
            resolved_root = Path(raw_root)

        if not resolved_root.is_dir():
            log_warn("root not found, skipping")
            log_path(resolved_root)
            all_skipped.append({"path": str(resolved_root), "reason": "root-not-found"})
            continue

        log_path(resolved_root)

        try:
            children = sorted(resolved_root.iterdir(), key=lambda p: p.name.lower())
        except PermissionError:
            log_warn("permission denied reading root")
            all_skipped.append({"path": str(resolved_root), "reason": "permission-denied"})
            continue

        for child in children:
            # Only consider directories (not symlinks-to-dirs)
            try:
                if not child.is_dir() or child.is_symlink():
                    continue
            except OSError:
                continue

            git_dir = child / ".git"
            has_git = git_dir.exists()

            if has_git:
                # Check for remote
                remote_url = _git_remote_url(child)
                if remote_url:
                    # Has remote → covered by repo-list, skip
                    log_info(f"project skipped (has git remote): name={child.name}")
                    all_skipped.append({
                        "path": str(child),
                        "reason": "has-git-remote",
                    })
                    continue
                included_reason = "git-no-remote"
            else:
                included_reason = "no-git"

            log_info(f"project included: name={child.name} reason={included_reason}")
            log_path(child)

            file_entries, summary, proj_skipped = _capture_project(
                child,
                projects_dir,
                exclude_patterns,
            )

            # Count excluded items
            excluded_file_count += sum(
                1 for s in proj_skipped if s.get("reason") == "excluded-glob"
            )

            all_files.extend(file_entries)
            all_skipped.extend(proj_skipped)

            proj_record: dict[str, Any] = {
                "name": child.name,
                "original_path": str(child),
                "file_count": summary["file_count"],
                "total_bytes": summary["total_bytes"],
                "included_reason": included_reason,
            }
            if summary["cap_hit"]:
                proj_record["cap_hit"] = True
                all_skipped.append({
                    "path": str(child),
                    "reason": "file-count-cap",
                    "project": child.name,
                })

            log_count(f"files_captured:{child.name}", summary["file_count"])
            projects.append(proj_record)

    # Sort projects by name for stable output
    projects.sort(key=lambda p: p["name"].lower())

    # Build and write inventory
    inventory: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scanned_at": _now_iso(),
        "projects": projects,
    }
    inventory_bytes = json.dumps(
        inventory, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    inventory_path = projects_dir / INVENTORY_FILENAME
    inventory_path.write_bytes(inventory_bytes)

    inv_rel = f"{PROJECTS_SUBDIR}/{INVENTORY_FILENAME}"
    inv_entry = _make_file_entry(
        inv_rel,
        size=len(inventory_bytes),
        mtime=_now_iso(),
        mode="0644",
        is_symlink=False,
        symlink_target=None,
    )
    all_files.insert(0, inv_entry)

    total_files = len(all_files) - 1  # exclude inventory itself
    log_count("projects_included", len(projects))
    log_count("total_files_captured", total_files)
    log_count("files_skipped", len(all_skipped))
    log_info(
        f"walk-fullsnap complete: projects={len(projects)} "
        f"files={total_files} skipped={len(all_skipped)}"
    )

    return {
        "category": CATEGORY_NAME,
        "workdir": workdir,
        "files": all_files,
        "coverage": {
            "globs": {},
            "skipped": all_skipped,
            "large_files_warned": [],
            "excluded_file_count": excluded_file_count,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_category_config(categories_path: str) -> tuple[list[str], list[str]]:
    """Load roots and exclude patterns from the local-only-projects category."""
    path = Path(categories_path)
    if not path.exists():
        log_error("categories file not found", path=path)
        sys.exit(1)

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    for cat in data.get("categories", []):
        if cat.get("name") == CATEGORY_NAME:
            return cat.get("roots", []), cat.get("exclude", [])

    log_error(f"category '{CATEGORY_NAME}' not found in {categories_path}")
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Walk local-only project directories (no git remote or no git at all), "
            "copy their full file trees into a workdir, and emit a JSON file-list + "
            "coverage report compatible with snapshot.py's --walk-output interface."
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
        help="Directory where captured project files are written (default: fresh tempdir)",
    )
    args = parser.parse_args(argv)

    roots, exclude_patterns = _load_category_config(args.categories)

    if args.workdir is None:
        workdir = tempfile.mkdtemp(prefix="rehydrate-fullsnap-")
        log_info("created workdir")
    else:
        workdir = args.workdir
        Path(workdir).mkdir(parents=True, exist_ok=True)

    result = walk_fullsnap(
        roots=roots,
        home=args.home,
        workdir=workdir,
        exclude_patterns=exclude_patterns,
    )

    output = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        log_info("output written")
    else:
        print(output)


if __name__ == "__main__":
    main()
