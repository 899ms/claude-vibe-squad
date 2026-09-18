"""Root-resolution and migration-reporter tests; safe for unittest discovery."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import validate_root_split as validator

ROOT = Path(__file__).resolve().parents[3]


def probe_vault_root(root, cwd, overrides=None):
    """Exercise the current public APIs, independent of the migration reporter.

    Preserve exit codes and stderr: a missing API, failed source, empty output,
    and an inherited hostile value must not become indistinguishable passes.
    """
    env = {"PATH": os.defpath, "HOME": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(overrides or {})
    python = (
        "import importlib.util,sys; "
        "s=importlib.util.spec_from_file_location('root_probe',sys.argv[1]); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "print(m.resolve_vault_root())"
    )
    commands = {
        "python": [sys.executable, "-I", "-B", "-c", python,
                   str(root / "scripts/python/repo_root.py")],
        "shell": ["/bin/bash", "--noprofile", "--norc", "-c",
                  'source "$1" || exit $?; printf "%s\\n" "${VAULT_ROOT-}"',
                  "root-probe", str(root / "shared/repo-root.sh")],
    }
    return {name: subprocess.run(command, env=env, cwd=cwd, capture_output=True,
                                 text=True, timeout=10)
            for name, command in commands.items()}


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
        with tempfile.TemporaryDirectory() as tmp:
            results = probe_vault_root(ROOT, Path(tmp), {
                "SQUAD_CODE_ROOT": str(Path(tmp) / "hostile-code-root")})
        self.assertEqual(set(results), {"python", "shell"})
        for name, proc in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stderr, "")
                # Independent source: this test is scripts/python/tests/<file>.
                self.assertEqual(proc.stdout.strip(), str(ROOT))

    def test_current_api_probe_detects_hostile_noop_and_missing_resolvers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "shared").mkdir()
            (root / "scripts/python").mkdir(parents=True)
            hostile = str(root / "hostile-code-root")
            fixtures = {
                "hostile": ('VAULT_ROOT="$SQUAD_CODE_ROOT"\n',
                            'import os\ndef resolve_vault_root():\n    return os.environ["SQUAD_CODE_ROOT"]\n'),
                "noop": (':\n', 'def resolve_vault_root():\n    return ""\n'),
                "missing_api": ('return 9\n', '# no resolver API\n'),
            }
            for case, (shell, python) in fixtures.items():
                (root / "shared/repo-root.sh").write_text(shell)
                (root / "scripts/python/repo_root.py").write_text(python)
                results = probe_vault_root(root, root, {"SQUAD_CODE_ROOT": hostile})
                self.assertEqual(set(results), {"python", "shell"})
                for name, proc in results.items():
                    with self.subTest(case=case, resolver=name):
                        self.assertNotEqual(proc.stdout.strip(), str(root))
                        if case == "hostile":
                            self.assertEqual(proc.returncode, 0, proc.stderr)
                            self.assertEqual(proc.stdout.strip(), hostile)
                        elif case == "noop":
                            self.assertEqual(proc.returncode, 0, proc.stderr)
                            self.assertEqual(proc.stdout.strip(), "")
                        elif name == "python":
                            self.assertNotEqual(proc.returncode, 0)
                            self.assertIn("AttributeError", proc.stderr)
                        else:
                            self.assertEqual(proc.returncode, 9)
                            self.assertEqual(proc.stdout, "")

    def test_migration_reporter_requires_the_unimplemented_split_api(self):
        # The September split-API revert left this reporter intact. FAIL here
        # means the split API is absent, not that VAULT_ROOT was redirected.
        results = validator.probe_code_root(ROOT)
        self.assertEqual([r["resolver"] for r in results], ["python", "shell"])
        self.assertEqual([r["status"] for r in results], ["FAIL", "FAIL"])
        self.assertIn("AttributeError", results[0]["detail"])
        self.assertIn("hostile-code-root", results[1]["detail"])

    def test_positive_control_detects_resolvers_honoring_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "shared").mkdir()
            (root / "scripts/python").mkdir(parents=True)
            (root / "shared/repo-root.sh").write_text(': "${SQUAD_CODE_ROOT}"\n')
            (root / "scripts/python/repo_root.py").write_text(
                'import os\ndef resolve_code_root():\n    return os.environ["SQUAD_CODE_ROOT"]\n')
            results = validator.probe_code_root(root)
            self.assertEqual([r["resolver"] for r in results], ["python", "shell"])
            self.assertEqual([r["status"] for r in results], ["FAIL", "FAIL"])
            self.assertTrue(all("hostile-code-root" in r["detail"] for r in results))

    def test_missing_resolvers_are_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = validator.probe_code_root(Path(tmp).resolve())
            self.assertEqual([r["resolver"] for r in results], ["python", "shell"])
            self.assertTrue(all(r["status"] != "PASS" for r in results))


class RootFailureTests(unittest.TestCase):
    def test_interactive_sourcing_refuses_without_exiting_the_session(self):
        proc = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-ic",
             'source "$1"; rc=$?; printf "source_exit=%s root=%s\\n" "$rc" "${VAULT_ROOT-unset}"',
             "probe", str(ROOT / "shared/repo-root.sh")],
            env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "source_exit=1 root=unset\n")
        self.assertIn("interactive sourcing is unsupported", proc.stderr)

    def test_all_wrapper_preambles_abort_with_loud_invalid_root_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "shared").mkdir()
            (root / "bin").mkdir()
            shutil.copyfile(ROOT / "shared/repo-root.sh", root / "shared/repo-root.sh")
            checked = []
            for path in sorted((ROOT / "bin").iterdir()):
                if not path.is_file():
                    continue
                lines = path.read_text(errors="replace").splitlines()
                sources = [i for i, line in enumerate(lines)
                           if line.startswith("source ") and "/shared/repo-root.sh" in line]
                if not sources:
                    continue
                self.assertEqual(len(sources), 1, path.name)
                index = sources[0]
                # Only shell option settings and the actual source line run;
                # wrapper bodies may launch processes or mutate live state.
                options = [line for line in lines[:index]
                           if re.fullmatch(r"set -[A-Za-z]+(?: pipefail)?", line)]
                wrapper = root / "bin" / path.name
                wrapper.write_text("\n".join(options + [lines[index], 'echo CONTINUED']) + "\n")
                for valid in (False, True):
                    env = {"PATH": os.defpath, "VAULT_ROOT": str(root if valid else root / "missing")}
                    proc = subprocess.run(["/bin/bash", str(wrapper)], env=env,
                                          capture_output=True, text=True, timeout=10)
                    with self.subTest(wrapper=path.name, valid=valid):
                        self.assertEqual(proc.returncode, 0 if valid else 1, proc.stderr)
                        self.assertEqual(proc.stdout, "CONTINUED\n" if valid else "")
                        if valid:
                            self.assertEqual(proc.stderr, "")
                        else:
                            self.assertIn("VAULT_ROOT is not a directory:", proc.stderr)
                checked.append(path.name)
            self.assertIn("squad", checked)
            self.assertIn("send-task.sh", checked)
            self.assertGreater(len(checked), 30)

    def test_conditionals_cannot_swallow_initialization_failure(self):
        for source in ('source "$1"', 'if source "$1"; then :; fi', 'source "$1" || :'):
            with self.subTest(source=source):
                proc = subprocess.run(
                    ["/bin/bash", "-c", source + '; echo CONTINUED', "probe",
                     str(ROOT / "shared/repo-root.sh")],
                    env={"PATH": os.defpath, "VAULT_ROOT": str(ROOT / "missing-root-control")},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertIn("VAULT_ROOT is not a directory:", proc.stderr)

    def test_both_resolvers_diagnose_mismatch_and_preserve_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp).resolve()
            for name, proc in probe_vault_root(ROOT, other, {"VAULT_ROOT": str(other)}).items():
                with self.subTest(resolver=name):
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.strip(), str(other))
                    self.assertIn("overrides location-derived root " + str(ROOT), proc.stderr)
                    self.assertIn("unset VAULT_ROOT", proc.stderr)

    def test_equivalent_symlink_override_stays_verbatim_and_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "same checkout"
            link.symlink_to(ROOT, target_is_directory=True)
            for value in (str(ROOT), str(link)):
                for name, proc in probe_vault_root(ROOT, Path(tmp), {"VAULT_ROOT": value}).items():
                    with self.subTest(resolver=name, value=value):
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(proc.stdout.strip(), value)
                        self.assertEqual(proc.stderr, "")

    def test_both_resolvers_reject_relative_and_missing_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value, message in ((".", "must be an absolute path"),
                                   (str(Path(tmp) / "missing"), "is not a directory")):
                for name, proc in probe_vault_root(ROOT, Path(tmp), {"VAULT_ROOT": value}).items():
                    with self.subTest(resolver=name, value=value):
                        self.assertNotEqual(proc.returncode, 0)
                        self.assertEqual(proc.stdout, "")
                        self.assertIn(message, proc.stderr)

    def test_readlink_failure_is_loud_and_does_not_fabricate_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "root-link.sh"
            link.symlink_to(ROOT / "shared/repo-root.sh")
            for failing in (False, True):
                code = 'source "$1"; '
                if failing:
                    code += 'readlink() { return 9; }; '
                code += 'vs_resolve_symlink "$2"'
                proc = subprocess.run(["/bin/bash", "-c", code, "probe",
                                       str(ROOT / "shared/repo-root.sh"), str(link)],
                                      env={"PATH": os.defpath}, capture_output=True,
                                      text=True, timeout=10)
                with self.subTest(failing=failing):
                    self.assertEqual(proc.returncode, 1 if failing else 0)
                    if failing:
                        self.assertEqual(proc.stdout, "")
                        self.assertIn("readlink failed resolving", proc.stderr)
                    else:
                        self.assertEqual(proc.stdout.strip(), str(ROOT / "shared/repo-root.sh"))
                        self.assertEqual(proc.stderr, "")


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
