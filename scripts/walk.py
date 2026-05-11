"""
walk.py — rehydrate Phase 2.3

Given a category from categories.yaml, expand its globs against --home,
collect file metadata (no hashing), and emit a JSON coverage report.

Usage:
    python3 scripts/walk.py --category dotfiles [--home PATH] [--categories PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import fnmatch
import glob as glob_module
import json
import os
import re
import stat as stat_module
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Allow running as a script directly OR as scripts.walk module
# ---------------------------------------------------------------------------
try:
    from scripts.no_pii_log import log_error, log_info, log_warn, log_debug
except ImportError:
    # Running as __main__ directly from repo root
    _here = Path(__file__).parent
    sys.path.insert(0, str(_here.parent))
    from scripts.no_pii_log import log_error, log_info, log_warn, log_debug

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_DEFAULT_CATEGORIES = _REPO_ROOT / "categories.yaml"


def _load_category(categories_path: str, category_name: str) -> dict[str, Any]:
    """Load and return the named category dict; exit on error."""
    path = Path(categories_path)
    if not path.exists():
        log_error("categories file not found", path=path)
        sys.exit(1)

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    categories: list[dict] = data.get("categories", [])
    for cat in categories:
        if cat.get("name") == category_name:
            if not cat.get("enabled", True):
                log_error(
                    f"category '{category_name}' is disabled (enabled: false) — "
                    "set enabled: true in categories.yaml to use it"
                )
                sys.exit(1)
            return cat

    log_error(
        f"category '{category_name}' not found in {categories_path}; "
        f"available: {[c.get('name') for c in categories]}"
    )
    sys.exit(1)


def _resolve_glob(pattern: str, home: str) -> str:
    """Replace a leading ~/ (or bare ~) with the --home directory."""
    if pattern.startswith("~/"):
        return home.rstrip("/") + "/" + pattern[2:]
    if pattern == "~":
        return home
    return pattern


def _rel_path(abs_path: str, home: str) -> str:
    """Return abs_path relative to home (no leading slash)."""
    home_norm = home.rstrip("/") + "/"
    if abs_path.startswith(home_norm):
        return abs_path[len(home_norm):]
    # Fallback: strip leading slash if outside home (shouldn't happen normally)
    return abs_path.lstrip("/")


def _mtime_iso(stat_result) -> str:
    """Convert stat mtime to ISO 8601 UTC string."""
    dt = datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mode_octal(stat_result) -> str:
    """Return the permission bits as a 4-digit octal string (e.g. '0644')."""
    return f"{stat_result.st_mode & 0o7777:04o}"


def _matches_any_exclude(abs_path: str, exclude_patterns: list[str], home: str) -> bool:
    """Return True if abs_path matches any exclude glob pattern."""
    for pattern in exclude_patterns:
        resolved = _resolve_glob(pattern, home)
        # fnmatch works on the full path for '**'-free patterns;
        # for '**' patterns use glob-style matching on the basename or full path.
        if "**" in resolved:
            # Normalise: match the absolute path against the expanded pattern.
            # We use fnmatch against the path itself, treating ** as matching
            # any sequence of path components.
            # Strategy: convert the glob pattern to a regex-free check by
            # checking if any suffix of the path components matches the tail.
            if _glob_match(abs_path, resolved):
                return True
        else:
            if fnmatch.fnmatch(abs_path, resolved):
                return True
            # Also check basename-only patterns like ".DS_Store"
            if fnmatch.fnmatch(os.path.basename(abs_path), os.path.basename(resolved)):
                return True
    return False


def _glob_match(path: str, pattern: str) -> bool:
    """
    Match path against a glob pattern that may contain **.
    Uses fnmatch.fnmatchcase on the normalised path after converting ** to a
    placeholder, matching the semantics of shell **-glob.
    """
    # Escape everything except * and ?
    # Build a regex: ** → .* (any chars including /), * → [^/]*, ? → [^/]
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

    regex = _to_regex(pattern)
    return bool(re.match(regex, path))


# ---------------------------------------------------------------------------
# Core walk
# ---------------------------------------------------------------------------

def walk_category(
    category: dict[str, Any],
    home: str,
) -> dict[str, Any]:
    """
    Perform the filesystem walk for a file-list category.

    Returns the full output dict (category, files, coverage).
    """
    category_name: str = category["name"]
    globs: list[str] = category.get("globs", [])
    exclude_patterns: list[str] = category.get("exclude", [])

    files: list[dict[str, Any]] = []
    glob_counts: dict[str, int] = {}
    skipped: list[dict[str, str]] = []
    large_files_warned: list[str] = []

    # Track seen absolute paths to avoid duplicates from overlapping globs
    seen: set[str] = set()

    for raw_glob in globs:
        resolved = _resolve_glob(raw_glob, home)
        use_recursive = "**" in resolved
        try:
            matches = glob_module.glob(resolved, recursive=use_recursive)
        except Exception as exc:  # noqa: BLE001
            log_warn(f"glob expansion failed for pattern '{raw_glob}': {exc}")
            glob_counts[raw_glob] = 0
            continue

        count = 0
        for abs_path in matches:
            abs_path = os.path.normpath(abs_path)

            if abs_path in seen:
                continue

            # Stat with lstat so symlinks are reported as themselves
            try:
                st = os.lstat(abs_path)
            except PermissionError:
                rel = _rel_path(abs_path, home)
                log_warn(f"permission denied: {rel}")
                skipped.append({"path": rel, "reason": "permission-denied"})
                seen.add(abs_path)
                continue
            except OSError:
                # Broken symlink or other OS error
                rel = _rel_path(abs_path, home)
                is_link = os.path.islink(abs_path)
                if is_link:
                    log_warn(f"broken symlink: {rel}")
                    skipped.append({"path": rel, "reason": "broken-symlink"})
                else:
                    log_warn(f"could not stat (skipping): {rel}")
                    skipped.append({"path": rel, "reason": "permission-denied"})
                seen.add(abs_path)
                continue

            is_link = stat_module.S_ISLNK(st.st_mode)
            is_reg = stat_module.S_ISREG(st.st_mode)

            # Skip directories and other non-file/non-symlink entries
            if not is_reg and not is_link:
                rel = _rel_path(abs_path, home)
                skipped.append({"path": rel, "reason": "not-a-file"})
                seen.add(abs_path)
                continue

            # For symlinks: verify target exists (broken symlink check)
            if is_link:
                target = os.readlink(abs_path)
                # os.path.exists follows symlinks; False means broken link
                if not os.path.exists(abs_path):
                    # os.path.exists follows symlinks; if False the link is broken
                    rel = _rel_path(abs_path, home)
                    log_warn(f"broken symlink: {rel}")
                    skipped.append({"path": rel, "reason": "broken-symlink"})
                    seen.add(abs_path)
                    continue
            else:
                target = None

            rel = _rel_path(abs_path, home)

            # Apply exclude patterns
            if _matches_any_exclude(abs_path, exclude_patterns, home):
                log_debug(f"excluded: {rel}")
                seen.add(abs_path)
                continue

            count += 1
            seen.add(abs_path)

            # Warn on large files but include them
            if st.st_size > LARGE_FILE_THRESHOLD:
                log_info(f"large file (>{LARGE_FILE_THRESHOLD // (1024*1024)}MB): {rel}")
                large_files_warned.append(rel)

            files.append({
                "path": rel,
                "size": st.st_size,
                "mtime": _mtime_iso(st),
                "mode": _mode_octal(st),
                "is_symlink": is_link,
                "symlink_target": target,
            })

        glob_counts[raw_glob] = count

    log_info(f"walk complete: category={category_name} files={len(files)} skipped={len(skipped)}")

    return {
        "category": category_name,
        "files": files,
        "coverage": {
            "globs": glob_counts,
            "skipped": skipped,
            "large_files_warned": large_files_warned,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Walk a rehydrate category and emit a JSON file list + coverage report.",
    )
    parser.add_argument(
        "--categories",
        default=str(_DEFAULT_CATEGORIES),
        help="Path to categories.yaml (default: categories.yaml at repo root)",
    )
    parser.add_argument(
        "--category",
        required=True,
        help="Name of the category to walk (e.g. dotfiles)",
    )
    parser.add_argument(
        "--home",
        default=os.path.expanduser("~"),
        help="Home directory to resolve ~ globs against (default: $HOME)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for JSON (default: stdout)",
    )
    args = parser.parse_args(argv)

    category = _load_category(args.categories, args.category)

    strategy = category.get("strategy")
    if strategy != "file-list":
        log_error(
            f"category '{args.category}' uses strategy '{strategy}'; "
            "walk.py only supports 'file-list'"
        )
        sys.exit(1)

    result = walk_category(category, home=args.home)

    output = json.dumps(result, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        log_info(f"output written to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
