"""Demand-side retirement checks, with real registry/document fixtures."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/python"))
import validate_capabilities as capabilities  # noqa: E402


class CapabilityRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.name = "fixture-retired"
        self.retired_path = f"shared/skills/_retired/{self.name}.md"
        self.rows = [self.row(self.retired_path, "authored-pattern-doc", "no")]
        self.write(self.retired_path, "# Historical methodology\n")
        self.write("shared/specialist-runtime-map.tsv", "specialist\n")
        self.write("shared/skills/catalog.txt", "# Empty fixture catalogue\n")
        self.write_source([])
        self.write_registry()

    def row(self, path: str, kind: str, state: str) -> dict[str, str]:
        return {
            "name": self.name,
            "record_kind": "skill",
            "type": kind,
            "path_or_source": path,
            "lanes": "all",
            "verified_state": state,
            "cost_tier": "none",
        }

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_registry(self) -> None:
        path = self.root / capabilities.REGISTRY_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.rows[0]), delimiter="\t")
            writer.writeheader()
            writer.writerows(self.rows)

    def write_source(self, skills: list[dict[str, str]]) -> Path:
        return self.write(
            capabilities.SPECIALIST_CAPABILITY_SOURCE_RELATIVE.as_posix(),
            json.dumps({
                "schema": "specialist-lane-capabilities/v1", "version": 1,
                "entries": [{
                    "specialist": "fixture-worker", "lane": "gpt-codex",
                    "coverage": "full", "skills": skills,
                }],
            }),
        )

    def card(self, skill: str = "—") -> str:
        return f"""---
id: project/control
mode: project
title: Retirement control
capability_state: live
state_reason: Fixture with no tools.
state_evidence: Local fixture.
overlays: []
gates: []
cost_note: —
---
| Step | Specialists | Tools `(lane · state · cost_tier)` | Skills `(type)` | Gate / Overlay |
|---|---|---|---|---|
| **S0** Intake | `Chrono` | — | — | — |
| **S3** Work | `Chrono` | — | {skill} | — |
| **S7** Capture | `Chrono` | — | — | — |
"""

    def validate_card(self, skill: str) -> dict[str, object]:
        return capabilities.Validator(self.root).validate_text(self.card(skill), "<control>", None)

    def public_card(self, skill: str = "—") -> str:
        # Match the exported shape: no private state/cost fields or typed tuples.
        return f"""---
id: project/control
mode: project
title: Published retirement control
overlays: []
gates: []
---
| Step | Specialists | Tools `` | Skills `(type)` | Gate / Overlay |
|---|---|---|---|---|
| **S0** Intake | `Chrono` | — | — | — |
| **S3** Work | `Chrono` | — | {skill} | — |
| **S7** Capture | `Chrono` | — | — | — |
"""

    def test_each_retired_registry_type_fails_at_the_demand(self) -> None:
        for kind, label in capabilities.SKILL_LABELS.items():
            with self.subTest(kind=kind):
                self.rows[0]["type"] = kind
                self.write_registry()
                result = self.validate_card(f"`{self.name}` ({label})")
                self.assertEqual(result["status"], "fail")
                self.assertEqual([error["code"] for error in result["errors"]], ["skill-retired"])

    def test_missing_retired_file_does_not_hide_retirement(self) -> None:
        self.rows[0]["path_or_source"] = "shared/skills/_retired/nonexistent.md"
        self.write_registry()
        result = self.validate_card(f"`{self.name}` (authored)")
        self.assertEqual([error["code"] for error in result["errors"]], ["skill-retired"])

    def test_archive_row_without_current_demand_is_allowed(self) -> None:
        self.write("docs/archive/old.md", f"Use `{self.name}`.\n")
        self.write("shared/capabilities/public/project/control.md", self.public_card())
        self.write("shared/capabilities/project/control.md", self.card())
        validator = capabilities.Validator(self.root)
        self.assertEqual(validator.validate_catalog_registry()["status"], "pass")
        demand = validator.validate_skill_demand()
        self.assertEqual(demand["status"], "pass")
        self.assertEqual(demand["retired_identifier_count"], 0)
        self.assertEqual(demand["scanned_files"], 3)

    def test_default_cli_checks_published_demands_without_private_schema(self) -> None:
        self.write("shared/capabilities/project/control.md", self.card())
        sites = (
            "shared/capabilities/public/project/control.md",
            "shared/capabilities/public/bounty/nested/control.md",
        )
        for path in sites:
            self.write(path, self.public_card())
        command = [sys.executable, "-B", str(ROOT / "scripts/python/validate_capabilities.py"), "--root", str(self.root)]

        def run() -> tuple[int, list[dict[str, object]]]:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.stderr, "")
            return result.returncode, [json.loads(line) for line in result.stdout.splitlines()]

        clean_exit, clean = run()
        self.assertEqual(clean_exit, 0, clean)
        private = [row for row in clean if row["type"] == "capability"]
        self.assertEqual(len(private), 1)
        self.assertEqual(private[0]["status"], "pass")
        for path in sites:
            with self.subTest(path=path):
                broken = self.public_card(f"`{self.name}`")
                self.write(path, broken)
                broken_exit, results = run()
                self.assertEqual(broken_exit, 1, results)
                self.assertEqual([row for row in results if row["type"] == "capability"], private)
                demand = next(row for row in results if row["type"] == "skill-demand")
                self.assertEqual(demand["status"], "fail")
                self.assertEqual(demand["demand_count"], 1)
                self.assertEqual(demand["demand_identifiers"], [self.name])
                self.assertEqual(demand["scanned_files"], 4)
                self.assertEqual([error["code"] for error in demand["errors"]], ["skill-retired"])
                line = next(i for i, text in enumerate(broken.splitlines(), 1) if text.startswith("| **S3**"))
                self.assertEqual(demand["errors"][0]["references"], [{"file": path, "line": line}])
                self.write(path, self.public_card())
                restored_exit, restored = run()
                self.assertEqual(restored_exit, 0, restored)
                self.assertEqual(restored, clean)

    def test_published_mentions_remain_visible_beside_a_fresh_demand(self) -> None:
        path = "shared/capabilities/public/project/control.md"
        mention = f"\nThis historical document mentions `{self.name}`.\n"
        self.write(path, self.public_card() + mention)
        validator = capabilities.Validator(self.root)
        result = validator.validate_skill_demand()
        self.assertEqual((result["status"], result["demand_count"], result["mention_count"]), ("pass", 0, 1))
        self.assertEqual(result["mentions"][0]["references"], [{
            "file": path, "line": 14, "reason": "descriptive-reference",
        }])
        self.write(path, self.public_card(f"`{self.name}`") + mention)
        result = validator.validate_skill_demand()
        self.assertEqual((result["status"], result["demand_count"], result["mention_count"]), ("fail", 1, 1))
        self.assertEqual(result["errors"][0]["references"], [{"file": path, "line": 11}])
        # The same identifier can also be a demand in published prose.
        self.write(path, self.public_card() + mention + f"Use `{self.name}`.\n")
        result = validator.validate_skill_demand()
        self.assertEqual((result["status"], result["demand_count"], result["mention_count"]), ("fail", 1, 1))
        self.assertEqual(result["errors"][0]["references"], [{"file": path, "line": 15}])

    def test_archive_symlink_cannot_turn_a_retired_path_into_a_successor(self) -> None:
        live_path = self.write("shared/skills/current.md", "Current document.\n")
        archived_alias = self.root / "shared/skills/_retired/alias.md"
        archived_alias.symlink_to(live_path)
        self.rows[0]["path_or_source"] = archived_alias.relative_to(self.root).as_posix()
        self.rows[0]["verified_state"] = "authored"
        self.write_registry()
        self.assertEqual(self.validate_card(f"`{self.name}` (authored)")["status"], "fail")

    def test_real_live_successor_allows_retained_archive_row(self) -> None:
        live_path = f"shared/skills/{self.name}/SKILL.md"
        self.write(live_path, f"---\nname: {self.name}\n---\nLive successor.\n")
        self.rows.append(self.row(live_path, "invokable", "yes"))
        self.write_registry()
        for label in ("authored", "SKILL.md"):
            with self.subTest(label=label):
                self.assertEqual(self.validate_card(f"`{self.name}` ({label})")["status"], "pass")

    def test_missing_or_unverified_successor_does_not_clear_retirement(self) -> None:
        self.rows.append(self.row("shared/skills/missing/SKILL.md", "invokable", "yes"))
        self.write_registry()
        self.assertEqual(self.validate_card(f"`{self.name}` (authored)")["status"], "fail")
        self.write("shared/skills/missing/SKILL.md", "A file alone is not a live row.\n")
        self.rows[-1]["verified_state"] = "no"
        self.write_registry()
        self.assertEqual(self.validate_card(f"`{self.name}` (authored)")["status"], "fail")

    def test_nonretired_stub_and_unregistered_untyped_backlog_still_allowed(self) -> None:
        self.rows[0] = self.row("shared/skills/backlog.md", "pattern-doc-stub", "no")
        self.write_registry()
        self.assertEqual(self.validate_card(f"`{self.name}` (stub)")["status"], "pass")
        self.assertEqual(self.validate_card("`unregistered-backlog` (untyped)")["status"], "pass")

    def test_individual_card_prose_is_checked_with_exact_identifier_boundaries(self) -> None:
        validator = capabilities.Validator(self.root)
        longer = self.card() + f"\n`{self.name}-successor` and `prefix-{self.name}`.\n"
        self.assertEqual(validator.validate_text(longer, "<control>", None)["status"], "pass")
        result = validator.validate_text(longer + f"Use `{self.name}`.\n", "<control>", None)
        self.assertEqual([error["code"] for error in result["errors"]], ["skill-retired"])

    def test_default_cli_includes_profiles_modes_and_specialist_prose(self) -> None:
        self.write("shared/capabilities/project/control.md", self.card())
        sites = (
            "shared/specialists/reviewer.md",
            "departments/coding/specialists/worker.md",
            "shared/modes/project.md",
            "shared/mode-profiles/project/fixture.md",
        )
        for path in sites:
            self.write(path, f"Use `{self.name}` and `{self.name}`.\n")
        command = [sys.executable, "-B", str(ROOT / "scripts/python/validate_capabilities.py"), "--root", str(self.root)]
        negative = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(negative.returncode, 1, negative.stdout + negative.stderr)
        demand = next(json.loads(line) for line in negative.stdout.splitlines() if json.loads(line)["type"] == "skill-demand")
        self.assertEqual(demand["retired_identifier_count"], 1)
        self.assertEqual(demand["retired_identifiers"], [self.name])
        self.assertEqual(demand["scanned_files"], 6)
        self.assertEqual(demand["errors"][0]["references"], [{"file": path, "line": 1} for path in sorted(sites)])
        for path in sites:
            self.write(path, "Use the current methodology.\n")
        positive = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(positive.returncode, 0, positive.stdout + positive.stderr)
        self.assertEqual(json.loads(positive.stdout.splitlines()[-1])["status"], "pass")

    def test_evidence_and_reason_fields_are_mentions_but_skill_cells_are_demands(self) -> None:
        for field in ("state_evidence", "state_reason"):
            for value in (f"The `{self.name}` methodology.", f"|\n  The `{self.name}` methodology."):
                with self.subTest(field=field, value=value):
                    text = self.card().replace(
                        "state_evidence: Local fixture." if field == "state_evidence"
                        else "state_reason: Fixture with no tools.",
                        f"{field}: {value}",
                    )
                    validator = capabilities.Validator(self.root)
                    result = validator.validate_text(text, "<mention>", None)
                    self.assertFalse(any(e["code"] == "skill-retired" for e in result["errors"]))
                    self.assertEqual([r["name"] for r in result["retired_skill_mentions"]], [self.name])
                    demanded = text.replace(
                        "| **S3** Work | `Chrono` | — | — |",
                        f"| **S3** Work | `Chrono` | — | `{self.name}` (authored) |",
                    )
                    result = validator.validate_text(demanded, "<demand>", None)
                    self.assertEqual(sum(e["code"] == "skill-retired" for e in result["errors"]), 1)

    def test_review_mention_shapes_and_same_identifier_demand_controls(self) -> None:
        # Synthetic identities exercise the review's grammar without an id/path allowlist.
        prose = (
            f"No build tool exists (only untyped `{self.name}` skills, not a toolchain).",
            f"The `{self.name}` entries are untyped skill docs, not tooling.",
            f"MCP reachability uses the lane shell + the\n`{self.name}` methodology.",
            f"This historical document mentions `{self.name}`.",
        )
        validator = capabilities.Validator(self.root)
        for mention in prose:
            with self.subTest(mention=mention):
                text = self.card() + "\n" + mention + "\n"
                self.write("shared/capabilities/project/control.md", text)
                result = validator.validate_skill_demand()
                self.assertEqual((result["status"], result["demand_count"], result["mention_count"]), ("pass", 0, 1))
                self.assertEqual(validator.validate_text(text, "<mention>", None)["status"], "pass")
                demanded = text + f"\nUse `{self.name}` methodology to complete the work.\n"
                self.write("shared/capabilities/project/control.md", demanded)
                result = validator.validate_skill_demand()
                self.assertEqual((result["status"], result["demand_count"], result["mention_count"]), ("fail", 1, 1))
                self.assertEqual(validator.validate_text(demanded, "<demand>", None)["status"], "fail")

    def test_negation_is_local_to_the_occurrence_and_cannot_mute_a_demand(self) -> None:
        texts = (
            f"Do not use `{self.name}`. Use `{self.name}`.",
            f"Never invoke `{self.name}`; invoke `{self.name}`.",
            f"`{self.name}` is not required, but use `{self.name}`.",
            f"Use `{self.name}`; do not use `{self.name}`.",
            f"Do not use `{self.name}`.\n\n## Required skills\n- `{self.name}`\n",
        )
        validator = capabilities.Validator(self.root)
        for text in texts:
            with self.subTest(text=text):
                refs = capabilities.retired_skill_references(text, validator.retired_only_skills())
                self.assertEqual(sorted(ref.kind for ref in refs), ["demand", "mention"])
                result = validator.validate_text(self.card() + text, "<mixed>", None)
                self.assertEqual(sum(e["code"] == "skill-retired" for e in result["errors"]), 1)

    def test_workflow_and_conditional_dependencies_remain_demands(self) -> None:
        texts = (
            f"Use\n`{self.name}` when the toolchain is available.",
            f"The workflow depends on `{self.name}`.",
            f"`{self.name}` is required for dispatch.",
            f"The protocol relies on `{self.name}`.",
            f"Findings are gated by `{self.name}` before delivery.",
            f"Correctness is validated against a reference\n(`{self.name}`) once tools are available.",
            f"The skills wired at S3 (`{self.name}`) cover the target.",
            f"## Evidence gate\n- My verification is the first leg of `{self.name}`.",
            f"- `{self.name}` (skill — required scaffolding)",
            f"Use [the methodology](shared/skills/{self.name}.md).",
        )
        for text in texts:
            with self.subTest(text=text):
                result = capabilities.Validator(self.root).validate_text(self.card() + text, "<demand>", None)
                self.assertEqual(sum(e["code"] == "skill-retired" for e in result["errors"]), 1)

    def test_gate_negation_does_not_hide_the_skills_cell(self) -> None:
        text = self.card(f"`{self.name}` (authored)").replace(
            f"`{self.name}` (authored) | — |",
            f"`{self.name}` (authored) | no live tooling; do not use unknown tools |",
        )
        result = capabilities.Validator(self.root).validate_text(text, "<demand>", None)
        self.assertEqual([e["code"] for e in result["errors"]], ["skill-retired"])

    def test_gate_can_prohibit_a_skill_while_the_skills_cell_still_demands_it(self) -> None:
        text = self.card().replace(
            "| **S3** Work | `Chrono` | — | — | — |",
            f"| **S3** Work | `Chrono` | — | — | Do not use `{self.name}` |",
        )
        validator = capabilities.Validator(self.root)
        result = validator.validate_text(text, "<prohibited>", None)
        self.assertEqual(result["status"], "pass")
        self.assertEqual([ref["name"] for ref in result["retired_skill_mentions"]], [self.name])
        text = text.replace(
            "| **S3** Work | `Chrono` | — | — |",
            f"| **S3** Work | `Chrono` | — | `{self.name}` (authored) |",
        )
        result = validator.validate_text(text, "<contradictory>", None)
        self.assertEqual([e["code"] for e in result["errors"]], ["skill-retired"])

    def test_historical_lists_and_explicit_denials_remain_visible_as_mentions(self) -> None:
        texts = (
            f"## Historical references\n- `{self.name}` methodology.\n",
            f"- `{self.name}` was retired.\n",
            f"- `{self.name}` is unavailable.\n",
            f"No `{self.name}` is required.\n",
            f"The workflow works without `{self.name}`.\n",
        )
        validator = capabilities.Validator(self.root)
        for text in texts:
            with self.subTest(text=text):
                refs = capabilities.retired_skill_references(text, validator.retired_only_skills())
                self.assertEqual([ref.kind for ref in refs], ["mention"])
                # A history heading does not exempt an instruction below it.
                refs = capabilities.retired_skill_references(text + f"Use `{self.name}`.\n", validator.retired_only_skills())
                self.assertEqual([ref.kind for ref in refs], ["mention", "demand"])

    def test_missing_source_fails_until_a_real_source_is_present(self) -> None:
        root = self.root / "missing-source-control"
        for relative in (capabilities.REGISTRY_RELATIVE, Path("shared/specialist-runtime-map.tsv")):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.root / relative).read_bytes())
        validator = capabilities.Validator(root)
        result = validator.validate_skill_demand()
        self.assertEqual(result["status"], "fail")
        self.assertEqual([e["code"] for e in result["errors"]], ["capability-source"])
        target = root / capabilities.SPECIALIST_CAPABILITY_SOURCE_RELATIVE
        target.parent.mkdir(parents=True)
        target.write_bytes((self.root / capabilities.SPECIALIST_CAPABILITY_SOURCE_RELATIVE).read_bytes())
        self.assertEqual(validator.validate_skill_demand()["status"], "pass")

    def test_source_stub_is_a_demand_and_superseded_is_a_mention(self) -> None:
        command = [sys.executable, "-B", str(ROOT / "scripts/python/validate_capabilities.py"), "--root", str(self.root)]
        for availability, evidence, expected_exit, counts in (
            ("authored:stub", "shared-skills:stub", 1, (1, 0)),
            ("superseded", "superseded", 0, (0, 1)),
            ("available", "installed-or-shared-authored", 1, (1, 0)),
            ("superseded", "shared-skills:stub", 1, (1, 0)),
        ):
            with self.subTest(availability=availability):
                self.write_source([{"id": self.name, "requirement": "preferred", "availability": availability, "evidence": evidence}])
                run = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(run.returncode, expected_exit, run.stdout + run.stderr)
                result = next(json.loads(line) for line in run.stdout.splitlines() if json.loads(line)["type"] == "skill-demand")
                self.assertEqual((result["demand_count"], result["mention_count"]), counts)
                self.assertEqual(result["retired_identifiers"], [self.name])
                groups = result["errors"] if expected_exit else result["mentions"]
                location = groups[0]["references"][0]
                self.assertEqual(location["json_path"], "$.entries[0].skills[0].id")
                self.assertEqual(location["specialist"], "fixture-worker")
                self.assertEqual(location["lane"], "gpt-codex")

    def test_source_rejection_is_not_a_silent_clear(self) -> None:
        for invalid in ("{", "[]", '{"schema":"wrong","entries":[]}'):
            with self.subTest(invalid=invalid):
                self.write(capabilities.SPECIALIST_CAPABILITY_SOURCE_RELATIVE.as_posix(), invalid)
                result = capabilities.Validator(self.root).validate_skill_demand()
                self.assertEqual(result["status"], "fail")
                self.assertEqual([e["code"] for e in result["errors"]], ["capability-source"])
        # Restore a valid source as the control; no malformed input is exempted.
        self.write_source([])
        self.assertEqual(capabilities.Validator(self.root).validate_skill_demand()["status"], "pass")


if __name__ == "__main__":
    unittest.main()
