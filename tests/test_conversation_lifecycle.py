"""The ``conversation:`` per-automation session lifecycle (drumbeat-5wt).

Three lifecycles, one closed vocabulary:

- ``continuous`` (default): one pinned conversation, resumed every run, rotated
  only by the engine's own health signals -- byte-for-byte the behavior that
  existed before this key.
- ``fresh``: a new conversation on every run.
- ``daily``: one conversation per host-local calendar day; the first run after
  local midnight rotates.

House style, mirroring ``test_auto_rotation_and_failure_push.py``: drive the
REAL production functions; mock only their inputs. The only seams mocked here
are ``runner._submit_turn`` (see ``runner._TurnOutcome``) and, for the
deterministic daily-boundary tests, ``runner._local_now`` (the single clock the
daily lifecycle reads). Every rotation is asserted against its real on-disk
side effects -- the pin store, ``session_rotations.jsonl``, and the outbox --
exactly as the ceiling-rotation test does, because ``fresh``/``daily`` rotate
through the very same ``_auto_rotate`` path.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import ExitStack, redirect_stderr
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import engine_events, runner, session_health, session_pins
from drumbeat.automation import AutomationError, load, load_from_text
from drumbeat.paths import derive_workspace_slug

_AUTOMATION = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: manual
  notify: never
{conversation_line}  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""


def _automation_text(*, name: str = "Lifecycle Check", conversation: str | None) -> str:
    line = "" if conversation is None else f"  conversation: {conversation}\n"
    return _AUTOMATION.format(name=name, conversation_line=line)


# ---------------------------------------------------------------------------
# Parser: closed vocabulary, default, and the retired-key remediation pointer
# ---------------------------------------------------------------------------


class TestConversationParsing(unittest.TestCase):
    def _load(self, conversation: str | None):
        return load_from_text(
            Path("mem://lifecycle.md"),
            _automation_text(conversation=conversation),
        )

    def test_absent_key_defaults_to_continuous(self) -> None:
        # "Given no key, behavior is byte-identical to today" starts here: the
        # parsed value is exactly the pre-existing behavior's name.
        self.assertEqual(self._load(None).conversation, "continuous")

    def test_each_valid_value_is_accepted(self) -> None:
        for value in ("continuous", "fresh", "daily"):
            with self.subTest(value=value):
                self.assertEqual(self._load(value).conversation, value)

    def test_unknown_value_is_refused_loudly(self) -> None:
        with self.assertRaises(AutomationError) as ctx:
            self._load("weekly")
        problem = ctx.exception.problem
        self.assertIn("conversation", problem)
        self.assertIn("weekly", problem)
        # The valid set is named so the author can fix it without the docs.
        self.assertIn("continuous", problem)
        self.assertIn("daily", problem)
        self.assertIn("fresh", problem)

    def test_retired_session_key_points_at_conversation(self) -> None:
        # The retired `session:` refusal stays a refusal, and now also routes
        # an author reaching for lifecycle control to the new key.
        text = (
            "---\n"
            "automation:\n"
            "  name: Old Pin\n"
            "  trigger:\n"
            "    type: manual\n"
            "  session: some-old-session-id\n"
            "---\n\n"
            "1. Do the thing.\n"
        )
        with self.assertRaises(AutomationError) as ctx:
            load_from_text(Path("mem://old.md"), text)
        self.assertIn("conversation:", ctx.exception.problem)


# ---------------------------------------------------------------------------
# The rotation-reason predicate in isolation (deterministic clock injection)
# ---------------------------------------------------------------------------


class TestConversationRotationReason(unittest.TestCase):
    """``runner._conversation_rotation_reason`` is the whole decision. Testing
    it directly is the deterministic core of the daily-boundary proof: the
    clock is injected, the anchor is written explicitly, and no subprocess or
    session directory is involved.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_dir = Path(self._tmp.name)

    def _automation(self, conversation: str):
        return load_from_text(
            Path("mem://a.md"), _automation_text(conversation=conversation)
        )

    def _at(self, day: str):
        """Patch the local clock to 09:00 on ``day`` (YYYY-MM-DD)."""
        y, m, d = (int(p) for p in day.split("-"))
        return mock.patch.object(
            runner, "_local_now", return_value=datetime(y, m, d, 9, 0).astimezone()
        )

    def test_continuous_never_rotates_and_reads_nothing(self) -> None:
        # continuous must return None WITHOUT touching the clock or the store,
        # so the resumed-run path stays byte-identical. A poisoned clock and a
        # poisoned store would both blow up if it read them; it must not.
        with (
            mock.patch.object(
                runner, "_local_now", side_effect=AssertionError("clock read")
            ),
            mock.patch.object(
                session_health,
                "read_lifecycle_anchor_day",
                side_effect=AssertionError("store read"),
            ),
        ):
            reason = runner._conversation_rotation_reason(
                self._automation("continuous"),
                session_id="s-1",
                runs_dir=self.runs_dir,
            )
        self.assertIsNone(reason)

    def test_fresh_always_rotates(self) -> None:
        reason = runner._conversation_rotation_reason(
            self._automation("fresh"), session_id="s-1", runs_dir=self.runs_dir
        )
        assert reason is not None
        self.assertIn("fresh", reason)

    def test_daily_same_day_does_not_rotate(self) -> None:
        session_health.record_lifecycle(
            session_id="s-1",
            mode="daily",
            anchor_day="2026-08-24",
            runs_dir=self.runs_dir,
        )
        with self._at("2026-08-24"):
            reason = runner._conversation_rotation_reason(
                self._automation("daily"), session_id="s-1", runs_dir=self.runs_dir
            )
        self.assertIsNone(reason)

    def test_daily_next_day_rotates(self) -> None:
        session_health.record_lifecycle(
            session_id="s-1",
            mode="daily",
            anchor_day="2026-08-24",
            runs_dir=self.runs_dir,
        )
        with self._at("2026-08-25"):
            reason = runner._conversation_rotation_reason(
                self._automation("daily"), session_id="s-1", runs_dir=self.runs_dir
            )
        assert reason is not None
        self.assertIn("daily", reason)
        self.assertIn("2026-08-24", reason)  # names the anchor it left behind
        self.assertIn("2026-08-25", reason)  # ...and the day that crossed it

    def test_daily_with_no_recorded_anchor_does_not_rotate(self) -> None:
        # "unknown must not read as yes": a session with no anchor (predating
        # the feature, or a failed lifecycle write) is never abandoned on
        # missing data. Same posture the drift check takes.
        with self._at("2026-08-25"):
            reason = runner._conversation_rotation_reason(
                self._automation("daily"), session_id="unseen", runs_dir=self.runs_dir
            )
        self.assertIsNone(reason)

    def test_daily_ignores_a_future_anchor(self) -> None:
        # Clock skew / a backwards jump must not rotate -- only a day STRICTLY
        # later than the anchor does.
        session_health.record_lifecycle(
            session_id="s-1",
            mode="daily",
            anchor_day="2026-08-26",
            runs_dir=self.runs_dir,
        )
        with self._at("2026-08-25"):
            reason = runner._conversation_rotation_reason(
                self._automation("daily"), session_id="s-1", runs_dir=self.runs_dir
            )
        self.assertIsNone(reason)


# ---------------------------------------------------------------------------
# End-to-end through runner.run(): real pins, real rotation records
# ---------------------------------------------------------------------------


class _RunnerFixture(unittest.TestCase):
    """Workspace/runs_dir/agent-home wiring, mirroring the auto-rotation test."""

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
            "os.environ",
            {
                "AMPLIFIER_AGENT_HOME": str(self.agent_home),
                "AMPLIFIER_AGENT_WORKSPACE": "",
                "CONTEXT_INTELLIGENCE_PERSONAL": "",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        self.workspace_slug = derive_workspace_slug(self.workspace)

    def _write_automation(self, conversation: str | None):
        path = self.workspace / "automations" / "lifecycle-check.md"
        path.write_text(_automation_text(conversation=conversation), encoding="utf-8")
        self.automation = load(path)

    def _make_session_dir(self, session_id: str) -> None:
        """Create the on-disk amplifier-agent session dir so a probe resolves
        it as EXISTS (also creates the workspace dir the probe checks first).
        """
        session_dir = (
            self.agent_home
            / "state"
            / "workspaces"
            / self.workspace_slug
            / "sessions"
            / session_id
        )
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

    def _pin_real_session(self, session_id: str) -> None:
        session_pins.upsert(
            self.automation.slug,
            session_id=session_id,
            session_workspace=self.workspace_slug,
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=self.runs_dir,
        )
        self._make_session_dir(session_id)

    def _rotation_lines(self) -> list[dict]:
        path = self.runs_dir / "session_rotations.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _rotated_events(self) -> list[dict]:
        events, _ = engine_events.read_since(self.runs_dir, 0)
        return [
            e.data
            for e in events
            if e.event_type is engine_events.EventType.SESSION_ROTATED
        ]

    def _run(self, *, now: datetime | None = None) -> runner.RunResult:
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    runner,
                    "_submit_turn",
                    return_value=runner._TurnOutcome(
                        reply="ok", tokens_in=3, tokens_out=3, duration_ms=5
                    ),
                )
            )
            if now is not None:
                stack.enter_context(
                    mock.patch.object(runner, "_local_now", return_value=now)
                )
            stack.enter_context(redirect_stderr(io.StringIO()))
            return runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )


class TestContinuousIsUnchanged(_RunnerFixture):
    """No key == continuous == today's behavior: the same conversation resumes
    every run and nothing rotates on its own.
    """

    def test_no_key_resumes_the_same_session_and_never_rotates(self) -> None:
        self._write_automation(None)
        original = f"{self.automation.slug}-20260820T000000Z-aaaaaa"
        self._pin_real_session(original)

        for _ in range(2):
            result = self._run()
            self.assertFalse(result.failed)
            self.assertEqual(result.session_id, original)

        self.assertEqual(self._rotation_lines(), [])
        self.assertEqual(self._rotated_events(), [])
        pin = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        assert pin is not None
        self.assertEqual(pin.session_id, original)
        # continuous writes NO lifecycle sidecar -- the session_contracts.json
        # entry is exactly what it was before this field existed.
        self.assertIsNone(
            session_health.read_lifecycle_anchor_day(original, runs_dir=self.runs_dir)
        )


class TestFreshStartsANewSessionEveryRun(_RunnerFixture):
    """conversation: fresh -- two runs, two distinct session ids."""

    def test_two_runs_yield_two_distinct_sessions(self) -> None:
        self._write_automation("fresh")

        # Run 1 bootstraps: no pin -> a brand-new session is created and pinned.
        result_1 = self._run()
        self.assertFalse(result_1.failed)
        first_id = result_1.session_id
        pin_1 = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        assert pin_1 is not None
        self.assertEqual(pin_1.session_id, first_id)

        # Make run 1's session resolvable so run 2 probes it as EXISTS and the
        # fresh lifecycle -- not a MISSING probe -- is what forces the new one.
        self._make_session_dir(first_id)

        # Run 2: fresh rotates run 1's session and starts another.
        result_2 = self._run()
        self.assertFalse(result_2.failed)
        second_id = result_2.session_id

        self.assertNotEqual(first_id, second_id)  # two runs, two distinct ids
        pin_2 = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        assert pin_2 is not None
        self.assertEqual(pin_2.session_id, second_id)

        # The rotation went through the shared path: exactly the run-2 rotation
        # is on record, naming the abandoned session, with a fresh-shaped reason.
        rotations = self._rotation_lines()
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["old_session_id"], first_id)
        self.assertTrue(rotations[0]["reason"].startswith("auto:"))
        self.assertIn("fresh", rotations[0]["reason"])
        self.assertEqual(len(self._rotated_events()), 1)


class TestDailyRotatesExactlyOnceAcrossMidnight(_RunnerFixture):
    """conversation: daily -- runs straddling a simulated midnight rotate the
    conversation exactly once, and same-day runs resume it.
    """

    def _at(self, day: str) -> datetime:
        y, m, d = (int(p) for p in day.split("-"))
        return datetime(y, m, d, 9, 0).astimezone()

    def test_one_rotation_across_the_boundary_then_resume(self) -> None:
        self._write_automation("daily")
        original = f"{self.automation.slug}-20260824T000000Z-aaaaaa"
        self._pin_real_session(original)
        # Anchor the pinned session to day 1, as a real creation would.
        session_health.record_lifecycle(
            session_id=original,
            mode="daily",
            anchor_day="2026-08-24",
            runs_dir=self.runs_dir,
        )

        # ---- Run A: same local day -> resume, no rotation ----------------
        result_a = self._run(now=self._at("2026-08-24"))
        self.assertFalse(result_a.failed)
        self.assertEqual(result_a.session_id, original)
        self.assertEqual(self._rotation_lines(), [])

        # ---- Run B: first run of the NEXT local day -> exactly one rotation
        result_b = self._run(now=self._at("2026-08-25"))
        self.assertFalse(result_b.failed)
        new_id = result_b.session_id
        self.assertNotEqual(new_id, original)

        rotations = self._rotation_lines()
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["old_session_id"], original)
        self.assertIn("daily", rotations[0]["reason"])
        self.assertEqual(len(self._rotated_events()), 1)

        pin_after_b = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        assert pin_after_b is not None
        self.assertEqual(pin_after_b.session_id, new_id)
        # The new conversation is re-anchored to day 2, so it will not rotate
        # again until the NEXT midnight.
        self.assertEqual(
            session_health.read_lifecycle_anchor_day(new_id, runs_dir=self.runs_dir),
            "2026-08-25",
        )

        # ---- Run C: a second run on day 2 -> resume, STILL one rotation ---
        self._make_session_dir(new_id)
        result_c = self._run(now=self._at("2026-08-25"))
        self.assertFalse(result_c.failed)
        self.assertEqual(result_c.session_id, new_id)
        self.assertEqual(len(self._rotation_lines()), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
