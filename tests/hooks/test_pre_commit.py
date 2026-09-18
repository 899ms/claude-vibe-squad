from __future__ import annotations

import os
import hashlib
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "hooks" / "pre-commit"
INSTALLER = "docs/install/install-pre-commit-hook.sh"
HOOK_DEFINITIONS = runpy.run_path(str(HOOK))


def fixture_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in ("BASH_ENV", "ENV", "PYTHONPATH", "PYTHONHOME")
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0", PYTHONDONTWRITEBYTECODE="1",
        PRE_COMMIT_PYTHON=sys.executable,
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    return environment


class PreCommitLeakGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="chrono-hook-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.environment = fixture_environment()
        self._git("-c", "init.templateDir=", "init", "-q")
        self._git("config", "user.name", "Hook Test")
        self._git("config", "user.email", "hook@example.invalid")
        # The release gate reads the index; commit its inputs so each test's
        # reset retains a consistent baseline while clearing staged test files.
        for name in ("CHANGELOG.md", "CLAUDE.md", "README.md", "SECURITY.md"):
            self._stage(name, (REPO_ROOT / name).read_bytes())
        self._git("commit", "-qm", "consistent release baseline")

    def _git(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
            env=self.environment,
            check=check,
            capture_output=True,
            text=True,
        )

    def _stage(self, relative: str, content: bytes, *, force: bool = False) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        arguments = ["add"]
        if force:
            arguments.append("-f")
        arguments.extend(["--", relative])
        self._git(*arguments)

    def _run_hook(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-B", str(HOOK)],
            cwd=self.repo,
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_restricted_frontmatter_is_refused_but_internal_is_allowed(self) -> None:
        self._stage(
            "restricted.md",
            b'---\nsensitivity: "restricted"\n---\nprivate\n',
        )
        blocked = self._run_hook()
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("restricted sensitivity", blocked.stderr)

        self._git("reset", "-q")
        self._stage("internal.md", b"---\nsensitivity: internal\n---\nsafe\n")
        allowed = self._run_hook()
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_database_bounty_and_phantom_paths_are_refused(self) -> None:
        cases = (
            ("nested/kg.db", b"SQLite format 3\x00"),
            ("nested/state.db-wal", b"binary\x00data"),
            ("_state/bounty/report.md", b"private bounty"),
            ("x/${CHRONO_VAULT_ROOT}/note.md", b"phantom vault"),
        )
        for relative, content in cases:
            with self.subTest(path=relative):
                self._git("reset", "-q")
                self._stage(relative, content, force=True)
                blocked = self._run_hook()
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("BLOCKED", blocked.stderr)

    def test_normal_file_and_send_task_without_auto_snapshot_are_allowed(self) -> None:
        self._stage("normal.txt", b"normal public content\n")
        normal = self._run_hook()
        self.assertEqual(normal.returncode, 0, normal.stderr)

        self._git("reset", "-q")
        self._stage(
            "bin/send-task.sh",
            b"#!/bin/bash\nset -uo pipefail\necho dispatch\n",
        )
        send_task = self._run_hook()
        self.assertEqual(send_task.returncode, 0, send_task.stderr)

    def test_hook_reads_staged_blob_not_unstaged_worktree(self) -> None:
        self._stage("partial.md", b"---\nsensitivity: internal\n---\nsafe\n")
        (self.repo / "partial.md").write_text(
            "---\nsensitivity: restricted\n---\nworking tree only\n",
            encoding="utf-8",
        )

        result = self._run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)

        self._stage("partial.md", b"---\nsensitivity: restricted\n---\nprivate\n")
        (self.repo / "partial.md").write_text("---\nsensitivity: internal\n---\nrepair\n")
        blocked = self._run_hook()
        self.assertEqual(blocked.returncode, 1, blocked.stderr)
        self.assertIn("restricted sensitivity", blocked.stderr)

    def test_gitignore_contains_defense_in_depth_patterns(self) -> None:
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("_state/bounty/**", lines)
        self.assertIn("**/kg.db*", lines)
        self.assertIn("**/*.db-wal", lines)
        self.assertIn("**/*.db-shm", lines)
        self.assertIn("**/${CHRONO_VAULT_ROOT}/", lines)


# These spies test orchestration, argv, trigger selection, short-circuiting and
# literal exit propagation through the REAL copied shell wrappers. Real-engine
# controls below separately establish that their payloads detect violations.
VALIDATOR_SPY = '''import json, os, pathlib, sys
name = pathlib.Path(__file__).stem
arguments = sys.argv[1:]
phase = "self-test" if "--self-test" in arguments else "validate"
with open(os.environ["HOOK_TEST_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "phase": phase, "argv": arguments,
        "ci": os.environ.get("SQUAD_CI_HOST_INDEPENDENT"),
        "root": os.environ.get("VAULT_ROOT")}) + "\\n")
failure = os.environ.get("HOOK_TEST_FAILURE")
if failure == name + ":" + phase:
    print("fixture violation: " + failure)
    raise SystemExit(7)
if phase == "self-test" and failure != "silent:" + name:
    if name == "validate_capabilities":
        print(json.dumps({"type": "self-test", "status": "pass"}))
    else:
        print("self-test PASSED (fixture attestation)")
'''


class InstalledSnapshotTests(PreCommitLeakGuardTests):
    def setUp(self) -> None:
        super().setUp()
        self.log = self.repo / "hook-test-calls.jsonl"
        self.environment["HOOK_TEST_LOG"] = str(self.log)
        self.hooks = self.repo / ".git/hooks"
        self._seed_sources()
        self._install_ok()

    def _seed_sources(self) -> None:
        paths = [INSTALLER, "scripts/hooks/pre-commit", "scripts/python/validate_release_version.py",
                 *HOOK_DEFINITIONS["SNAPSHOT_INPUTS"], *HOOK_DEFINITIONS["MOAT_INPUTS"]]
        for relative in paths:
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / relative, target)
        for name in ("validate_capabilities", "validate_specialists", "validate_capability_homes", "validate_skill_wiring"):
            (self.repo / f"scripts/python/{name}.py").write_text(VALIDATOR_SPY)
        typescript = Path(os.environ.get("PRE_COMMIT_TEST_TYPESCRIPT_ROOT", str(REPO_ROOT / "moat/node_modules/typescript")))
        for name in ("package.json", "lib/typescript.js"):
            if (typescript / name).is_file():
                destination = self.repo / "moat/node_modules/typescript" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(typescript / name, destination)

    def _install(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(self.repo / INSTALLER), *arguments],
                              cwd=self.repo, env=self.environment, text=True, capture_output=True)

    def _install_ok(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self._install(*arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _run_hook(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.hooks / "pre-commit")], cwd=self.repo,
                              env=self.environment, text=True, capture_output=True)

    def _assert_exit(self, expected: int, text: str = "") -> subprocess.CompletedProcess[str]:
        result = self._run_hook()
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected, output)
        self.assertIn(text, output)
        if os.environ.get("PRE_COMMIT_TEST_EVIDENCE") == "1":
            print(f"CONTROL {self.id().rsplit('.', 1)[-1]}: exit {result.returncode}; {text or 'allowed'}")
        return result

    def _calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def _clear_calls(self) -> None:
        self.log.write_text("")

    def _real_engine(self, name: str) -> None:
        relative = f"scripts/python/{name}.py"
        shutil.copyfile(REPO_ROOT / relative, self.repo / relative)
        self._install_ok()

    def test_real_capability_engine_blocks_staged_invalid_registry(self) -> None:
        self._real_engine("validate_capabilities")
        registry = self.repo / "shared/registries/invalid.tsv"
        registry.parent.mkdir(parents=True, exist_ok=True)
        # The shared registry is genuinely unpublished in this fixture: this
        # is the maintained, attested not-applicable path, not a silent zero.
        self._stage("shared/capabilities/test.md", b"fixture\n")
        self._assert_exit(0, '"code": "registry-not-published"')
        self._git("reset", "-q")
        self._stage("shared/registries/skill-tool-registry.tsv", b"invalid registry header\ninvalid row\n")
        self._assert_exit(1, "BLOCKED: capability validation failed")

    def test_real_skill_engine_rejects_bad_description_and_accepts_repair(self) -> None:
        # Skill wiring imports the capability validator's retirement helpers.
        # Restore that dependency too: the orchestration spy has no such API.
        self._real_engine("validate_capabilities")
        self._real_engine("validate_skill_wiring")
        # No staged skill means the conditional guard remains dormant, even
        # though this deliberately minimal fixture is not fully wired yet.
        self._assert_exit(0)
        valid = ("---\nname: fixture\naudience: specialist\n"
                 "description: Use when validating an isolated pre-commit fixture with deterministic inputs.\n"
                 "---\nFixture body.\n")
        for relative in (".claude/skills/fixture/SKILL.md", ".agents/skills/fixture/SKILL.md",
                         "model-lanes/gemini/.agents/skills/fixture/SKILL.md"):
            self._stage(relative, valid.encode())
        supervisor = self.repo / "bin/board-supervisor.sh"
        supervisor.write_text('#!/bin/sh\nset -e\n# --skills-dir\n')
        self._assert_exit(0)
        self._stage(".claude/skills/fixture/SKILL.md", valid.replace("Use when validating an isolated pre-commit fixture with deterministic inputs.", "short").encode())
        self._assert_exit(1, "BLOCKED: skill wiring validation failed")
        self._stage(".claude/skills/fixture/SKILL.md", valid.encode())
        self._assert_exit(0)
        self._stage("shared/capabilities/test.md", b"fixture\n")
        self._assert_exit(0, "self-test PASSED (")

    def test_real_specialist_engine_rejects_a_staged_unresolved_tool(self) -> None:
        # Reuse the maintained validator's own small data fixture; its test
        # module is read only, and all writes remain in this disposable repo.
        original_path = list(sys.path)
        try:
            sys.path[:0] = [str(REPO_ROOT), str(REPO_ROOT / "scripts/python")]
            definitions = runpy.run_path(str(REPO_ROOT / "scripts/python/tests/test_validate_specialists.py"))
        finally:
            sys.path[:] = original_path
        fixture = definitions["Fixture"](self.repo)
        self._real_engine("validate_specialists")
        self._assert_exit(0, "Total: 1  Passed: 1  Failed: 0")
        fixture.row[23] = "[missing-tool]"
        fixture.flush()
        self._git("add", "shared/specialist-runtime-map.tsv")
        self._assert_exit(1, "unresolved-tool-reference:required:missing-tool")
        fixture.row[23] = "[]"
        fixture.flush()
        self._git("add", "shared/specialist-runtime-map.tsv")
        self._assert_exit(0, "Total: 1  Passed: 1  Failed: 0")

    def test_real_live_existence_payload_detects_absent_present_and_mislabelled_tools(self) -> None:
        self._real_engine("validate_capability_homes")
        runtime = self.repo / "shared/specialist-runtime-map.tsv"
        runtime.write_text("specialist\trequires_approval\nfixture\t[]\n")
        registry = self.repo / "shared/registries/skill-tool-registry.tsv"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("name\trecord_kind\ttype\tlanes\tverified_state\n")
        # Exercise the real host-PATH probe inside the installed module. Full
        # wrapper selection/exit propagation is independently covered above;
        # this payload control does not claim a whole-host capability audit.
        code = '''import json, os
from pathlib import Path
from specialist_capability_source import CapabilityRef
import validate_capability_homes as homes
inventory = {lane: {kind: set() for kind in ("skills", "tools", "mcps", "staged_mcps")} for lane in homes.LANES}
inventory["gpt-codex"]["skills"].add("repo-shell")
ref = CapabilityRef("vibe-hook-existence-fixture", "preferred", os.environ["HOOK_TEST_AVAILABILITY"], "host-PATH", "")
source = {("fixture", "gpt-codex"): {"specialist": "fixture", "lane": "gpt-codex", "skills": (), "tools": (ref,), "mcps": ()}}
issues = homes.source_existence_diagnostics(Path.cwd(), source, lane_inventory=inventory,
    catalog_tools={lane: set() for lane in homes.LANES}, skill_names={lane: set() for lane in homes.LANES})
print(json.dumps(issues))
raise SystemExit(1 if issues else 0)
'''
        tool_dir = self.repo / "fixture-tools"
        tool_dir.mkdir()
        self.environment["PATH"] = str(tool_dir) + os.pathsep + self.environment["PATH"]
        self.environment["HOOK_TEST_AVAILABILITY"] = "available"

        def probe(expected: int, message: str) -> None:
            result = subprocess.run([sys.executable, "-I", "-B", str(self.hooks / "vibe-squad-pre-commit"),
                                     "--snapshot-python", "-c", code], cwd=self.repo,
                                    env=self.environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertIn(message, result.stdout)
            if os.environ.get("PRE_COMMIT_TEST_EVIDENCE") == "1":
                print(f"CONTROL real snapshot host-existence: exit {result.returncode}; {message}")

        probe(1, "capability claims host-PATH evidence but is absent from PATH")
        executable = tool_dir / "vibe-hook-existence-fixture"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        probe(0, "[]")
        self.environment["HOOK_TEST_AVAILABILITY"] = "uninstalled"
        probe(1, "capability is marked uninstalled but is now present on PATH")

    def test_unconditional_specialist_and_home_guards_strip_ci_even_without_changes(self) -> None:
        self.environment["SQUAD_CI_HOST_INDEPENDENT"] = "1"
        self.environment["VAULT_ROOT"] = "/does-not-exist"
        self._assert_exit(0)
        calls = self._calls()
        self.assertEqual([call["name"] for call in calls], ["validate_specialists", "validate_capability_homes"])
        self.assertTrue(all(call["ci"] is None and Path(call["root"]).resolve() == self.repo.resolve() for call in calls))
        self.assertIn("existence", calls[1]["argv"][-1])
        for name in ("validate_specialists", "validate_capability_homes"):
            self.environment["HOOK_TEST_FAILURE"] = name + ":validate"
            self._assert_exit(1, "BLOCKED: specialist or live capability validation failed")

    def test_maintainer_home_gate_keeps_all_checks(self) -> None:
        registry = self.repo / "shared/registries/skill-tool-registry.tsv"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("fixture registry exists\n")
        self._assert_exit(0)
        self.assertNotIn("--only", self._calls()[-1]["argv"])

    def test_capability_pair_triggers_for_both_prefixes_and_blocks_each_failure(self) -> None:
        for relative in ("shared/capabilities/project/test.md", "shared/registries/test.tsv"):
            with self.subTest(path=relative):
                self._git("reset", "-q")
                self._clear_calls()
                self.environment["HOOK_TEST_FAILURE"] = "validate_capabilities:validate"
                self._assert_exit(0)
                self.assertFalse(any(call["name"] == "validate_capabilities" for call in self._calls()))
                self._stage(relative, b"staged fixture\n")
                self._assert_exit(1, "BLOCKED: capability validation failed")
                self.environment["HOOK_TEST_FAILURE"] = "validate_capabilities:self-test"
                self._assert_exit(1, "BLOCKED: capability validator self-test failed")
                self.environment.pop("HOOK_TEST_FAILURE")
                self._clear_calls()
                self._assert_exit(0)
                self.assertEqual([(c["name"], c["phase"]) for c in self._calls()[:4]], [
                    ("validate_capabilities", "validate"), ("validate_skill_wiring", "validate"),
                    ("validate_capabilities", "self-test"), ("validate_skill_wiring", "self-test")])
                self.assertTrue(all(str(self.repo.resolve()) in c["argv"] for c in self._calls()[:4]))

    def test_capability_wrapper_keeps_both_selftest_attestations(self) -> None:
        self._stage("shared/registries/test.tsv", b"fixture\n")
        for failure in ("silent:validate_capabilities", "silent:validate_skill_wiring", "validate_skill_wiring:self-test"):
            self.environment["HOOK_TEST_FAILURE"] = failure
            self._assert_exit(1, "BLOCKED: capability validator self-test failed")
        self.environment["HOOK_TEST_FAILURE"] = "validate_skill_wiring:validate"
        self._assert_exit(1, "BLOCKED: capability validation failed")

    def test_deleted_paths_keep_capability_and_skill_triggers_but_skip_moat(self) -> None:
        paths = ("shared/capabilities/test.md", ".claude/skills/test/SKILL.md", "moat/deleted.json")
        for relative in paths:
            self._stage(relative, b"fixture\n")
        self._git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture deletion baseline")
        for relative, failure, expected in (
            (paths[0], "validate_capabilities:validate", "BLOCKED: capability validation failed"),
            (paths[1], "validate_skill_wiring:validate", "BLOCKED: skill wiring validation failed"),
        ):
            self._git("reset", "-q")
            self.environment["HOOK_TEST_FAILURE"] = failure
            self._git("update-index", "--force-remove", "--", relative)
            self._assert_exit(1, expected)
        self._git("reset", "-q")
        self.environment.pop("HOOK_TEST_FAILURE")
        self._git("update-index", "--force-remove", "--", paths[2])
        self.assertNotIn("Tier-A", self._assert_exit(0).stdout)

    def test_skill_wiring_has_all_three_triggers_and_no_unstaged_trigger(self) -> None:
        for relative in ("shared/skills/test.md", ".claude/skills/test/SKILL.md", ".agents/skills/test/SKILL.md"):
            with self.subTest(path=relative):
                self._git("reset", "-q")
                self.environment["HOOK_TEST_FAILURE"] = "validate_skill_wiring:validate"
                self._assert_exit(0)
                self._stage(relative, b"staged skill fixture\n")
                self._assert_exit(1, "BLOCKED: skill wiring validation failed")

    def test_format_warnings_keep_exact_triggers_and_never_weaken_blockers(self) -> None:
        self._stage("notes.txt", b"no safety flags\n")
        self.assertNotIn("WARN:", self._assert_exit(0).stdout)
        self._stage("script.sh", b"#!/bin/sh\necho fixture\n")
        self._assert_exit(0, "WARN: script.sh has no 'set -' safety flags")
        self._stage("script.sh", b"#!/bin/sh\nset -e\n")
        self.assertNotIn("WARN:", self._assert_exit(0).stdout)
        self._stage("shared/dispatch-toolkit.sh", b"#!/bin/sh\nset -e\n")
        self._assert_exit(0, "WARN: dispatch-toolkit.sh missing no-delete rule block")
        self._stage("private.md", b"---\nsensitivity: restricted\n---\n")
        self._assert_exit(1, "restricted sensitivity frontmatter")
        self._git("reset", "-q")
        self.assertNotIn("WARN:", self._assert_exit(0).stdout)
        self._stage("shared/dispatch-toolkit.sh", b"#!/bin/sh\nset -e\n# spec-1.5-no-delete-rule\n")
        self.assertNotIn("WARN:", self._assert_exit(0).stdout)

    def test_release_checks_staged_claims_unconditionally(self) -> None:
        original = (self.repo / "README.md").read_bytes()
        import re
        drifted = re.sub(rb"version-v[0-9]+\.[0-9]+\.[0-9]+-", b"version-v999.999.999-", original)
        self.assertNotEqual(original, drifted)
        self._stage("README.md", drifted)
        (self.repo / "README.md").write_bytes(original)
        self._assert_exit(1, "release-version guard: COMMIT BLOCKED")
        self._stage("README.md", original)
        (self.repo / "README.md").write_text("unstaged invalid claims\n")
        self._assert_exit(0)

    def test_installed_snapshot_does_not_execute_changed_worktree_code_or_imports(self) -> None:
        for relative in ("scripts/hooks/pre-commit", "scripts/python/validate_release_version.py", *HOOK_DEFINITIONS["SNAPSHOT_INPUTS"]):
            if relative.endswith((".py", ".sh")) or relative == "scripts/hooks/pre-commit":
                (self.repo / relative).write_text("raise SystemExit(91)\n")
        (self.repo / "json.py").write_text("raise SystemExit(92)\n")
        self.environment["PYTHONPATH"] = str(self.repo)
        # Use the interpreter's isolated mode for the entrypoint as well in this
        # hostile inherited-environment control; the public shell shim is unchanged.
        result = subprocess.run([sys.executable, "-I", "-B", str(self.hooks / "vibe-squad-pre-commit")],
                                cwd=self.repo, env=self.environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.environment.pop("PYTHONPATH")
        self._stage("shared/capabilities/test.md", b"fixture\n")
        self._assert_exit(0)
        self._stage("private.md", b"---\nsensitivity: restricted\n---\n")
        self._assert_exit(1, "restricted sensitivity frontmatter")

    def test_missing_or_modified_companions_fail_closed(self) -> None:
        pointer = json.loads((self.hooks / HOOK_DEFINITIONS["SNAPSHOT_POINTER"]).read_text())
        helper = self.hooks / pointer["directory"] / "scripts/python/repo_root.py"
        helper.write_text("raise SystemExit(0)\n")
        self._assert_exit(1, "reviewed snapshot changed")

    def test_unmanaged_hook_refuses_by_default_and_by_wrong_identity_without_configuration_change(self) -> None:
        entrypoint = self.hooks / "pre-commit"
        original = b"#!/bin/sh\nexit 23\n"
        entrypoint.write_bytes(original)
        self._git("config", "core.hooksPath", ".githooks")
        for arguments in ((), ("--replace-unmanaged-sha256", "0" * 64)):
            result = self._install(*arguments)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("refusing to replace existing hook", result.stderr)
            self.assertEqual(entrypoint.read_bytes(), original)
            self.assertEqual(self._git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")
            self.assertEqual(list(self.hooks.glob("pre-commit.unmanaged.*")), [])
        self._install_ok("--replace-unmanaged-sha256", hashlib.sha256(original).hexdigest())
        backups = list(self.hooks.glob("pre-commit.unmanaged.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertFalse(backups[0].is_symlink())
        self.assertEqual(self._git("config", "--get", "core.hooksPath", check=False).returncode, 1)
        self._stage("private.md", b"---\nsensitivity: restricted\n---\n")
        self._assert_exit(1, "restricted sensitivity frontmatter")

    def test_symlink_and_spoofed_managed_marker_are_not_replacement_authority(self) -> None:
        entrypoint = self.hooks / "pre-commit"
        original = entrypoint.read_text()
        entrypoint.write_text(original + "\nexit 0\n")
        self.assertEqual(self._install().returncode, 1)
        entrypoint.rename(self.hooks / "saved-test-entrypoint")
        entrypoint.symlink_to(self.hooks / "saved-test-entrypoint")
        result = self._install("--replace-unmanaged-sha256", hashlib.sha256(entrypoint.read_bytes()).hexdigest())
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing symlink", result.stderr)

    def test_incomplete_sources_cannot_replace_an_existing_hook(self) -> None:
        old = (self.hooks / "pre-commit").read_bytes()
        source = self.repo / "scripts/python/validate_skill_wiring.py"
        source.rename(source.with_suffix(".fixture-missing"))
        result = self._install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing reviewed snapshot input", result.stderr)
        self.assertEqual((self.hooks / "pre-commit").read_bytes(), old)

    def test_global_override_still_prevents_installer_success(self) -> None:
        global_config = self.repo / "fixture-global-config"
        self._git("config", "--file", str(global_config), "core.hooksPath", ".githooks")
        self.environment["GIT_CONFIG_GLOBAL"] = str(global_config)
        result = self._install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("installed guard is inactive", result.stderr)
        self.assertIn(str(global_config), result.stderr)
        self.environment["GIT_CONFIG_GLOBAL"] = os.devnull
        self._install_ok()
        self._assert_exit(0)

    def test_real_moat_scanner_blocks_staged_data_and_ignores_worktree_repairs(self) -> None:
        if not shutil.which("node") or not shutil.which("gitleaks") or not (self.repo / HOOK_DEFINITIONS["TYPESCRIPT_INPUTS"][1]).is_file():
            self.skipTest("real moat controls require node, gitleaks and PRE_COMMIT_TEST_TYPESCRIPT_ROOT or local TypeScript")
        self.assertNotIn("Tier-A", self._assert_exit(0).stdout)
        self._stage("moat/fixture.json", b'{"content_class":"private"}\n')
        (self.repo / "moat/fixture.json").write_text('{"content_class":"public-safe"}\n')
        self._assert_exit(1, "MOAT_BOUNDARY_CONTENT_CLASS")
        self._stage("moat/fixture.json", b'{"content_class":"public-safe"}\n')
        (self.repo / "moat/fixture.json").write_text('{"content_class":"private"}\n')
        self._assert_exit(0, "running Tier-A boundary check")
        # Neither the staged nor checked-out scanner/dependency code can become
        # executable authority for the installed snapshot.
        (self.repo / "moat/boundary/tier-a.mjs").write_text("process.exit(91);\n")
        (self.repo / HOOK_DEFINITIONS["TYPESCRIPT_INPUTS"][1]).write_text("throw new Error('unreviewed');\n")
        self._stage("moat/spaced name.json", b'{"content_class":"private"}\n')
        self._assert_exit(1, "MOAT_BOUNDARY_CONTENT_CLASS")

    def test_moat_missing_scanner_and_node_keep_explicit_nonblocking_skip(self) -> None:
        scanner = self.repo / "moat/boundary/tier-a.mjs"
        scanner.rename(scanner.with_suffix(".fixture-missing"))
        self._stage("moat/fixture.json", b'{"content_class":"private"}\n')
        self._assert_exit(0, "unavailable; skipping Tier-A")
        scanner.with_suffix(".fixture-missing").rename(scanner)
        # The hook deliberately prepends host tool directories. Model a host
        # without node at the discovery boundary, without changing host state.
        result = subprocess.run([sys.executable, "-I", "-B", "-c",
            'import runpy,sys; from unittest.mock import patch; '
            'p=patch("shutil.which",return_value=None); p.start(); '
            'sys.argv=[sys.argv[1]]; runpy.run_path(sys.argv[0],run_name="__main__")',
            str(self.hooks / "vibe-squad-pre-commit")], cwd=self.repo,
            env=self.environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unavailable; skipping Tier-A", result.stdout)

    def test_linked_worktree_uses_common_snapshot_and_its_own_index(self) -> None:
        linked = self.repo / "linked-fixture"
        self._git("worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
        result = subprocess.run([str(self.hooks / "pre-commit")], cwd=linked,
                                env=self.environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all(Path(call["root"]).resolve() == linked.resolve() for call in self._calls()))
        (linked / "private.md").write_text("---\nsensitivity: restricted\n---\n")
        self._git("-C", str(linked), "add", "private.md")
        result = subprocess.run([str(self.hooks / "pre-commit")], cwd=linked,
                                env=self.environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("restricted sensitivity frontmatter", result.stderr)


if __name__ == "__main__":
    unittest.main()
