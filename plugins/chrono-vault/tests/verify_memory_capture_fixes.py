"""Run the repaired checks, then falsify them in disposable source copies.

Usage: python -B plugins/chrono-vault/tests/verify_memory_capture_fixes.py --baseline COMMIT
Uses explicit unittest names, never discovery. Each child test owns its stores.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
TEST = REPO / "plugins/chrono-vault/tests/test_memory_capture_fixes.py"
CANARIES = {
    "redaction": "test_capture_email_canary",
    "recall": "test_recall_returns_late_body_and_metadata_evidence",
    "retirement": "test_retirement_preserves_history_relationships_and_feedback",
    "resume": "test_resume_does_not_collect_or_render_retired_signal",
    "watcher": "test_watcher_reports_missing_root_and_settlement_continues",
}
FILES = [
    "plugins/chrono-vault/autocapture.py",
    "plugins/chrono-vault/notes.py",
    "plugins/chrono-vault/recall.py",
    "scripts/python/chrono_state/resume.py",
    "bin/outbox-watcher.sh",
]


def run(source, tests):
    env = dict(os.environ, CHRONO_MEMORY_TEST_ROOT=str(source), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", str(TEST), "-q", *tests]
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
    print("command=" + repr(command))
    print("stdout:\n" + result.stdout, end="")
    print("stderr:\n" + result.stderr, end="")
    print(f"exit={result.returncode}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="pre-fix Git commit")
    baseline = parser.parse_args().baseline
    print("=== repaired source ===")
    if run(REPO, []).returncode != 0:
        return 1
    with tempfile.TemporaryDirectory(prefix="memory-source-control-") as temporary:
        source = Path(temporary)
        for path in (REPO / "plugins/chrono-vault").glob("*.py"):
            destination = source / "plugins/chrono-vault" / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        for relative in FILES:
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = subprocess.run(
                ["git", "show", f"{baseline}:{relative}"], cwd=REPO,
                capture_output=True, check=True,
            ).stdout
            destination.write_bytes(content)
        for label, name in CANARIES.items():
            print(f"=== pre-fix source: {label} ===")
            result = run(source, ["ScratchMemoryTests." + name])
            if result.returncode != 1 or "FAILED (failures=" not in result.stderr:
                print("CONTROL FAILED: expected assertion failure, not success or an import/runtime error")
                return 1
        # Break only privacy in the repaired source, proving the check is not
        # relying on incidental differences in the historical implementation.
        for relative in FILES:
            shutil.copyfile(REPO / relative, source / relative)
        privacy = source / "plugins/chrono-vault/privacy.py"
        text = privacy.read_text()
        needle = '        text = pattern.sub("[REDACTED]", text)'
        if text.count(needle) != 1:
            raise RuntimeError("privacy mutation anchor changed")
        privacy.write_text(text.replace(needle, "        text = text  # synthetic broken control"))
        print("=== repaired source with privacy disabled ===")
        result = run(source, ["ScratchMemoryTests.test_capture_email_canary"])
        if result.returncode != 1 or "FAILED (failures=" not in result.stderr:
            return 1
        shutil.copyfile(REPO / "plugins/chrono-vault/privacy.py", privacy)
        print("=== scratch privacy restored ===")
        if run(source, ["ScratchMemoryTests.test_capture_email_canary"]).returncode != 0:
            return 1
    print("controls: repaired suite passed; five pre-fix checks failed; privacy mutation failed; restoration passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
