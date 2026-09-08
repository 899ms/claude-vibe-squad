"""Local privacy boundary tests; no provider calls or live stores.

CHRONO_PRIVACY_TEST_ROOT selects a scratch plugin for fault-injection controls.
The sentinel patterns test transformation contracts without credential bypasses.
"""
from __future__ import annotations

import json
import contextlib
import io
import os
import re
import sys
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(os.environ.get("CHRONO_PRIVACY_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(PLUGIN))
import autocapture
import jsonl
import notes
import privacy
import recall
import index as vault_index


class PrivacyBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="privacy-boundary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / ".chrono-vault").write_text(json.dumps({"vault_id": "privacy-test", "schema_version": 1}))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CHRONO_", "VAULT_"))}
        env.update(CHRONO_VAULT_ROOT=str(self.vault), CHRONO_VAULT_AUDIT_DIR=str(self.root / "audit"),
                   CHRONO_AUTOCAPTURE_DISTILL="on", CHRONO_AUTOCAPTURE_DISTILL_RESTRICTED="on")
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.enterContext(mock.patch.object(autocapture, "REPO_ROOT", self.root))

    def sentinel(self, pattern="CaptureSentinel"):
        self.enterContext(mock.patch.object(privacy, "_PATTERNS", (re.compile(pattern),)))

    def response(self, **fields):
        outbox = self.root / "departments/coding/outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / "TASK-privacy-control-response.md"
        metadata = {"specialist": "backend-engineer", "status": "complete", "mode": "build", **fields}
        path.write_text("---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in metadata.items()) +
                        "\n---\n\nCaptureControl preserves useful retry behavior and reusable failure context for later readers.\n")
        return path

    def recipient(self):
        return mock.Mock(return_value={"title": "CaptureControl", "body": "Useful retry context.",
                                      "aliases": [], "keywords": [], "attack_class": "none"})

    def rows(self):
        paths = list((self.root / "_state/episodic").glob("*.jsonl"))
        self.assertTrue(paths, "missing spool is not a pass")
        return [row for path in paths for row in jsonl.read_objects(path)]

    def test_slug_screens_complete_transform_before_bound(self):
        with mock.patch.object(autocapture, "redact_text", wraps=privacy.redact_text) as screen:
            self.assertEqual(autocapture._slug("ordinary value", "fallback"), "ordinary-value")
        self.assertIn(mock.call("ordinary-value"), screen.call_args_list)
        self.sentinel(r"ordinary-value")
        self.assertEqual(autocapture._slug("ordinary value", "fallback"), "REDACTED")
        self.assertEqual(autocapture._slug("", "ordinary-value"), "[REDACTED]")

    def test_line_cleanup_retains_previous_control_fix(self):
        self.sentinel()
        self.assertEqual(privacy.redact_text("CaptureSentinel"), "[REDACTED]")
        self.assertEqual(autocapture._clean_one_line(privacy.redact_text("Capture\x01Sentinel")), "[REDACTED]")
        self.assertEqual(autocapture._clean_one_line("ordinary\twords"), "ordinary words")

    def test_summary_screens_join_and_path_normalization_before_bound(self):
        self.sentinel(r"Artifacts: safe/file|Heading\n\nBody")
        self.assertEqual(autocapture._bounded_summary("", "", ["safe\\file"]), "[REDACTED]")
        self.assertEqual(autocapture._bounded_summary("Body", "Heading", []), "[REDACTED]")

    def test_jsonl_boundary_minimizes_values_and_preserves_shape(self):
        self.sentinel()
        path = self.root / "row.jsonl"
        jsonl.append_line(path, {"value": ["CaptureSentinel"], "count": 3})
        self.assertEqual(jsonl.read_objects(path), [{"value": ["[REDACTED]"], "count": 3}])
        print('jsonl: {"count": 3, "value": ["[REDACTED]"]}')

    def test_json_serialization_guard_can_reject(self):
        self.sentinel(r'"left": "right"')
        with self.assertRaisesRegex(ValueError, "privacy boundary"):
            privacy.screened_json({"left": "right"})
        self.assertEqual(privacy.screened_json({"left": "ordinary"}), '{"left": "ordinary"}')

    def test_json_escape_matches_keep_word_separation(self):
        self.sentinel(r"nSuffix")
        serialized = privacy.screened_json({"body": "Useful prefix\nSuffix"}, ensure_ascii=False)
        self.assertEqual(json.loads(serialized), {"body": "Useful prefix Suffix"})
        self.assertEqual(privacy.require_screened(serialized), serialized)

    def test_recall_screens_marker_removal_and_quote_format(self):
        self.sentinel(r"CaptureSentinel|> ordinary")
        snippet = recall._quoted_snippet("Capture\x01Sentinel\x02\nordinary")
        self.assertNotIn("CaptureSentinel", snippet)
        self.assertNotIn("> ordinary", snippet)
        self.assertIn("[REDACTED]", snippet)

    def test_index_screens_final_list_joins(self):
        self.sentinel(r"ordinary\njoined")
        result = notes.record("learning", {"title": "Ordinary", "body": "Useful context.",
                                          "aliases": ["ordinary", "joined"], "keywords": ["ordinary", "joined"]})
        parsed = vault_index._parse_note(Path(result["path"]))
        self.assertEqual(parsed["aliases_text"], "ordinary\njoined", "join positive control")
        with contextlib.closing(sqlite3.connect(":memory:")) as connection:
            vault_index._initialize(connection)
            vault_index._upsert_connection(connection, parsed)
            row = connection.execute("SELECT aliases, keywords FROM notes_fts").fetchone()
        self.assertEqual(row, ("[REDACTED]", "[REDACTED]"))
        print("index joined fields: aliases=REDACTED; keywords=REDACTED")

    def test_cli_serialization_and_stderr_are_screened(self):
        self.sentinel()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(autocapture, "capture_response", return_value={
            "captured": False, "note_id": None, "reason": "distillation_failed:CaptureSentinel"
        }), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(autocapture.main(["TASK-control-response.md"]), 1)
        self.assertNotIn("CaptureSentinel", stdout.getvalue() + stderr.getvalue())
        self.assertIn("[REDACTED]", stdout.getvalue())
        self.assertIn("[REDACTED]", stderr.getvalue())

    def test_markdown_serialization_guard_can_reject(self):
        self.sentinel(r'title: "Ordinary"')
        note = notes._normalize("learning", {"title": "Ordinary", "body": "Useful content."})
        with self.assertRaisesRegex(ValueError, "privacy boundary"):
            notes._serialize(note)
        note["title"] = "Different"
        self.assertIn(b'title: "Different"', notes._serialize(note))

    def test_distiller_prompt_screens_final_format_and_input_before_bound(self):
        self.sentinel(r'role="ordinary"|CaptureSentinel')
        prompt = autocapture._distill_prompt({"body": "CaptureSentinel"},
                                            {"role": "ordinary", "mode": "build", "namespace": "coding"})
        self.assertNotIn('role="ordinary"', prompt)
        self.assertNotIn("CaptureSentinel", prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertEqual(privacy.require_screened(prompt), prompt)
        print("distiller prompt: final format screened; input screened")

    def test_distilled_fields_screen_before_truncation(self):
        self.sentinel("CaptureSentinel" + "z" * 1600)
        value = "CaptureSentinel" + "z" * 1600
        result = autocapture._normalize_distilled({"title": value, "body": value, "aliases": [value],
                                                  "keywords": [value], "attack_class": "none"})
        self.assertEqual(result["title"], "[REDACTED]")
        self.assertEqual(result["body"], "[REDACTED]")
        self.assertEqual(result["aliases"], ["[REDACTED]"])
        self.assertEqual(result["keywords"], ["[REDACTED]"])

    def test_external_process_receives_screened_prompt(self):
        self.sentinel()
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.recipient().return_value), "")
        with mock.patch.object(autocapture, "_distill_model_id", return_value="synthetic-model"), \
             mock.patch.object(autocapture, "_lane_executable", return_value=Path("/synthetic/agy")), \
             mock.patch.object(autocapture.subprocess, "run", return_value=completed) as launch:
            autocapture.distill({"body": "CaptureSentinel"},
                                {"role": "ordinary", "mode": "CaptureSentinel", "namespace": "coding"})
        launch.assert_called_once()
        prompt = launch.call_args.args[0][-1]
        self.assertNotIn("CaptureSentinel", prompt)
        self.assertIn("[REDACTED]", prompt)
        self.assertEqual(privacy.require_screened(prompt), prompt)
        print("external process argv: prompt screened; provider execution mocked")

    def test_relationship_formatting_is_screened_before_hash_and_write(self):
        self.sentinel("coexists-condition:ordinary")
        try:
            result = notes.record("learning", {"title": "Ordinary", "body": "Useful context.",
                "contradiction": {"relationship": "coexists-with", "note_id": "mem-123456abcdef", "condition": "ordinary"}})
        except ValueError:
            self.fail("relationship could not be safely persisted")
        content = Path(result["path"]).read_text()
        self.assertNotIn("coexists-condition:ordinary", content)
        self.assertIn("[REDACTED]", content)

    def test_failure_log_and_exception_text_are_screened(self):
        self.sentinel()
        autocapture._record_write_path_failure("CaptureSentinel", "CaptureSentinel")
        row = jsonl.read_objects(self.root / "_state/autocapture-failures.jsonl")[0]
        self.assertEqual(row["reason"], "[REDACTED]")
        self.assertEqual(row["response_path"], "[REDACTED]")

    def test_intact_format_controls_reach_both_checked_sinks(self):
        formats = {"anthropic": "sk-ant-api03-" + "B" * 48,
                   "slack": "xoxb-" + "1234567890-" * 2 + "I" * 24}
        for label, value in formats.items():
            self.assertIn("[REDACTED]", privacy.redact_text(value), label)
            for field in ("target", "status", "mode"):
                with self.subTest(format=label, field=field):
                    recipient = self.recipient()
                    path = self.response(**{field: value})
                    # Keep the real pipeline, spool, and note writer; only provider execution is replaced.
                    result = autocapture.capture_response(str(path), distiller=recipient)
                    self.assertEqual(result["reason"], "captured")
                    recipient.assert_called_once()
                    row = self.rows()[-1]
                    self.assertNotIn(value, json.dumps([row, recipient.call_args.args]))
                    self.assertIn("REDACTED", row[field])
                    print(f"intact {label}/{field}: captured; spool=REDACTED; recipient=screened")

    def test_faulty_upstream_transform_is_contained_at_both_sinks(self):
        self.sentinel()
        original = autocapture._slug
        for field in ("target", "status", "mode", "specialist"):
            recipient = self.recipient()
            path = self.response(**{field: "ordinary"})
            with mock.patch.object(autocapture, "_slug", side_effect=lambda value, fallback:
                                   "CaptureSentinel" if value == "ordinary" else original(value, fallback)):
                result = autocapture.capture_response(str(path), distiller=recipient)
            self.assertEqual(result["reason"], "captured", result)
            recipient.assert_called_once()
            self.assertNotIn("CaptureSentinel", json.dumps([self.rows()[-1], recipient.call_args.args]))
            print(f"fault injection/{field}: captured; spool=screened; recipient=screened")


if __name__ == "__main__":
    unittest.main()
