"""Proof that two already-implemented run-health mechanisms are actually wired.

Both mechanisms live in ``src/drumbeat/runner.py`` and were ported from an
earlier codebase. This file does not
implement anything new -- it drives the real production functions
end-to-end and checks their real, on-disk and in-outbox side effects. If a
test here goes red, the mechanism it names is broken; the fix belongs in
``src/drumbeat/``, not in this file.

Mechanism 1 -- ceiling-detect + auto-rotate
    ``runner.run()`` persists the run record (``_persist_run``, runner.py
    :3280) and only THEN checks ``session_health.detect_ceiling_hit`` over
    the run's captured stderr (runner.py:3297-3321). A hit calls
    ``_auto_rotate`` (runner.py:523), which deletes the automation's pin
    (``session_pins.delete``), appends one line to
    ``<runs_dir>/session_rotations.jsonl`` (``rotation_log.log_session_rotation``,
    ``reason=f"auto: {reason}"``), forgets the session's contract
    fingerprint, and emits an ``engine_events.EventType.SESSION_ROTATED``
    event. Guarded by ``if failed and not dry_run:``.

Mechanism 2 -- fail-loud failure push
    ``runner._notify_run_failure`` (runner.py:3797, called from
    ``_persist_run`` at runner.py:3949) emits an
    ``engine_events.EventType.AUTOMATION_ERROR`` event for ANY failed run,
    deliberately bypassing the automation's own ``notify:`` frontmatter
    policy -- a ``notify: never`` automation still reports its failures.

Real-world facts these tests are anchored to (see session_health.py's own
module docstring for the first two, measured on this project's history):

- ``teams-check`` failed on the owner's live box with
  ``prompt is too long: 210347 tokens > 200000 maximum``: its pinned
  session had crossed the provider's 200k-token ceiling. amplifier-agent's
  context bundle only auto-compacts at 240k, so a session whose prompt
  lands in the 200k-240k dead zone can never compact its way out and fails
  EVERY subsequent run until rotated.
- ``recording-brief`` failed 10 runs in a row on a provider edge; a manual
  rotation cleared it. Auto-rotation would have capped that at 1 run.
- The prior deployment's own history: a dead, log-only automation stayed
  dead for 27 hours because nothing pushed its failure anywhere -- the reason
  ``_notify_run_failure`` overrides ``notify:`` policy instead of obeying it.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import engine_events, runner, session_pins
from drumbeat.automation import load
from drumbeat.paths import derive_workspace_slug

# The owner's own measured failure string (session_health.CEILING_RE match).
CEILING_STDERR = "prompt is too long: 210347 tokens > 200000 maximum\n"

_AUTOMATION = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: manual
  notify: {notify}
---

1. Do the thing.
"""


class _RunnerFixture(unittest.TestCase):
    """Shared workspace/runs_dir/agent-home wiring for every test below.

    House style (see test_soft_launch_gates.py): drive the REAL production
    functions; mock only their inputs. The only thing mocked anywhere in
    this file is ``runner._submit_turn`` -- the established seam for faking
    the SDK-driven turn (its exact ``_TurnOutcome`` contract is read from
    ``runner._TurnOutcome`` and ``_execute_turn`` in runner.py, not guessed).
    """

    NOTIFY = "never"

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

        # Real inputs, not a mocked mechanism: AMPLIFIER_AGENT_HOME/
        # AMPLIFIER_AGENT_WORKSPACE are the exact two env overrides
        # drumbeat.paths documents, and CONTEXT_INTELLIGENCE_PERSONAL unset
        # keeps ci_events/ci_upload's own (already best-effort, never
        # raising) no-API-key path deterministic rather than dependent on
        # whatever the host shell happens to export.
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

        automation_path = self.workspace / "automations" / "teams-check.md"
        automation_path.write_text(
            _AUTOMATION.format(name="Teams Check", notify=self.NOTIFY),
            encoding="utf-8",
        )
        self.automation = load(automation_path)
        self.workspace_slug = derive_workspace_slug(self.workspace)

    # ---- helpers -----------------------------------------------------

    def _pin_real_session(self, session_id: str) -> None:
        """Upsert a pin AND create the on-disk session dir it must probe as EXISTS.

        Mirrors amplifier-agent's own session layout exactly (see
        ``runner._session_dir``), so ``runner.run()``'s pinned-session
        probe (``runner._probe_session``) resolves this as a real,
        resumable session -- never MISSING -- which is what makes the
        ceiling-hit rotation below act on the SAME session id we pinned,
        not a silently-recreated one.
        """
        session_pins.upsert(
            self.automation.slug,
            session_id=session_id,
            session_workspace=self.workspace_slug,
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=self.runs_dir,
        )
        session_dir = (
            self.agent_home
            / "state"
            / "workspaces"
            / self.workspace_slug
            / "sessions"
            / session_id
        )
        session_dir.mkdir(parents=True)
        (session_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

    def _rotation_lines(self) -> list[dict]:
        path = self.runs_dir / "session_rotations.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _outbox_events(self, event_type: engine_events.EventType) -> list[dict]:
        events, _ = engine_events.read_since(self.runs_dir, 0)
        return [e.data for e in events if e.event_type is event_type]

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


class TestCeilingHitAutoRotatesExactlyOnceThenSelfHeals(_RunnerFixture):
    """Guards the real, owner-measured deadlock: ``teams-check`` on the
    owner's live box failed with
    ``prompt is too long: 210347 tokens > 200000 maximum`` -- its pinned
    session had crossed the provider's 200k-token ceiling.
    amplifier-agent's context bundle only auto-compacts at 240k, so a
    session whose prompt lands in the 200k-240k dead zone can NEVER
    compact its way out and would fail every subsequent run forever
    without rotation (measured elsewhere on this project's history as 12
    consecutive channels-check failures, and separately as
    recording-brief's 10 straight failures on a provider edge, cleared only
    by a manual rotation). Auto-rotation exists to cap that blast radius at
    exactly the one run that already failed -- this test proves it does,
    and that the very next run self-heals with a brand-new pin instead of
    staying dead.

    Ported from an earlier codebase (a ceiling check and an auto-rotate
    step); this project's equivalents are runner.py:3297-3321 and
    runner.py:523.
    """

    def test_ceiling_failure_rotates_once_and_next_run_gets_a_fresh_pin(self) -> None:
        old_session_id = f"{self.automation.slug}-20260801T000000Z-aaaaaa"
        self._pin_real_session(old_session_id)

        # ---- A: the ceiling-failing run -------------------------------
        result_a = self._run(
            outcome=runner._TurnOutcome(
                error="amplifier-agent exited 1", stderr_text=CEILING_STDERR
            )
        )

        self.assertTrue(result_a.failed)
        self.assertEqual(result_a.session_id, old_session_id)

        # The evidence must outlive the rotation: the failed run's own
        # result.json is on disk (written by _persist_run BEFORE run()
        # ever checks for a ceiling hit -- runner.py:3280 vs :3297).
        run_dir = self.runs_dir / self.automation.slug / result_a.run_id
        self.assertTrue((run_dir / "result.json").is_file())

        # EXACTLY one rotation was logged, naming the abandoned session
        # and a ceiling-shaped reason.
        rotations = self._rotation_lines()
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["old_session_id"], old_session_id)
        self.assertTrue(rotations[0]["reason"].startswith("auto:"))
        self.assertIn("ceiling", rotations[0]["reason"])

        # The pin for this slug is gone.
        pins_after_a = session_pins.read_all(self.runs_dir)
        self.assertNotIn(self.automation.slug, pins_after_a)

        # A session_rotated event landed in the outbox.
        rotated_events = self._outbox_events(engine_events.EventType.SESSION_ROTATED)
        self.assertEqual(len(rotated_events), 1)
        self.assertEqual(rotated_events[0]["old_session_id"], old_session_id)

        # ---- B: the very next run self-heals with a fresh pin ---------
        result_b = self._run(
            outcome=runner._TurnOutcome(
                reply="ok", tokens_in=3, tokens_out=3, duration_ms=5
            )
        )

        self.assertFalse(result_b.failed)
        pin_after_b = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin_after_b)
        assert pin_after_b is not None
        self.assertNotEqual(pin_after_b.session_id, old_session_id)
        self.assertEqual(pin_after_b.session_id, result_b.session_id)

        # Still exactly one rotation on record: self-healing never rotates
        # again on its own.
        self.assertEqual(len(self._rotation_lines()), 1)


class TestNonCeilingFailureDoesNotRotate(_RunnerFixture):
    """Negative control. Without this, TestCeilingHitAutoRotates... could
    pass for the wrong reason (e.g. "any failed run rotates" instead of
    "a ceiling hit rotates"). An ordinary provider/tool error -- a bare
    Python traceback with no ceiling string in it -- must leave the pinned
    session exactly as it was: this is what proves the check can genuinely
    fail to fire, not just always trigger.
    """

    def test_ordinary_failure_leaves_the_pin_alone(self) -> None:
        session_id = f"{self.automation.slug}-20260802T000000Z-bbbbbb"
        self._pin_real_session(session_id)

        ordinary_stderr = (
            "Traceback (most recent call last):\n"
            '  File "amplifier_agent/cli.py", line 42, in run\n'
            "    raise ValueError('boom')\n"
            "ValueError: boom\n"
        )
        result = self._run(
            outcome=runner._TurnOutcome(
                error="amplifier-agent exited 1", stderr_text=ordinary_stderr
            )
        )

        self.assertTrue(result.failed)
        self.assertEqual(self._rotation_lines(), [])
        self.assertEqual(
            self._outbox_events(engine_events.EventType.SESSION_ROTATED), []
        )

        pin_after = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin_after)
        assert pin_after is not None
        self.assertEqual(pin_after.session_id, session_id)


class TestNotifyNeverStillPushesFailure(_RunnerFixture):
    """Guards the prior deployment's own history: a dead, log-only automation (the
    ``notify: never`` shape -- it runs for side effects, never pushes)
    stayed dead for 27 hours because nothing pushed its failure anywhere.
    ``_notify_run_failure`` (runner.py:3797, invoked from ``_persist_run``
    at :3949) is the fix: it bypasses ``automation.notify`` entirely and
    emits ``automation_error`` unconditionally for ANY failed run.
    Ported from an earlier codebase.

    The automation here declares ``notify: never`` (this fixture's
    default); the assertions prove the failure report is a SEPARATE,
    unconditional channel and not the ordinary notify policy leaking:
    ``result.notified`` stays False (never pushed as a normal
    notification) while an ``automation_error`` event still lands in the
    outbox, carrying this run's id and a real reason.
    """

    def test_notify_never_still_emits_automation_error(self) -> None:
        self.assertEqual(self.automation.notify, "never")
        session_id = f"{self.automation.slug}-20260803T000000Z-cccccc"
        self._pin_real_session(session_id)

        ordinary_stderr = "some ordinary tool failure, not a ceiling hit\n"
        result = self._run(
            outcome=runner._TurnOutcome(
                error="amplifier-agent exited 1", stderr_text=ordinary_stderr
            )
        )

        self.assertTrue(result.failed)
        # The point: the failure report is a separate channel, not the
        # notify policy leaking -- a notify: never run is never "notified"
        # in the ordinary sense even though its failure IS reported.
        self.assertFalse(result.notified)

        errors = self._outbox_events(engine_events.EventType.AUTOMATION_ERROR)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["run_id"], result.run_id)
        self.assertTrue(errors[0]["reason"].strip())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
