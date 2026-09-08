from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import index as vault_index  # noqa: E402
import autocapture  # noqa: E402
import jsonl  # noqa: E402
import lifecycle  # noqa: E402
import notes  # noqa: E402
import privacy  # noqa: E402
import recall as vault_recall  # noqa: E402


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vault_root = Path(
            os.path.realpath(tempfile.mkdtemp(prefix="chrono-lifecycle-test-"))
        )
        self.addCleanup(shutil.rmtree, self.vault_root, ignore_errors=True)
        (self.vault_root / ".chrono-vault").write_text(
            json.dumps({"vault_id": "lifecycle-test", "schema_version": 1}),
            encoding="utf-8",
        )
        # These fixtures are standalone temporary vaults, not the host engagement.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CHRONO_", "VAULT_"))}
        env.update(CHRONO_VAULT_ROOT=str(self.vault_root),
                   CHRONO_VAULT_AUDIT_DIR=str(self.vault_root / "audit"))
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        # A throwaway repo root, separate from the vault, so tests that flag a
        # note to the curation queue never write into this checkout's real
        # `_state/curation-queue.jsonl`.
        self.repo_root = Path(
            os.path.realpath(tempfile.mkdtemp(prefix="chrono-lifecycle-repo-"))
        )
        self.addCleanup(shutil.rmtree, self.repo_root, ignore_errors=True)

    def _record(self, token: str, *, status: str = "candidate") -> dict:
        return notes.record(
            "finding",
            {
                "title": f"{token} lifecycle finding",
                "body": f"The {token} body is canonical markdown.",
                "target": "example-chain",
                "component": "executor",
                "attack_class": "lifecycle",
                "status": status,
                "keywords": ["lifecycle"],
                "source_task": "TASK-lifecycle-fixture",
            },
        )

    @property
    def db_path(self) -> Path:
        return self.vault_root / "index" / "kg.db"

    def test_get_note_returns_full_frontmatter_and_body(self) -> None:
        recorded = self._record("GetNoteToken", status="verified")

        note = lifecycle.get_note(recorded["id"])

        self.assertEqual(note["id"], recorded["id"])
        self.assertEqual(note["type"], "finding")
        self.assertEqual(note["status"], "verified")
        self.assertEqual(note["revision"], 1)
        self.assertEqual(note["keywords"], ["lifecycle"])
        self.assertEqual(note["body"], "The GetNoteToken body is canonical markdown.\n")

    def _legacy_contact(self, token: str, *, status: str = "candidate") -> dict:
        recorded = self._record(token, status=status)
        path = Path(recorded["path"])
        original = path.read_text(encoding="utf-8")
        body = "The upstream maintainer is reachable at maintainer@example.com and confirmed the retry semantics.\n"
        path.write_text(original.replace(f"The {token} body is canonical markdown.\n", body), encoding="utf-8")
        legacy = lifecycle.get_note(recorded["id"])
        self.assertEqual(legacy["body"], body, "legacy fixture must reach disk unscreened")
        with self.assertRaisesRegex(ValueError, "privacy boundary"):
            notes._serialize(legacy)
        return recorded

    def _assert_screened_note(self, note_id: str) -> dict:
        stored = lifecycle.get_note(note_id)
        self.assertEqual(stored["body"], "The upstream maintainer is reachable at [REDACTED] and confirmed the retry semantics.\n")
        content_refs = list(stored["evidence_refs"])
        notes._refresh_content_ref(stored)
        self.assertEqual(stored["evidence_refs"], content_refs, "content reference must describe screened content")
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT body FROM notes_fts JOIN meta ON notes_fts.rowid=meta.docid WHERE meta.id=?",
                (note_id,),
            ).fetchone()
        self.assertIsNotNone(row, "missing index row is not a pass")
        self.assertEqual(row[0], stored["body"])
        self.assertEqual(privacy.require_screened(notes._serialize(stored).decode()), notes._serialize(stored).decode())
        return stored

    def test_legacy_email_status_update_screens_before_serialization(self) -> None:
        recorded = self._legacy_contact("LegacyContactToken")
        try:
            result = lifecycle.set_status(recorded["id"], "verified", "reviewed evidence", expected_revision=1)
        except lifecycle.LifecycleError as exc:
            self.fail(f"legacy email transition rejected: {exc}")
        stored = self._assert_screened_note(recorded["id"])
        self.assertEqual(stored["status"], "verified")
        self.assertEqual(stored["revision"], 2)
        self.assertTrue(stored["verified_at"])
        self.assertEqual({key: result[key] for key in stored}, stored)
        print("legacy email: status=verified; revision=2; body=screened; index=screened; return=screened; content_ref=valid")

    def test_legacy_email_supersession_screens_both_notes(self) -> None:
        original = self._legacy_contact("LegacyOriginalToken")
        replacement = self._legacy_contact("LegacyReplacementToken", status="verified")
        lifecycle.set_status(original["id"], "superseded", "reviewed replacement", expected_revision=1, supersedes=replacement["id"])
        old = self._assert_screened_note(original["id"])
        new = self._assert_screened_note(replacement["id"])
        self.assertEqual(old["superseded_by"], replacement["id"])
        self.assertIn(original["id"], new["supersedes"])
        self.assertEqual((old["revision"], new["revision"]), (2, 2))

    def test_legacy_email_publish_failure_restores_original_bytes(self) -> None:
        original = self._legacy_contact("LegacyRollbackToken")
        replacement = self._legacy_contact("LegacyRollbackReplacementToken", status="verified")
        before = {Path(note["path"]): Path(note["path"]).read_bytes() for note in (original, replacement)}
        real_publish = lifecycle._publish_stage
        calls = []

        def fail_second_publish(stage):
            calls.append(stage["path"])
            if len(calls) == 2:
                raise OSError("simulated second publish failure")
            real_publish(stage)

        with mock.patch.object(lifecycle, "_publish_stage", side_effect=fail_second_publish):
            with self.assertRaisesRegex(lifecycle.LifecycleError, "canonical notes restored"):
                lifecycle.set_status(original["id"], "superseded", "must roll back", expected_revision=1, supersedes=replacement["id"])
        self.assertEqual(len(calls), 2, "must reach publication to exercise rollback")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def _assert_screened_surface(self, label: str, text: str) -> None:
        with self.subTest(surface=label):
            self.assertTrue(text, f"{label}: empty surface is unmeasured")
            self.assertTrue(privacy.redact_text(text) == text, f"{label}: detectable content remains")
            print(f"{label}: bytes={len(text.encode('utf-8'))}; screened=yes")

    def test_status_preserves_clean_markdown_bytes(self) -> None:
        bodies = [
            "# Notes\n\nConnect to redis://cache:6379\n@ops-team owns it.\n\n"
            "- first item\n- second item\n\n```sh\ncat /etc/hosts\n```\n",
            "# Notes\n\nSee the writeup.\n@team.example.com owns triage.\n\n"
            "- café\n\n```text\n  keep\tindentation\n```\n\n",
            "# T\n\n- a\n\t- indented with a tab\n\n```\n\tliteral\ttabs\n```\n",
            "# CRLF\r\n\r\nline one\r\nline two\r\n",
            "# Trail\n\nbody text\n\n\n\n",
            "# UTF\n\nemoji 🔐 and café and 中文\n\n- item\n",
            "\n\n# Late heading\n\ntext\n",
            "# X\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n<div>\n  raw html\n</div>\n",
            "# Y\n\n```yaml\nkey: value\nlist:\n  - one\n```\n",
        ]
        for number, body in enumerate(bodies):
            with self.subTest(body=body):
                self.assertEqual(privacy.redact_text(body), body, "raw text is clean")
                encoded = json.dumps(body)
                if number < 2:
                    self.assertNotEqual(privacy.redact_text(encoded), encoded, "JSON spelling positive control")
                recorded = self._record("MarkdownControl")
                path = Path(recorded["path"])
                header, _ = path.read_bytes().split(b"\n---\n", 1)
                path.write_bytes(header + b"\n---\n" + body.encode("utf-8"))
                before = path.read_bytes().split(b"\n---\n", 1)[1]
                self.assertEqual(before, body.encode("utf-8"), "fixture reached disk intact")

                result = lifecycle.set_status(recorded["id"], "verified", "format control", expected_revision=1)

                after = path.read_bytes().split(b"\n---\n", 1)[1]
                print(f"body_before={before!r}\nbody_after ={after!r}\nBODY_BYTE_IDENTICAL={after == before}")
                self.assertEqual(after, before)
                self.assertEqual(result["body"].encode("utf-8"), before)
                self.assertEqual((result["status"], result["revision"]), ("verified", 2))
                stored = lifecycle.get_note(recorded["id"])
                refs = list(stored["evidence_refs"])
                notes._refresh_content_ref(stored)
                self.assertEqual(stored["evidence_refs"], refs)
                with closing(sqlite3.connect(self.db_path)) as connection:
                    row = connection.execute(
                        "SELECT body FROM notes_fts JOIN meta ON notes_fts.rowid=meta.docid WHERE meta.id=?",
                        (recorded["id"],),
                    ).fetchone()
                self.assertIsNotNone(row, "missing index row is not a pass")
                self.assertEqual(row[0].encode("utf-8"), before)

    def test_legacy_frontmatter_escape_spelling_preserves_value(self) -> None:
        value = "redis://cache:6379\n@ops-team"
        self.assertEqual(privacy.redact_text(value), value, "decoded value is clean")
        encoded = json.dumps([value])
        with self.assertRaisesRegex(ValueError, "privacy boundary"):
            privacy.require_screened(encoded)
        recorded = self._record("EscapeSpelling")
        path = Path(recorded["path"])
        original = path.read_text()
        path.write_text(original.replace('keywords: ["lifecycle"]', f"keywords: {encoded}"))
        before = lifecycle.get_note(recorded["id"])["keywords"][0].encode("utf-8")
        self.assertEqual(before, value.encode("utf-8"), "fixture reaches disk intact")
        print(f"value_before={before!r}")
        try:
            result = lifecycle.set_status(recorded["id"], "verified", "escape spelling control", expected_revision=1)
        except lifecycle.LifecycleError as exc:
            print(f"set_status=FAILED; error={exc}; cause={exc.__cause__!r}")
            self.assertEqual(path.read_text(), original.replace('keywords: ["lifecycle"]', f"keywords: {encoded}"))
            raise
        stored = lifecycle.get_note(recorded["id"])
        after = stored["keywords"][0].encode("utf-8")
        print(f"value_after ={after!r}\nVALUE_BYTE_IDENTICAL={after == before}; status={stored['status']}; revision={stored['revision']}")
        print(next(line for line in path.read_text().splitlines() if line.startswith("keywords: ")))
        self.assertEqual(after, before)
        self.assertEqual(result["keywords"], stored["keywords"])
        self.assertEqual((stored["status"], stored["revision"]), ("verified", 2))
        self.assertEqual(privacy.require_screened(path.read_text()), path.read_text())
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute("SELECT keywords FROM notes_fts JOIN meta ON notes_fts.rowid=meta.docid WHERE meta.id=?", (recorded["id"],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0].encode("utf-8"), before)
        refs = list(stored["evidence_refs"])
        notes._refresh_content_ref(stored)
        self.assertEqual(stored["evidence_refs"], refs)

    def test_frontmatter_spelling_checks_decoded_and_encoded_values(self) -> None:
        values = [
            "redis://cache:6379\n@ops-team", "redis://cache:6379\r\n@ops-team",
            "redis://cache:6379\t@ops-team", "\n@team.example.com",
            "redis://cache:6379\n@ops-team 😀 café 中文",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(privacy.redact_text(value), value)
                with self.assertRaisesRegex(ValueError, "privacy boundary"):
                    privacy.require_screened(json.dumps(value, ensure_ascii=False))
                for item in (value, [value, "ordinary"]):
                    encoded = notes._frontmatter_json(item)
                    self.assertEqual(json.loads(encoded), item)
                    self.assertEqual(privacy.require_screened(encoded), encoded)
        for value in ("alpha\nbeta", 'quote " and backslash \\', "😀 café 中文", None, 1, []):
            with self.subTest(clean=value):
                self.assertEqual(notes._frontmatter_json(value), json.dumps(value, ensure_ascii=False))
        secret = "sk-ant-api03-" + "B" * 48
        self.assertNotEqual(privacy.redact_text(secret), secret, "detector positive control")
        for value in (secret, [secret], "redis://cache:6379\n@ops-team " + secret):
            with self.subTest(secret_shape=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "privacy boundary"):
                    notes._frontmatter_json(value)

    def test_legacy_escape_spelling_all_transitions_and_supersession(self) -> None:
        value = "redis://cache:6379\n@ops-team 😀"
        for status in sorted(notes.STATUSES):
            with self.subTest(status=status):
                original = self._record("EscapeTransitions")
                replacement = self._record("EscapeReplacement", status="verified")
                fields = {"program": value, "component": value, "aliases": [value],
                          "keywords": [value], "evidence_refs": [value]}
                for recorded in (original, replacement):
                    path = Path(recorded["path"])
                    lines = path.read_text().splitlines(keepends=True)
                    path.write_text("".join(
                        f"{line.partition(': ')[0]}: {json.dumps(fields[line.partition(': ')[0]])}\n"
                        if line.partition(': ')[0] in fields else line for line in lines
                    ))
                    parsed = lifecycle.get_note(recorded["id"])
                    for field, expected in fields.items():
                        self.assertEqual(parsed[field], expected, "legacy fixture reached parser")
                options = {"supersedes": replacement["id"]} if status == "superseded" else {}
                result = lifecycle.set_status(original["id"], status, "escape transition", expected_revision=1, **options)
                self.assertEqual((result["status"], result["revision"]), (status, 2))
                changed = (original, replacement) if status == "superseded" else (original,)
                for recorded in changed:
                    stored = lifecycle.get_note(recorded["id"])
                    for field, expected in fields.items():
                        actual = stored[field][:1] if field == "evidence_refs" else stored[field]
                        self.assertEqual(actual, expected)
                    self.assertEqual(stored["revision"], 2)
                    content = Path(recorded["path"]).read_text()
                    self.assertEqual(privacy.require_screened(content), content)
                    refs = list(stored["evidence_refs"])
                    notes._refresh_content_ref(stored)
                    self.assertEqual(stored["evidence_refs"], refs)

    def test_cross_delimiter_reconstructions_stay_closed(self) -> None:
        seeds = [
            "https://user:password@host/p", "redis://cache:637901@ops-team", "Bearer " + "A" * 40,
            "ops@example.com", "?api_key=" + "A" * 30, "sk-" + "A" * 30,
            "sk-ant-" + "A" * 30, "sk-proj-" + "A" * 30, "xai-" + "A" * 30,
            "pplx-" + "A" * 30, "AIza" + "A" * 35, "ghp_" + "A" * 30,
            "github_pat_" + "A" * 30, "AKIA" + "A" * 16, "ASIA" + "A" * 16,
            "xoxb-" + "A" * 30, "eyJ" + "A" * 10 + "." + "B" * 10 + "." + "C" * 10,
            "hf_" + "A" * 30, "sk_live_" + "A" * 20, "apify_api_" + "A" * 30,
        ]
        for seed in seeds:
            self.assertNotEqual(privacy.redact_text(seed), seed, "seed positive control")
        self.assertTrue(all(any(pattern.search(seed) for seed in seeds) for pattern in privacy._PATTERNS),
                        "every production pattern has a positive control")
        pairs = [(seed[:i], seed[i:]) for seed in seeds for i in range(1, len(seed))
                 if all(privacy.redact_text(value) == value for value in (seed[:i], seed[i:]))]
        old_matches = 0
        for left, right in pairs:
            compact = json.dumps([left, right], separators=(",", ":"))
            old_matches += privacy.redact_text(compact) != compact
            for values in ([left, right], ["ordinary", left, right]):
                note = notes._normalize("learning", {"title": "Join control", "body": "Useful context.\n", "keywords": values})
                content = notes._serialize(note).decode()
                self.assertEqual(privacy.require_screened(content), content)
                line = next(line for line in content.splitlines() if line.startswith("keywords: "))
                self.assertEqual(json.loads(line.partition(": ")[2]), values)
        self.assertEqual(old_matches, 27, "compact separator is the known-broken control")
        print(f"join controls: seeds={len(seeds)}; patterns={len(privacy._PATTERNS)}; clean_pairs={len(pairs)}; compact_reconstructions={old_matches}; spaced_reconstructions=0")

    def test_clean_frontmatter_lists_survive_status_and_record(self) -> None:
        values = ["redis://cache:6379", "@ops-team"]
        self.assertEqual(privacy.redact_fields(values), values, "individual values are clean")
        compact = json.dumps(values, separators=(",", ":"))
        with self.assertRaisesRegex(ValueError, "privacy boundary"):
            privacy.require_screened(compact)
        spaced = json.dumps(values, separators=(", ", ": "))
        self.assertEqual(privacy.require_screened(spaced), spaced)
        print(f"compact_positive_control={compact!r}; spaced_control={spaced!r}")
        for field in ("keywords", "aliases", "evidence_refs"):
            with self.subTest(field=field):
                recorded = self._record("ListControl")
                path = Path(recorded["path"])
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                path.write_text("".join(f"{field}: {compact}\n" if line.startswith(f"{field}: ") else line
                                        for line in lines), encoding="utf-8")
                self.assertEqual(lifecycle.get_note(recorded["id"])[field], values, "legacy fixture control")
                result = lifecycle.set_status(recorded["id"], "verified", "list control", expected_revision=1)
                stored = lifecycle.get_note(recorded["id"])
                self.assertEqual(stored[field][:len(values)], values)
                self.assertEqual(result[field], stored[field])
                self.assertEqual((stored["status"], stored["revision"]), ("verified", 2))
                self.assertIn(spaced[1:-1], path.read_text(encoding="utf-8"))
                print(f"legacy {field}: before={values!r}; after={stored[field][:len(values)]!r}; status=verified; revision=2")

                fresh = notes.record("learning", {"title": "List control", "body": "Useful context.\n", field: values})
                self.assertEqual(lifecycle.get_note(fresh["id"])[field][:len(values)], values)
                self.assertTrue(fresh["indexed"])
                print(f"record {field}: values preserved; indexed=True")

    def test_existing_capture_credential_closures(self) -> None:
        formats = {
            "anthropic": "sk-ant-api03-" + "B" * 48,
            "slack": "xoxb-" + "1234567890-" * 2 + "I" * 24,
        }
        for label, value in formats.items():
            self.assertTrue(privacy.redact_text(value) != value, f"{label}: detector positive control")
            encoded = json.dumps({"value": value})
            self.assertTrue(privacy.redact_text(encoded) != encoded, f"{label}: JSON positive control")
            broken = value.replace("-", " ")
            self.assertTrue(privacy.redact_text(broken) == broken, f"{label}: pre-transform control")
        print("key controls: both intact formats detected; JSON detected; broken forms undetected")
        cases = [
            (f"{label}/{field}", {field: value.replace("-", " ")}, "", [])
            for label, value in formats.items() for field in ("specialist", "target")
        ]
        # A relative prefix reaches artifact serialization; a leading scheme
        # would be rejected even with screening disabled, making this vacuous.
        artifact = "docs/https:" + "\\" * 2 + "user:password@localhost/path"
        normalized = artifact.replace("\\", "/")
        self.assertTrue(privacy.redact_text(artifact) == artifact, "path pre-transform control")
        self.assertTrue(privacy.redact_text(normalized) != normalized, "path normalization positive control")
        cases.extend([
            ("control-byte/body", {}, formats["anthropic"].replace("sk-", "sk-\x01"), []),
            ("path-normalization/artifacts", {}, "", [artifact]),
        ])
        outbox = self.repo_root / "departments/coding/outbox"
        outbox.mkdir(parents=True)
        with mock.patch.object(autocapture, "REPO_ROOT", self.repo_root), mock.patch.dict(
            os.environ, {"CHRONO_AUTOCAPTURE_DISTILL": "on", "CHRONO_AUTOCAPTURE_DISTILL_RESTRICTED": "on"}
        ):
            for number, (label, fields, extra_body, artifacts) in enumerate(cases):
                with self.subTest(case=label):
                    metadata = {"specialist": "backend-engineer", "target": "retry-component", "status": "complete", "mode": "build", "artifacts": artifacts, **fields}
                    path = outbox / f"TASK-closure-{number}-response.md"
                    body = "Retry handling preserves useful failure context and records the confirmed behavior for future maintenance. " * 4
                    path.write_text("---\n" + "\n".join(f"{key}: {json.dumps(value)}" for key, value in metadata.items()) + "\n---\n\n" + body + extra_body, encoding="utf-8")
                    recipient = mock.Mock(return_value={"title": "Retry handling", "body": body, "aliases": [], "keywords": [], "attack_class": "none"})
                    result = autocapture.capture_response(str(path), distiller=recipient)
                    self.assertEqual(result["reason"], "captured")
                    recipient.assert_called_once()
                    spool_paths = list((self.repo_root / "_state/episodic").glob("*.jsonl"))
                    self.assertTrue(spool_paths, "missing spool is unmeasured")
                    rows = [line for item in spool_paths for line in item.read_text().splitlines()
                            if json.loads(line)["response_path"] == str(path)]
                    self.assertEqual(len(rows), 1, "must inspect this case's spool row")
                    capture_fields, context = recipient.call_args.args
                    note_paths = list((self.vault_root / "notes").glob(f"*/{result['note_id']}.md"))
                    self.assertEqual(len(note_paths), 1, "missing note is unmeasured")
                    surfaces = {"spool": rows[0], "capture_fields": json.dumps(capture_fields),
                                "prompt": autocapture._distill_prompt(capture_fields, context),
                                "note": note_paths[0].read_text()}
                    # Only count sinks this fixture reaches with screening disabled.
                    # Targets are not sent to the distiller; artifact lines are
                    # removed from its input; the injected distiller replaces body.
                    measured = ("spool", "capture_fields", "prompt", "note")
                    if label.endswith("/target"):
                        measured = ("spool", "note")
                    elif label == "control-byte/body":
                        measured = ("spool", "capture_fields", "prompt")
                    elif label == "path-normalization/artifacts":
                        measured = ("spool",)
                    for surface in measured:
                        self._assert_screened_surface(f"{label}/{surface}", surfaces[surface])

    def test_existing_additional_privacy_closures(self) -> None:
        key = "sk-ant-api03-" + "B" * 48
        raw_json = json.dumps({"body": key})
        self.assertTrue(privacy.redact_text(raw_json) != raw_json, "L1 positive control")
        spool = self.repo_root / "closure.jsonl"
        jsonl.append_line(spool, {"body": key})
        self._assert_screened_surface("L1/jsonl", spool.read_text())

        aliases = ["Bearer", "sk" + "K" * 40]
        self.assertTrue(all(privacy.redact_text(value) == value for value in aliases), "L2 individual controls")
        self.assertTrue(privacy.redact_text("\n".join(aliases)) != "\n".join(aliases), "L2 joined positive control")
        result = notes.record("learning", {"title": "Alias control", "body": "Useful context.", "aliases": aliases})
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute("SELECT aliases FROM notes_fts JOIN meta ON notes_fts.rowid=meta.docid WHERE meta.id=?", (result["id"],)).fetchone()
        self.assertIsNotNone(row, "L2 missing index row is unmeasured")
        self._assert_screened_surface("L2/index aliases", row[0])

        google = "AIza" + "G" * 35
        self.assertTrue(privacy.redact_text(google) != google, "bounded-key positive control")
        self.assertTrue(privacy.redact_text(google + "G") == google + "G", "overlong negative control")
        for label, bound, transform in (
            ("L3/slug", 120, lambda value: autocapture._slug(value, "fallback")),
            ("L4/summary", 1500, lambda value: autocapture._bounded_summary(value, "", [])),
        ):
            value = "a" * (bound - len(google) - 1) + "-" + google + "G"
            self.assertTrue(privacy.redact_text(value) == value, f"{label} pre-bound control")
            self.assertTrue(privacy.redact_text(value[:bound]) != value[:bound], f"{label} truncation positive control")
            self._assert_screened_surface(label, transform(value))
        self._assert_screened_surface("L5/recall snippet", vault_recall._quoted_snippet(key.replace("sk-", "sk-\x01")))

    def test_stale_revision_and_unknown_status_are_rejected_without_change(self) -> None:
        recorded = self._record("CasToken")

        with self.assertRaises(lifecycle.RevisionConflict):
            lifecycle.set_status(
                recorded["id"],
                "verified",
                "reviewed evidence",
                expected_revision=2,
            )
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.set_status(
                recorded["id"],
                "unknown",
                "invalid transition",
                expected_revision=1,
            )

        unchanged = lifecycle.get_note(recorded["id"])
        self.assertEqual(unchanged["status"], "candidate")
        self.assertEqual(unchanged["revision"], 1)

    def test_supersede_updates_both_notes_and_default_recall(self) -> None:
        original = self._record("SupersedeToken")
        replacement = self._record("ReplacementToken", status="verified")

        result = lifecycle.set_status(
            original["id"],
            "superseded",
            "replaced by stronger evidence",
            supersedes=replacement["id"],
            expected_revision=1,
        )

        old_note = lifecycle.get_note(original["id"])
        new_note = lifecycle.get_note(replacement["id"])
        self.assertEqual(result["id"], original["id"])
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(old_note["superseded_by"], replacement["id"])
        self.assertIn(original["id"], new_note["supersedes"])
        self.assertEqual(new_note["revision"], 2)

        default = vault_recall.recall("SupersedeToken")
        explicit = vault_recall.recall(
            "SupersedeToken",
            filters={"status": "superseded"},
        )
        self.assertEqual(default["results"], [])
        self.assertEqual([row["id"] for row in explicit["results"]], [original["id"]])

    def test_supersede_requires_an_existing_target(self) -> None:
        original = self._record("MissingTargetToken")

        with self.assertRaises(lifecycle.NoteNotFound):
            lifecycle.set_status(
                original["id"],
                "superseded",
                "missing replacement",
                supersedes="mem-000000000000",
                expected_revision=1,
            )

        unchanged = lifecycle.get_note(original["id"])
        self.assertEqual(unchanged["status"], "candidate")
        self.assertEqual(unchanged["revision"], 1)

    def test_status_change_drops_and_restores_default_recall(self) -> None:
        recorded = self._record("RestoreToken", status="verified")
        original_ref = lifecycle.get_note(recorded["id"])["evidence_refs"][-1]

        invalidated = lifecycle.set_status(
            recorded["id"],
            "invalidated",
            "evidence disproved",
            expected_revision=1,
        )
        hidden = vault_recall.recall("RestoreToken")
        invalidated_ref = lifecycle.get_note(recorded["id"])["evidence_refs"][-1]
        restored = lifecycle.set_status(
            recorded["id"],
            "verified",
            "replacement evidence verified",
            expected_revision=2,
        )
        visible = vault_recall.recall("RestoreToken")

        self.assertEqual(invalidated["revision"], 2)
        self.assertNotEqual(invalidated_ref, original_ref)
        self.assertEqual(hidden["results"], [])
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["evidence_refs"][-1], original_ref)
        self.assertEqual([row["id"] for row in visible["results"]], [recorded["id"]])

    def test_second_publish_failure_restores_first_note(self) -> None:
        original = self._record("RollbackToken")
        replacement = self._record("RollbackReplacementToken")
        real_publish = lifecycle._publish_stage
        calls = 0

        def fail_second_publish(stage):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated second publish failure")
            real_publish(stage)

        with mock.patch.object(
            lifecycle,
            "_publish_stage",
            side_effect=fail_second_publish,
        ):
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.set_status(
                    original["id"],
                    "superseded",
                    "must roll back",
                    supersedes=replacement["id"],
                    expected_revision=1,
                )

        old_note = lifecycle.get_note(original["id"])
        new_note = lifecycle.get_note(replacement["id"])
        self.assertEqual(old_note["status"], "candidate")
        self.assertIsNone(old_note["superseded_by"])
        self.assertEqual(old_note["revision"], 1)
        self.assertEqual(new_note["supersedes"], [])
        self.assertEqual(new_note["revision"], 1)

    def test_record_usage_persists_apply_feedback_across_rebuild(self) -> None:
        recorded = self._record("UsageToken", status="verified")
        recall_id = vault_recall.recall("UsageToken")["recall_id"]
        generation_before = vault_index.index_generation()

        result = lifecycle.record_usage(
            recall_id,
            recorded["id"],
            "used",
            source_task="TASK-usage-consumer",
        )

        self.assertEqual(result["recall_id"], recall_id)
        self.assertEqual(result["note_id"], recorded["id"])
        self.assertEqual(result["outcome"], "used")
        self.assertEqual(vault_index.index_generation(), generation_before)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT recall_id, note_id, outcome, source_task, ts "
                "FROM usage WHERE recall_id=?",
                (recall_id,),
            ).fetchone()
        self.assertEqual(row[:4], (recall_id, recorded["id"], "used", "TASK-usage-consumer"))
        self.assertTrue(row[4].endswith("Z"))

        vault_index.rebuild_index()
        with closing(sqlite3.connect(self.db_path)) as connection:
            preserved = connection.execute(
                "SELECT outcome FROM usage WHERE recall_id=? AND note_id=?",
                (recall_id, recorded["id"]),
            ).fetchone()
        self.assertEqual(preserved, ("used",))

    def test_record_usage_is_idempotent_but_rejects_conflicting_feedback(self) -> None:
        recorded = self._record("UsageRetryToken")
        recall_id = vault_recall.recall("UsageRetryToken")["recall_id"]

        first = lifecycle.record_usage(
            recall_id, recorded["id"], "not_useful", repo_root=self.repo_root
        )
        repeated = lifecycle.record_usage(
            recall_id, recorded["id"], "not_useful", repo_root=self.repo_root
        )

        self.assertEqual(repeated, first)
        with self.assertRaises(lifecycle.UsageConflict):
            lifecycle.record_usage(
                recall_id, recorded["id"], "incorrect", repo_root=self.repo_root
            )

        # The identical retry above is a replay of the same observation, not a
        # second one -- it must not double-count in the curation queue.
        queue_path = self.repo_root / "_state" / "curation-queue.jsonl"
        rows = queue_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(rows), 1)
        flagged = json.loads(rows[0])
        self.assertEqual(flagged["note_id"], recorded["id"])
        self.assertEqual(flagged["reason"], "not_useful")

    def test_record_usage_rejects_bad_outcome_and_missing_note(self) -> None:
        recorded = self._record("UsageValidationToken")
        recall_id = vault_recall.recall("UsageValidationToken")["recall_id"]

        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.record_usage(recall_id, recorded["id"], "maybe")
        with self.assertRaises(lifecycle.NoteNotFound):
            lifecycle.record_usage(recall_id, "mem-000000000000", "incorrect")

    def test_record_usage_used_does_not_flag_curation_queue(self) -> None:
        recorded = self._record("UsageNoFlagToken")
        recall_id = vault_recall.recall("UsageNoFlagToken")["recall_id"]

        lifecycle.record_usage(
            recall_id, recorded["id"], "used", repo_root=self.repo_root
        )

        self.assertFalse((self.repo_root / "_state" / "curation-queue.jsonl").exists())

    def test_record_usage_incorrect_flags_but_never_invalidates(self) -> None:
        recorded = self._record("UsageIncorrectToken", status="verified")
        recall_id = vault_recall.recall("UsageIncorrectToken")["recall_id"]

        lifecycle.record_usage(
            recall_id,
            recorded["id"],
            "incorrect",
            source_task="TASK-demotion",
            repo_root=self.repo_root,
        )

        # Status is untouched -- only a human review of the curation queue may
        # set `invalidated`.
        self.assertEqual(lifecycle.get_note(recorded["id"])["status"], "verified")
        queue_path = self.repo_root / "_state" / "curation-queue.jsonl"
        flagged = json.loads(queue_path.read_text(encoding="utf-8").strip())
        self.assertEqual(flagged["note_id"], recorded["id"])
        self.assertEqual(flagged["reason"], "incorrect")
        self.assertEqual(flagged["source_task"], "TASK-demotion")
        self.assertNotIn("status", flagged)


if __name__ == "__main__":
    unittest.main()
