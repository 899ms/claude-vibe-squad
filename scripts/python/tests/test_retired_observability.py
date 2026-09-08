"""Hermetic controls for retiring obsolete diagnostic channels.

The detached shell block is production code. Its trusted child, receipt
finalizer and settlement dependencies are fixture executables; this tests
diagnostic routing without launching a worker or observing host processes.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "bin/board-supervisor.sh"
FAILURES = {
    "capture": "receipt capture staging failed",
    "publication": "blocked completion publication failed",
    "reconciliation": "registry reconciliation failed",
    "cleanup": "canary cleanup failed",
}


def run_detached(source, stage, *, bad_log=False):
    begin = source.index('if [[ "${1:-}" == "detached-launch" ]]')
    end = source.index('if [[ "${1:-}" == "trusted-launch" ]]', begin)
    body = source[begin:end]
    with tempfile.TemporaryDirectory(prefix="retired-observability-") as temp:
        root = Path(temp)
        (root / "bin").mkdir()
        (root / "scripts/python").mkdir(parents=True)
        child = root / "bin/board-supervisor.sh"
        child.write_text(
            '#!/bin/bash\nprintf "fixture child diagnostic\\n" >&2\n'
            'printf \'{"status":"%s"}\\n\' "$FIXTURE_STATUS"\n'
        )
        child.chmod(0o755)
        finalizer = root / "scripts/python/board_process_truth.py"
        finalizer.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "capture, descriptor, receipt = map(Path, sys.argv[2:])\n"
            "result = json.loads(capture.read_text())\n"
            "result.update(json.loads(descriptor.read_text()))\n"
            "receipt.write_text(json.dumps(result))\n"
        )
        builder = root / "builder.py"
        builder.write_text(
            "import os, sys\n"
            "stage = os.environ['FIXTURE_STAGE']\n"
            "sys.exit(1 if (stage, sys.argv[1]) in "
            "{('publication', 'blocked'), ('cleanup', 'cleanup-canary')} else 0)\n"
        )
        context = root / "context.json"
        context.write_text(json.dumps({"authority": {
            "task_id": "TASK-fixture", "attempt_id": "d-fixture", "generation": 1,
        }}))
        descriptor = root / "dispatch.json"
        descriptor.write_text(json.dumps({
            "schema": "board-dispatch-receipt/v2", "task_id": "TASK-fixture",
            "attempt_id": "d-fixture", "generation": 1,
            "completed_at": "2026-09-07T00:00:00Z", "terminal_outcome": "complete",
        }))
        log = root / "attempt.log"
        if bad_log:
            log.mkdir()
        receipt = root / ("missing/receipt.json" if stage == "capture" else "receipt.json")
        marker = root / "settlement-error"
        script = root / "detached.sh"
        script.write_text(
            'set -uo pipefail\n'
            f'python_bin={shlex.quote(sys.executable)}\n'
            f'repo_root={shlex.quote(str(root))}\n'
            'trusted_host_path="$PATH"\n'
            'controller_quarantine_marker=fixture\n'
            'board_dispatch_descriptor="$BOARD_DISPATCH_DESCRIPTOR_PATH"\n'
            + body
        )
        args = ["bash", str(script), "detached-launch", str(context), str(log), str(receipt)]
        args.extend([
            str(builder), str(root), "TASK-fixture", "codex", "result.md", "coding",
            "/usr/bin/false" if stage == "reconciliation" else "/usr/bin/true",
        ])
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=15,
            env={**os.environ, "FIXTURE_STAGE": stage,
                 "FIXTURE_STATUS": "denied" if stage == "publication" else "launched",
                 "BOARD_DISPATCH_DESCRIPTOR_PATH": str(descriptor)},
        )
        return {
            "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            "log": log.read_text() if log.is_file() else "",
            "marker": marker.read_text() if marker.is_file() else "",
            "receipt": json.loads(receipt.read_text()) if receipt.is_file() else None,
        }


class DetachedDiagnosticTests(unittest.TestCase):
    def test_failure_paths_have_diagnostics(self):
        source = SUPERVISOR.read_text()
        for stage, message in FAILURES.items():
            with self.subTest(stage=stage):
                result = run_detached(source, stage)
                self.assertEqual(result["rc"], 70, result)
                self.assertIn(message, result["log"], result)
                self.assertIn("task=TASK-fixture lane=codex", result["log"])
                self.assertIn(f"stage={stage}", result["log"])
                self.assertIn(message, result["stderr"], result)
                self.assertEqual(result["marker"], "", result)
                if stage == "capture":
                    self.assertIsNone(result["receipt"])

    def test_unwritable_attempt_log_preserves_stderr_fallback(self):
        result = run_detached(SUPERVISOR.read_text(), "capture", bad_log=True)
        self.assertEqual(result["rc"], 70, result)
        self.assertIn(FAILURES["capture"], result["stderr"])
        self.assertIn("attempt log diagnostic write failed", result["stderr"])
        self.assertEqual(result["marker"], "")

    def test_diagnostic_loss_mutation_is_detected(self):
        source = SUPERVISOR.read_text()
        broken = source.replace('    settlement_diagnostic capture "receipt capture staging failed"', '    :')
        self.assertNotEqual(source, broken)
        result = unittest.TestResult()
        with mock.patch(__name__ + ".SUPERVISOR") as supervisor:
            supervisor.read_text.return_value = broken
            DetachedDiagnosticTests("test_failure_paths_have_diagnostics").run(result)
        self.assertEqual(len(result.failures), 1, result.failures)
        self.assertEqual(result.errors, [])
        self.assertIn("receipt capture staging failed", result.failures[0][1])

    def test_success_retains_separate_receipt_and_transcript(self):
        result = run_detached(SUPERVISOR.read_text(), "success")
        self.assertEqual(result["rc"], 0, result)
        self.assertEqual(result["receipt"]["status"], "launched")
        self.assertIn("fixture child diagnostic", result["log"])
        self.assertIn("board_supervisor_rc=0 status=launched", result["log"])
        self.assertEqual(result["marker"], "")

    def test_real_entrypoint_logs_pre_open_failure(self):
        with tempfile.TemporaryDirectory(prefix="real-pre-open-") as temp:
            root = Path(temp)
            descriptor = root / "dispatch.json"
            descriptor.write_text("{}")
            log = root / "attempt.log"
            result = subprocess.run(
                ["bash", str(SUPERVISOR), "detached-launch",
                 str(root / "context.json"), str(log), str(root / "missing/receipt.json"),
                 "/usr/bin/false", str(root), "TASK-fixture", "codex", "result.md",
                 "coding", "/usr/bin/false"],
                env={**os.environ, "SQUAD_BASE_BRANCH": "fixture",
                     "BOARD_DISPATCH_DESCRIPTOR_PATH": str(descriptor),
                     "CHRONO_DOCTOR_LOG_DIR": str(root / "doctor-logs")},
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 70, result.stdout + result.stderr)
            self.assertIn(FAILURES["capture"], log.read_text())
            self.assertIn("task=TASK-fixture lane=codex", log.read_text())
            self.assertIn(FAILURES["capture"], result.stderr)
            self.assertEqual(list(root.glob("*.settlement-error")), [])


def run_where(source, *, watchers=True):
    body = source[source.index("# Tmux pane state"):]
    with tempfile.TemporaryDirectory(prefix="where-log-guidance-") as temp:
        tmux = Path(temp) / "tmux"
        tmux.write_text(
            '#!/bin/bash\ncase "$1" in\n'
            'has-session) exit 0;;\n'
            'list-windows) printf "chrono\\n"; '
            + ('printf "watchers/status\\n";' if watchers else ':;')
            + '\n;;\ncapture-pane) printf "fixture pane output\\n";;\nesac\n'
        )
        tmux.chmod(0o755)
        prelude = (
            'set -uo pipefail\nVAULT_ROOT=/fixture\nDATE=2026-09-07\n'
            'hr() { :; }\ncolor() { printf "%s\\n" "$2"; }\n'
            'runtime_window_name() { case "$1" in watchers) echo watchers/status;; '
            '*) echo "$1";; esac; }\n'
            'runtime_display_name() { echo "$1"; }\n'
        )
        return subprocess.run(
            ["bash", "-c", prelude + body], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{temp}:/usr/bin:/bin"}, timeout=10,
        )


class LogGuidanceTests(unittest.TestCase):
    def test_current_windows_and_attempt_log_guidance(self):
        result = run_where((ROOT / "bin/where-are-we.sh").read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("missing window", result.stdout)
        self.assertNotIn("<model-lane>.log", result.stdout)
        self.assertIn("chrono.log", result.stdout)
        self.assertIn("watchers-status.log", result.stdout)
        self.assertIn("<task>.<attempt>.log", result.stdout)
        self.assertIn("<task>.<attempt>.receipt.json", result.stdout)

    def test_missing_current_window_still_reports(self):
        result = run_where((ROOT / "bin/where-are-we.sh").read_text(), watchers=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("watchers/status: missing window", result.stdout)

if __name__ == "__main__":
    unittest.main()
