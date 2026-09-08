#!/usr/bin/env python3
"""Regression tests for atomic workboard focus handoff."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import workboard  # noqa: E402


class FocusGateSeamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ledger = Path(tmp.name) / "OPEN-WORK.md"
        self.event_index = 0
        if os.environ.get("WORKBOARD_FOCUS_GATE_CONTROL") == "without_added_issues":
            patcher = mock.patch.object(workboard, "_added_issues", return_value=())
            patcher.start()
            self.addCleanup(patcher.stop)

    def append(self, kind: str, **facts: str) -> workboard.WorkEvent:
        self.event_index += 1
        return workboard.append_event(
            kind,
            path=self.ledger,
            event_id=f"EV-FOCUS-{self.event_index:02d}",
            at=f"2026-09-05T20:00:{self.event_index:02d}Z",
            **facts,
        )

    def start_a_queue_b(self) -> tuple[workboard.WorkEvent, workboard.WorkEvent]:
        active = self.append(
            "start",
            item_id="A",
            summary="active item",
            why="the fixture needs a focus",
            next_action="advance A",
        )
        queued = self.append(
            "queue",
            item_id="B",
            summary="queued successor",
            why="the handoff needs a successor",
            resume_action="advance B",
        )
        return active, queued

    def test_bare_terminal_of_sole_focus_is_refused_before_it_can_wedge(self):
        for kind in ("complete", "archive", "drop"):
            with self.subTest(kind=kind):
                self.setUp()
                self.start_a_queue_b()
                before = self.ledger.read_bytes()
                facts = {"item_id": "A"}
                if kind == "drop":
                    facts.update(summary="drop A", why="A is no longer owed")

                try:
                    self.append(kind, **facts)
                except workboard.WorkboardConsistencyError:
                    pass
                else:
                    wedged = workboard.load_workboard(self.ledger)
                    self.assertTrue(
                        any(
                            "exactly one active item; found 0" in issue
                            for issue in wedged.issues
                        ),
                        "the pre-fix path must reproduce the invalid zero-active "
                        "projection",
                    )
                    self.fail("a bare terminal append removed the sole active focus")

                self.assertEqual(self.ledger.read_bytes(), before)
                valid = workboard.load_workboard(self.ledger, strict=True)
                self.assertEqual(valid.active_item_id, "A")

    def test_atomic_complete_with_successor_is_valid_and_terminal_never_refills(self):
        active, queued = self.start_a_queue_b()

        self.append(
            "advance",
            work_id=active.fields["work_id"],
            next_action="complete A and hand off to B",
        )
        self.append(
            "complete",
            work_id=active.fields["work_id"],
            successor_work_id=queued.fields["work_id"],
            next_action="continue B after A",
        )
        self.append(
            "advance",
            work_id=queued.fields["work_id"],
            next_action="finish B",
        )
        self.append(
            "queue",
            item_id="A",
            summary="a distinct later obligation with the same alias",
            why="aliases are reusable but work identities are not",
            resume_action="handle the new A",
        )

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(view.active_work_id, queued.fields["work_id"])
        self.assertEqual(view.next_action, "finish B")
        self.assertIn(active.fields["work_id"], view.terminal_work_ids)
        self.assertNotIn(
            active.fields["work_id"], {item.work_id for item in view.items}
        )

    def test_atomic_complete_can_declare_idle(self):
        active, _queued = self.start_a_queue_b()

        self.append(
            "complete",
            work_id=active.fields["work_id"],
            focus_state="idle",
        )

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertIsNone(view.active_work_id)
        self.assertTrue(view.idle)
        self.assertIsNone(view.waiting_work_id)

    def test_atomic_complete_can_declare_one_waiting_item(self):
        active, queued = self.start_a_queue_b()

        self.append(
            "complete",
            work_id=active.fields["work_id"],
            waiting_work_id=queued.fields["work_id"],
            resume_action="wait for the operator before B",
        )

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertIsNone(view.active_work_id)
        self.assertFalse(view.idle)
        self.assertEqual(view.waiting_work_id, queued.fields["work_id"])
        waiting = next(
            item
            for item in view.items
            if item.work_id == queued.fields["work_id"]
        )
        self.assertEqual(waiting.state, "waiting")
        self.assertEqual(waiting.resume_action, "wait for the operator before B")

    def test_bare_block_of_sole_active_item_declares_blocked_pause(self):
        active = self.append(
            "start",
            item_id="A",
            summary="active item",
            why="the fixture needs a focus",
            next_action="advance A",
        )

        self.append(
            "block",
            work_id=active.fields["work_id"],
            resume_action="unblock A externally",
        )

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertIsNone(view.active_work_id)
        self.assertEqual(view.waiting_work_id, active.fields["work_id"])
        self.assertEqual(view.items[0].state, "blocked")
        self.assertEqual(view.items[0].resume_action, "unblock A externally")

    def test_terminal_waiting_declaration_preserves_blocked_target(self):
        for kind in ("complete", "archive", "drop"):
            with self.subTest(kind=kind):
                self.setUp()
                active, queued = self.start_a_queue_b()
                blocked_event = self.append(
                    "block",
                    work_id=queued.fields["work_id"],
                    resume_action="B is blocked on legal review",
                )
                facts = {
                    "work_id": active.fields["work_id"],
                    "waiting_work_id": queued.fields["work_id"],
                    "resume_action": "B's own operator wait",
                }
                if kind == "drop":
                    facts.update(summary="drop A", why="A is no longer owed")
                before = self.ledger.read_bytes()

                declared = self.append(kind, **facts)

                self.assertTrue(self.ledger.read_bytes().startswith(before))
                view = workboard.load_workboard(self.ledger, strict=True)
                self.assertIsNone(view.active_work_id)
                self.assertFalse(view.idle)
                self.assertEqual(view.waiting_work_id, queued.fields["work_id"])
                self.assertIn(active.fields["work_id"], view.terminal_work_ids)
                self.assertEqual(len(view.items), 1)
                self.assertEqual(view.items[0].state, "blocked")
                self.assertEqual(
                    view.items[0].resume_action,
                    blocked_event.fields["resume_action"],
                )
                self.assertEqual(view.items[0].last_event_id, declared.event_id)

    def test_projection_waiting_guard_survives_block_schema_relaxation(self):
        for kind in ("complete", "archive", "drop", "block"):
            for identity in ("opaque", "legacy"):
                for target_state in ("blocked", "queued"):
                    with self.subTest(
                        kind=kind, identity=identity, target_state=target_state
                    ):
                        opaque = identity == "opaque"
                        a_id, b_id = "W-" + "a" * 32, "W-" + "b" * 32
                        a = {"work_id": a_id} if opaque else {"item_id": "A"}
                        b = {"work_id": b_id} if opaque else {"item_id": "B"}
                        records = [
                            workboard.WorkEvent(
                                "EV-A",
                                "2026-09-05T20:00:00Z",
                                "start",
                                {
                                    **a,
                                    **({"alias": "A"} if opaque else {}),
                                    "summary": "active A",
                                    "why": "fixture focus",
                                    "next_action": "advance A",
                                },
                                1,
                            ),
                            workboard.WorkEvent(
                                "EV-B",
                                "2026-09-05T20:00:01Z",
                                "queue",
                                {
                                    **b,
                                    **({"alias": "B"} if opaque else {}),
                                    "summary": "queued B",
                                    "why": "fixture target",
                                    "resume_action": "advance B",
                                },
                                2,
                            ),
                        ]
                        if target_state == "blocked":
                            records.append(
                                workboard.WorkEvent(
                                    "EV-BLOCK-B",
                                    "2026-09-05T20:00:02Z",
                                    "block",
                                    {
                                        **b,
                                        "resume_action": "B is blocked on legal review",
                                    },
                                    3,
                                )
                            )
                        facts = {
                            **a,
                            "waiting_work_id" if opaque else "waiting_id": (
                                b_id if opaque else "B"
                            ),
                            "resume_action": "A is blocked on vendor",
                        }
                        if kind == "drop":
                            if not opaque:
                                del facts["item_id"]
                                facts["request_id"] = "A"
                            facts.update(
                                summary="drop A", why="A is no longer owed"
                            )
                        event = workboard.WorkEvent(
                            "EV-DECLARE", "2026-09-05T20:00:03Z", kind, facts, 4,
                        )
                        document = workboard.WorkboardDocument(
                            self.ledger, tuple(records) + (event,), (), True,
                        )
                        # Only the test admits the removed block variant. The
                        # production schema must still reject this same event.
                        if kind == "block":
                            self.assertTrue(workboard._event_schema_issues(event))
                        variants = workboard._REQUIRED_FIELD_VARIANTS[kind]
                        relaxed = (
                            {kind: variants + (frozenset(facts),)}
                            if kind == "block" else {}
                        )
                        with mock.patch.dict(
                            workboard._REQUIRED_FIELD_VARIANTS, relaxed,
                        ):
                            view = workboard.project_workboard(document)
                            self.assertEqual(
                                workboard.validate_workboard(document, view), ()
                            )
                            if kind == "block":
                                self.ledger.write_text(
                                    "".join(
                                        workboard.format_event(
                                            record.kind,
                                            event_id=record.event_id,
                                            at=record.at,
                                            **record.fields,
                                        )
                                        for record in records
                                    ),
                                    encoding="utf-8",
                                )
                                before = self.ledger.read_bytes()
                                prior = workboard.load_workboard(self.ledger, strict=True)
                                with self.assertRaisesRegex(
                                    workboard.WorkboardConsistencyError,
                                    "was not reflected as block; projection postcondition failed",
                                ):
                                    workboard.append_event(
                                        kind, path=self.ledger,
                                        event_id=event.event_id, at=event.at, **facts,
                                    )
                                self.assertEqual(self.ledger.read_bytes(), before)
                        self.assertEqual(view.transition_issues, ())
                        target = next(
                            item for item in view.items if item.alias == "B"
                        )
                        if kind == "block":
                            self.assertEqual(
                                target, next(item for item in prior.items if item.alias == "B")
                            )
                            source = next(item for item in view.items if item.alias == "A")
                            self.assertEqual(source.state, "blocked")
                            self.assertEqual(source.resume_action, facts["resume_action"])
                            self.assertEqual(view.waiting_work_id, source.work_id)
                            self.assertIsNone(view.active_work_id)
                            self.assertFalse(view.idle)
                            continue
                        self.assertEqual(
                            target.state,
                            "blocked" if target_state == "blocked" else "waiting",
                        )
                        self.assertEqual(
                            target.resume_action,
                            "B is blocked on legal review" if target_state == "blocked"
                            else facts["resume_action"],
                        )
                        self.assertEqual(target.last_event_id, event.event_id)
                        self.assertEqual(target.last_index, len(records))
                        self.assertEqual(view.waiting_work_id, target.work_id)
                        self.assertIsNone(view.active_work_id)
                        self.assertFalse(view.idle)
                        self.assertIn("A", view.terminal_ids)

    def test_blocked_pause_survives_start_and_adopted_start(self):
        for opening in ("start", "adopted-start"):
            with self.subTest(opening=opening):
                self.setUp()
                active = self.append(
                    "start",
                    item_id="A",
                    summary="active item",
                    why="the fixture needs a focus",
                    next_action="advance A",
                )
                self.append(
                    "block",
                    work_id=active.fields["work_id"],
                    resume_action="clear A's external obstruction",
                )

                if opening == "start":
                    opened = self.append(
                        "start",
                        item_id="C",
                        summary="new active item",
                        why="blocked A does not prevent another focus",
                        next_action="advance C",
                    )
                else:
                    source_event_id = "EV-FOCUS-COLLIDED-START"
                    self.ledger.write_text(
                        self.ledger.read_text(encoding="utf-8")
                        + workboard.format_event(
                            "start",
                            event_id=source_event_id,
                            at="2026-09-05T20:00:03Z",
                            work_id=active.fields["work_id"],
                            alias="C",
                            summary="new adopted active item",
                            why="exercise the adopted-start projection path",
                            next_action="advance adopted C",
                        ),
                        encoding="utf-8",
                    )
                    opened = self.append(
                        workboard.ADOPTION_KIND,
                        source_event_id=source_event_id,
                    )

                view = workboard.load_workboard(self.ledger, strict=True)
                blocked = next(
                    item
                    for item in view.items
                    if item.work_id == active.fields["work_id"]
                )
                self.assertEqual(blocked.state, "blocked")
                self.assertEqual(
                    blocked.resume_action, "clear A's external obstruction"
                )
                self.assertEqual(view.active_work_id, opened.fields["work_id"])
                self.assertIsNone(view.waiting_work_id)

    def test_block_can_atomically_declare_idle(self):
        active = self.append(
            "start",
            item_id="A",
            summary="active item",
            why="the fixture needs a focus",
            next_action="advance A",
        )

        self.append(
            "block",
            work_id=active.fields["work_id"],
            resume_action="clear A's external obstruction",
            focus_state="idle",
        )

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertTrue(view.idle)
        self.assertIsNone(view.active_work_id)
        self.assertIsNone(view.waiting_work_id)
        self.assertEqual(view.items[0].state, "blocked")

    def test_block_cannot_declare_a_distinct_waiting_item(self):
        active, queued = self.start_a_queue_b()
        before = self.ledger.read_bytes()

        with self.assertRaisesRegex(
            workboard.WorkboardConsistencyError,
            r"block has unexpected fact\(s\): waiting_work_id",
        ):
            self.append(
                "block",
                work_id=active.fields["work_id"],
                resume_action="clear A's external obstruction",
                waiting_work_id=queued.fields["work_id"],
            )

        self.assertEqual(self.ledger.read_bytes(), before)
        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(view.active_work_id, active.fields["work_id"])
        self.assertEqual(
            next(
                item
                for item in view.items
                if item.work_id == queued.fields["work_id"]
            ).state,
            "queued",
        )

    def test_queue_cannot_be_first_structured_event(self):
        with self.assertRaisesRegex(
            workboard.WorkboardConsistencyError, "introduced issue\\(s\\)"
        ):
            self.append(
                "queue",
                item_id="A",
                summary="queued item",
                why="the fixture pins the first-event contract",
                resume_action="start or declare idle first",
            )

        self.assertFalse(self.ledger.exists())

    def test_block_of_declared_waiting_item_preserves_declared_pause(self):
        active, queued = self.start_a_queue_b()
        self.append(
            "complete",
            work_id=active.fields["work_id"],
            waiting_work_id=queued.fields["work_id"],
            resume_action="wait for operator input",
        )

        self.append(
            "block",
            work_id=queued.fields["work_id"],
            resume_action="clear the external obstruction",
        )

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertIsNone(view.active_work_id)
        self.assertEqual(view.waiting_work_id, queued.fields["work_id"])
        waiting = next(
            item for item in view.items if item.work_id == queued.fields["work_id"]
        )
        self.assertEqual(waiting.state, "blocked")
        self.assertEqual(waiting.resume_action, "clear the external obstruction")

    def test_resume_render_surfaces_idle_waiting_and_blocked_focus_pauses(self):
        self.append("idle")
        idle_rows = workboard.resume_rows(self.ledger)
        for show_detail in (False, True):
            with self.subTest(pause="idle", show_detail=show_detail):
                self.assertIn(
                    "- focus: idle (no commitment)",
                    workboard.render_resume_rows(idle_rows, show_detail),
                )

        self.setUp()
        active, queued = self.start_a_queue_b()
        self.append(
            "complete",
            work_id=active.fields["work_id"],
            waiting_work_id=queued.fields["work_id"],
            resume_action="wait for the operator before B",
        )
        waiting_rows = workboard.resume_rows(self.ledger)
        expected = "- focus: waiting on B — wait for the operator before B"
        for show_detail in (False, True):
            with self.subTest(pause="waiting", show_detail=show_detail):
                self.assertIn(
                    expected,
                    workboard.render_resume_rows(waiting_rows, show_detail),
                )

        blocked_action = "clear the external obstruction " * 8
        self.append(
            "block",
            work_id=queued.fields["work_id"],
            resume_action=blocked_action,
        )
        blocked_rows = workboard.resume_rows(self.ledger)
        expected = (
            "- focus: blocked on B — "
            + workboard._clip(blocked_action, workboard.ACTION_CLIP)
        )
        for show_detail in (False, True):
            with self.subTest(pause="blocked", show_detail=show_detail):
                rendered = workboard.render_resume_rows(blocked_rows, show_detail)
                self.assertIn(expected, rendered)
                self.assertNotIn("- focus: waiting on B", rendered)

    def test_validator_rejects_raw_undeclared_zero_active_projection(self):
        active, _queued = self.start_a_queue_b()
        self.ledger.write_text(
            self.ledger.read_text(encoding="utf-8")
            + workboard.format_event(
                "complete",
                event_id="EV-RAW-WEDGE",
                at="2026-09-05T20:01:00Z",
                work_id=active.fields["work_id"],
            ),
            encoding="utf-8",
        )

        view = workboard.load_workboard(self.ledger)
        self.assertTrue(
            any("exactly one active item; found 0" in issue for issue in view.issues)
        )
        with self.assertRaisesRegex(
            workboard.WorkboardConsistencyError,
            "exactly one active item; found 0",
        ):
            workboard.load_workboard(self.ledger, strict=True)

    def test_all_terminal_kinds_keep_closed_work_identity_terminal(self):
        for kind in ("complete", "archive", "drop"):
            with self.subTest(kind=kind):
                self.setUp()
                active, queued = self.start_a_queue_b()
                facts = {
                    "work_id": active.fields["work_id"],
                    "successor_work_id": queued.fields["work_id"],
                    "next_action": "continue B",
                }
                if kind == "drop":
                    facts.update(summary="drop A", why="A is no longer owed")

                self.append(kind, **facts)
                self.append(
                    "advance",
                    work_id=queued.fields["work_id"],
                    next_action="finish B",
                )

                view = workboard.load_workboard(self.ledger, strict=True)
                self.assertIn(active.fields["work_id"], view.terminal_work_ids)
                self.assertNotIn(
                    active.fields["work_id"],
                    {item.work_id for item in view.items},
                )

    def test_standalone_idle_is_a_valid_explicit_zero_focus_declaration(self):
        self.append("idle")

        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertTrue(view.idle)
        self.assertIsNone(view.active_work_id)
        self.assertIsNone(view.waiting_work_id)


if __name__ == "__main__":
    unittest.main()
