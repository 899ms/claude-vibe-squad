"""Falsify privacy checks using local sentinel-only source mutations.

Explicit test names, no discovery, provider invocation, or live store access.
Use --baseline COMMIT to also compare selected checks with pre-fix source.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
TEST = PLUGIN / "tests/test_privacy_boundaries.py"
MUTATIONS = (
    ("jsonl.py", 'screened_json(payload, sort_keys=True, ensure_ascii=False)',
     'json.dumps(payload, sort_keys=True, ensure_ascii=False)', 'test_jsonl_boundary_minimizes_values_and_preserves_shape'),
    ("privacy.py", '    if redact_text(text) != text:',
     '    if False:  # scratch fault injection', 'test_json_serialization_guard_can_reject'),
    ("notes.py", '    note = redact_json_fields(note)',
     '    note = note  # scratch fault injection', 'test_relationship_formatting_is_screened_before_hash_and_write'),
    ("autocapture.py", '        redact_fields(capture_fields), redact_fields(context)',
     '        capture_fields, context', 'test_faulty_upstream_transform_is_contained_at_both_sinks'),
    ("index.py", 'tuple(redact_text(note[field]) for field in (',
     'tuple(note[field] for field in (', 'test_index_screens_final_list_joins'),
)


def run(root, names=()):
    command = [sys.executable, "-B", str(TEST), "-q", *("PrivacyBoundaryTests." + n for n in names)]
    env = dict(os.environ, CHRONO_PRIVACY_TEST_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    print("command=" + repr(command))
    print("stdout:\n" + result.stdout, end="")
    print("stderr:\n" + result.stderr, end="")
    print("exit=" + str(result.returncode))
    return result


def assertion_failed(result):
    return result.returncode == 1 and "FAILED (failures=" in result.stderr and "ERROR:" not in result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline")
    args = parser.parse_args()
    if run(PLUGIN).returncode:
        return 1
    with tempfile.TemporaryDirectory(prefix="privacy-source-control-") as temporary:
        root = Path(temporary) / "repo/plugins/chrono-vault"
        root.mkdir(parents=True)
        for source in PLUGIN.glob("*.py"):
            shutil.copyfile(source, root / source.name)
        if args.baseline:
            for name in ("privacy.py", "autocapture.py", "jsonl.py", "notes.py", "recall.py"):
                content = subprocess.run(["git", "show", f"{args.baseline}:plugins/chrono-vault/{name}"],
                                         cwd=PLUGIN, capture_output=True, check=True).stdout
                (root / name).write_bytes(content)
            for test in ("test_slug_screens_complete_transform_before_bound",
                         "test_jsonl_boundary_minimizes_values_and_preserves_shape",
                         "test_summary_screens_join_and_path_normalization_before_bound"):
                print("baseline sentinel control=" + test)
                if not assertion_failed(run(root, [test])):
                    return 1
            if run(root, ["test_line_cleanup_retains_previous_control_fix"]).returncode:
                return 1
            for source in PLUGIN.glob("*.py"):
                shutil.copyfile(source, root / source.name)
        for name, needle, replacement, test in MUTATIONS:
            print("scratch mutation=" + name + ":" + test)
            original = (root / name).read_text()
            if original.count(needle) != 1:
                raise RuntimeError("mutation anchor must occur exactly once")
            (root / name).write_text(original.replace(needle, replacement))
            result = run(root, [test])
            if not assertion_failed(result):
                return 1
            (root / name).write_text(original)
            print("scratch restoration=" + test)
            if run(root, [test]).returncode:
                return 1
    print("controls: all expected assertion failures observed; all restorations passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
