from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

EXPORT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPORT_DIR))

import remote_ref_audit  # noqa: E402
from remote_ref_audit import RemoteRefAuditError, audit_refs  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class PreviousPublicTipTests(unittest.TestCase):
    def test_tipless_ledger_refuses(self) -> None:
        ledger = Path("/fixture/export-ledger.jsonl")
        tip = "a" * 40
        with (
            mock.patch.object(Path, "is_symlink", return_value=False),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch.object(
                Path, "read_text", return_value=f'{{"public_tip":"{tip}"}}\n'
            ) as read,
        ):
            self.assertEqual(
                remote_ref_audit._previous_public_tip(ledger.parent, ledger), tip
            )
            read.return_value = '{}\n{"public_tip":null}\n[]\n'
            with self.assertRaisesRegex(
                RemoteRefAuditError, "has no recorded public tip"
            ):
                remote_ref_audit._previous_public_tip(ledger.parent, ledger)

    def test_unreadable_ledger_refuses(self) -> None:
        ledger = Path("/fixture/export-ledger.jsonl")
        tip = "a" * 40
        for error in (
            PermissionError("fixture ledger read denied"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(Path, "is_symlink", return_value=False),
                mock.patch.object(Path, "exists", return_value=True),
                mock.patch.object(
                    Path, "read_text", return_value=f'{{"public_tip":"{tip}"}}\n'
                ) as read,
            ):
                self.assertEqual(
                    remote_ref_audit._previous_public_tip(ledger.parent, ledger), tip
                )
                read.side_effect = error
                with self.assertRaisesRegex(
                    RemoteRefAuditError, "cannot read export ledger"
                ) as caught:
                    remote_ref_audit._previous_public_tip(ledger.parent, ledger)
                self.assertIs(caught.exception.__cause__, error)


class RemoteRefAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.origin = self.base / "origin"
        self.origin.mkdir()
        _git(self.origin, "init", "-q", "--bare")
        self.work = self.base / "work"
        self.work.mkdir()
        _git(self.work, "init", "-q")
        _git(self.work, "checkout", "-q", "-b", "main")
        _git(self.work, "config", "user.email", "t@t")
        _git(self.work, "config", "user.name", "t")
        _git(self.work, "remote", "add", "origin", str(self.origin))
        (self.work / "README.md").write_text("clean\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "clean")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.clean_sha = _git(self.work, "rev-parse", "HEAD")
        self.ledger = self.base / "export-ledger.jsonl"
        self._record_public_tip(self.clean_sha)

    def _record_public_tip(self, tip: str, field: str = "public_tip") -> None:
        self.ledger.write_text(f'{{"{field}":"{tip}"}}\n', encoding="utf-8")

    def _run_production_caller(
        self, public_tip: str
    ) -> tuple[subprocess.CompletedProcess[str], str, Path]:
        tool_root = self.base / "trusted-private-checkout"
        export_root = tool_root / "tools/export"
        export_root.mkdir(parents=True)
        for name in (
            "content_scan.py",
            "gitleaks_filter.py",
            "path_policy.py",
            "remote_ref_audit.py",
        ):
            shutil.copy2(EXPORT_DIR / name, export_root / name)
        shutil.copytree(EXPORT_DIR / "policy", export_root / "policy")
        (export_root / "identifier-denylist.txt").write_text(
            "never-present-private-identifier\n", encoding="utf-8"
        )
        private_ledger = tool_root / remote_ref_audit.DEFAULT_LEDGER_PATH
        private_ledger.parent.mkdir(parents=True)
        private_ledger.write_text(
            f'{{"public_tip":"{public_tip}"}}\n', encoding="utf-8"
        )
        fake_gitleaks = self.base / "gitleaks-fixture"
        fake_gitleaks.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "path = pathlib.Path(sys.argv[sys.argv.index('--report-path') + 1])\n"
            "path.write_text('[]', encoding='utf-8')\n",
            encoding="utf-8",
        )
        fake_gitleaks.chmod(0o755)
        if "public" not in _git(self.work, "remote").splitlines():
            _git(self.work, "remote", "add", "public", str(self.origin))
        report = self.base / "product-hygiene-report.md"
        environment = os.environ.copy()
        environment.update(
            {
                "GITLEAKS_BIN": str(fake_gitleaks),
                "GITLEAKS_CONFIG": str(export_root / "policy/gitleaks.toml"),
                "SQUAD_EXPORT_TOOL_ROOT": str(tool_root),
            }
        )
        result = subprocess.run(
            [
                "bash",
                str(EXPORT_DIR.parents[1] / "bin/product-hygiene.sh"),
                "--public-export",
                "--root",
                str(self.work),
                "--policy",
                str(export_root / "policy/path-policy.json"),
                "--identifier-denylist",
                str(export_root / "identifier-denylist.txt"),
                "--report",
                str(report),
            ],
            text=True,
            capture_output=True,
            env=environment,
        )
        return result, report.read_text(encoding="utf-8"), private_ledger

    def test_all_clean_when_only_main_advertised(self) -> None:
        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        self.assertTrue(records)
        self.assertTrue(all(r["status"].startswith("clean") for r in records))

    @unittest.skipUnless(
        (EXPORT_DIR / "target_scan.py").is_file(),
        "private integration: projector imports the withheld target_scan.py",
    )
    def test_default_ledger_path_matches_the_projector_oracle(self) -> None:
        from projector import DEFAULT_LEDGER_PATH as PROJECTOR_LEDGER_PATH

        self.assertEqual(remote_ref_audit.DEFAULT_LEDGER_PATH, PROJECTOR_LEDGER_PATH)

    def _run_with_ledger(self, ledger: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--ledger",
                str(ledger),
            ],
            text=True,
            capture_output=True,
        )

    def test_absent_ledger_refuses_before_classification(self) -> None:
        missing = self.base / "missing-export-ledger.jsonl"

        result = self._run_with_ledger(missing)

        self.assertEqual(result.returncode, 2)
        self.assertIn(f"export ledger {missing} is absent", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_empty_ledger_refuses_before_classification(self) -> None:
        empty = self.base / "empty-export-ledger.jsonl"
        empty.write_text("", encoding="utf-8")

        result = self._run_with_ledger(empty)

        self.assertEqual(result.returncode, 2)
        self.assertIn(f"export ledger {empty} is empty", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_dangling_ledger_symlink_refuses_before_classification(self) -> None:
        dangling = self.base / "dangling-export-ledger.jsonl"
        dangling.symlink_to(self.base / "missing-ledger-target.jsonl")

        result = self._run_with_ledger(dangling)

        self.assertEqual(result.returncode, 2)
        self.assertIn(f"export ledger {dangling} is a dangling symlink", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_append_only_baseline_accepts_previous_published_tip(self) -> None:
        previous_tip = _git(self.work, "rev-parse", "HEAD")
        self._record_public_tip(previous_tip, field="published_tip")
        (self.work / "next.txt").write_text("append-only publication\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "advance publication")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("PASS: every advertised ref is clean", result.stdout)
        self.assertNotIn("not append-only", result.stderr)

    def test_production_caller_passes_private_ledger_on_append_only_history(
        self,
    ) -> None:
        previous_tip = self.clean_sha
        (self.work / "README.md").write_text("clean\nappend-only publication\n")
        _git(self.work, "add", "README.md")
        _git(self.work, "commit", "-qm", "advance publication through caller")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

        result, report, private_ledger = self._run_production_caller(previous_tip)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr + report)
        self.assertIn(f"- Ledger precondition: {private_ledger.resolve()}", report)
        self.assertIn("- Remote-ref audit status: 0", report)
        self.assertIn("PASS: every advertised ref is clean", report)
        self.assertNotIn("not append-only", report)

    def test_production_caller_refuses_root_preserving_rewrite(self) -> None:
        (self.work / "README.md").write_text("published then purged\n")
        _git(self.work, "add", "README.md")
        _git(self.work, "commit", "-qm", "published sensitive content")
        previous_tip = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

        _git(self.work, "checkout", "-q", "-B", "rewritten", self.clean_sha)
        (self.work / "README.md").write_text("root-preserving replacement\n")
        _git(self.work, "add", "README.md")
        _git(self.work, "commit", "-qm", "rewrite through caller")
        _git(self.work, "push", "-q", "--force", "origin", "HEAD:refs/heads/main")
        _git(
            self.work,
            "push",
            "-q",
            "origin",
            f"{previous_tip}:refs/pull/10/head",
        )

        result, report, private_ledger = self._run_production_caller(previous_tip)

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"- Ledger precondition: {private_ledger.resolve()}", report)
        self.assertIn("- Remote-ref audit status: 2", report)
        self.assertIn("published history is not append-only", report)
        self.assertNotIn("PASS: every advertised ref is clean", report)

    def test_root_preserving_rewrite_fails_before_same_root_fork_is_accepted(
        self,
    ) -> None:
        (self.work / "sensitive.txt").write_text("published then purged\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "published sensitive content")
        previous_tip = _git(self.work, "rev-parse", "HEAD")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")
        self._record_public_tip(previous_tip)

        _git(self.work, "checkout", "-q", "-B", "rewritten", self.clean_sha)
        (self.work / "replacement.txt").write_text("root-preserving rewrite\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "rewrite without sensitive content")
        _git(self.work, "push", "-q", "--force", "origin", "HEAD:refs/heads/main")
        _git(
            self.work,
            "push",
            "-q",
            "origin",
            f"{previous_tip}:refs/pull/10/head",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("published history is not append-only", result.stderr)
        self.assertIn(previous_tip, result.stderr)
        self.assertNotIn("PASS:", result.stdout)

        # Positive control: with only the append-only precondition disabled,
        # the unchanged root-set classifier accepts this retained same-root ref.
        with mock.patch.object(
            remote_ref_audit, "_previous_public_tip", return_value=None
        ):
            records = audit_refs(
                self.work,
                "origin",
                "refs/remotes/origin/main",
                ledger_path=self.ledger,
            )
        statuses = {record["ref"]: record["status"] for record in records}
        self.assertEqual(statuses["refs/pull/10/head"], "contribution")

    def test_empty_merge_base_is_a_leak(self) -> None:
        # A disjoint orphan has no merge-base with the published baseline.
        _git(self.work, "checkout", "-q", "--orphan", "leak")
        (self.work / "secret.txt").write_text("old private\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "leak")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/1/head")
        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}
        self.assertEqual(statuses["refs/pull/1/head"], "LEAK")
        self.assertIn(statuses["refs/heads/main"], ("clean-equal", "clean-ancestor"))

    def test_ref_descended_from_baseline_is_a_contribution(self) -> None:
        (self.work / "contribution.txt").write_text("new public work\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "contribution")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/2/head")
        auditor = self.base / "auditor"
        auditor.mkdir()
        _git(auditor, "init", "-q")
        _git(auditor, "remote", "add", "origin", str(self.origin))

        # The auditor starts with no objects. Its default fetch refspec obtains
        # main but not refs/pull/*, so the explicit advertised-ref fetch is the
        # only reason it can evaluate the contribution commit.
        records = audit_refs(
            auditor,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}

        self.assertEqual(statuses["refs/pull/2/head"], "contribution")

    def test_same_root_fork_that_merges_baseline_is_a_contribution(self) -> None:
        _git(self.work, "checkout", "-q", "-b", "contribution")
        (self.work / "contribution.txt").write_text("fork work\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "fork contribution")
        _git(self.work, "checkout", "-q", "main")
        (self.work / "baseline.txt").write_text("advance baseline\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "advance baseline")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(self.work, "checkout", "-q", "contribution")
        _git(self.work, "merge", "-q", "--no-ff", "main", "-m", "merge baseline")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/5/head")

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}

        self.assertEqual(statuses["refs/pull/5/head"], "contribution")

    def test_disjoint_lineage_that_merges_baseline_is_a_leak(self) -> None:
        _git(self.work, "checkout", "-q", "--orphan", "old-lineage")
        _git(self.work, "rm", "-q", "-rf", ".")
        (self.work / "secret.txt").write_text("pre-clean-slate secret\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "old disjoint lineage")
        _git(
            self.work,
            "merge",
            "-q",
            "--no-ff",
            "--allow-unrelated-histories",
            "main",
            "-m",
            "merge clean baseline",
        )
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/9/head")

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}
        self.assertEqual(statuses["refs/pull/9/head"], "LEAK")

        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("refs/pull/9/head  LEAK", result.stdout)
        self.assertNotIn("PASS:", result.stdout)

    def test_merge_bypass_poc_is_a_leak(self) -> None:
        _git(self.work, "checkout", "-q", "--orphan", "retained-lineage")
        _git(self.work, "rm", "-q", "-rf", ".")
        (self.work / "secret.txt").write_text("retained private history\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "retained lineage")
        retained_sha = _git(self.work, "rev-parse", "HEAD")

        _git(self.work, "checkout", "-q", "main")
        _git(
            self.work,
            "merge",
            "-q",
            "--no-ff",
            "--allow-unrelated-histories",
            retained_sha,
            "-m",
            "merge retained lineage into baseline",
        )
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/8/head")

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}

        self.assertEqual(statuses["refs/pull/8/head"], "LEAK")

    def test_older_published_fork_is_a_contribution(self) -> None:
        _git(self.work, "checkout", "-q", "-b", "stale-contribution")
        (self.work / "stale.txt").write_text("forked before clean tip\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "stale contribution")
        _git(self.work, "checkout", "-q", "main")
        (self.work / "baseline.txt").write_text("new clean baseline\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "advance clean baseline")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(
            self.work,
            "push",
            "-q",
            "origin",
            "stale-contribution:refs/pull/3/head",
        )

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}

        self.assertEqual(statuses["refs/pull/3/head"], "contribution")

    def test_older_published_fork_with_an_extra_root_is_a_leak(self) -> None:
        _git(self.work, "checkout", "-q", "-b", "stale-contribution")
        (self.work / "stale.txt").write_text("forked before clean tip\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "stale contribution")

        _git(self.work, "checkout", "-q", "main")
        (self.work / "baseline.txt").write_text("new clean baseline\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "advance clean baseline")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

        _git(self.work, "checkout", "-q", "--orphan", "extra-root")
        _git(self.work, "rm", "-q", "-rf", ".")
        (self.work / "secret.txt").write_text("outside clean root set\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "extra root")
        extra_root_sha = _git(self.work, "rev-parse", "HEAD")

        _git(self.work, "checkout", "-q", "stale-contribution")
        _git(
            self.work,
            "merge",
            "-q",
            "--no-ff",
            "--allow-unrelated-histories",
            extra_root_sha,
            "-m",
            "merge extra root",
        )
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/7/head")

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in records}

        self.assertEqual(statuses["refs/pull/7/head"], "LEAK")

    def test_unfetchable_advertised_ref_is_unknown_and_fails_closed(self) -> None:
        (self.work / "unfetchable.txt").write_text("opaque contribution\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "unfetchable contribution")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/4/head")
        original_git = remote_ref_audit._git

        def fail_pull_fetch(repo: Path, *args: str) -> str:
            if args == (
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                "refs/pull/4/head",
            ):
                raise RemoteRefAuditError("injected per-ref fetch failure")
            return original_git(repo, *args)

        with mock.patch.object(remote_ref_audit, "_git", side_effect=fail_pull_fetch):
            records = audit_refs(
                self.work,
                "origin",
                "refs/remotes/origin/main",
                ledger_path=self.ledger,
            )
        statuses = {r["ref"]: r["status"] for r in records}
        self.assertEqual(statuses["refs/pull/4/head"], "UNKNOWN")

        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch.object(remote_ref_audit, "audit_refs", return_value=records),
            mock.patch.object(sys, "argv", ["remote_ref_audit.py"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = remote_ref_audit.main()
        self.assertEqual(result, 1)
        self.assertIn("UNKNOWN", stdout.getvalue())
        self.assertIn("could not be evaluated", stderr.getvalue())

    def test_accepted_risk_is_sha_pinned_and_reports_classification(self) -> None:
        _git(self.work, "checkout", "-q", "--orphan", "keep")
        _git(self.work, "rm", "-q", "-rf", ".")
        (self.work / "b.txt").write_text("intentional\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "keep")
        accepted_sha = _git(self.work, "rev-parse", "HEAD")
        accepted_ref = "refs/pull/1/head"
        _git(self.work, "push", "-q", "origin", f"HEAD:{accepted_ref}")
        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            accepted_refs={accepted_ref: accepted_sha},
            ledger_path=self.ledger,
        )
        record = next(r for r in records if r["ref"] == accepted_ref)
        self.assertEqual(record["status"], "accepted-risk")
        self.assertEqual(record["classification"], "LEAK")

        accepted_result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--accept-ref",
                f"{accepted_ref}={accepted_sha}",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(accepted_result.returncode, 0)
        self.assertIn("accepted-risk (suppressed LEAK)", accepted_result.stdout)

        _git(self.work, "checkout", "-q", "--orphan", "drift")
        _git(self.work, "rm", "-q", "-rf", ".")
        (self.work / "new-secret.txt").write_text("never accepted\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "different disjoint lineage")
        _git(self.work, "push", "-q", "--force", "origin", f"HEAD:{accepted_ref}")

        drifted = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            accepted_refs={accepted_ref: accepted_sha},
            ledger_path=self.ledger,
        )
        statuses = {r["ref"]: r["status"] for r in drifted}
        self.assertEqual(statuses[accepted_ref], "LEAK")

        drift_result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--accept-ref",
                f"{accepted_ref}={accepted_sha}",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(drift_result.returncode, 1)
        self.assertIn(f"{accepted_ref}  LEAK", drift_result.stdout)

    def test_unmatched_acceptance_is_reported_but_not_fatal(self) -> None:
        clean_sha = _git(self.work, "rev-parse", "HEAD")
        accepted_ref = "refs/heads/mian"

        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--accept-ref",
                f"{accepted_ref}={clean_sha}",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn(
            f"STALE ACCEPTANCE: {accepted_ref}: ref was not advertised",
            result.stderr,
        )

    def test_acceptance_matching_clean_ref_is_reported_as_stale(self) -> None:
        clean_sha = _git(self.work, "rev-parse", "HEAD")
        accepted_ref = "refs/heads/main"

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            accepted_refs={accepted_ref: clean_sha},
            ledger_path=self.ledger,
        )
        record = next(r for r in records if r["ref"] == accepted_ref)
        self.assertEqual(record["status"], "clean-equal")
        self.assertTrue(record["stale_acceptance"])

        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--accept-ref",
                f"{accepted_ref}={clean_sha}",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn(f"{accepted_ref}  clean-equal", result.stdout)
        self.assertNotIn("accepted-risk", result.stdout)
        self.assertIn(
            f"STALE ACCEPTANCE: {accepted_ref}: underlying classification is clean-equal",
            result.stderr,
        )

    def test_annotated_tag_is_evaluated_once(self) -> None:
        _git(self.work, "tag", "-a", "release", "-m", "release")
        _git(self.work, "push", "-q", "origin", "refs/tags/release")

        records = audit_refs(
            self.work,
            "origin",
            "refs/remotes/origin/main",
            ledger_path=self.ledger,
        )
        tag_records = [r for r in records if r["ref"].startswith("refs/tags/release")]

        self.assertEqual(len(tag_records), 1)
        self.assertEqual(tag_records[0]["ref"], "refs/tags/release")

    def test_zero_advertised_refs_fails_as_incomplete(self) -> None:
        _git(self.work, "fetch", "-q", "origin")
        empty = self.base / "empty"
        empty.mkdir()
        _git(empty, "init", "-q", "--bare")
        _git(self.work, "remote", "set-url", "origin", str(empty))

        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "origin",
                "--clean-ref",
                "refs/remotes/origin/main",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("remote advertised no evaluable refs", result.stderr)
        self.assertNotIn("PASS:", result.stdout)

    def test_shallow_checkout_and_linked_worktree_fail_with_evidence_error(self) -> None:
        (self.work / "second.txt").write_text("second commit\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "second")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

        shallow = self.base / "shallow"
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "--branch",
                "main",
                "--no-local",
                str(self.origin),
                str(shallow),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        linked = self.base / "linked"
        _git(shallow, "worktree", "add", "-q", "-b", "linked", str(linked))

        for repo in (shallow, linked):
            with self.subTest(repo=repo.name):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(EXPORT_DIR / "remote_ref_audit.py"),
                        "--repo",
                        str(repo),
                        "--remote",
                        "origin",
                        "--clean-ref",
                        "refs/remotes/origin/main",
                        "--ledger",
                        str(self.ledger),
                    ],
                    text=True,
                    capture_output=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("repository is shallow", result.stderr)
                self.assertIn("could not complete", result.stderr)
                self.assertNotIn("LEAK", result.stdout)

    def test_cli_reports_fetch_failure_without_a_traceback(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(EXPORT_DIR / "remote_ref_audit.py"),
                "--repo",
                str(self.work),
                "--remote",
                "missing",
                "--clean-ref",
                "refs/remotes/missing/main",
                "--ledger",
                str(self.ledger),
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("remote-ref audit could not complete", result.stderr)
        self.assertIn("git fetch --quiet missing exited 128", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
