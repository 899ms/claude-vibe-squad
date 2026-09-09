from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import dispatch_preflight as preflight  # noqa: E402
from verification_contract import (  # noqa: E402
    author_family_for_lane,
    derive_verification_contract,
)


ORCHESTRATOR_BLINDNESS_SHAPE = """
Audit squad-wide prompt and generated-adapter hygiene. Analyze the routing owner
mismatch that assigned harness script drift to a documentation specialist.
Compare the canonical specialist boundaries and design a mandatory dispatch
preflight that prevents the same mismatch before publication.
""".strip()


class DispatchPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self._copy_sources()

    def _copy(self, relative: str) -> None:
        source = ROOT / relative
        destination = self.repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def _copy_sources(self) -> None:
        for relative in (
            "shared/specialist-runtime-map.tsv",
            "shared/registries/profiles.tsv",
            "shared/specialists/triage.md",
            "shared/specialists/prompt-engineer.md",
            "departments/sysmgmt/specialists/harness-optimizer.md",
            "departments/content/specialists/technical-writer.md",
            "model-lanes/specialist-lane-capabilities.v1.json",
        ):
            self._copy(relative)
        source = ROOT / "model-lanes/specialist-lane-capabilities.v1.json"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        grep = subprocess.run(
            ["git", "grep", "-lz", digest, "--"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        self.coupled_hash_pinners = tuple(
            path.decode()
            for path in grep.stdout.split(b"\0")
            if path and path.decode() != source.relative_to(ROOT).as_posix()
        )
        for relative in self.coupled_hash_pinners:
            self._copy(relative)
        (self.repo / "_state").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "init", "-q"], cwd=self.repo, check=True, stdout=subprocess.DEVNULL
        )
        subprocess.run(
            ["git", "add", "."], cwd=self.repo, check=True, stdout=subprocess.DEVNULL
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Dispatch Preflight Test",
                "-c",
                "user.email=dispatch-preflight@example.invalid",
                "commit",
                "-qm",
                "baseline",
            ],
            cwd=self.repo,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def _packet(
        self,
        *,
        specialist: str,
        acknowledgement: bool = False,
        corrupt_contract_hash: bool = False,
        body: str = ORCHESTRATOR_BLINDNESS_SHAPE,
        write_scope: tuple[str, ...] | None = None,
    ) -> Path:
        source_namespace = {
            "technical-writer": "content",
            "harness-optimizer": "sysmgmt",
        }[specialist]
        suffix = "tech" if specialist == "technical-writer" else "harness"
        if acknowledgement:
            suffix += "-ack"
        task_id = f"TASK-2026-08-10-0771-{suffix}"
        contract = derive_verification_contract(
            {
                "task_id": task_id,
                "run_id": "V4-PREFLIGHT-TEST",
                "mode": "project",
                "result_type": "normal",
                "to_model": "claude",
                "dispatch_kind": "single",
                "capability": None,
                "expected_gates": [],
            }
        )
        contract_text = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        contract_hash = hashlib.sha256(contract_text.encode("ascii")).hexdigest()
        if corrupt_contract_hash:
            contract_hash = "0" * 64
        ack = (
            f"{preflight.ACK_FIELD}: [{preflight.OWNER_MISMATCH}]\n"
            if acknowledgement
            else ""
        )
        return_artifact = "_state/v4-runtime/orchestration/result.md"
        declared_scope = write_scope or ("_state/v4-runtime/orchestration/",)
        packet = self.repo / "packets" / f"{task_id}.md"
        packet.parent.mkdir(exist_ok=True)
        packet.write_text(
            "---\n"
            f"id: {task_id}\n"
            "run_id: V4-PREFLIGHT-TEST\n"
            "to_model: claude\n"
            f"specialist: {specialist}\n"
            f"source_namespace: {source_namespace}\n"
            f"compatibility_namespace: {source_namespace}\n"
            "mode: project\n"
            "result_type: normal\n"
            f"write_scope: [{', '.join(declared_scope)}]\n"
            "read_scope: []\n"
            f"return_artifact: {return_artifact}\n"
            "parallel_safe: false\n"
            "direct_lane_work_allowed: true\n"
            "mandatory_review: false\n"
            "review_model: none\n"
            f"{ack}"
            f"verification_contract: {contract_text}\n"
            f"verification_contract_sha256: {contract_hash}\n"
            "---\n\n"
            f"{body}\n",
            encoding="utf-8",
        )
        return packet

    def test_preflight_module_stays_small(self) -> None:
        # Ratchet, not a target. Raised 665 -> 682 for `_scan_failed_advisory`:
        # the two advisory handlers used to answer a crashed scan with `()`,
        # which is the same answer a clean packet gets.
        module = ROOT / "scripts" / "python" / "dispatch_preflight.py"
        nonblank = sum(bool(line.strip()) for line in module.read_text().splitlines())
        self.assertLessEqual(nonblank, 682)

    def test_exact_contract_violation_is_refused(self) -> None:
        packet = self._packet(
            specialist="technical-writer", corrupt_contract_hash=True
        )
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "deny")
        self.assertEqual(verdict.exit_code, preflight.EXIT_REFUSE)
        self.assertEqual(verdict.refusals[0]["code"], "exact_contract_violation")
        self.assertIn("does not match", verdict.refusals[0]["message"])

    def test_owner_mismatch_requires_packet_acknowledgement(self) -> None:
        packet = self._packet(specialist="technical-writer")
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "needs_ack")
        self.assertEqual(verdict.exit_code, preflight.EXIT_ACK_REQUIRED)
        self.assertEqual(len(verdict.warnings), 1)
        warning = verdict.warnings[0]
        self.assertEqual(warning["code"], "owner_mismatch")
        self.assertEqual(warning["selected_specialist"], "technical-writer")
        self.assertEqual(warning["recommended_specialist"], "harness-optimizer")
        self.assertFalse(warning["acknowledged"])
        self.assertEqual(
            warning["required_ack"],
            "dispatch_preflight_ack: [owner_mismatch]",
        )

    def test_packet_acknowledgement_allows_the_warning(self) -> None:
        packet = self._packet(
            specialist="technical-writer", acknowledgement=True
        )
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "allow")
        self.assertEqual(verdict.exit_code, preflight.EXIT_PASS)
        self.assertTrue(verdict.warnings[0]["acknowledged"])
        self.assertRegex(verdict.warning_set_sha256 or "", r"^[0-9a-f]{64}$")
        self.assertRegex(verdict.ack_sha256 or "", r"^[0-9a-f]{64}$")

    def test_acknowledged_owner_warning_and_dirty_advisory_can_coexist(self) -> None:
        target = self.repo / "shared/specialists/triage.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\ndirty\n", encoding="utf-8"
        )
        packet = self._packet(
            specialist="technical-writer",
            acknowledgement=True,
            body=ORCHESTRATOR_BLINDNESS_SHAPE,
            write_scope=(
                "_state/v4-runtime/orchestration/result.md",
                "shared/specialists/triage.md",
            ),
        )
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "allow")
        self.assertEqual(
            [item["code"] for item in verdict.warnings],
            [preflight.OWNER_MISMATCH, preflight.DIRTY_WRITE_SCOPE],
        )
        self.assertRegex(verdict.ack_sha256 or "", r"^[0-9a-f]{64}$")

    def test_coupled_hash_pin_ordinary_packet_stays_silent(self) -> None:
        artifact = "_state/v4-runtime/orchestration/result.md"
        packet = self._packet(
            specialist="harness-optimizer",
            body="Update `shared/specialists/triage.md` to clarify the route.",
            write_scope=(artifact, "shared/specialists/triage.md"),
        )
        self.assertEqual(preflight.authoring_warnings(self.repo, packet), ())

    def test_dirty_tracked_write_scope_warns_but_allows(self) -> None:
        target = self.repo / "shared/specialists/triage.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\ndirty\n", encoding="utf-8"
        )
        artifact = "_state/v4-runtime/orchestration/result.md"
        packet = self._packet(
            specialist="harness-optimizer",
            body="Update `shared/specialists/triage.md` to clarify the route.",
            write_scope=(artifact, "shared/specialists/triage.md"),
        )
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "allow")
        self.assertEqual(verdict.exit_code, preflight.EXIT_PASS)
        self.assertEqual(
            [item["code"] for item in verdict.warnings],
            [preflight.DIRTY_WRITE_SCOPE],
        )
        self.assertEqual(
            verdict.warnings[0]["paths"], ["shared/specialists/triage.md"]
        )

    def test_empty_scope_does_not_treat_every_dirty_path_as_in_scope(self) -> None:
        target = self.repo / "shared/specialists/triage.md"
        target.write_text(
            target.read_text(encoding="utf-8") + "\ndirty\n", encoding="utf-8"
        )
        packet = self._packet(
            specialist="harness-optimizer",
            body="Inspect the current dispatch design and report the result.",
            write_scope=(),
        )
        packet.write_text(
            packet.read_text(encoding="utf-8").replace(
                "write_scope: [_state/v4-runtime/orchestration/]", "write_scope: []"
            ).replace(
                "return_artifact: _state/v4-runtime/orchestration/result.md",
                'return_artifact: ""',
            ),
            encoding="utf-8",
        )
        self.assertEqual(preflight.authoring_warnings(self.repo, packet), ())

    def test_advisory_failure_is_fail_open_but_not_fail_silent(self) -> None:
        """A crashed advisory scan still allows dispatch, and now says it crashed.

        This used to assert `warnings == ()` -- the same verdict a clean packet
        produces, so a scan that never ran was indistinguishable from a scan
        that found nothing. Fail-open is about the decision, not the reporting.
        """
        packet = self._packet(
            specialist="harness-optimizer",
            body="Update `shared/specialists/triage.md` to clarify the route.",
        )
        with mock.patch.object(
            preflight, "_git_paths", side_effect=RuntimeError("diagnostic failed")
        ):
            verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "allow")
        self.assertEqual(verdict.exit_code, preflight.EXIT_PASS)
        self.assertEqual(
            [warning["code"] for warning in verdict.warnings],
            [preflight.ADVISORY_SCAN_FAILED],
        )
        self.assertIn("diagnostic failed", verdict.warnings[0]["message"])

    def test_malformed_advisory_output_is_fail_open(self) -> None:
        packet = self._packet(specialist="harness-optimizer")
        with mock.patch.object(
            preflight,
            "authoring_warnings",
            return_value=({"gate": "advisory"},),
        ):
            status = preflight.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--packet",
                    str(packet),
                    "--authoring-warnings-only",
                ]
            )
        self.assertEqual(status, preflight.EXIT_PASS)

    def test_coupled_hash_pin_prints_in_dry_and_live_paths_without_gating(self) -> None:
        artifact = "_state/v4-runtime/orchestration/result.md"
        source = "model-lanes/specialist-lane-capabilities.v1.json"
        pinners = list(self.coupled_hash_pinners)
        self.assertTrue(pinners, "fixture must include a tracked digest pinner")
        packet = self._packet(
            specialist="harness-optimizer",
            body=f"Update `{source}` to change a projected capability.",
            write_scope=(artifact, source),
        )
        live_stdout, live_stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(live_stdout), redirect_stderr(live_stderr):
            live_status = preflight.main(
                ["--repo-root", str(self.repo), "--packet", str(packet)]
            )
        dry_stdout, dry_stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(dry_stdout), redirect_stderr(dry_stderr):
            dry_status = preflight.main(
                [
                    "--repo-root",
                    str(self.repo),
                    "--packet",
                    str(packet),
                    "--authoring-warnings-only",
                ]
            )
        self.assertEqual((dry_status, live_status), (preflight.EXIT_PASS,) * 2)
        rendered_live = json.loads(live_stdout.getvalue())
        self.assertEqual(rendered_live["decision"], "allow")
        warning = next(
            item
            for item in rendered_live["warnings"]
            if item["code"] == preflight.COUPLED_HASH_PIN
        )
        self.assertEqual(warning["source_path"], source)
        self.assertEqual(warning["missing_count"], len(pinners))
        # `paths` is a deliberate SAMPLE -- dispatch_preflight caps it at
        # `missing[:10]` so a warning cannot dump 166 paths into a dispatch
        # envelope. `missing_count` above already pins the true total, so
        # comparing the sample to the full list asserted the cap did not exist.
        # It only passed while the fixture happened to have fewer than ten
        # pinners; regenerating the capability source took it to 166.
        self.assertEqual(warning["paths"], pinners[:10])
        self.assertLessEqual(len(warning["paths"]), 10)
        self.assertFalse(warning["blocking"])
        self.assertEqual(dry_stdout.getvalue(), "")
        for rendered in (dry_stderr.getvalue(), live_stderr.getvalue()):
            self.assertIn(f"[{preflight.COUPLED_HASH_PIN}]", rendered)
            self.assertIn(pinners[0], rendered)

    def test_same_shape_owned_by_harness_optimizer_is_clean(self) -> None:
        dispatch_log = self.repo / "_state" / "dispatch-log.jsonl"
        dispatch_log.write_text(
            "".join(
                json.dumps(
                    {
                        "specialist": "harness-optimizer",
                        "model_lane": "claude",
                    }
                )
                + "\n"
                for _ in range(60)
            ),
            encoding="utf-8",
        )
        packet = self._packet(specialist="harness-optimizer")
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "allow")
        self.assertEqual(verdict.warnings, ())
        route = next(
            item for item in verdict.informational if item["code"] == "route_resolution"
        )
        self.assertEqual(route["profile_id"], "claude.fable.xhigh")
        self.assertEqual(route["registry_model"], route["effective_model"])
        concentration = next(
            item
            for item in verdict.informational
            if item["code"] == "recent_dispatch_concentration"
        )
        self.assertEqual(concentration["gate"], "informational")
        self.assertEqual(concentration["window_records"], 50)
        self.assertEqual(concentration["selected_specialist_count"], 50)

    def test_verdict_binding_rejects_changed_packet_bytes(self) -> None:
        packet = self._packet(
            specialist="technical-writer", acknowledgement=True
        )
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertTrue(preflight.verdict_matches_packet(verdict, packet))
        packet.write_bytes(packet.read_bytes() + b"\nchanged after verdict\n")
        self.assertFalse(preflight.verdict_matches_packet(verdict, packet))
        rebound = preflight.evaluate_packet(self.repo, packet)
        self.assertNotEqual(verdict.packet_sha256, rebound.packet_sha256)
        self.assertNotEqual(verdict.ack_sha256, rebound.ack_sha256)

    def test_oversized_final_prompt_is_an_exact_refusal(self) -> None:
        packet = self._packet(
            specialist="harness-optimizer",
            body="x" * (preflight.context_builder.TRUSTED_LAUNCH_PROMPT_LIMIT + 1),
        )
        verdict = preflight.evaluate_packet(self.repo, packet)
        self.assertEqual(verdict.decision, "deny")
        self.assertIn("too large", verdict.refusals[0]["message"])

    def test_cli_emits_the_bound_warning_verdict_and_nonzero_status(self) -> None:
        packet = self._packet(specialist="technical-writer")
        output = io.StringIO()
        with redirect_stdout(output):
            status = preflight.main(
                ["--repo-root", str(self.repo), "--packet", str(packet)]
            )
        rendered = json.loads(output.getvalue())
        self.assertEqual(status, preflight.EXIT_ACK_REQUIRED)
        self.assertEqual(rendered["schema"], "dispatch-preflight/v1")
        self.assertEqual(rendered["decision"], "needs_ack")
        self.assertEqual(
            rendered["packet_sha256"], hashlib.sha256(packet.read_bytes()).hexdigest()
        )

    def test_send_task_invokes_preflight_before_single_host_admission(self) -> None:
        sender = (ROOT / "bin" / "send-task.sh").read_text(encoding="utf-8")
        host_admit = sender.split("board_host_admit() {", 1)[1].split("\n}", 1)[0]
        invocation = host_admit.index('python3 "$DISPATCH_PREFLIGHT"')
        packet_digest = host_admit.index('digest="$(shasum -a 256')
        binding = host_admit.index('[[ "$digest" == "$preflight_hash" ]]')
        host_policy = host_admit.index("host_admission.py")
        self.assertLess(invocation, packet_digest)
        self.assertLess(packet_digest, binding)
        self.assertLess(binding, host_policy)
        self.assertEqual(sender.count('python3 "$DISPATCH_PREFLIGHT"'), 1)

        common = sender.index('board_host_admit "$ACTUAL_TASK_FILE"')
        inbox_publish = sender.index("# ── copy to unified board inbox", common)
        self.assertLess(common, inbox_publish)
        self.assertEqual(sender.count('board_host_admit "$ACTUAL_TASK_FILE"'), 1)

        wrapper = (ROOT / "scripts" / "send-task.sh").read_text(encoding="utf-8")
        dry_advisory = wrapper.index("--authoring-warnings-only")
        hardened = wrapper.index('"${HARDENED_DISPATCH}" "${DISPATCH_ARGS[@]}"')
        self.assertLess(dry_advisory, hardened)
        advisory_block = wrapper[dry_advisory - 300 : dry_advisory + 100]
        self.assertIn('[[ "${DRY_RUN}" == "true"', advisory_block)
        self.assertIn("|| true", advisory_block)


class DispatchOverrideTests(unittest.TestCase):
    """Execute wrapper authoring and real guards without reaching board dispatch."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="dispatch-overrides-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        for relative in (
            "shared/lead-windows.sh",
            "shared/namespaces.sh",
            "shared/specialist-runtime-map.tsv",
            "shared/registries/profiles.tsv",
            "departments/coding/specialists/backend-engineer.md",
        ):
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        stub_bin = self.repo / "bin"
        stub_bin.mkdir()
        # The wrapper can only reach this stdout capture, never the real sender.
        dispatcher = stub_bin / "send-task.sh"
        dispatcher.write_text(
            '#!/bin/bash\nset -eu\nprintf "PACKET_BEGIN\\n"\n'
            'cat "$1"\nprintf "\\nPACKET_END\\n"\n', encoding="utf-8"
        )
        dispatcher.chmod(0o755)
        uuidgen = stub_bin / "uuidgen"
        uuidgen.write_text(
            "#!/bin/bash\nprintf '12345678-1234-1234-1234-123456789abc\\n'\n",
            encoding="utf-8",
        )
        uuidgen.chmod(0o755)
        # A cleaned staging directory cannot prove staging was never attempted.
        # Record mktemp entry separately so refusal tests measure that boundary.
        mktemp = stub_bin / "mktemp"
        mktemp.write_text(
            '#!/bin/bash\nprintf "staging\\n" >> "${VAULT_ROOT}/staging-attempted"\n'
            'exec /usr/bin/mktemp "$@"\n', encoding="utf-8",
        )
        mktemp.chmod(0o755)
        self.body = self.repo / "body.md"
        self.body.write_text("Repair the local service build.\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def _wrapper(
        self,
        *,
        to_model: str | None = "claude",
        review_model: str | None = None,
        reason: str | None = "Primary lane is unavailable.",
        triggers: str = "[architecture]",
        specialist: str = "backend-engineer",
        write_scope: str | None = None,
        delete_paths: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.repo / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "VAULT_ROOT": str(self.repo),
            "TMPDIR": str(self.repo),
            "PYTHONDONTWRITEBYTECODE": "1",
            "REVIEWS": "none",
            "REVIEW_TRIGGERS": triggers,
        }
        if review_model is not None:
            environment["REVIEW_MODEL"] = review_model
        if reason is not None:
            environment["MODEL_OVERRIDE_REASON"] = reason
        if write_scope is not None:
            environment["WRITE_SCOPE"] = write_scope
        if delete_paths is not None:
            environment["AUTHORIZED_DELETE_PATHS"] = delete_paths
        arguments = ["/bin/bash", str(ROOT / "scripts/send-task.sh"),
                     "coding", str(self.body), specialist]
        if to_model is not None:
            arguments.append(to_model)
        return subprocess.run(
            [*arguments, "--mode", "modeless"], env=environment,
            capture_output=True, text=True, timeout=10, check=False,
        )

    def _captured_packet(self, result: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.split("PACKET_BEGIN\n", 1)[1].split("\nPACKET_END", 1)[0]

    def _guards(self, fields: dict[str, str]) -> subprocess.CompletedProcess[str]:
        sender = (ROOT / "bin/send-task.sh").read_text(encoding="utf-8")
        review_start = sender.index('if [[ "$MANDATORY_REVIEW" == "true" ]]; then\n')
        review_end = sender.index('\nif [[ "$REVIEW_CLASS"', review_start)
        override_start = sender.index('    if [[ "$TO_MODEL" != "$MAP_MODEL" ]]; then\n')
        override_end = sender.index('    # A sanctioned override', override_start)
        # Keep the production conditions byte-for-byte. Only die is replaced:
        # exit 64 identifies a guard refusal without cleanup/registry effects.
        script = (
            'set -euo pipefail\ndie() { printf "%s\\n" "$*" >&2; exit 64; }\n'
            + sender[review_start:review_end]
            + "\n"
            + sender[override_start:override_end]
        )
        environment = {
            "PATH": f"{Path(sys.executable).parent}:{os.defpath}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "DISPATCH_PREFLIGHT": str(ROOT / "scripts/python/dispatch_preflight.py"),
            "SPECIALIST": "backend-engineer",
            "MAP_MODEL": "gpt-codex",
            "TO_MODEL": fields["to_model"],
            "REVIEW_MODEL": fields.get("review_model", "none"),
            "MANDATORY_REVIEW": fields.get("mandatory_review", "false"),
            # parse_task_frontmatter strips the raw scalar before this guard.
            "MODEL_OVERRIDE_REASON": fields.get("model_override_reason", "").strip(),
        }
        return subprocess.run(
            ["/bin/bash", "-c", script], env=environment, capture_output=True,
            text=True, timeout=10, check=False,
        )

    def _preflight(self, packet_text: str) -> tuple[int, dict[str, object]]:
        fields, _ = preflight.context_builder._parse_task_text(packet_text)
        # Emulate only the dispatcher's pure contract derivation, not admission.
        contract = derive_verification_contract({
            "task_id": fields["id"], "run_id": fields["run_id"],
            "mode": fields["mode"], "to_model": fields["to_model"],
            "result_type": "normal", "dispatch_kind": "single",
            "review_required": fields["mandatory_review"] == "true",
            "capability": None, "expected_gates": [],
        })
        encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
        packet_text = packet_text.replace(
            "\n---\n", f"\nverification_contract: {encoded}\n"
            f"verification_contract_sha256: {hashlib.sha256(encoded.encode()).hexdigest()}\n---\n",
            1,
        )
        packet = self.repo / "prepared.md"
        packet.write_text(packet_text, encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            status = preflight.main(["--repo-root", str(self.repo), "--packet", str(packet)])
        return status, json.loads(output.getvalue())

    def _reason_packet(self, reason: str | None, *, primary: bool = False) -> str:
        reason_line = "" if reason is None else f"model_override_reason: {reason}\n"
        model = "gpt-codex" if primary else "claude"
        return (
            "---\nid: TASK-2026-09-08-1625-control\nrun_id: none\nmode: modeless\n"
            f"to_model: {model}\nspecialist: backend-engineer\nsource_namespace: coding\n"
            "return_artifact: result.md\nwrite_scope: [result.md]\n"
            "direct_lane_work_allowed: false\nmandatory_review: false\n"
            "review_model: none\nreview_triggers: []\nreviews: none\n"
            f"{reason_line}---\n\nRepair the local service build.\n"
        )

    def test_placeholder_reason_is_refused_by_shell_guard(self) -> None:
        for reason in ("none", "NONE", " none ", '"none"', "' None '"):
            with self.subTest(reason=reason):
                fields, _ = preflight.context_builder._parse_task_text(self._reason_packet(reason))
                result = self._guards(fields)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("model_override_reason", result.stderr)

    def _assert_wrapper_single_line(self, parameter: str, variable: str) -> None:
        ordinary = {
            "specialist": "backend-engineer",
            "to_model": "claude",
            "reason": "Primary lane is unavailable.",
            "write_scope": "scripts/send-task.sh, scripts/python/dispatch_preflight.py",
            "delete_paths": '"scripts/legacy-a.sh", "scripts/legacy-b.sh"',
            "triggers": "[architecture]",
        }
        # Inert formatting inputs only: no added frontmatter keys or dispatch.
        # splitlines has ten individual characters plus the CRLF sequence.
        # Include both edges: checking only the number of splitlines would miss
        # a trailing separator, because splitlines drops the final empty line.
        for separator in ("\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d",
                          "\x1e", "\x85", "\u2028", "\u2029"):
            for value in (separator + ordinary[parameter],
                          ordinary[parameter] + separator,
                          ordinary[parameter] + separator + "continued"):
                with self.subTest(variable=variable, value=value):
                    arguments = {**ordinary, parameter: value}
                    result = self._wrapper(review_model="gpt-codex", **arguments)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(variable, result.stdout + result.stderr)
                    self.assertIn("single-line", result.stdout + result.stderr)
                    self.assertNotIn("PACKET_BEGIN", result.stdout)
                    self.assertFalse((self.repo / "staging-attempted").exists())
                    self.assertEqual(list(self.repo.glob("squad-task.*")), [])

        # Same fixture and inputs, now single-line: retain the caller's bytes.
        packet = self._captured_packet(self._wrapper(review_model="gpt-codex", **ordinary))
        fields, _ = preflight.context_builder._parse_task_text(packet)
        self.assertTrue((self.repo / "staging-attempted").exists())
        self.assertEqual(len(fields), 29)
        self.assertEqual(fields["specialist"], ordinary["specialist"])
        self.assertEqual(fields["to_model"], ordinary["to_model"])
        self.assertEqual(fields["review_triggers"], ordinary["triggers"])
        self.assertEqual(fields["model_override_reason"], ordinary["reason"])
        self.assertEqual(fields["write_scope"],
                         f'[{fields["return_artifact"]}, {ordinary["write_scope"]}]')
        self.assertEqual(fields["authorized_delete_paths"], f'[{ordinary["delete_paths"]}]')
        self.assertEqual(
            preflight.context_builder.parse_scope(fields["write_scope"], field="write_scope"),
            (fields["return_artifact"], "scripts/send-task.sh", "scripts/python/dispatch_preflight.py"),
        )
        self.assertEqual(json.loads(fields["authorized_delete_paths"]),
                         ["scripts/legacy-a.sh", "scripts/legacy-b.sh"])
        self.assertEqual(self._guards(fields).returncode, 0)
        self.assertEqual(self._preflight(packet)[0], 0)

    def test_wrapper_specialist_requires_single_line(self) -> None:
        self._assert_wrapper_single_line("specialist", "SPECIALIST")

    def test_wrapper_model_requires_single_line(self) -> None:
        self._assert_wrapper_single_line("to_model", "TO_MODEL")

    def test_wrapper_override_reason_requires_single_line(self) -> None:
        self._assert_wrapper_single_line("reason", "MODEL_OVERRIDE_REASON")

    def test_wrapper_write_scope_requires_single_line(self) -> None:
        self._assert_wrapper_single_line("write_scope", "WRITE_SCOPE")

    def test_wrapper_delete_paths_require_single_line(self) -> None:
        self._assert_wrapper_single_line("delete_paths", "AUTHORIZED_DELETE_PATHS")

    def test_wrapper_review_triggers_require_single_line(self) -> None:
        self._assert_wrapper_single_line("triggers", "REVIEW_TRIGGERS")

    def test_wrapper_specialist_without_explicit_model_rejects_before_lookup(self) -> None:
        result = self._wrapper(to_model=None, specialist="backend-engineer\ncontinued")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SPECIALIST must be single-line", result.stdout + result.stderr)
        self.assertFalse((self.repo / "staging-attempted").exists())

        packet = self._captured_packet(self._wrapper(to_model=None, reason=None))
        fields, _ = preflight.context_builder._parse_task_text(packet)
        self.assertEqual(len(fields), 29)
        self.assertEqual(fields["specialist"], "backend-engineer")
        self.assertEqual(fields["to_model"], "gpt-codex")

    def test_wrapper_preserves_single_line_spacing_and_unicode(self) -> None:
        for reason in ("Primary lane is unavailable.", "Quota\tlimit", "Primary\u00a0busy",
                       "不可用", "занято", r"Literal \n text"):
            with self.subTest(reason=reason):
                packet = self._captured_packet(self._wrapper(review_model="gpt-codex", reason=reason))
                fields, _ = preflight.context_builder._parse_task_text(packet)
                self.assertEqual(len(fields), 29)
                self.assertEqual(fields["model_override_reason"], reason)

    NON_EXPLANATORY_REASONS = (
        "...", "—?!", '"..."', "[ ]", "⚠️", "nоnе", "попе", "ΝΟΝΕ",
        "ｎｏｎｅ", "𝗻𝗼𝗻𝗲", "' n.o.n.e '", "no\u200bne", "n\u0301one",
    )

    def test_non_explanatory_reason_is_refused_by_shell_guard(self) -> None:
        for reason in self.NON_EXPLANATORY_REASONS:
            with self.subTest(reason=reason):
                fields, _ = preflight.context_builder._parse_task_text(self._reason_packet(reason))
                result = self._guards(fields)
                self.assertEqual(result.returncode, 64, result.stderr)
                self.assertIn("model_override_reason", result.stderr)

    def test_non_explanatory_reason_is_refused_by_preflight(self) -> None:
        for reason in self.NON_EXPLANATORY_REASONS:
            with self.subTest(reason=reason):
                status, verdict = self._preflight(self._reason_packet(reason))
                self.assertEqual(status, 3, verdict)
                self.assertEqual(verdict["decision"], "deny")
                self.assertIn("model_override_reason", verdict["refusals"][0]["message"])

    def test_route_report_uses_packet_reviewer(self) -> None:
        for reviewer, triggers, expected in (
            ("gpt-codex", "[architecture]", "codex"),
            ("codex", "[architecture]", "codex"),
            ("gemini", "[architecture]", "gemini"),
            ("gpt-codex", "[]", "none"),
        ):
            with self.subTest(reviewer=reviewer, triggers=triggers):
                packet = self._captured_packet(self._wrapper(review_model=reviewer, triggers=triggers))
                status, verdict = self._preflight(packet)
                self.assertEqual(status, 0, verdict)
                route = next(item for item in verdict["informational"] if item["code"] == "route_resolution")
                self.assertEqual(route["review_lane"], expected)

    def _tier_packet(self, tier: str | None) -> str:
        relative = "departments/security/specialists/experimental-attacker.md"
        destination = self.repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
        packet = self._reason_packet("Operator requested the mapped review profile.")
        packet = packet.replace("specialist: backend-engineer", "specialist: experimental-attacker")
        packet = packet.replace("source_namespace: coding", "source_namespace: security")
        if tier is not None:
            packet = packet.replace("to_model: claude\n", f"to_model: claude\nroute_tier: {tier}\n")
        return packet

    def test_review_tier_reports_the_model_selected_for_launch(self) -> None:
        for tier in ("review", '"review"'):
            with self.subTest(tier=tier):
                packet = self._tier_packet(tier)
                status, verdict = self._preflight(packet)
                self.assertEqual(status, 0, verdict)
                self.assertEqual(verdict["decision"], "allow")
                route = next(item for item in verdict["informational"] if item["code"] == "route_resolution")
                self.assertEqual(route["profile_id"], "claude.opus5.max")
                self.assertEqual(route["effective_model"], "claude-opus-5")
                self.assertEqual(route["registry_model"], route["effective_model"])
                fields, _ = preflight.context_builder._parse_task_text(packet)
                launch_args = preflight.context_builder.trusted_lane_args_for(
                    self.repo, lane="claude", specialist="experimental-attacker",
                    route_tier=preflight.context_builder.packet_route_tier(fields),
                )
                self.assertEqual(launch_args[launch_args.index("--model") + 1], route["effective_model"])

    def test_codex_tier_reports_the_model_selected_for_launch(self) -> None:
        relative = "departments/coding/specialists/devops-engineer.md"
        shutil.copy2(ROOT / relative, self.repo / relative)
        for tier, profile, model in (
            ("primary", "codex.astra.max", "gpt-6-astra"),
            ("escalate", "codex.sol.ultra", "gpt-5.6-sol"),
        ):
            with self.subTest(tier=tier):
                packet = self._reason_packet(None, primary=True).replace(
                    "specialist: backend-engineer", "specialist: devops-engineer"
                ).replace("to_model: gpt-codex\n", f"to_model: gpt-codex\nroute_tier: {tier}\n")
                status, verdict = self._preflight(packet)
                self.assertEqual(status, 0, verdict)
                route = next(item for item in verdict["informational"] if item["code"] == "route_resolution")
                self.assertEqual(route["profile_id"], profile)
                self.assertEqual(route["effective_model"], model)
                self.assertEqual(route["registry_model"], model)
                fields, _ = preflight.context_builder._parse_task_text(packet)
                launch_args = preflight.context_builder.trusted_lane_args_for(
                    self.repo, lane="codex", specialist="devops-engineer",
                    route_tier=preflight.context_builder.packet_route_tier(fields),
                )
                self.assertEqual(launch_args[launch_args.index("--model") + 1], model)

    def test_omitted_tier_preserves_legacy_route_report(self) -> None:
        status, verdict = self._preflight(self._tier_packet(None))
        self.assertEqual(status, 0, verdict)
        self.assertEqual(verdict["decision"], "allow")
        route = next(item for item in verdict["informational"] if item["code"] == "route_resolution")
        self.assertEqual(route, {
            "code": "route_resolution", "gate": "required",
            "profile_id": "claude.fable.max", "registry_model": "claude-fable-5",
            "effective_model": "claude-fable-5", "review_lane": "none",
            "model_override": True,
        })

    def test_invalid_or_wrong_lane_tier_is_refused(self) -> None:
        for tier in ("", '""', "none", "unknown", "primary"):
            with self.subTest(tier=tier):
                status, verdict = self._preflight(self._tier_packet(tier))
                self.assertEqual(status, 3, verdict)
                self.assertEqual(verdict["decision"], "deny")
                self.assertIn("route_tier", verdict["refusals"][0]["message"])

    def test_review_tier_does_not_bypass_override_reason_refusal(self) -> None:
        packet = self._tier_packet("review").replace(
            "model_override_reason: Operator requested the mapped review profile.",
            "model_override_reason: none",
        )
        status, verdict = self._preflight(packet)
        self.assertEqual(status, 3, verdict)
        self.assertEqual(verdict["decision"], "deny")
        self.assertIn("model_override_reason", verdict["refusals"][0]["message"])

    def test_placeholder_reason_is_refused_by_preflight(self) -> None:
        for reason in ("none", "NONE", " none ", '"none"', "' None '"):
            with self.subTest(reason=reason):
                status, verdict = self._preflight(self._reason_packet(reason))
                self.assertEqual(status, 3, verdict)
                self.assertEqual(verdict["decision"], "deny")
                self.assertIn("model_override_reason", verdict["refusals"][0]["message"])

    def test_empty_reason_stays_refused(self) -> None:
        for reason in (None, "", "   ", '""', "' '"):
            with self.subTest(reason=reason):
                packet = self._reason_packet(reason)
                fields, _ = preflight.context_builder._parse_task_text(packet)
                self.assertEqual(self._guards(fields).returncode, 64)
                self.assertEqual(self._preflight(packet)[0], 3)

    def test_explained_override_remains_allowed(self) -> None:
        for reason in ("Primary lane is unavailable.", '"Operator requested a second perspective."',
                       "none of the primary workers are available", "OOM", "IO", "quota",
                       "429 quota", "不可用", "занято"):
            with self.subTest(reason=reason):
                packet = self._reason_packet(reason)
                fields, _ = preflight.context_builder._parse_task_text(packet)
                self.assertEqual(self._guards(fields).returncode, 0)
                status, verdict = self._preflight(packet)
                self.assertEqual(status, 0, verdict)
                route = next(item for item in verdict["informational"] if item["code"] == "route_resolution")
                self.assertTrue(route["model_override"])

    def test_primary_route_does_not_require_reason(self) -> None:
        for reason in (None, "", "none"):
            with self.subTest(reason=reason):
                packet = self._reason_packet(reason, primary=True)
                fields, _ = preflight.context_builder._parse_task_text(packet)
                self.assertEqual(self._guards(fields).returncode, 0)
                self.assertEqual(self._preflight(packet)[0], 0)

    def test_wrapper_explicit_reviewer_makes_override_dispatchable(self) -> None:
        for reviewer in ("gpt-codex", "codex"):
            with self.subTest(reviewer=reviewer):
                packet = self._captured_packet(self._wrapper(review_model=reviewer))
                fields, _ = preflight.context_builder._parse_task_text(packet)
                self.assertEqual(fields["to_model"], "claude")
                self.assertEqual(fields["review_model"], "gpt-codex")
                self.assertEqual(fields["mandatory_review"], "true")
                self.assertEqual(fields["review_triggers"], "[architecture]")
                self.assertNotEqual(author_family_for_lane(fields["to_model"]),
                                    author_family_for_lane(fields["review_model"]))
                guard = self._guards(fields)
                self.assertEqual(guard.returncode, 0, guard.stderr)
                status, verdict = self._preflight(packet)
                self.assertEqual(status, 0, verdict)

    def test_wrapper_refuses_default_same_family_before_emission(self) -> None:
        result = self._wrapper()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cross-family", result.stdout + result.stderr)
        self.assertIn("REVIEW_MODEL", result.stdout + result.stderr)
        self.assertNotIn("PACKET_BEGIN", result.stdout)
        self.assertEqual(list(self.repo.glob("squad-task.*")), [])

    def test_wrapper_refuses_no_mapped_reviewer_before_emission(self) -> None:
        path = self.repo / "shared/specialist-runtime-map.tsv"
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            rows, fieldnames = list(reader), reader.fieldnames
        for row in rows:
            if row["specialist"] == "backend-engineer":
                row["review_lane"] = "none"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        result = self._wrapper(to_model=None)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("cross-family", result.stdout + result.stderr)
        self.assertNotIn("PACKET_BEGIN", result.stdout)
        # Same missing-map fixture, now with an explicit independent reviewer.
        packet = self._captured_packet(self._wrapper(to_model=None, review_model="claude"))
        fields, _ = preflight.context_builder._parse_task_text(packet)
        self.assertEqual(self._guards(fields).returncode, 0)
        self.assertEqual(self._preflight(packet)[0], 0)

    def test_wrapper_refuses_same_family_explicit_reviewer(self) -> None:
        for author, reviewer in (("claude", "claude"), ("codex", "gpt-codex"),
                                 ("gpt-codex", "codex"), ("gpt-codex", "gpt-codex")):
            with self.subTest(author=author, reviewer=reviewer):
                result = self._wrapper(to_model=author, review_model=reviewer)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("cross-family", result.stdout + result.stderr)
                self.assertNotIn("PACKET_BEGIN", result.stdout)

    def test_wrapper_refuses_unknown_or_absent_explicit_reviewer(self) -> None:
        for reviewer in ("unknown", "none"):
            with self.subTest(reviewer=reviewer):
                result = self._wrapper(review_model=reviewer)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("review", (result.stdout + result.stderr).lower())
                self.assertNotIn("PACKET_BEGIN", result.stdout)

    def test_wrapper_primary_and_unreviewed_defaults_unchanged(self) -> None:
        for triggers, reviewer in (("[architecture]", "claude"), ("[]", "none")):
            with self.subTest(triggers=triggers):
                packet = self._captured_packet(self._wrapper(to_model=None, reason=None, triggers=triggers))
                fields, _ = preflight.context_builder._parse_task_text(packet)
                self.assertEqual(fields["to_model"], "gpt-codex")
                self.assertEqual(fields["review_model"], reviewer)
                self.assertEqual(fields["model_override_reason"], "none")
                self.assertEqual(self._guards(fields).returncode, 0)
                self.assertEqual(self._preflight(packet)[0], 0)
        packet = self._captured_packet(self._wrapper(review_model="gpt-codex", triggers="[]"))
        fields, _ = preflight.context_builder._parse_task_text(packet)
        self.assertEqual(fields["mandatory_review"], "false")
        self.assertEqual(fields["review_model"], "none")
        self.assertEqual(self._guards(fields).returncode, 0)

        # Supplying a reviewer cannot make the wrapper's default reason valid.
        packet = self._captured_packet(self._wrapper(review_model="gpt-codex", reason=None))
        fields, _ = preflight.context_builder._parse_task_text(packet)
        self.assertEqual(fields["model_override_reason"], "none")
        self.assertEqual(self._guards(fields).returncode, 64)
        self.assertEqual(self._preflight(packet)[0], 3)


if __name__ == "__main__":
    unittest.main()
