"""Unit tests for the modality-agnostic automation-management library.

These drive the REAL library functions (``automation_manager.list_automations``,
``get_automation``, ``edit_schedule``) against real automation files written to
a temp directory in the exact frontmatter shape ``automation.load`` accepts.
Only inputs are constructed -- the parsing, validation, and surgical-edit logic
under test is production code.

The load-bearing cases here are the malformed-input REFUSALS: an automation runs
unattended, so a write that would leave a file the parser cannot load must never
reach disk. Each refusal test asserts both that the error is raised with a
machine-readable code AND that the file on disk is left byte-for-byte untouched.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from drumbeat import automation as automation_mod
from drumbeat import automation_manager as am

_SCHEDULE_AUTOMATION = """---
automation:
  name: Email Check
  enabled: false
  trigger:
    type: schedule
    expression: every 45 minutes
  notify: auto
  # PLACEHOLDER tool names -- substitute whatever your packs provide.
  requires:
    - mail-cli
    - guidance/EMAIL.md
  inject:
    - argv: ["items-cli", "inject-turn"]
      label: "open items"
---

1. First step, which spans
   two lines.
2. Second step.
"""

_MANUAL_AUTOMATION = """---
automation:
  name: Manual Task
  enabled: true
  trigger:
    type: manual
  notify: never
---

1. Do the thing.
"""

_BROKEN_AUTOMATION = """---
automation:
  name: Broken
  enabled: true
  trigger:
    type: schedule
    expression: every 10 minutes
  notify: auto
---

This paragraph before step 1 is refused by the parser.

1. A step.
"""


class AutomationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, text: str) -> Path:
        path = self.dir / filename
        path.write_text(text, encoding="utf-8")
        return path

    # ---- list ----

    def test_list_returns_summaries_for_valid_files(self) -> None:
        self._write("email-check.md", _SCHEDULE_AUTOMATION)
        self._write("manual-task.md", _MANUAL_AUTOMATION)

        listing = am.list_automations(self.dir)

        self.assertEqual(listing.failures, ())
        slugs = [a.slug for a in listing.automations]
        # Deterministic, sorted by filename (email-check before manual-task).
        self.assertEqual(slugs, ["email-check", "manual-task"])

        email = listing.automations[0]
        self.assertEqual(email.name, "Email Check")
        self.assertFalse(email.enabled)
        self.assertEqual(email.trigger.type, "schedule")
        self.assertEqual(email.trigger.expression, "every 45 minutes")
        self.assertEqual(email.notify, "auto")
        self.assertEqual(email.requires, ("mail-cli", "guidance/EMAIL.md"))
        self.assertEqual(email.step_count, 2)

    def test_list_surfaces_broken_file_without_hiding_good_ones(self) -> None:
        self._write("email-check.md", _SCHEDULE_AUTOMATION)
        self._write("broken.md", _BROKEN_AUTOMATION)

        listing = am.list_automations(self.dir)

        self.assertEqual([a.slug for a in listing.automations], ["email-check"])
        self.assertEqual(len(listing.failures), 1)
        failure = listing.failures[0]
        self.assertTrue(failure.path.endswith("broken.md"))
        self.assertTrue(failure.problem)  # a real, non-empty reason

    def test_list_missing_directory_raises_not_found(self) -> None:
        with self.assertRaises(am.AutomationManagerError) as caught:
            am.list_automations(self.dir / "does-not-exist")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    # ---- get ----

    def test_get_returns_full_detail(self) -> None:
        path = self._write("email-check.md", _SCHEDULE_AUTOMATION)

        detail = am.get_automation("email-check", self.dir)

        self.assertEqual(detail.name, "Email Check")
        self.assertEqual(detail.trigger.expression, "every 45 minutes")
        self.assertEqual(
            detail.steps,
            ("First step, which spans\ntwo lines.", "Second step."),
        )
        self.assertEqual(detail.requires, ("mail-cli", "guidance/EMAIL.md"))
        self.assertEqual(len(detail.inject), 1)
        self.assertEqual(detail.inject[0].argv, ("items-cli", "inject-turn"))
        self.assertEqual(detail.inject[0].label, "open items")
        # Raw content is the file verbatim, for a raw-text editor to round-trip.
        self.assertEqual(detail.content, path.read_text(encoding="utf-8"))

    def test_get_unknown_slug_raises_not_found(self) -> None:
        self._write("email-check.md", _SCHEDULE_AUTOMATION)
        with self.assertRaises(am.AutomationManagerError) as caught:
            am.get_automation("nope", self.dir)
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_get_broken_file_reports_why_not_just_missing(self) -> None:
        self._write("broken.md", _BROKEN_AUTOMATION)
        with self.assertRaises(am.AutomationManagerError) as caught:
            am.get_automation("broken", self.dir)
        # INVALID (it exists but doesn't parse), not NOT_FOUND, and it names why.
        self.assertEqual(caught.exception.code, "INVALID")
        self.assertIn("does not parse", caught.exception.message)

    # ---- edit_schedule: happy paths ----

    def test_edit_schedule_changes_cadence_and_preserves_everything_else(self) -> None:
        path = self._write("email-check.md", _SCHEDULE_AUTOMATION)
        original = path.read_text(encoding="utf-8")

        detail = am.edit_schedule("email-check", self.dir, expression="every 2 hours")

        self.assertEqual(detail.trigger.expression, "every 2 hours")
        # Only the expression line's value changed; everything else byte-for-byte.
        expected = original.replace(
            "    expression: every 45 minutes\n",
            "    expression: every 2 hours\n",
        )
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        # A backup of the prior version was kept.
        backup = path.with_name(path.name + ".bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        # And the file on disk still parses under the real parser.
        reparsed = automation_mod.load(path)
        self.assertEqual(reparsed.trigger.expression, "every 2 hours")

    def test_edit_schedule_preserves_comment_and_body(self) -> None:
        path = self._write("email-check.md", _SCHEDULE_AUTOMATION)

        am.edit_schedule("email-check", self.dir, expression="every hour")

        new_text = path.read_text(encoding="utf-8")
        self.assertIn(
            "# PLACEHOLDER tool names -- substitute whatever your packs provide.",
            new_text,
        )
        self.assertIn("1. First step, which spans\n   two lines.", new_text)
        self.assertIn("2. Second step.", new_text)

    def test_edit_schedule_accepts_daily_expression_with_colon(self) -> None:
        path = self._write("email-check.md", _SCHEDULE_AUTOMATION)

        detail = am.edit_schedule("email-check", self.dir, expression="daily at 03:00")

        self.assertEqual(detail.trigger.expression, "daily at 03:00")
        self.assertEqual(automation_mod.load(path).trigger.expression, "daily at 03:00")

    # ---- edit_schedule: refusals (malformed input must never reach disk) ----

    def test_edit_schedule_refuses_unparseable_expression(self) -> None:
        path = self._write("email-check.md", _SCHEDULE_AUTOMATION)
        original = path.read_text(encoding="utf-8")

        with self.assertRaises(am.AutomationManagerError) as caught:
            am.edit_schedule("email-check", self.dir, expression="every fortnight")

        self.assertEqual(caught.exception.code, "INVALID")
        # The file is untouched and no backup was created -- nothing was written.
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse(path.with_name(path.name + ".bak").exists())

    def test_edit_schedule_refuses_empty_expression(self) -> None:
        path = self._write("email-check.md", _SCHEDULE_AUTOMATION)
        original = path.read_text(encoding="utf-8")

        with self.assertRaises(am.AutomationManagerError) as caught:
            am.edit_schedule("email-check", self.dir, expression="   ")

        self.assertEqual(caught.exception.code, "INVALID")
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse(path.with_name(path.name + ".bak").exists())

    def test_edit_schedule_refuses_on_manual_trigger(self) -> None:
        path = self._write("manual-task.md", _MANUAL_AUTOMATION)
        original = path.read_text(encoding="utf-8")

        with self.assertRaises(am.AutomationManagerError) as caught:
            am.edit_schedule("manual-task", self.dir, expression="every 30 minutes")

        self.assertEqual(caught.exception.code, "UNSUPPORTED")
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse(path.with_name(path.name + ".bak").exists())

    def test_edit_schedule_unknown_slug_raises_not_found(self) -> None:
        self._write("email-check.md", _SCHEDULE_AUTOMATION)
        with self.assertRaises(am.AutomationManagerError) as caught:
            am.edit_schedule("nope", self.dir, expression="every 30 minutes")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    # ---- wrapper seam: views serialise cleanly ----

    def test_views_are_json_serializable(self) -> None:
        self._write("email-check.md", _SCHEDULE_AUTOMATION)

        listing = am.list_automations(self.dir).to_dict()
        detail = am.get_automation("email-check", self.dir).to_dict()

        # A thin tool wrapper can hand these straight to json.dumps.
        json.dumps(listing)
        json.dumps(detail)
        self.assertEqual(listing["automations"][0]["slug"], "email-check")
        self.assertEqual(detail["trigger"]["expression"], "every 45 minutes")
        self.assertEqual(detail["inject"][0]["argv"], ["items-cli", "inject-turn"])

    def test_error_code_must_be_known(self) -> None:
        # The exception itself refuses an unknown code -- fail loud, no coercion.
        with self.assertRaises(ValueError):
            am.AutomationManagerError("TEAPOT", "nope")


if __name__ == "__main__":
    unittest.main()
