"""Regression proofs for the three d6-brain-checks failures.

Every test here drives a REAL production function and checks its real,
on-disk / in-return side effect. If one goes red, the mechanism it names is
broken and the fix belongs in ``src/drumbeat/``, not in this file. Each is
anchored to a specific failure measured on the owner's live box.

1. SILENT-FAILURE LOUDNESS (f199 run half + gsuite-email-check 3/3)
   Measured: channels-check and gsuite-email-check runs on disk with
   ``result.json`` ``"failed": true`` while ``"error": null`` -- and a
   ``steps[].error`` of exactly ``"amplifier-agent exited 1"``. The failing
   step's error never reached the run's top-level ``error`` field, so
   ``drumbeat doctor`` / the run record / the app saw a failure with no reason.
   ``runner.run`` must surface the failing turn's error at the top level.

2. INJECT_SKIPPED OUTBOX GUARD (the carried-patch upstream)
   The inject_skipped bookkeeping emit was wrapped in a bare
   ``except OSError``. An inject_skipped write that fails field validation
   raises ``OutboxWriteError`` -- NOT an ``OSError`` -- which escaped the
   guard and killed the whole turn. A bookkeeping write must never take down
   the run it only annotates. (A carried patch upstream existed solely to
   compensate for this; upstreaming it here makes that record obsolete.)

3. RESTART-SAFE SCHEDULE (f199 schedule half)
   The scheduler kept ``next_due`` in memory only, so every restart reset the
   clock to ``now + interval``. The longest-interval automation was starved
   worst: ``channels-check`` runs ``every 90 minutes``, so a scheduler that
   bounced more often than that deferred it forever -- "not running
   automatically". ``serve`` must reload persisted due times so a restart
   resumes the schedule instead of resetting it.
"""

from __future__ import annotations

import io
import json
import os
import time
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat.automation import load

from drumbeat import engine_events, runner, schedule_state, scheduler

_STEP_AUTOMATION = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""

_INJECT_AUTOMATION = """---
automation:
  name: Inject Guard Check
  enabled: true
  trigger:
    type: manual
  notify: never
  inject:
    - argv: ["ledger-items", "inject-turn"]
      label: "open items"
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""

_SCHEDULED_AUTOMATION = """---
automation:
  name: Channels Check
  enabled: true
  trigger:
    type: schedule
    expression: every 90 minutes
  notify: never
  steps:
    - id: sweep-channels
      prompt: Sweep the channels.
---
"""


class _RunFixture(unittest.TestCase):
    """Minimal real workspace/runs_dir/agent-home wiring to drive ``runner.run``.

    The only seam mocked is ``runner._submit_turn`` -- the established fake
    seam for the SDK-driven turn (see ``runner._TurnOutcome``); everything
    else is the real code path.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        self.workspace = self.tmp_path / "workspace"
        for sub in ("automations", "guidance", "prompts"):
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
        self.prompts_dir = self.workspace / "prompts"

        self.runs_dir = self.tmp_path / "runs"
        self.runs_dir.mkdir()
        self.agent_home = self.tmp_path / "agent-home"

        env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFIER_AGENT_HOME": str(self.agent_home),
                "AMPLIFIER_AGENT_WORKSPACE": "",
                "CONTEXT_INTELLIGENCE_PERSONAL": "",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _write_automation(self, body: str, filename: str) -> None:
        path = self.workspace / "automations" / filename
        path.write_text(body, encoding="utf-8")
        self.automation = load(path)

    def _run(self, *, outcome: runner._TurnOutcome) -> runner.RunResult:
        with (
            mock.patch.object(runner, "_submit_turn", return_value=outcome),
            redirect_stderr(io.StringIO()),
        ):
            return runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )


class TestFailedRunSurfacesItsError(_RunFixture):
    """A failed run must carry a non-null top-level ``error`` (the failing
    turn's own error), not the silent ``failed: true, error: null`` shape
    measured on channels-check and gsuite-email-check.
    """

    def test_step_failure_error_reaches_top_level_and_result_json(self) -> None:
        self._write_automation(
            _STEP_AUTOMATION.format(name="Teams Check"), "teams-check.md"
        )

        # a failed turn -> _execute_turn records step error "amplifier-agent
        # exited 1" (runner.py). The exact stderr text is irrelevant; the
        # error string is derived from the outcome's error.
        result = self._run(
            outcome=runner._TurnOutcome(
                error="amplifier-agent exited 1", stderr_text="boom on the provider\n"
            )
        )

        self.assertTrue(result.failed)
        # THE FIX: before it, this was None even though a step carried the
        # error -- the silent-failure defect class.
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("amplifier-agent exited 1", result.error)

        # The step that failed did record the error all along -- proving the
        # top-level null was a dropped-on-the-floor propagation bug, not a
        # missing error.
        step_errors = [s.error for s in result.steps if s.error]
        self.assertIn("amplifier-agent exited 1", step_errors)

        # And it is durable: the persisted result.json a human / drumbeat doctor
        # reads is loud too.
        run_dir = self.runs_dir / self.automation.slug / result.run_id
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(persisted["failed"])
        self.assertIsNotNone(persisted["error"])
        self.assertIn("amplifier-agent exited 1", persisted["error"])

    def test_successful_run_has_no_error(self) -> None:
        """Negative control: the loudness change must not manufacture an error
        on a run that actually succeeded.
        """
        self._write_automation(
            _STEP_AUTOMATION.format(name="Teams Check"), "teams-check.md"
        )
        result = self._run(
            outcome=runner._TurnOutcome(
                reply="ok", tokens_in=3, tokens_out=3, duration_ms=5
            )
        )
        self.assertFalse(result.failed)
        self.assertIsNone(result.error)


class TestChatMessageSurfacesItsError(_RunFixture):
    """``run_chat_message`` must carry the same non-null top-level ``error``
    a scheduled ``run()`` does when a turn fails.

    Before this fix, ``run_chat_message`` derived its top-level ``error``
    from ``turns[-1].error`` -- a POSITIONAL pattern, unlike ``_run_body``'s
    robust ``next((s.error for s in step_results if s.error), <fallback>)``
    search. ``_persist_run``'s own choke-point fix (see that function's
    docstring) named this "the weaker turns[-1].error pattern" as the next
    latent silent-failure risk without itself closing it -- it only
    happened to be safe because every failing turn here is either the last
    one appended before a ``break`` or is followed solely by
    ``if not failed:``-gated code. This locks in the now-structurally-
    guaranteed (not just incidentally true) behavior through the real
    ``run_chat_message`` entry point, mirroring
    ``TestFailedRunSurfacesItsError`` for the scheduled path.
    """

    def _run_chat(self, *, outcome: runner._TurnOutcome) -> runner.RunResult:
        with (
            mock.patch.object(runner, "_submit_turn", return_value=outcome),
            redirect_stderr(io.StringIO()),
        ):
            return runner.run_chat_message(
                self.automation,
                "what needs my attention?",
                cwd=self.workspace,
                runs_dir=self.runs_dir,
            )

    def test_message_turn_failure_reaches_top_level_and_result_json(self) -> None:
        self._write_automation(_STEP_AUTOMATION.format(name="Chat"), "chat.md")

        # a failed turn -> _execute_turn records step error "amplifier-agent
        # exited 1" on the identity turn (this automation's one declared
        # step, fired first on a brand-new chat session).
        result = self._run_chat(
            outcome=runner._TurnOutcome(
                error="amplifier-agent exited 1", stderr_text="boom on the provider\n"
            )
        )

        self.assertTrue(result.failed)
        # THE FIX: before it, a caller could reach ``turns[-1].error`` being
        # None while `failed` was True if a later turn ever ran after the
        # failing one; this proves the top-level error is always populated
        # from whichever turn actually failed.
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("amplifier-agent exited 1", result.error)

        step_errors = [s.error for s in result.steps if s.error]
        self.assertIn("amplifier-agent exited 1", step_errors)

        run_dir = self.runs_dir / self.automation.slug / result.run_id
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(persisted["failed"])
        self.assertIsNotNone(persisted["error"])
        self.assertIn("amplifier-agent exited 1", persisted["error"])

    def test_successful_chat_message_has_no_error(self) -> None:
        """Negative control: a chat message that actually succeeds must not
        have an error manufactured for it.
        """
        self._write_automation(_STEP_AUTOMATION.format(name="Chat"), "chat.md")
        result = self._run_chat(
            outcome=runner._TurnOutcome(
                reply="ok", tokens_in=3, tokens_out=3, duration_ms=5
            )
        )
        self.assertFalse(result.failed)
        self.assertIsNone(result.error)


class TestInjectSkippedGuardCatchesOutboxError(_RunFixture):
    """An inject_skipped bookkeeping write that raises ``OutboxWriteError``
    (NOT an ``OSError``) must be caught and swallowed -- the run proceeds to
    its step and completes. A bare ``except OSError`` let it escape and kill
    the whole turn.
    """

    def test_outbox_error_on_inject_skipped_does_not_kill_the_run(self) -> None:
        self._write_automation(_INJECT_AUTOMATION, "inject-guard-check.md")

        real_append = engine_events.append_event

        def fake_append(runs_dir, event_type, payload):
            if event_type is engine_events.EventType.INJECT_SKIPPED:
                # Same class the real emit raises on field-validation failure
                # (e.g. a blank run_id) -- a subclass of OutboxError, and
                # crucially NOT an OSError.
                raise engine_events.OutboxWriteError(
                    "simulated inject_skipped validation failure"
                )
            return real_append(runs_dir, event_type, payload)

        with (
            mock.patch.object(
                runner, "_run_inject_tool", return_value=runner.InjectOutcome(idle=True)
            ),
            mock.patch.object(
                runner.engine_events, "append_event", side_effect=fake_append
            ),
            mock.patch.object(
                runner,
                "_submit_turn",
                return_value=runner._TurnOutcome(
                    reply="ok", tokens_in=3, tokens_out=3, duration_ms=5
                ),
            ),
            redirect_stderr(io.StringIO()),
        ):
            # The assertion is simply that this returns rather than raising
            # OutboxWriteError out of the inject loop.
            result = runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )

        self.assertFalse(result.failed)
        self.assertIsNone(result.error)


class TestScheduleStateRoundTrip(unittest.TestCase):
    """The persistence primitive: save/load is lossless, and load is
    fail-loud-not-fail-silent on a corrupt file (returns {} rather than
    raising or guessing).
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_dir = Path(self._tmp.name)

    def test_save_then_load_roundtrips(self) -> None:
        due = {"channels-check": 1786609804.7, "email-check": 1786612289.5}
        schedule_state.save(self.runs_dir, due)
        self.assertEqual(schedule_state.load(self.runs_dir), due)

    def test_missing_file_loads_empty(self) -> None:
        self.assertEqual(schedule_state.load(self.runs_dir), {})

    def test_corrupt_file_loads_empty_without_raising(self) -> None:
        schedule_state.state_path(self.runs_dir).write_text(
            "{not json", encoding="utf-8"
        )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(schedule_state.load(self.runs_dir), {})

    def test_foreign_format_is_refused_not_guessed(self) -> None:
        schedule_state.state_path(self.runs_dir).write_text(
            json.dumps({"schedule_state_format": 999, "next_due": {"x": 1.0}}),
            encoding="utf-8",
        )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(schedule_state.load(self.runs_dir), {})


class _StopTick(Exception):
    """Sentinel raised in place of the scheduler's end-of-tick sleep to stop
    ``serve`` after exactly one tick."""


class TestSchedulerReloadsPersistedDueTimes(unittest.TestCase):
    """f199 schedule half: on startup ``serve`` must seed ``next_due`` from
    disk, so an automation whose persisted due time already elapsed while the
    scheduler was down fires on the FIRST tick after restart -- instead of
    being re-registered a full interval into the future (the clock-reset that
    starved the 90-minute channels-check).
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.automations_dir = self.tmp_path / "automations"
        self.automations_dir.mkdir()
        self.runs_dir = self.tmp_path / "runs"
        self.runs_dir.mkdir()
        self.prompts_dir = self.tmp_path / "prompts"
        self.prompts_dir.mkdir()

        path = self.automations_dir / "channels-check.md"
        path.write_text(_SCHEDULED_AUTOMATION, encoding="utf-8")
        self.slug = load(path).slug

    def _serve_one_tick(self) -> list[str]:
        """Run serve() for exactly one tick, returning the slugs it invoked
        ``run`` for."""
        ran: list[str] = []

        def fake_run(automation, **kwargs):
            ran.append(automation.slug)
            return types.SimpleNamespace(
                failed=False, run_id="test-run", notified=False
            )

        def stop_sleep(_seconds):
            raise _StopTick

        with (
            mock.patch.object(scheduler, "run", side_effect=fake_run),
            mock.patch("drumbeat.scheduler.time.sleep", side_effect=stop_sleep),
            redirect_stderr(io.StringIO()),
        ):
            try:
                scheduler.serve(
                    self.automations_dir,
                    cwd=self.tmp_path,
                    runs_dir=self.runs_dir,
                    prompts_dir=self.prompts_dir,
                    write_fingerprint=False,
                )
            except _StopTick:
                pass
        return ran

    def test_past_due_persisted_time_fires_on_first_tick(self) -> None:
        # Persist a due time already 10 minutes in the past (as a real
        # restart-after-downtime would leave behind).
        schedule_state.save(self.runs_dir, {self.slug: time.time() - 600})

        ran = self._serve_one_tick()

        # THE FIX: reloaded past-due -> runs immediately. Without persistence
        # reload, next_due starts empty, the automation registers as
        # now + 90min, and it would NOT run on the first tick.
        self.assertIn(self.slug, ran)

    def test_fresh_start_does_not_fire_immediately(self) -> None:
        """Negative control: with NO persisted state, a first start registers
        the automation for a future run and must NOT fire on tick one -- this
        is what proves the test above passes because of the reload, not because
        the scheduler fires everything on tick one.
        """
        ran = self._serve_one_tick()
        self.assertNotIn(self.slug, ran)


class TestPersistRunChokePointEnforcesLoudness(_RunFixture):
    """The single run-persistence choke point (`_persist_run`) must ITSELF
    enforce the loudness invariant -- not trust each of its callers to have
    populated ``result.error`` first.

    The earlier loudness fix taught exactly ONE upstream caller (``_run_body``,
    the scheduled path) to lift the failing step's error to the top level. But
    ``_persist_run`` -- the single choke point every run passes through --
    still wrote ``result.error`` VERBATIM into both result.json and the
    ``RUN_COMPLETED`` event; only ``failures.log`` and the ``automation_error``
    notification carried a step-derived fallback. So the loudness was enforced
    per-caller, not structurally, and the run record + the RUN_COMPLETED event
    (the surfaces ``drumbeat doctor`` and the runs API read) stayed silent for
    ANY RunResult that reached the choke point with the live silent shape:

        failed=True, error=None, steps=[StepResult(error="amplifier-agent exited 1")]

    That is the exact shape measured on the owner's box (recency-check 4/4,
    ``result.json`` ``"failed": true, "error": null`` with the real reason
    stranded in ``steps[0].error``). This drives the choke point directly with
    that shape and proves both durable surfaces are loud -- so no present or
    future caller can reintroduce the second silent path.
    """

    def _persist_live_silent_shape(self) -> runner.RunResult:
        automation = runner._synthesize_session_automation("recency-check-silent")
        step = runner.StepResult(
            index=0,
            text="[drumbeat] ... Required guidance files: guidance/RECENCY.md",
            reply="",
            error="amplifier-agent exited 1",
            duration_ms=0,
            tokens_in=0,
            tokens_out=0,
        )
        result = runner.RunResult(
            automation=automation.name,
            run_id="20260819T035046Z-3cafc1",
            session_id="recency-check-silent",
            started_at="2026-08-19T03:50:46Z",
            finished_at="2026-08-19T03:50:48Z",
            steps=[step],
            final_reply="",
            notified=False,
            failed=True,
            # The live silent shape: a step carries the real reason, the
            # top-level error is null. A caller that forgot to lift it (or a
            # future caller with a weaker pattern than _run_body's) lands here.
            error=None,
            session_resumed=True,
        )
        with redirect_stderr(io.StringIO()):
            runner._persist_run(
                automation=automation,
                result=result,
                runs_dir=self.runs_dir,
                stderr_chunks=[(0, "[PROVIDER] Anthropic API error ...\n")],
                intent=runner._aborted_run_intent("amplifier-agent exited 1"),
                final_reply_rule="test choke-point loudness",
            )
        return result

    def test_result_json_is_loud_even_when_caller_left_error_null(self) -> None:
        result = self._persist_live_silent_shape()

        run_dir = (
            self.runs_dir
            / runner._sanitize_run_id("recency-check-silent")
            / result.run_id
        )
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))

        self.assertTrue(persisted["failed"])
        # RED before the choke-point fix: result.json's top-level error was
        # written verbatim as null, so `drumbeat doctor` / the runs API saw a
        # failure with no reason.
        self.assertIsNotNone(persisted["error"])
        self.assertIn("amplifier-agent exited 1", persisted["error"])

    def test_run_completed_event_is_loud_even_when_caller_left_error_null(
        self,
    ) -> None:
        result = self._persist_live_silent_shape()

        events, _ = engine_events.read_since(self.runs_dir, 0)
        completed = [
            e.data
            for e in events
            if e.event_type is engine_events.EventType.RUN_COMPLETED
            and e.data.get("run_id") == result.run_id
        ]
        self.assertEqual(len(completed), 1)
        # RED before the fix: the RUN_COMPLETED event -- the surface downstream
        # analytics read -- carried "error": null while the step held the real
        # reason.
        self.assertIsNotNone(completed[0]["error"])
        self.assertIn("amplifier-agent exited 1", completed[0]["error"])

    def test_choke_point_does_not_manufacture_error_on_success(self) -> None:
        """Negative control: a NON-failed run persisted through the choke point
        must keep ``error`` null -- the loudness normalization only fires for
        failed runs.
        """
        automation = runner._synthesize_session_automation("recency-check-ok")
        step = runner.StepResult(
            index=0,
            text="do the thing",
            reply="all good",
            error=None,
            duration_ms=5,
            tokens_in=3,
            tokens_out=3,
        )
        result = runner.RunResult(
            automation=automation.name,
            run_id="20260819T040000Z-abc123",
            session_id="recency-check-ok",
            started_at="2026-08-19T04:00:00Z",
            finished_at="2026-08-19T04:00:01Z",
            steps=[step],
            final_reply="all good",
            notified=False,
            failed=False,
            error=None,
            session_resumed=True,
        )
        with redirect_stderr(io.StringIO()):
            runner._persist_run(
                automation=automation,
                result=result,
                runs_dir=self.runs_dir,
                stderr_chunks=[(0, "")],
                intent=runner._session_run_intent("all good"),
                final_reply_rule="test success control",
            )

        run_dir = (
            self.runs_dir / runner._sanitize_run_id("recency-check-ok") / result.run_id
        )
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(persisted["failed"])
        self.assertIsNone(persisted["error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
