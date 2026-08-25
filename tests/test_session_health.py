"""Tests for the two automatic session-rotation triggers.

These guard the *contracts* the rotation policy claims, not the
implementation details:

  * a real provider ceiling refusal is recognised, and unrelated failures
    are not (a false positive here rotates a healthy session every run);
  * "I don't know whether this drifted" never masquerades as "yes";
  * the fingerprint covers steps only -- if it ever covered frontmatter,
    the runner's own ``session:`` write-back would make every session look
    drifted against itself, forever;
  * the ``URGENT:`` marker parse tolerates the markdown the agent actually
    writes. That one is a regression test for a verified withheld push
    (``runs/session-growth-check/20260807T195155Z``).
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from drumbeat.automation import Step, load_from_text
from drumbeat.runner import _URGENT_MARKER_RE

from drumbeat import session_health, session_pins

# The exact stderr amplifier-agent produced for
# runs/channels-check/20260807T201036Z -- the failure this whole mechanism
# exists to catch.
REAL_CEILING_STDERR = (
    '[PROVIDER] Anthropic API error: {"type": "error", "error": {"type": '
    '"invalid_request_error", "message": "prompt is too long: 219685 tokens > '
    '200000 maximum"}, "request_id": "req_011CdoziEPntMbFP1csVWSU4"}\n'
    "[result/final] \n"
)

_AUTOMATION_TEXT = """---
automation:
  name: Demo
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
  steps:
    - id: first-step
      prompt: First step.
    - id: second-step
      prompt: Second step.
---
"""


class TestCeilingDetection(unittest.TestCase):
    """Trigger 1: the provider refused the prompt."""

    def test_detects_real_provider_stderr(self) -> None:
        hit = session_health.detect_ceiling_hit(REAL_CEILING_STDERR)
        assert hit is not None
        self.assertEqual(hit.prompt_tokens, 219685)
        self.assertEqual(hit.limit_tokens, 200000)

    def test_reports_the_largest_of_several(self) -> None:
        hit = session_health.detect_ceiling_hit(
            "prompt is too long: 201361 tokens > 200000 maximum\n"
            "prompt is too long: 219685 tokens > 200000 maximum\n"
        )
        assert hit is not None
        self.assertEqual(hit.prompt_tokens, 219685)

    def test_unrelated_failures_are_not_ceiling_hits(self) -> None:
        # A false positive here auto-rotates a healthy session on every
        # ordinary failure -- strictly worse than not rotating at all.
        for text in (
            "",
            "amplifier-agent exited 1",
            "Module 'hooks-redaction' has no valid Python package at /x/y",
            "the prompt was long but fine",
            "rate_limit_error: too many requests",
        ):
            with self.subTest(text=text):
                self.assertIsNone(session_health.detect_ceiling_hit(text))

    def test_is_limit_agnostic(self) -> None:
        hit = session_health.detect_ceiling_hit(
            "prompt is too long: 1100000 tokens > 1000000 maximum"
        )
        assert hit is not None
        self.assertEqual((hit.prompt_tokens, hit.limit_tokens), (1100000, 1000000))


class TestContractDrift(unittest.TestCase):
    """Trigger 2: the automation was rewritten under a live session."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _automation(self, text: str = _AUTOMATION_TEXT):
        return load_from_text(self.tmp_path / "demo.md", text)

    def test_fingerprint_is_stable_and_order_sensitive(self) -> None:
        fp = session_health.contract_fingerprint
        a = Step(id="a", prompt="a")
        b = Step(id="b", prompt="b")
        self.assertEqual(fp([a, b]), fp([a, b]))
        self.assertNotEqual(fp([a, b]), fp([b, a]))
        # Identity/label are authoring metadata, not the contract: a step whose
        # prompt is unchanged keeps its fingerprint even if its id/label change.
        self.assertEqual(
            fp([Step(id="x", prompt="a", label="L")]), fp([Step(id="y", prompt="a")])
        )

    def test_fingerprint_ignores_frontmatter(self) -> None:
        """The load-bearing invariant: the STEPS are the contract.

        Historically this mattered doubly -- ``runner.run()`` wrote
        ``session:``/``session_workspace:`` into the frontmatter the instant
        it created a session, so a whole-file hash would have registered
        that write as drift and rotated every automation on every single
        run, forever. Pins are engine state now,
        but the scope is unchanged: editing anything outside the steps must
        not abandon the conversation.
        """
        edited = _AUTOMATION_TEXT.replace("  notify: auto", "  notify: never")
        before = self._automation()
        after = self._automation(edited)
        self.assertEqual(after.notify, "never")
        self.assertEqual(
            session_health.contract_fingerprint(before.steps),
            session_health.contract_fingerprint(after.steps),
        )

    def test_unrecorded_session_is_never_reported_as_drifted(self) -> None:
        # "We don't know" must not masquerade as "yes" -- an unknown
        # contract reading as drifted would rotate every pre-existing
        # session at once.
        drifted, recorded = session_health.contract_drift(
            session_id="never-recorded",
            steps=[Step(id="a", prompt="a")],
            runs_dir=self.tmp_path,
        )
        self.assertFalse(drifted)
        self.assertIsNone(recorded)

    def test_recorded_then_unchanged_is_not_drift(self) -> None:
        automation = self._automation()
        session_health.record_contract(
            session_id="demo-1",
            automation_slug="demo",
            fingerprint=session_health.contract_fingerprint(automation.steps),
            recorded_at="2026-08-07T00:00:00Z",
            runs_dir=self.tmp_path,
        )
        drifted, recorded = session_health.contract_drift(
            session_id="demo-1", steps=automation.steps, runs_dir=self.tmp_path
        )
        self.assertFalse(drifted)
        self.assertIsNotNone(recorded)

    def test_rewritten_steps_are_drift(self) -> None:
        automation = self._automation()
        session_health.record_contract(
            session_id="demo-1",
            automation_slug="demo",
            fingerprint=session_health.contract_fingerprint(automation.steps),
            recorded_at="2026-08-07T00:00:00Z",
            runs_dir=self.tmp_path,
        )
        rewritten = self._automation(
            _AUTOMATION_TEXT.replace(
                "prompt: Second step.", "prompt: A different step."
            )
        )
        drifted, _ = session_health.contract_drift(
            session_id="demo-1", steps=rewritten.steps, runs_dir=self.tmp_path
        )
        self.assertTrue(drifted)

    def test_forget_contract_prunes_only_the_rotated_session(self) -> None:
        for sid in ("demo-1", "demo-2"):
            session_health.record_contract(
                session_id=sid,
                automation_slug="demo",
                fingerprint="f" + sid,
                recorded_at="2026-08-07T00:00:00Z",
                runs_dir=self.tmp_path,
            )
        session_health.forget_contract("demo-1", runs_dir=self.tmp_path)
        self.assertIsNone(
            session_health.read_contract("demo-1", runs_dir=self.tmp_path)
        )
        self.assertEqual(
            session_health.read_contract("demo-2", runs_dir=self.tmp_path), "fdemo-2"
        )

    def test_corrupt_contract_store_cannot_be_read_as_drift(self) -> None:
        # FAIL LOUD but never block: an unreadable store is announced on
        # stderr and renders drift unevaluable, not "drifted".
        session_health.contract_store_path(self.tmp_path).write_text(
            "{not json", encoding="utf-8"
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            drifted, recorded = session_health.contract_drift(
                session_id="demo-1",
                steps=[Step(id="a", prompt="a")],
                runs_dir=self.tmp_path,
            )
        self.assertFalse(drifted)
        self.assertIsNone(recorded)
        self.assertIn("not valid JSON", buf.getvalue())


def _pin(slug: str, session_id: str, *, runs_dir: Path) -> None:
    """Seed the engine pin store -- pins are engine state, not frontmatter."""
    session_pins.upsert(
        slug,
        session_id=session_id,
        session_workspace=None,
        created_by=session_pins.CREATED_BY_RUN,
        runs_dir=runs_dir,
    )


class TestHealthReport(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_run(
        self, runs_dir: Path, run_id: str, *, session_id: str, failed: bool
    ) -> None:
        run_dir = runs_dir / "demo" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(
            json.dumps({"failed": failed, "session_id": session_id}), encoding="utf-8"
        )
        if failed:
            (run_dir / "stderr.log").write_text(REAL_CEILING_STDERR, encoding="utf-8")

    def test_flags_dead_and_leaves_size_as_an_aside(self) -> None:
        runs_dir = self.tmp_path / "runs"
        self._write_run(runs_dir, "20260807T000000Z", session_id="demo-1", failed=True)

        automation = load_from_text(self.tmp_path / "demo.md", _AUTOMATION_TEXT)
        _pin(automation.slug, "demo-1", runs_dir=runs_dir)
        (report,) = session_health.health_for(
            [automation],
            runs_dir=runs_dir,
            agent_home=self.tmp_path / "agent-home",
            workspace="-ws",
        )
        assert report.ceiling_hit is not None
        self.assertEqual(report.ceiling_hit.prompt_tokens, 219685)
        self.assertEqual(report.consecutive_failures, 1)
        self.assertIn("DEAD", report.detail)
        # No transcript on disk in this fixture: size is unknown, and that
        # is explicitly not an error, because size is never a trigger.
        self.assertIsNone(report.transcript_bytes)

    def test_a_predecessors_ceiling_hit_is_not_charged_to_the_fresh_session(
        self,
    ) -> None:
        """Regression: rotation must actually clear the DEAD verdict.

        The first version scanned by slug alone and reported channels-check
        as DEAD minutes after its rotation, by reading a ceiling hit out of
        the *previous* session's stderr. A rotation that still reports DEAD
        is indistinguishable from a rotation that did not work.
        """
        runs_dir = self.tmp_path / "runs"
        self._write_run(runs_dir, "20260807T000000Z", session_id="demo-1", failed=True)
        self._write_run(runs_dir, "20260807T010000Z", session_id="demo-2", failed=False)

        automation = load_from_text(self.tmp_path / "demo.md", _AUTOMATION_TEXT)
        _pin(automation.slug, "demo-2", runs_dir=runs_dir)
        (report,) = session_health.health_for(
            [automation],
            runs_dir=runs_dir,
            agent_home=self.tmp_path / "agent-home",
            workspace="-ws",
        )
        self.assertIsNone(report.ceiling_hit)
        self.assertEqual(report.consecutive_failures, 0)
        self.assertNotIn("DEAD", report.detail)

    def test_handles_an_unpinned_automation(self) -> None:
        automation = load_from_text(self.tmp_path / "demo.md", _AUTOMATION_TEXT)
        (report,) = session_health.health_for(
            [automation],
            runs_dir=self.tmp_path / "runs",
            agent_home=self.tmp_path / "agent-home",
            workspace="-ws",
        )
        self.assertIsNone(report.session_id)
        self.assertIsNone(report.ceiling_hit)
        self.assertFalse(report.contract_drifted)


class TestUrgentMarker(unittest.TestCase):
    """Regression: a genuinely urgent report was withheld from the phone.

    ``runs/session-growth-check/20260807T195155Z`` opened with
    ``**URGENT: ...**`` and was demoted for carrying "no URGENT marker".
    """

    def test_survives_markdown_decoration(self) -> None:
        for reply in (
            (
                "**URGENT: agent-sessions-check at 41 MB -- 8 MB past proven "
                "33 MB failure point**\n\n## Session Growth Check"
            ),
            "URGENT: two sessions dead",
            "  URGENT: leading indent",
            "## URGENT: heading form",
            "- **URGENT:** bullet plus bold",
            "> URGENT: blockquote",
            "__URGENT: underscore emphasis__",
            "some preamble\n**URGENT: found on a later line**\ntrailing prose",
        ):
            with self.subTest(reply=reply[:40]):
                self.assertIsNotNone(_URGENT_MARKER_RE.search(reply))

    def test_is_not_widened_into_a_substring_search(self) -> None:
        for reply in (
            "NOTHING_TO_REPORT",
            "Nothing urgent to report.",
            "this is not urgent: nothing here",
            "The word URGENT appears mid-sentence: but not at line start",
            "**URGENT**",  # no colon -- not the marker contract
        ):
            with self.subTest(reply=reply[:40]):
                self.assertIsNone(_URGENT_MARKER_RE.search(reply))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
