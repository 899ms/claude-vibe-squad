#!/usr/bin/env python3
"""Retention reaper tests against isolated, real filesystem fixtures.

The rescue fixture is a real Git repository because the safety boundary is an
exact match to an indexed regular-file blob.  A same-name file is deliberately
different to prove names and extensions never classify rescued work.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "python" / "reap_unbounded_state.py"

SCRATCH_ROOT = Path(tempfile.gettempdir())
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()
RECENT = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc).timestamp()


class ReapUnboundedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(
            tempfile.mkdtemp(prefix="reap-unbounded-", dir=SCRATCH_ROOT)
        ).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "retention@test.invalid"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Retention Test"],
            cwd=self.root,
            check=True,
        )
        (self.root / "canonical.txt").write_bytes(b"tracked duplicate\n")
        subprocess.run(["git", "add", "canonical.txt"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "fixture"], cwd=self.root, check=True
        )

        self.state = self.root / "_state"
        self.receipt_dir = self.root / "receipts"
        self.snapshots = self.root / "vault-snapshots"
        for relative in (
            "board-codex-homes",
            "board-worktrees",
            "chrono-notify-receipts",
            "long-running-noted",
            "rescued-worker-artifacts",
        ):
            (self.state / relative).mkdir(parents=True)
        self.snapshots.mkdir()

    @staticmethod
    def _set_mtime(path: Path, timestamp: float) -> None:
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)

    def _old_file(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._set_mtime(path, OLD)
        return path

    def _run(
        self, *mode: str, include_snapshot_dir: bool = True
    ) -> subprocess.CompletedProcess[str]:
        snapshot_args = (
            ["--vault-snapshot-dir", str(self.snapshots)] if include_snapshot_dir else []
        )
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                *mode,
                "--root",
                str(self.root),
                *snapshot_args,
                "--receipt-dir",
                str(self.receipt_dir),
                "--now",
                NOW.isoformat().replace("+00:00", "Z"),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )

    def _build_candidates(self) -> dict[str, Path]:
        homes = self.state / "board-codex-homes"
        orphan = homes / "d-orphan"
        self._old_file(orphan / "cache.bin", b"orphan cache")
        self._set_mtime(orphan, OLD)

        active = homes / "d-active"
        self._old_file(active / "cache.bin", b"active cache")
        self._set_mtime(active, OLD)
        (self.state / "board-worktrees" / active.name).mkdir()

        fresh = homes / "d-fresh"
        self._old_file(fresh / "cache.bin", b"fresh cache")
        self._set_mtime(fresh / "cache.bin", RECENT)
        self._set_mtime(fresh, RECENT)

        notifications = self.state / "chrono-notify-receipts"
        old_notifications = []
        for index in range(2):
            old_notifications.append(
                self._old_file(notifications / f"old-{index}.sent", b"old")
            )
        for index in range(1000):
            path = notifications / f"recent-{index:04d}.sent"
            path.write_bytes(b"recent")
            self._set_mtime(path, RECENT + index)

        markers = self.state / "long-running-noted"
        marker = self._old_file(markers / "old.noted", b"")
        recent_marker = markers / "recent.noted"
        recent_marker.write_bytes(b"")
        self._set_mtime(recent_marker, RECENT)
        self._old_file(markers / ".gitkeep", b"")

        oldest_snapshot = self._old_file(
            self.snapshots / "chrono-vault-20260701-120000Z.tar.gz", b"old-snapshot"
        )
        for day in range(25, 32):
            path = self.snapshots / f"chrono-vault-202608{day}-120000Z.tar.gz"
            path.write_bytes(f"snapshot-{day}".encode())
            self._set_mtime(path, RECENT + day)

        rescue = self.state / "rescued-worker-artifacts" / "TASK-old"
        duplicate = self._old_file(rescue / "copy" / "anything.bin", b"tracked duplicate\n")
        unique = self._old_file(rescue / "copy" / "canonical.txt", b"unique poc\n")
        recent_duplicate = rescue / "fresh" / "duplicate.txt"
        recent_duplicate.parent.mkdir(parents=True)
        recent_duplicate.write_bytes(b"tracked duplicate\n")
        self._set_mtime(recent_duplicate, RECENT)

        return {
            "orphan": orphan,
            "active": active,
            "fresh": fresh,
            "notification_0": old_notifications[0],
            "notification_1": old_notifications[1],
            "marker": marker,
            "snapshot": oldest_snapshot,
            "rescue_duplicate": duplicate,
            "rescue_unique": unique,
            "rescue_recent_duplicate": recent_duplicate,
        }

    def test_default_is_dry_run_and_receipts_every_planned_removal(self) -> None:
        paths = self._build_candidates()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=preserve", result.stdout)
        for name in (
            "orphan",
            "notification_0",
            "notification_1",
            "marker",
            "snapshot",
            "rescue_duplicate",
        ):
            self.assertTrue(paths[name].exists(), f"dry run removed {name}")
        receipt = json.loads(
            (self.receipt_dir / "latest-preserve.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["schema"], "unbounded-state-reaper-receipt/v1")
        self.assertEqual(receipt["mode"], "preserve")
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["totals"]["planned_items"], 5)
        self.assertGreater(receipt["totals"]["planned_logical_bytes"], 0)
        self.assertEqual(receipt["totals"]["removed_items"], 0)
        self.assertEqual(receipt["removed"], [])
        self.assertEqual(len(receipt["planned"]), 5)
        self.assertNotIn(
            "board_codex_homes",
            receipt["categories"],
            "the general retention reaper duplicated the established owner: "
            "bin/prune-board-worktrees.sh",
        )

    def test_apply_removes_only_candidates_and_records_exact_receipt(self) -> None:
        paths = self._build_candidates()

        result = self._run("--apply")

        self.assertEqual(result.returncode, 0, result.stderr)
        for name in (
            "notification_0",
            "notification_1",
            "marker",
            "snapshot",
            "rescue_duplicate",
        ):
            self.assertFalse(paths[name].exists(), f"apply retained candidate {name}")
        for name in (
            "orphan",
            "active",
            "fresh",
            "rescue_unique",
            "rescue_recent_duplicate",
        ):
            self.assertTrue(paths[name].exists(), f"apply removed retained {name}")

        receipt_path = self.receipt_dir / "latest-apply.json"
        self.assertTrue(receipt_path.is_file())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["mode"], "apply")
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["totals"]["planned_items"], 5)
        self.assertEqual(receipt["totals"]["removed_items"], 5)
        self.assertEqual(
            receipt["totals"]["removed_logical_bytes"],
            receipt["totals"]["planned_logical_bytes"],
        )
        self.assertEqual(
            {item["path"] for item in receipt["removed"]},
            {item["path"] for item in receipt["planned"]},
        )

    def test_rescue_classification_uses_content_not_name_or_extension(self) -> None:
        paths = self._build_candidates()

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(
            (self.receipt_dir / "latest-preserve.json").read_text(encoding="utf-8")
        )
        rescue_planned = [
            item for item in receipt["planned"] if item["category"] == "rescued_worker_artifacts"
        ]
        self.assertEqual(len(rescue_planned), 1)
        self.assertEqual(
            rescue_planned[0]["path"],
            str(paths["rescue_duplicate"].relative_to(self.root)),
        )
        self.assertEqual(rescue_planned[0]["evidence"]["kind"], "exact_git_index_blob")
        self.assertIn("canonical.txt", rescue_planned[0]["evidence"]["tracked_paths"])
        self.assertTrue(paths["rescue_unique"].exists())

    def test_healthy_empty_state_reaps_nothing_and_still_emits_receipt(self) -> None:
        result = self._run("--preserve")

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt_path = self.receipt_dir / "latest-preserve.json"
        self.assertTrue(receipt_path.is_file(), "no-op run emitted no receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["totals"]["planned_items"], 0)
        self.assertEqual(receipt["totals"]["planned_logical_bytes"], 0)
        self.assertEqual(receipt["totals"]["removed_items"], 0)
        self.assertEqual(receipt["planned"], [])
        self.assertIn("planned=0", result.stdout)

    def test_apply_and_preserve_are_mutually_exclusive(self) -> None:
        result = self._run("--preserve", "--apply")

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_default_snapshot_directory_selects_real_archives(self) -> None:
        paths = self._build_candidates()
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            os.environ.pop("VAULT_SNAPSHOT_DEST", None)
            result = self._run(include_snapshot_dir=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.receipt_dir / "latest-preserve.json").read_text())
        self.assertEqual(receipt["vault_snapshot_dir"], str(self.snapshots))
        snapshots = [item for item in receipt["planned"] if item["category"] == "vault_snapshots"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["path"], str(paths["snapshot"].relative_to(self.root)))
        self.assertTrue(paths["snapshot"].is_file())

    def test_empty_snapshot_environment_does_not_select_the_working_directory(self) -> None:
        decoy = self._old_file(self.root / "chrono-vault-decoy.tar.gz", b"wrong directory")
        with mock.patch.dict(os.environ, {"HOME": str(self.root), "VAULT_SNAPSHOT_DEST": ""}):
            result = self._run(include_snapshot_dir=False)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads((self.receipt_dir / "latest-preserve.json").read_text())
        self.assertEqual(receipt["vault_snapshot_dir"], str(self.snapshots))
        self.assertEqual(receipt["categories"]["vault_snapshots"]["observed_items"], 0)
        self.assertTrue(decoy.is_file())

    def test_snapshot_environment_selects_real_archives(self) -> None:
        paths = self._build_candidates()
        with mock.patch.dict(os.environ, {"VAULT_SNAPSHOT_DEST": str(self.snapshots)}):
            result = self._run(include_snapshot_dir=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.receipt_dir / "latest-preserve.json").read_text())
        self.assertEqual(receipt["vault_snapshot_dir"], str(self.snapshots))
        snapshots = [item for item in receipt["planned"] if item["category"] == "vault_snapshots"]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["path"], str(paths["snapshot"].relative_to(self.root)))
        self.assertTrue(paths["snapshot"].is_file(), "preserve removed a snapshot")

    def test_explicit_snapshot_directory_overrides_environment(self) -> None:
        other = self.root / "other-snapshots"
        other.mkdir()
        with mock.patch.dict(os.environ, {"VAULT_SNAPSHOT_DEST": str(other)}):
            result = self._run()

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.receipt_dir / "latest-preserve.json").read_text())
        self.assertEqual(receipt["vault_snapshot_dir"], str(self.snapshots))


class SnapshotDestinationIdentityTests(unittest.TestCase):
    """Execute all three consumers in a scratch tree, including a resolver mutation."""

    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="snapshot-dest-", dir=SCRATCH_ROOT)).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.repo = self.scratch / "repo with spaces"
        self.user_home = self.scratch / "user home"
        self.user_home.mkdir()
        self.vault = self.scratch / "private vault"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / ".chrono-vault").write_text("fixture\n")
        (self.vault / "notes" / "one.md").write_text("snapshot identity fixture\n")
        for relative in (
            "bin/vault-snapshot.sh", "bin/run-nightly.sh", "bin/doctor-log-home.sh",
            "shared/repo-root.sh", "shared/vault-snapshot-dest.sh",
            "scripts/python/reap_unbounded_state.py",
        ):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / relative, target)

        # Real snapshot producer; every other nightly phase is a local no-op.
        # Read the phase list from the script so this fixture cannot call live phases.
        nightly = (self.repo / "bin/run-nightly.sh").read_text()
        phases = re.findall(r'run_phase\s+"[^"]+"\s+"\$\{VAULT_ROOT\}/bin/([^"]+)"', nightly)
        self.assertIn("vault-snapshot.sh", phases)
        for name in set(phases) - {"vault-snapshot.sh"}:
            stub = self.repo / "bin" / name
            stub.write_text("#!/bin/bash\nexit 0\n")
            stub.chmod(0o755)
        self.resolver = self.repo / "shared/vault-snapshot-dest.sh"
        self.receipts = self.scratch / "receipts"

    def _invoke(
        self, entry: str, *args: str, override: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.defpath,
            "HOME": str(self.user_home),
            "VAULT_ROOT": str(self.repo),
            "CHRONO_VAULT_ROOT": str(self.vault),
            "CHRONO_DOCTOR_LOG_DIR": str(self.scratch / "doctor-logs"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if override is not None:
            environment["VAULT_SNAPSHOT_DEST"] = override
        interpreter = sys.executable if entry.endswith(".py") else "/bin/bash"
        return subprocess.run(
            [interpreter, str(self.repo / entry), *args],
            cwd=self.repo, env=environment, capture_output=True, text=True,
            timeout=30, check=False,
        )

    def _reap(self, *args: str, override: str | None = None) -> subprocess.CompletedProcess[str]:
        return self._invoke(
            "scripts/python/reap_unbounded_state.py", "--preserve",
            "--root", str(self.repo), "--receipt-dir", str(self.receipts),
            *args, override=override,
        )

    def _destinations(self, override: str | None = None) -> dict[str, Path]:
        producer = self._invoke("bin/vault-snapshot.sh", override=override)
        self.assertEqual(producer.returncode, 0, producer.stdout + producer.stderr)
        archive = re.search(r"^Archive  : (.+)$", producer.stdout, re.MULTILINE)
        self.assertIsNotNone(archive, producer.stdout)
        self.assertIn("OK: 1 notes captured and verified.", producer.stdout)
        archive_path = Path(archive[1])
        if not archive_path.is_absolute():
            archive_path = self.repo / archive_path
        self.assertTrue(archive_path.is_file())

        nightly = self._invoke("bin/run-nightly.sh", override=override)
        self.assertEqual(nightly.returncode, 0, nightly.stdout + nightly.stderr)
        report = re.search(r"vault snapshots: (\d+) archive\(s\), .* total in (.+)", nightly.stdout)
        self.assertIsNotNone(report, nightly.stdout)
        self.assertGreater(int(report[1]), 0, "nightly never observed the producer's archive")
        report_path = Path(report[2])
        if not report_path.is_absolute():
            report_path = self.repo / report_path

        result = self._reap(override=override)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads((self.receipts / "latest-preserve.json").read_text())
        self.assertEqual(receipt["categories"]["vault_snapshots"]["observed_items"], int(report[1]))
        self.assertEqual(receipt["totals"]["removed_items"], 0)
        return {
            "vault-snapshot": archive_path.parent.resolve(),
            "run-nightly": report_path.resolve(),
            "reaper": Path(receipt["vault_snapshot_dir"]),
        }

    def _assert_identity(self, expected: Path, override: str | None = None) -> dict[str, Path]:
        destinations = self._destinations(override)
        for entry, destination in destinations.items():
            self.assertEqual(destination, expected, entry)
        return destinations

    def test_default_and_empty_override_agree_at_all_entry_points(self) -> None:
        for override in (None, ""):
            with self.subTest(override=override):
                self._assert_identity(self.user_home / "vault-snapshots", override)

    def test_environment_override_is_literal_at_all_entry_points(self) -> None:
        destination = self.scratch / "snapshots 'quoted' $literal [glob] "
        self._assert_identity(destination, str(destination))

    def test_relative_environment_override_agrees_at_all_entry_points(self) -> None:
        self._assert_identity(self.repo / "relative snapshots", "relative snapshots")

    def test_single_home_mutation_moves_all_three_entry_points(self) -> None:
        before = self._assert_identity(self.user_home / "vault-snapshots")
        original = self.resolver.read_text()
        self.assertEqual(original.count("/vault-snapshots"), 1)
        self.resolver.write_text(original.replace("/vault-snapshots", "/moved-snapshots"))
        after = self._assert_identity(self.user_home / "moved-snapshots")
        for entry in before:
            self.assertNotEqual(before[entry], after[entry], entry)

    def test_snapshot_cli_destination_overrides_environment(self) -> None:
        destination = self.scratch / "explicit snapshots"
        result = self._invoke(
            "bin/vault-snapshot.sh", "--dest", str(destination),
            override=str(self.scratch / "unused override"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Archive  : {destination}/chrono-vault-", result.stdout)
        self.assertEqual(len(list(destination.glob("chrono-vault-*.tar.gz"))), 1)

    def test_failed_or_empty_resolver_stops_reaper_before_receipts(self) -> None:
        original = self.resolver.read_text()
        for replacement, message in (
            ("    return 9 # printf ", "snapshot destination resolver failed"),
            ("    printf '' # printf ", "snapshot destination resolver returned an empty destination"),
        ):
            with self.subTest(replacement=replacement):
                self.resolver.write_text(original.replace("    printf ", replacement))
                result = self._reap()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.receipts.exists())


if __name__ == "__main__":
    unittest.main()
