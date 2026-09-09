#!/usr/bin/env python3
"""Report root-split migration debt. Exit 0 ALWAYS: zero does not mean clean.

Count literal occurrences (not lines) in Git tracked and unignored files.
Comments/examples are included, with production, tests, and documentation
reported separately. Ignore runtime data, dependencies, binaries and symlinks;
print every exclusion/error. This is a lexical guard, not alias/dataflow analysis
or a runtime import-closure proof. Directory sets come from design §2.3.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

DATA_DIRS = ("_state", "departments", "chrono", "runs", "cache", "home")
CODE_DIRS = ("bin", "scripts", "shared", "daemon", "plugins", "tools", ".venv", "model-lanes")
CODE_ROOTS = ("VAULT_ROOT", "SQUAD_CODE_ROOT")
DATA_ROOTS = ("SQUAD_DATA_ROOT",)
EXCLUDED_DIRS = {".git", "_state", ".venv", "node_modules", "__pycache__", "vendor"}
MAILBOX_DIRS = {"inbox", "outbox", "active", "archive"}


@dataclass(frozen=True)
class Violation:
    invariant: int
    file: str
    line: int
    column: int
    reference: str
    category: str


def category(path: str) -> str:
    parts = Path(path).parts
    if any(p in {"test", "tests", "fixtures", "__tests__"} for p in parts) or Path(path).name.startswith("test_"):
        return "tests"
    if Path(path).suffix in {".md", ".rst", ".txt"} or parts[0] == "docs":
        return "documentation"
    return "production"


def patterns(roots: tuple[str, ...], dirs: tuple[str, ...]):
    root = "(?:" + "|".join(map(re.escape, roots)) + ")"
    directory = "(?:" + "|".join(map(re.escape, dirs)) + ")(?![\\w.-])"
    # Shell, f-string interpolation, and Path / literal. A quote between the
    # variable and slash handles shell's "${ROOT}"/dir spelling too.
    yield re.compile(
        rf"(?:\$\{{{root}\}}|\${root}(?!\w)|(?<![\w$])\{{{root}\}}|(?<![\w$.]){root}(?!\w))"
        rf"[\"']?\s*/\s*[\"']?{directory}"
    )
    # Explicit Path(ROOT), Path(os.environ[ROOT]) and os.environ.get(ROOT, ...).
    value = rf"(?:{root}|os\.environ\[\s*['\"]{root}['\"]\s*\]|os\.environ\.get\(\s*['\"]{root}['\"](?:\s*,\s*['\"][^'\"]*['\"])?\s*\))"
    yield re.compile(rf"\bPath\(\s*{value}\s*\)\s*/\s*['\"]{directory}")
    yield re.compile(rf"\bos\.path\.join\(\s*{value}\s*,\s*['\"]{directory}")
    yield re.compile(rf"(?<![\w$.]){root}\.joinpath\(\s*['\"]{directory}")


RULES = [(1, tuple(patterns(CODE_ROOTS, DATA_DIRS))),
         (2, tuple(patterns(DATA_ROOTS, CODE_DIRS)))]


def scan_text(path: str, source: str) -> list[Violation]:
    hits = []
    for invariant, regexes in RULES:
        for regex in regexes:
            for match in regex.finditer(source):
                start = match.start()
                hits.append(Violation(invariant, path, source.count("\n", 0, start) + 1,
                                      start - source.rfind("\n", 0, start),
                                      match.group().replace("\n", "\\n"), category(path)))
    return sorted(hits, key=lambda hit: (hit.file, hit.line, hit.column, hit.invariant))


def excluded(path: str) -> bool:
    parts = Path(path).parts
    return bool(EXCLUDED_DIRS.intersection(parts)) or (
        len(parts) > 2 and parts[0] == "departments" and parts[2] in MAILBOX_DIRS
    )


def scan_tree(root: Path) -> tuple[list[Violation], list[str], int]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=True, timeout=30)
    hits, notices, scanned = [], [], 0
    for name in sorted(set(os.fsdecode(result.stdout).split("\0")) - {""}):
        path = root / name
        if excluded(name):
            notices.append(f"excluded data/dependency: {name}")
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            notices.append(f"not scanned (symlink/outside root): {name}")
            continue
        try:
            raw = path.read_bytes()
            if b"\0" in raw:
                notices.append(f"not scanned (binary): {name}")
                continue
            source = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            notices.append(f"not scanned: {name}: {exc}")
            continue
        scanned += 1
        hits.extend(scan_text(name, source))
    return hits, notices, scanned


def probe_code_root(root: Path) -> list[dict[str, str]]:
    """Exercise only the two resolvers, in clean child environments.

    Valid isolated data prevents eager data resolution from obscuring the code
    root assertion. Never import launchers or touch the operator's HOME/state.
    The legacy VAULT_ROOT environment override is NOT covered by invariant 3.
    """
    results = []
    with tempfile.TemporaryDirectory(prefix="root-split-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as tmp:
        data = Path(tmp).resolve()
        (data / "_state").mkdir()
        hostile = str(data / "hostile-code-root")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(data), "PYTHONDONTWRITEBYTECODE": "1",
               "SQUAD_CODE_ROOT": hostile, "SQUAD_DATA_ROOT": str(data)}
        python_probe = (
            "import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('root_probe',sys.argv[1]); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "print(m.resolve_code_root())"
        )
        commands = {
            "python": [sys.executable, "-I", "-B", "-c", python_probe, str(root / "scripts/python/repo_root.py")],
            "shell": ["/bin/bash", "--noprofile", "--norc", "-c",
                      'source "$1"; printf "%s\\n" "$SQUAD_CODE_ROOT"',
                      "root-probe", str(root / "shared/repo-root.sh")],
        }
        for language, command in commands.items():
            try:
                proc = subprocess.run(command, env=env, cwd=data, capture_output=True, text=True, timeout=10)
                actual = proc.stdout.strip()
                status = "PASS" if proc.returncode == 0 and actual == str(root) else "FAIL"
                detail = f"expected={root}; actual={actual!r}; exit={proc.returncode}; stderr={proc.stderr.strip()!r}"
            except (OSError, subprocess.SubprocessError) as exc:
                status, detail = "NOT_CHECKABLE", str(exc)
            results.append({"resolver": language, "status": status, "detail": detail})
    return results


def positive_control() -> dict[str, int | str]:
    # Assemble fixtures so the validator's own source does not add migration debt.
    source = 'state="${%s}/%s/control"\nPYTHONPATH="${%s}/%s/python"\n' % (
        CODE_ROOTS[1], DATA_DIRS[0], DATA_ROOTS[0], CODE_DIRS[1])
    counts = Counter(hit.invariant for hit in scan_text("bin/planted-control.sh", source))
    negative = scan_text("bin/clean-control.sh", 'x="${OTHER}/_state"\ny="${SQUAD_DATA_ROOT}/_state"\n')
    return {"status": "PASS" if counts == {1: 1, 2: 1} and not negative else "FAIL",
            "invariant_1": counts[1], "invariant_2": counts[2], "negative_control": len(negative)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help="Git worktree to scan (default: this file's root)")
    parser.add_argument("--json", action="store_true", help="emit the same report as JSON")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    hits, notices, scanned = scan_tree(root)
    counts = {str(i): {"total": sum(h.invariant == i for h in hits),
                       "by_category": dict(Counter(h.category for h in hits if h.invariant == i)),
                       "files": dict(sorted(Counter(h.file for h in hits if h.invariant == i).items()))}
              for i in (1, 2)}
    legacy_shell = [h for h in hits if h.invariant == 1 and h.category == "production"
                    and re.match(r"\$(?:\{VAULT_ROOT\}|VAULT_ROOT)/", h.reference)]
    report = {"reporting_only": True, "root": str(root), "files_scanned": scanned,
              "counts": counts, "violations": [vars(h) for h in hits], "notices": notices,
              "design_119_comparison": {
                  "count": len(legacy_shell),
                  "files": dict(sorted(Counter(h.file for h in legacy_shell).items())),
                  "method": "Production direct braced/bare VAULT_ROOT slash references only; excludes quoted-root slash, Python paths and bare comment references. This reproduces the design's narrower 119-site census, not the full invariant."},
              "invariant_3": probe_code_root(root),
              "invariant_4": "NOT_YET_CHECKABLE: snapshot read-only permissions and ownership; no installed snapshot supplied",
              "positive_control": positive_control(),
              "limitations": "Literal names only; no alias tracking, dynamic path evaluation or transitive runtime closure. Comments/examples counted. VAULT_ROOT override not tested by invariant 3."}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("REPORTING ONLY — always exits 0; zero does not mean clean.")
        print(f"Root: {root}; files scanned: {scanned}")
        for invariant, summary in counts.items():
            print(f"Invariant {invariant}: {summary['total']} violations; categories={summary['by_category']}")
            for file, count in summary["files"].items():
                print(f"  {count:4d} {file}")
        for hit in hits:
            print(f"I{hit.invariant} {hit.file}:{hit.line}:{hit.column}: {hit.reference}")
        comparison = report["design_119_comparison"]
        print(f"Design 119 comparison: {comparison['count']} sites in {len(comparison['files'])} files")
        print(f"Comparison method: {comparison['method']}")
        for probe in report["invariant_3"]:
            print(f"Invariant 3 ({probe['resolver']}): {probe['status']}: {probe['detail']}")
        print(f"Invariant 4: {report['invariant_4']}")
        print(f"Positive control: {report['positive_control']}")
        print(f"Limits: {report['limitations']}")
        for notice in notices:
            print(f"NOTICE: {notice}")
    return 0


def reporting_main(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except SystemExit:
        # argparse's usage errors must also remain reporting-only in step 2.
        return 0
    except Exception as exc:
        print(f"NOT_CHECKABLE: validator error: {type(exc).__name__}: {exc}; reporting-only exit 0", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(reporting_main())
