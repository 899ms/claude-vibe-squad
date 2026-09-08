from __future__ import annotations

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
        (self.work / "a.txt").write_text("clean\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "clean")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/heads/main")

    def test_all_clean_when_only_main_advertised(self) -> None:
        records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
        self.assertTrue(records)
        self.assertTrue(all(r["status"].startswith("clean") for r in records))

    def test_disjoint_advertised_ref_is_flagged(self) -> None:
        # A disjoint (orphan) lineage pushed to a pull-like ref — the leak shape.
        _git(self.work, "checkout", "-q", "--orphan", "leak")
        (self.work / "secret.txt").write_text("old private\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-qm", "leak")
        _git(self.work, "push", "-q", "origin", "HEAD:refs/pull/1/head")
        records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
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
        records = audit_refs(auditor, "origin", "refs/remotes/origin/main")
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

        records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
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

        records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
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
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("refs/pull/9/head  LEAK", result.stdout)
        self.assertNotIn("PASS:", result.stdout)

    def test_ref_diverged_from_older_baseline_commit_is_a_leak(self) -> None:
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

        records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
        statuses = {r["ref"]: r["status"] for r in records}

        self.assertEqual(statuses["refs/pull/3/head"], "LEAK")

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
            records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
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

        records = audit_refs(self.work, "origin", "refs/remotes/origin/main")
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
