"""Proof that an oversized pinned session rotates BEFORE the turn, not on the crash.

The engine already rotates a pinned session AFTER the provider refuses a
prompt (``runner``'s Trigger 1, ceiling hit -- see
``test_auto_rotation_and_failure_push.py``). That backstop is correct and
stays; it is also, by construction, always one failed run late.

The gate measures PROMPT TOKENS -- the unit the provider actually refuses on
(``prompt is too long: 219685 tokens > 200000 maximum``) and the count the
agent library itself reports on every turn. The two sessions whose true prompt
sizes the provider ever reported crashed at 219,685 and 201,361 tokens, so
``runner._DEFAULT_SESSION_ROTATE_TOKENS == 150_000`` sits a quarter below the
smallest observed refusal, with room for one more turn's growth.

House style (see ``test_auto_rotation_and_failure_push.py``): drive the REAL
production functions and check their real on-disk side effects. The only
mocked thing is ``runner._submit_turn`` -- the established seam for faking a
turn -- and it is mocked here precisely so its recorded call arguments can
prove WHICH session the turn ran against, which is the whole "before the
turn, not after" claim.
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
from drumbeat.paths import agent_session_storage, derive_workspace_slug

_AUTOMATION = """---
automation:
  name: Fleet Check
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""

# Small enough to keep the fixture's recorded token counts tiny, exercised
# through the SAME env-var seam an operator uses. The default itself is
# asserted separately (TestDefaultThresholdIsTheMeasuredOne) so shrinking it
# here can never quietly become "the default is whatever the test says".
_TEST_GATE_TOKENS = 1_000


class _SizeRotationFixture(unittest.TestCase):
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


        env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFIER_AGENT_WORKSPACE": "",
                "CONTEXT_INTELLIGENCE_PERSONAL": "",
                "DRUMBEAT_SESSION_ROTATE_TOKENS": str(_TEST_GATE_TOKENS),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        automation_path = self.workspace / "automations" / "fleet-check.md"
        automation_path.write_text(_AUTOMATION, encoding="utf-8")
        self.automation = load(automation_path)
        self.workspace_slug = derive_workspace_slug(self.workspace)

    # ---- helpers -----------------------------------------------------

    def _pin_session_with_prompt_tokens(
        self, session_id: str, *, tokens_in: int
    ) -> Path:
        """Pin ``session_id``, give it real agent storage, and leave a real run
        record behind carrying ``tokens_in``.

        Both halves matter and neither is stubbed: the storage directory is what
        the pinned-session probe stats to resolve EXISTS, and the run record is
        where ``runner._session_prompt_tokens`` reads the measurement -- the
        same file ``_persist_run`` writes on every real run.
        """
        session_pins.upsert(
            self.automation.slug,
            session_id=session_id,
            session_workspace=self.workspace_slug,
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=self.runs_dir,
        )
        storage = agent_session_storage(session_id, runs_dir=self.runs_dir)
        storage.mkdir(parents=True)

        run_dir = self.runs_dir / self.automation.slug / "20260801T000000Z-prior"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(
            json.dumps(
                {
                    "run_id": "20260801T000000Z-prior",
                    "session_id": session_id,
                    "failed": False,
                    "steps": [
                        {"index": 0, "tokens_in": tokens_in, "tokens_out": 7}
                    ],
                }
            ),
            encoding="utf-8",
        )
        return storage

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

    def _run(self) -> tuple[runner.RunResult, mock.MagicMock, str]:
        """One real run with a stubbed turn. Returns (result, turn mock, stderr)."""
        outcome = runner._TurnOutcome(
            reply="ok", tokens_in=3, tokens_out=3, duration_ms=5
        )
        buf = io.StringIO()
        with (
            mock.patch.object(
                runner, "_submit_turn", return_value=outcome
            ) as submit_turn,
            redirect_stderr(buf),
        ):
            result = runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )
        return result, submit_turn, buf.getvalue()

    def _turn_session_ids(self, submit_turn: mock.MagicMock) -> list[str]:
        return [call.kwargs["session_id"] for call in submit_turn.call_args_list]


class TestOverThresholdRotatesBeforeTheTurn(_SizeRotationFixture):
    """The whole point: the rotation happens ahead of the turn, and the run
    then proceeds -- on the fresh session -- rather than failing first."""

    def test_oversized_session_rotates_first_then_the_run_proceeds(self) -> None:
        old_session_id = f"{self.automation.slug}-20260801T000000Z-aaaaaa"
        storage = self._pin_session_with_prompt_tokens(
            old_session_id, tokens_in=_TEST_GATE_TOKENS + 1
        )

        result, submit_turn, stderr = self._run()

        # 1. The run did NOT fail: pre-emption replaces a crash, it is not one.
        self.assertFalse(result.failed, result.error)

        # 2. Rotation happened BEFORE the turn. This is the load-bearing
        #    assertion -- every turn in this run ran against the NEW session
        #    id, and the abandoned one was never submitted at all.
        turn_sessions = self._turn_session_ids(submit_turn)
        self.assertTrue(turn_sessions, "the run executed no turns")
        self.assertNotIn(old_session_id, turn_sessions)
        self.assertEqual(set(turn_sessions), {result.session_id})
        self.assertNotEqual(result.session_id, old_session_id)

        # 3. ...and it was created fresh, not resumed.
        self.assertFalse(result.session_resumed)
        self.assertTrue(submit_turn.call_args_list[0].kwargs["fresh"])

        # 4. One rotation on record, with a SIZE reason carrying the MEASURED
        #    bytes and the gate it crossed.
        rotations = self._rotation_lines()
        self.assertEqual(len(rotations), 1)
        entry = rotations[0]
        self.assertEqual(entry["old_session_id"], old_session_id)
        self.assertTrue(entry["reason"].startswith("auto:"))
        self.assertIn("size threshold", entry["reason"])
        self.assertIn(str(_TEST_GATE_TOKENS + 1), entry["reason"])
        self.assertIn(str(_TEST_GATE_TOKENS), entry["reason"])

        # 5. Never silent: the operator sees it on stderr too.
        self.assertIn("AUTO-ROTATING", stderr)
        self.assertIn("size threshold", stderr)

        # 6. Same continuity contract the crash path already honours: the pin
        #    is re-written to the fresh session, a session_rotated event is
        #    emitted, and the abandoned session's storage is left on disk
        #    untouched.
        pin = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(pin.session_id, result.session_id)
        rotated_events = self._outbox_events(engine_events.EventType.SESSION_ROTATED)
        self.assertEqual(len(rotated_events), 1)
        self.assertEqual(rotated_events[0]["old_session_id"], old_session_id)
        self.assertTrue(storage.is_dir())

    def test_rotation_log_entry_has_the_full_recorded_shape(self) -> None:
        old_session_id = f"{self.automation.slug}-20260801T000000Z-cccccc"
        self._pin_session_with_prompt_tokens(
            old_session_id, tokens_in=_TEST_GATE_TOKENS * 3
        )

        self._run()

        entry = self._rotation_lines()[0]
        self.assertEqual(
            set(entry),
            {"time", "automation", "slug", "path", "old_session_id", "reason"},
        )
        self.assertEqual(entry["automation"], self.automation.name)
        self.assertEqual(entry["slug"], self.automation.slug)
        self.assertEqual(entry["path"], str(self.automation.path))
        self.assertIn(str(_TEST_GATE_TOKENS * 3), entry["reason"])


class TestUnderThresholdNeverRotates(_SizeRotationFixture):
    """Negative control. Without it, the test above could pass for the wrong
    reason ("every resumed run rotates") instead of the right one ("a session
    OVER the gate rotates")."""

    def test_under_threshold_session_is_resumed_untouched(self) -> None:
        session_id = f"{self.automation.slug}-20260802T000000Z-bbbbbb"
        self._pin_session_with_prompt_tokens(session_id, tokens_in=_TEST_GATE_TOKENS - 1)

        result, submit_turn, _ = self._run()

        self.assertFalse(result.failed, result.error)
        self.assertEqual(result.session_id, session_id)
        self.assertTrue(result.session_resumed)
        self.assertEqual(set(self._turn_session_ids(submit_turn)), {session_id})
        self.assertEqual(self._rotation_lines(), [])
        self.assertEqual(
            self._outbox_events(engine_events.EventType.SESSION_ROTATED), []
        )
        pin = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(pin.session_id, session_id)

    def test_exactly_at_the_threshold_does_not_rotate(self) -> None:
        """The gate is strictly ``>``. A boundary that rotates AT the value
        would make the documented number mean something other than it says."""
        session_id = f"{self.automation.slug}-20260802T000000Z-dddddd"
        self._pin_session_with_prompt_tokens(session_id, tokens_in=_TEST_GATE_TOKENS)

        result, _, _ = self._run()

        self.assertEqual(result.session_id, session_id)
        self.assertTrue(result.session_resumed)
        self.assertEqual(self._rotation_lines(), [])


class TestDefaultThresholdIsTheMeasuredOne(unittest.TestCase):
    """The default is a measured claim (see this module's docstring), so it is
    pinned here rather than left to drift silently."""

    def test_default_is_the_calibrated_token_gate(self) -> None:
        self.assertEqual(runner._DEFAULT_SESSION_ROTATE_TOKENS, 150_000)

    def test_unset_env_yields_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"DRUMBEAT_SESSION_ROTATE_TOKENS": ""}):
            self.assertEqual(
                runner._session_rotate_tokens(), runner._DEFAULT_SESSION_ROTATE_TOKENS
            )

    def test_positive_override_is_honoured(self) -> None:
        with mock.patch.dict(
            os.environ, {"DRUMBEAT_SESSION_ROTATE_TOKENS": "123456789"}
        ):
            self.assertEqual(runner._session_rotate_tokens(), 123456789)

    def test_unusable_override_falls_back_loudly(self) -> None:
        """FAIL LOUD: an unusable value must not be silently honoured as
        "no gate" -- that would disable the mechanism by typo."""
        for raw in ("not-a-number", "0", "-1", "5e6"):
            with self.subTest(raw=raw):
                buf = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"DRUMBEAT_SESSION_ROTATE_TOKENS": raw}),
                    redirect_stderr(buf),
                ):
                    value = runner._session_rotate_tokens()
                self.assertEqual(value, runner._DEFAULT_SESSION_ROTATE_TOKENS)
                self.assertIn("DRUMBEAT_SESSION_ROTATE_TOKENS", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
