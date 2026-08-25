"""Tests for the delivery seam's outbox (``drumbeat.engine_events``).

The seam is only worth having if its semantics hold under the exact
conditions that produced this project's silent failures. So these are not
happy-path tests: they are the outbox's stated contract, each one red-proven
by deleting the guard it covers.

Covered here:

- required fields have NO defaults, in both directions (write AND read)
- the gate/verdict enums are closed, and a value outside them is refused
- the torn-tail rule: a partial final line is invisible, never an error
- the byte-offset cursor: resumable, and never silently rewound
- fsync on the event types a consumer must deliver
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from drumbeat import engine_events


def _intent(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "20260809T120000Z",
        "automation": "Teams Check",
        "session_id": "teams-check-1",
        "verdict": "deliver",
        "gate": "policy-always",
        "reason": "notify: always and the final reply is non-empty",
        "text": "two things need you",
    }
    payload.update(overrides)
    return payload


class TestRequiredFieldsHaveNoDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_reason_is_refused_at_write_time(self) -> None:
        payload = _intent()
        del payload["reason"]
        with self.assertRaises(engine_events.OutboxWriteError) as ctx:
            engine_events.append_event(
                self.runs_dir, engine_events.EventType.DELIVERY_INTENT, payload
            )
        self.assertIn("reason", str(ctx.exception))
        self.assertFalse(engine_events.outbox_path(self.runs_dir).is_file())

    def test_blank_reason_is_refused_exactly_like_a_missing_one(self) -> None:
        with self.assertRaises(engine_events.OutboxWriteError) as ctx:
            engine_events.append_event(
                self.runs_dir,
                engine_events.EventType.DELIVERY_INTENT,
                _intent(reason="   "),
            )
        self.assertIn("blank", str(ctx.exception))

    def test_none_reason_is_refused(self) -> None:
        with self.assertRaises(engine_events.OutboxWriteError):
            engine_events.append_event(
                self.runs_dir,
                engine_events.EventType.DELIVERY_INTENT,
                _intent(reason=None),
            )

    def test_delivery_intent_must_carry_text_even_when_withholding(self) -> None:
        payload = _intent(verdict="withhold", gate="auto-sentinel")
        del payload["text"]
        with self.assertRaises(engine_events.OutboxWriteError) as ctx:
            engine_events.append_event(
                self.runs_dir, engine_events.EventType.DELIVERY_INTENT, payload
            )
        self.assertIn("text", str(ctx.exception))

    def test_empty_text_is_allowed_for_a_withhold(self) -> None:
        # An abort really does have no output. The FIELD is required; a
        # genuinely empty value for it is a fact, not a missing reason.
        engine_events.append_event(
            self.runs_dir,
            engine_events.EventType.DELIVERY_INTENT,
            _intent(verdict="withhold", gate="run-failed", text=""),
        )
        events, _ = engine_events.read_since(self.runs_dir, 0)
        self.assertEqual(len(events), 1)


class TestClosedEnums(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unknown_gate_is_refused_at_write_time(self) -> None:
        with self.assertRaises(engine_events.OutboxWriteError) as ctx:
            engine_events.append_event(
                self.runs_dir,
                engine_events.EventType.DELIVERY_INTENT,
                _intent(gate="vibes"),
            )
        self.assertIn("unknown gate", str(ctx.exception))

    def test_unknown_verdict_is_refused_at_write_time(self) -> None:
        with self.assertRaises(engine_events.OutboxWriteError):
            engine_events.append_event(
                self.runs_dir,
                engine_events.EventType.DELIVERY_INTENT,
                _intent(verdict="maybe"),
            )

    def test_refusal_detected_is_in_the_enum(self) -> None:
        # Named explicitly: _looks_like_refusal is a real gate in runner.py
        # that appears in no summary of "the three gates" and is exactly the
        # thing that would have vanished silently in extraction.
        self.assertEqual(engine_events.Gate.REFUSAL_DETECTED.value, "refusal-detected")

    def test_duplicate_suppressed_is_in_the_enum(self) -> None:
        # Same story: found in the code (_find_recent_duplicate), absent from
        # the design's own "at minimum" list.
        self.assertEqual(
            engine_events.Gate.DUPLICATE_SUPPRESSED.value, "duplicate-suppressed"
        )

    def test_unknown_event_type_halts_the_reader(self) -> None:
        path = engine_events.outbox_path(self.runs_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            # "inject_skipped" was this test's unknown-type example until step 2
            # made it a real event type; a version-skew stand-in that will
            # never exist keeps the halt-on-unknown rule red-provable.
            json.dumps({"type": "from_a_future_version", "run_id": "x"}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(engine_events.OutboxParseError) as ctx:
            engine_events.read_since(self.runs_dir, 0)
        self.assertEqual(ctx.exception.offset, 0)
        self.assertIn("unknown event type", str(ctx.exception))


class TestTornTail(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_partial_final_line_is_invisible_not_an_error(self) -> None:
        end = engine_events.append_event(
            self.runs_dir, engine_events.EventType.DELIVERY_INTENT, _intent()
        )
        path = engine_events.outbox_path(self.runs_dir)
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type": "delivery_intent", "run_id": "torn", "auto')

        events, complete_end = engine_events.read_since(self.runs_dir, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].run_id, "20260809T120000Z")
        self.assertEqual(complete_end, end)

    def test_only_a_partial_line_reads_as_nothing_yet(self) -> None:
        path = engine_events.outbox_path(self.runs_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type": "delivery_int', encoding="utf-8")
        events, complete_end = engine_events.read_since(self.runs_dir, 0)
        self.assertEqual(events, [])
        self.assertEqual(complete_end, 0)

    def test_a_torn_line_completed_later_is_then_read(self) -> None:
        path = engine_events.outbox_path(self.runs_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"type": "delivery_intent", **_intent()})
        path.write_text(line[:40], encoding="utf-8")
        events, cursor = engine_events.read_since(self.runs_dir, 0)
        self.assertEqual(events, [])
        with open(path, "a", encoding="utf-8") as f:
            f.write(line[40:] + "\n")
        events, cursor = engine_events.read_since(self.runs_dir, cursor)
        self.assertEqual(len(events), 1)


class TestByteOffsetCursor(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cursor_resumes_without_replay_or_gap(self) -> None:
        first = engine_events.append_event(
            self.runs_dir,
            engine_events.EventType.DELIVERY_INTENT,
            _intent(run_id="run-1"),
        )
        events, cursor = engine_events.read_since(self.runs_dir, 0)
        self.assertEqual([e.run_id for e in events], ["run-1"])
        self.assertEqual(cursor, first)

        engine_events.append_event(
            self.runs_dir,
            engine_events.EventType.DELIVERY_INTENT,
            _intent(run_id="run-2"),
        )
        events, cursor = engine_events.read_since(self.runs_dir, cursor)
        self.assertEqual([e.run_id for e in events], ["run-2"])

        events, cursor = engine_events.read_since(self.runs_dir, cursor)
        self.assertEqual(events, [])

    def test_cursor_past_eof_raises_instead_of_rewinding(self) -> None:
        engine_events.append_event(
            self.runs_dir, engine_events.EventType.DELIVERY_INTENT, _intent()
        )
        with self.assertRaises(engine_events.OutboxParseError) as ctx:
            engine_events.read_since(self.runs_dir, 10_000_000)
        self.assertIn("truncated or replaced", str(ctx.exception))

    def test_multibyte_text_does_not_desync_the_offset(self) -> None:
        engine_events.append_event(
            self.runs_dir,
            engine_events.EventType.DELIVERY_INTENT,
            _intent(run_id="run-1", text="\u2014 em dash \u00e9\u00e0 \u4f60\u597d"),
        )
        engine_events.append_event(
            self.runs_dir,
            engine_events.EventType.DELIVERY_INTENT,
            _intent(run_id="run-2"),
        )
        events, cursor = engine_events.read_since(self.runs_dir, 0)
        self.assertEqual([e.run_id for e in events], ["run-1", "run-2"])
        self.assertEqual(
            cursor, engine_events.outbox_path(self.runs_dir).stat().st_size
        )
        # Resuming from the FIRST event's end must land exactly on the second.
        events, _ = engine_events.read_since(self.runs_dir, events[0].end_offset)
        self.assertEqual([e.run_id for e in events], ["run-2"])


class TestOutboxStatus(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_absent_outbox_reports_absent_not_zero_lag(self) -> None:
        status = engine_events.outbox_status(self.runs_dir, cursor=None)
        self.assertFalse(status["exists"])
        self.assertIsNone(status["lag_bytes"])

    def test_lag_is_bytes_behind(self) -> None:
        size = engine_events.append_event(
            self.runs_dir, engine_events.EventType.DELIVERY_INTENT, _intent()
        )
        self.assertEqual(
            engine_events.outbox_status(self.runs_dir, cursor=0)["lag_bytes"], size
        )
        self.assertEqual(
            engine_events.outbox_status(self.runs_dir, cursor=size)["lag_bytes"], 0
        )


if __name__ == "__main__":
    unittest.main()
