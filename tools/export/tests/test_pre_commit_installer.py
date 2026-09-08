"""Regression controls for the worktree-independent pre-commit installer."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "docs/install/install-pre-commit-hook.sh"
GUARD = REPO_ROOT / "scripts/hooks/pre-commit"
DOCTOR = REPO_ROOT / "bin/doctor.sh"
EXPORT_DIR = REPO_ROOT / "tools/export"
sys.path.insert(0, str(EXPORT_DIR))

from path_policy import load_policy  # noqa: E402


POLICY_PATH = EXPORT_DIR / "policy/path-policy.json"
CONFIG_COMMAND = re.compile(
    r"git\s+config(?P<options>[^\n`]*?)\bcore\.hooksPath\b(?P<value>[^\n`]*)",
    re.IGNORECASE,
)
SYMLINK_COMMAND = re.compile(
    r"\bln\s+-(?P<flags>[A-Za-z]*s[A-Za-z]*)\s+"
    r"(?P<source>[^\s`]+)\s+(?P<destination>[^\s`]+)",
)
QUERY_OR_REMOVAL_OPTIONS = (
    "--get",
    "--get-all",
    "--get-regexp",
    "--list",
    "--show-origin",
    "--unset",
    "--unset-all",
)


class PreCommitInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "clone"
        self.root.mkdir()
        self.global_config = Path(self.temporary.name) / "global.gitconfig"
        self.global_config.write_text("", encoding="utf-8")
        self.env = os.environ.copy()
        self.env["GIT_CONFIG_GLOBAL"] = str(self.global_config)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self._git("init", "-q")
        self._git("config", "user.name", "Hook Test")
        self._git("config", "user.email", "hook-test@example.invalid")

        installer = self.root / "docs/install/install-pre-commit-hook.sh"
        guard = self.root / "scripts/hooks/pre-commit"
        installer.parent.mkdir(parents=True)
        guard.parent.mkdir(parents=True)
        shutil.copy2(INSTALLER, installer)
        shutil.copy2(GUARD, guard)
        (self.root / "README.md").write_text("# fixture\n", encoding="utf-8")
        self._git("add", ".")
        self._git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            env=self.env,
        )

    def _install(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "docs/install/install-pre-commit-hook.sh")],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def _doctor_hook_check(self) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env["VAULT_ROOT"] = str(self.root)
        return subprocess.run(
            ["bash", str(DOCTOR), "--check-pre-commit-hook"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_hostile_tracked_hook_cannot_replace_installed_guard(self) -> None:
        self._git("config", "core.hooksPath", ".githooks")
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        hooks_path = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "config",
                "--local",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(hooks_path.returncode, 1)
        self.assertEqual(hooks_path.stdout, "")

        marker = Path(self.temporary.name) / "hostile-hook-executed"
        hostile_hook = self.root / ".githooks/pre-commit"
        hostile_hook.parent.mkdir()
        hostile_hook.write_text(
            f"#!/bin/sh\nprintf executed > {marker!s}\n", encoding="utf-8"
        )
        hostile_hook.chmod(0o755)
        self._git("add", ".githooks/pre-commit")
        committed = self._git("commit", "-qm", "hostile branch hook")
        self.assertEqual(committed.returncode, 0)
        self.assertFalse(marker.exists())

        private_path = self.root / "_state/bounty/secret.md"
        private_path.parent.mkdir(parents=True)
        private_path.write_text("private fixture\n", encoding="utf-8")
        self._git("add", "-f", "_state/bounty/secret.md")
        blocked = subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "must block"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("COMMIT BLOCKED", blocked.stderr)
        self.assertFalse(marker.exists())

    def test_unrelated_existing_hook_is_not_overwritten(self) -> None:
        hooks_dir = Path(
            self._git(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ) / "hooks"
        existing = hooks_dir / "pre-commit"
        existing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        before = existing.read_bytes()

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to replace existing hook", result.stderr)
        self.assertEqual(existing.read_bytes(), before)

    def test_effective_global_hooks_path_is_rejected(self) -> None:
        self._git("config", "--global", "core.hooksPath", ".githooks")

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertIn("installed guard is inactive", result.stderr)
        self.assertIn("global.gitconfig", result.stderr)
        self.assertIn(".githooks", result.stderr)
        self.assertEqual(self._git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")

    def test_doctor_objects_to_broken_hook_then_accepts_installer(self) -> None:
        hooks_dir = Path(
            self._git(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ) / "hooks"
        existing = hooks_dir / "pre-commit"
        existing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        existing.chmod(0o755)

        broken = self._doctor_hook_check()
        self.assertEqual(broken.returncode, 1)
        self.assertIn("BROKEN", broken.stderr)
        self.assertIn("not the managed Vibe Squad entrypoint", broken.stderr)

        existing.unlink()
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        healthy = self._doctor_hook_check()
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertIn("OK: managed guard is active outside the worktree", healthy.stdout)


class PublishedHookInstructionTests(unittest.TestCase):
    @staticmethod
    def _is_instruction_surface(path: str) -> bool:
        suffix = Path(path).suffix.casefold()
        return suffix in {".md", ".rst", ".txt"} or path.startswith(
            (".githooks/", "scripts/hooks/")
        )

    @staticmethod
    def _violations(path: str, text: str) -> list[str]:
        violations: list[str] = []
        for match in CONFIG_COMMAND.finditer(text):
            options = match.group("options")
            if any(option in options for option in QUERY_OR_REMOVAL_OPTIONS):
                continue
            value_tail = match.group("value").lstrip(" =\t")
            if not value_tail:
                continue
            value = value_tail.split()[0].strip("'\"),;:")
            outside_worktree = (
                value.startswith(("/", "~/", "$HOME/", "${HOME}/"))
                and value != "/dev/null"
            )
            if not outside_worktree:
                line = text.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{path}:{line}: configures core.hooksPath as {value!r}"
                )

        for match in SYMLINK_COMMAND.finditer(text):
            source = match.group("source").strip("'\"),;:")
            destination = match.group("destination").strip("'\"),;:")
            if ".git/hooks/" not in destination:
                continue
            line = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"{path}:{line}: symlinks Git hook {destination!r} to worktree path {source!r}"
            )
        return violations

    def test_detector_has_positive_and_negative_controls(self) -> None:
        vulnerable = """\
git config core.hooksPath .githooks
ln -sf ../../scripts/hooks/pre-commit .git/hooks/pre-commit
"""
        safe = """\
git config --show-origin --get-all core.hooksPath
git config --unset-all core.hooksPath
bash docs/install/install-pre-commit-hook.sh
"""
        self.assertEqual(len(self._violations("fixture.md", vulnerable)), 2)
        self.assertEqual(self._violations("fixture.md", safe), [])

    def test_no_published_instruction_loads_hook_code_from_worktree(self) -> None:
        policy = load_policy(POLICY_PATH)
        tracked = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        violations: list[str] = []
        for raw_path in tracked:
            if not raw_path:
                continue
            path = os.fsdecode(raw_path)
            if policy.classify(path) != "public" or not self._is_instruction_surface(path):
                continue
            candidate = REPO_ROOT / path
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            violations.extend(self._violations(path, text))

        self.assertEqual(
            violations,
            [],
            "published instructions must install a copied hook outside the worktree:\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
