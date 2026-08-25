"""Tests for the four architecture-audit placement corrections
(2026-08-05): daily-at-a-time schedules, fail-loud rejection of an
enabled ``trigger.type: event`` automation, a stderr backstop for
demoted ``URGENT:`` push decisions symmetric with the existing quiet-hours
backstop, and narrowed refusal detection that no longer punishes an
honest disclosure of one obstacle inside an otherwise-substantive report.

Each class drives the real production function directly -- no mocking of
the logic under test, only of its inputs (a synthetic clock, a synthetic
timezone, an in-memory automation file). Same discipline as
test_subscription_pruning.py and test_sender_kind.py.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from drumbeat.automation import AutomationError, load_from_text
from drumbeat.runner import _looks_like_refusal, _record_demotion
from drumbeat.scheduler import (
    DailySchedule,
    IntervalSchedule,
    parse_schedule,
    seconds_until_next_fire,
)


class TestDailySchedule(unittest.TestCase):
    """parse_schedule's new daily-at-a-time form and its next-fire math."""

    def test_parses_daily_at_hhmm(self) -> None:
        self.assertEqual(
            parse_schedule("daily at 03:00"), DailySchedule(hour=3, minute=0)
        )
        self.assertEqual(
            parse_schedule("every day at 3:00"), DailySchedule(hour=3, minute=0)
        )
        self.assertEqual(
            parse_schedule("Daily At 23:45"), DailySchedule(hour=23, minute=45)
        )

    def test_interval_form_unaffected(self) -> None:
        self.assertEqual(
            parse_schedule("every 30 minutes"), IntervalSchedule(seconds=1800)
        )
        self.assertEqual(parse_schedule("every hour"), IntervalSchedule(seconds=3600))

    def test_unparseable_still_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule("sometime soon")
        with self.assertRaises(ValueError):
            parse_schedule("daily at 25:00")

    def test_next_fire_skips_to_tomorrow_when_anchor_already_passed(self) -> None:
        # America/Los_Angeles, noon local on 2026-08-05 -- well past 03:00.
        with mock.patch(
            "drumbeat.scheduler.server_timezone", return_value="America/Los_Angeles"
        ):
            now = datetime(2026, 8, 5, 19, 0, 0, tzinfo=UTC).timestamp()  # 12:00 PDT
            schedule = parse_schedule("daily at 03:00")
            delay = seconds_until_next_fire(schedule, now)
            fire_at = datetime.fromtimestamp(now + delay, UTC)
            # 03:00 PDT on 2026-08-06 == 10:00 UTC on 2026-08-06.
            self.assertEqual(fire_at, datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC))

    def test_next_fire_same_day_when_anchor_still_ahead(self) -> None:
        with mock.patch(
            "drumbeat.scheduler.server_timezone", return_value="America/Los_Angeles"
        ):
            now = datetime(2026, 8, 5, 8, 0, 0, tzinfo=UTC).timestamp()  # 01:00 PDT
            schedule = parse_schedule("daily at 03:00")
            delay = seconds_until_next_fire(schedule, now)
            fire_at = datetime.fromtimestamp(now + delay, UTC)
            self.assertEqual(fire_at, datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC))

    def test_bad_server_timezone_raises_valueerror_not_crash(self) -> None:
        with mock.patch("drumbeat.scheduler.server_timezone", return_value="Not/AZone"):
            schedule = parse_schedule("daily at 03:00")
            with self.assertRaises(ValueError):
                seconds_until_next_fire(schedule, 0.0)


class TestEventTriggerFailsLoud(unittest.TestCase):
    """An enabled trigger.type: event automation must never validate silently."""

    def test_enabled_event_trigger_rejected(self) -> None:
        text = (
            "---\nautomation:\n  name: Test Event\n  enabled: true\n"
            "  trigger:\n    type: event\n  notify: never\n---\n\n1. Do something.\n"
        )
        with self.assertRaises(AutomationError) as ctx:
            load_from_text(Path("automations/_test-event.md"), text)
        self.assertIn("no scheduler support", str(ctx.exception))

    def test_disabled_event_trigger_accepted_as_placeholder(self) -> None:
        text = (
            "---\nautomation:\n  name: Test Event\n  enabled: false\n"
            "  trigger:\n    type: event\n  notify: never\n---\n\n1. Do something.\n"
        )
        automation = load_from_text(Path("automations/_test-event-disabled.md"), text)
        self.assertEqual(automation.trigger.type, "event")
        self.assertFalse(automation.enabled)


class TestUrgentDemotionBackstop(unittest.TestCase):
    """_record_demotion: loud stderr on every demotion, symmetric with the
    existing quiet-hours UNRECORDED backstop, plus the durable log line."""

    def test_demotion_prints_loud_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            captured = io.StringIO()
            with redirect_stderr(captured):
                _record_demotion(
                    "Swept 42 channels since last check. Nothing new.",
                    automation="Channels Check",
                    run_id="run-001",
                    reason="notify: urgent-only and no `URGENT: <reason>` marker",
                    runs_dir=runs_dir,
                )
            stderr_text = captured.getvalue()
            self.assertIn("[urgent-demote]", stderr_text)
            self.assertIn("Channels Check", stderr_text)
            self.assertIn("URGENT", stderr_text)

            log_path = runs_dir / "demoted_notifications.log"
            self.assertTrue(log_path.is_file())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("automation=Channels Check", log_text)
            self.assertIn("run_id=run-001", log_text)


class TestRefusalDetectionNarrowing(unittest.TestCase):
    """_looks_like_refusal: an honest obstacle disclosure inside a real
    report must not fail the run; a genuine total task refusal still must."""

    def test_honest_obstacle_report_not_flagged(self) -> None:
        text = (
            "I cannot access the message body due to encryption -- rights "
            "protection blocks the message content.\n\n"
            "Here is everything else I found this pass:\n"
            "- Item 1: a colleague's transcript question, resolved.\n"
            "- Item 2: a colleague's planning sync, still open.\n"
        )
        self.assertFalse(_looks_like_refusal(text))

    def test_genuine_total_refusal_still_flagged(self) -> None:
        text = (
            "I cannot make this determination without additional guidance "
            "about which repos are in scope."
        )
        self.assertTrue(_looks_like_refusal(text))

    def test_genuine_refusal_alternate_phrasing_still_flagged(self) -> None:
        text = "I don't have enough information to proceed with this check."
        self.assertTrue(_looks_like_refusal(text))

    def test_mid_sentence_cannot_never_flagged(self) -> None:
        text = "Everything looks fine. I cannot think of anything else to add."
        self.assertFalse(_looks_like_refusal(text))

    def test_empty_reply_not_flagged(self) -> None:
        self.assertFalse(_looks_like_refusal(""))
        self.assertFalse(_looks_like_refusal("   "))


if __name__ == "__main__":
    unittest.main()
