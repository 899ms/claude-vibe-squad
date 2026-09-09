"""Focused, isolated tests; run this file directly, never discovery/bin/test."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate_root_split as validator


class LiteralInvariantTests(unittest.TestCase):
    def test_every_forbidden_component_and_supported_spelling(self):
        for invariant, roots, directories in ((1, validator.CODE_ROOTS, validator.DATA_DIRS),
                                                (2, validator.DATA_ROOTS, validator.CODE_DIRS)):
            for root in roots:
                for directory in directories:
                    spellings = [
                        '${' + root + '}/' + directory,
                        '$' + root + '/' + directory,
                        '"${' + root + '}"/' + directory,
                        root + ' / "' + directory + '"',
                        'Path(' + root + ') / "' + directory + '"',
                        'os.path.join(' + root + ', "' + directory + '")',
                        root + '.joinpath("' + directory + '")',
                        'f"{' + root + '}/' + directory + '"',
                        'Path(os.environ["' + root + '"]) / "' + directory + '"',
                        'Path(os.environ.get("' + root + '", ".")) / "' + directory + '"',
                    ]
                    for source in spellings:
                        with self.subTest(source=source):
                            hits = validator.scan_text("bin/control.sh", source)
                            self.assertEqual(len(hits), 1, hits)
                            self.assertEqual(hits[0].invariant, invariant)

    def test_positive_and_negative_controls(self):
        self.assertEqual(validator.positive_control(), {
            "status": "PASS", "invariant_1": 1, "invariant_2": 1, "negative_control": 0})

    def test_pythonpath_is_code_selection(self):
        hits = validator.scan_text("bin/control.sh", 'PYTHONPATH="${SQUAD_DATA_ROOT}/plugins/a:${SQUAD_DATA_ROOT}/scripts/python"')
        self.assertEqual([hit.invariant for hit in hits], [2, 2])

    def test_no_substring_or_other_root_false_positives(self):
        source = '\n'.join([
            '${CHRONO_VAULT_ROOT}/_state', 'CHRONO_VAULT_ROOT / "_state"',
            '${OTHER}/bin', '${VAULT_ROOT}/_stateful', '$VAULT_ROOT_BACKUP/_state',
            '${SQUAD_DATA_ROOT}/scripts-backup', '${VAULT_ROOT}/home.txt',
            '${VAULT_ROOT}/bin', '${SQUAD_DATA_ROOT}/_state',
            'object.VAULT_ROOT / "_state"',
        ])
        self.assertEqual(validator.scan_text("bin/control.sh", source), [])

    def test_occurrences_not_lines_and_comments_are_visible(self):
        source = '# example\nx="${VAULT_ROOT}/_state" y="${VAULT_ROOT}/chrono"\n'
        hits = validator.scan_text("docs/example.md", source)
        self.assertEqual([(h.line, h.column, h.category) for h in hits],
                         [(2, 4, "documentation"), (2, 29, "documentation")])
        self.assertEqual(len(validator.scan_text("bin/control.sh", '# ${VAULT_ROOT}/_state')), 1)

    def test_multiline_path_and_test_category(self):
        hits = validator.scan_text("scripts/python/tests/test_x.py", 'value = (VAULT_ROOT\n / "runs")')
        self.assertEqual([(h.invariant, h.line, h.category) for h in hits], [(1, 1, "tests")])


class EnumerationTests(unittest.TestCase):
    def test_git_inventory_reports_exclusions_and_cannot_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "script.py").write_text('x = VAULT_ROOT / "_state"')
            (root / "binary").write_bytes(b'\0VAULT_ROOT/_state')
            (root / "link").symlink_to(root / "script.py")
            inventory = b'script.py\0script.py\0binary\0link\0missing\0_state/no-read\0departments/coding/outbox/no-read\0'
            with mock.patch.object(validator.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=inventory)):
                hits, notices, scanned = validator.scan_tree(root)
            self.assertEqual(scanned, 1)
            self.assertEqual(len(hits), 1)
            self.assertEqual(len(notices), 5)
            self.assertTrue(any("symlink/outside root" in n for n in notices))
            self.assertTrue(any("missing" in n for n in notices))

    def test_exact_counts_per_file_and_category_in_json(self):
        hits = validator.scan_text("bin/one.sh", '${VAULT_ROOT}/_state ${VAULT_ROOT}/chrono')
        hits += validator.scan_text("tests/t.py", 'SQUAD_DATA_ROOT / "bin"')
        output = io.StringIO()
        with mock.patch.object(validator, "scan_tree", return_value=(hits, [], 2)), \
             mock.patch.object(validator, "probe_code_root", return_value=[]), \
             contextlib.redirect_stdout(output):
            self.assertEqual(validator.reporting_main(["--json"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["counts"]["1"], {"total": 2, "by_category": {"production": 2}, "files": {"bin/one.sh": 2}})
        self.assertEqual(report["counts"]["2"]["by_category"], {"tests": 1})
        self.assertEqual(report["design_119_comparison"]["count"], 2)
        self.assertIn("NOT_YET_CHECKABLE", report["invariant_4"])
        self.assertIn("snapshot", report["invariant_4"])


class ResolverProbeTests(unittest.TestCase):
    def test_current_resolvers_ignore_exported_squad_code_root(self):
        root = Path(__file__).resolve().parents[3]
        for result in validator.probe_code_root(root):
            with self.subTest(resolver=result["resolver"]):
                self.assertEqual(result["status"], "PASS", result["detail"])

    def test_positive_control_detects_resolvers_honoring_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "shared").mkdir()
            (root / "scripts/python").mkdir(parents=True)
            (root / "shared/repo-root.sh").write_text(': "${SQUAD_CODE_ROOT}"\n')
            (root / "scripts/python/repo_root.py").write_text(
                'import os\ndef resolve_code_root():\n    return os.environ["SQUAD_CODE_ROOT"]\n')
            results = validator.probe_code_root(root)
            self.assertEqual([r["status"] for r in results], ["FAIL", "FAIL"])
            self.assertTrue(all("hostile-code-root" in r["detail"] for r in results))

    def test_missing_resolvers_are_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(all(r["status"] != "PASS" for r in validator.probe_code_root(Path(tmp).resolve())))


class ReportingOnlyTests(unittest.TestCase):
    def test_operational_errors_exit_zero_and_report_unmeasured(self):
        output = io.StringIO()
        with mock.patch.object(validator, "scan_tree", side_effect=OSError("planted read error")), contextlib.redirect_stderr(output):
            self.assertEqual(validator.reporting_main([]), 0)
        self.assertIn("NOT_CHECKABLE", output.getvalue())
        self.assertIn("planted read error", output.getvalue())

    def test_help_and_bad_arguments_exit_zero(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(validator.reporting_main(["--help"]), 0)
            self.assertEqual(validator.reporting_main(["--unknown-planted-option"]), 0)
        self.assertIn("Exit 0 ALWAYS: zero does not mean clean", output.getvalue())

    def test_real_cli_positive_control_and_reporting_exit(self):
        proc = subprocess.run([sys.executable, "-B", str(Path(validator.__file__)), "--json"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["positive_control"]["status"], "PASS")
        self.assertTrue(report["reporting_only"])
        self.assertGreater(report["files_scanned"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
