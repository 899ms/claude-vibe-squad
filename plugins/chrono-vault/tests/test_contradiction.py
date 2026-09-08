from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import notes  # noqa: E402


class ContradictionDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vault_root = Path(
            os.path.realpath(tempfile.mkdtemp(prefix="chrono-contradiction-test-"))
        )
        self.addCleanup(shutil.rmtree, self.vault_root, ignore_errors=True)
        (self.vault_root / ".chrono-vault").write_text(
            json.dumps({"vault_id": "contradiction-test", "schema_version": 1}),
            encoding="utf-8",
        )
        self.env = mock.patch.dict(
            os.environ,
            {
                "CHRONO_VAULT_ROOT": str(self.vault_root),
                "CHRONO_VAULT_AUDIT_DIR": str(self.vault_root / "audit"),
                "CHRONO_VAULT_CLEARANCE": "internal",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("CHRONO_VAULT_CONTEXT", None)

    def _record(self, title: str, body: str) -> dict:
        return notes.record(
            "finding",
            {
                "title": title,
                "body": body,
                "target": "example-chain",
                "component": "bridge-executor",
                "attack_class": "authorization",
            },
        )

    def test_same_subject_is_not_an_automatic_contradiction(self) -> None:
        original = self._record(
            "Executor requires an authorized signer",
            "Only an authorized signer can invoke the executor.",
        )
        self._record(
            "Executor accepts an unauthorized signer",
            "An unauthorized signer can invoke the same executor.",
        )

        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.vault_root / "audit" / "contradiction").glob(
                "evt-*.json"
            )
        ]
        flagged = [event for event in events if event["result"] == "flagged"]

        self.assertEqual(events, [])
        self.assertEqual(flagged, [])
        record_events = list((self.vault_root / "audit" / "record").glob("evt-*.json"))
        self.assertEqual(len(record_events), 2, "the writer and audit must really run")


if __name__ == "__main__":
    unittest.main()
