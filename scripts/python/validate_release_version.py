#!/usr/bin/env python3
"""Keep the four current-release claims aligned with CHANGELOG.md.

The first released level-two changelog header wins (skip only Unreleased).
Unlike tags, it is present in source archives and can be updated in the same
commit as the claims. No VERSION file or hardcoded current version is needed.

Only the named claims in CLAUDE.md, README.md, and SECURITY.md are checked.
Dated docs, archived plans/charters, the export ledger, older changelog entries,
and historical/future examples alongside the claims are deliberately excluded.
Missing, malformed, or duplicate current claims fail closed.

Use --staged in pre-commit to check index blobs, never unstaged worktree repairs.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


VERSION = r"v[0-9]+\.[0-9]+\.[0-9]+"
INPUT_PATHS = ("CHANGELOG.md", "CLAUDE.md", "README.md", "SECURITY.md")


def _one_version(text: str, pattern: str) -> str:
    matches = re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one current-release claim; found {len(matches)}")
    return matches[0].casefold()


def changelog_version(text: str) -> str:
    """Do not skip a malformed release header and silently select an older one."""
    date = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    for header in re.findall(r"^##[ \t]+(.+?)[ \t]*$", text, flags=re.MULTILINE):
        tokens = header.split(maxsplit=1)
        if tokens and tokens[0].casefold() in {"unreleased", "[unreleased]"}:
            continue
        match = re.fullmatch(
            rf"({VERSION})(?:[ \t]+(?:[-—][ \t]+{date}|\({date}\)))?",
            header,
            flags=re.IGNORECASE,
        )
        if match is None:
            raise ValueError("top released header must be '## vX.Y.Z' (optionally followed by a date)")
        return match[1].casefold()
    raise ValueError("missing released level-two header")


def root_instruction_version(text: str) -> str:
    return _one_version(
        text,
        rf"^[ \t]*-[ \t]+\*\*`({VERSION})`[ \t]+is the current release version\.\*\*",
    )


def readme_badge_version(text: str) -> str:
    return _one_version(
        text,
        rf"^!\[version\]\(https://img\.shields\.io/badge/version-({VERSION})-"
        r"[a-z0-9]+(?:\?[^\s)]+)?\)[ \t]*$",
    )


def readme_prose_version(text: str) -> str:
    return _one_version(text, rf"^\*\*({VERSION})\*\*[ \t]+is the current release\b")


def security_version(text: str) -> str:
    return _one_version(text, rf"\bThe current release is[ \t]+\*\*({VERSION})\*\*(?=[.;])")


SITES = (
    ("CLAUDE.md current-release passage", "CLAUDE.md", root_instruction_version),
    ("README.md badge image URL", "README.md", readme_badge_version),
    ("README.md current-release prose", "README.md", readme_prose_version),
    ("SECURITY.md current-release passage", "SECURITY.md", security_version),
)


def _read(root: Path, path: str, staged: bool) -> str:
    if not staged:
        return (root / path).read_text(encoding="utf-8")
    result = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", f":{path}"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode:
        raise ValueError("unable to read staged blob (missing, unmerged, or Git failed)")
    return result.stdout.decode("utf-8")


def validate(root: Path, *, staged: bool = False) -> tuple[str | None, list[str]]:
    documents: dict[str, str] = {}
    errors: list[str] = []
    for path in INPUT_PATHS:
        try:
            documents[path] = _read(root, path, staged)
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        return None, errors

    try:
        expected = changelog_version(documents["CHANGELOG.md"])
    except ValueError as exc:
        return None, [f"CHANGELOG.md source of truth: {exc}"]

    for site, path, extract in SITES:
        try:
            actual = extract(documents[path])
        except ValueError as exc:
            errors.append(f"{site}: {exc}")
            continue
        if actual != expected:
            errors.append(f"{site}: found {actual}; expected {expected} from CHANGELOG.md")
    return expected, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="checkout to check (default: this script's repository)")
    parser.add_argument("--staged", action="store_true", help="read Git's index in the current checkout")
    args = parser.parse_args(argv)
    root = args.root or (Path.cwd() if args.staged else Path(__file__).resolve().parents[2])
    expected, errors = validate(root, staged=args.staged)
    if errors:
        print("release-version: FAIL; refusing commit", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    surface = "staged index" if args.staged else "worktree"
    print(f"release-version: PASS ({expected}; 4 sites; CHANGELOG.md source; {surface})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
