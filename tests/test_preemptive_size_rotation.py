"""Proof that an oversized pinned session rotates BEFORE the turn, not on the crash.

The engine already rotates a pinned session AFTER the provider refuses a
prompt (``runner``'s Trigger 1, ceiling hit -- see
``test_auto_rotation_and_failure_push.py``). That backstop is correct and
stays; it is also, by construction, always one failed run late.

Measured on this deployment's own 8-day window (4,133 runs carrying a
``session_transcript_bytes_at_start``, 157 sessions):

- every one of the 41 observed ``ContextLengthError`` runs started from a
  transcript of at least 5,586,751 bytes;
- 1,138 runs started at or below 5,000,000 bytes and NOT ONE of them hit
  the ceiling;
- measured per-run transcript growth is 0.29 / 0.41 / 0.64 MB at the
  25th / 50th / 75th percentile.

Hence ``runner._DEFAULT_SESSION_ROTATE_BYTES == 5_000_000``: a gate that
would have pre-empted every observed crash while still giving a session on
the order of a dozen runs from a cold start.

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
from drumbeat.paths import derive_workspace_slug

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

# Small enough to keep the fixture's fake transcripts tiny, exercised through
# the SAME env-var seam an operator uses. The default itself is asserted
# separately (TestDefaultThresholdIsTheMeasuredOne) so shrinking it here can
# never quietly become "the default is whatever the test says".
_TEST_GATE_BYTES = 1_000


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

        self.agent_home = self.tmp_path / "agent-home"

        env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFIER_AGENT_HOME": str(self.agent_home),
                "AMPLIFIER_AGENT_WORKSPACE": "",
                "CONTEXT_INTELLIGENCE_PERSONAL": "",
                "DRUMBEAT_SESSION_ROTATE_BYTES": str(_TEST_GATE_BYTES),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        automation_path = self.workspace / "automations" / "fleet-check.md"
        automation_path.write_text(_AUTOMATION, encoding="utf-8")
        self.automation = load(automation_path)
        self.workspace_slug = derive_workspace_slug(self.workspace)

    # ---- helpers -----------------------------------------------------

    def _pin_session_with_transcript(self, session_id: str, *, size_bytes: int) -> Path:
        """Pin ``session_id`` and give it a real transcript of exactly ``size_bytes``.

        Mirrors amplifier-agent's on-disk session layout (see
        ``runner._session_dir``) so the pinned-session probe resolves EXISTS
        and ``runner._transcript_stats`` reads a genuine ``st_size`` -- the
        gate must be proven against a real file, never a stubbed number.
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
        transcript = session_dir / "transcript.jsonl"
        # One well-formed JSONL line padded to the requested size, so the file
        # is both the right size AND readable as a transcript.
        filler = "x" * max(0, size_bytes - len('{"pad": ""}\n'))
        transcript.write_text(json.dumps({"pad": filler}) + "\n", encoding="utf-8")
        actual = transcript.stat().st_size
        self.assertEqual(actual, size_bytes, "fixture transcript is the wrong size")
        return transcript

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
        transcript = self._pin_session_with_transcript(
            old_session_id, size_bytes=_TEST_GATE_BYTES + 1
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
        self.assertIn(str(_TEST_GATE_BYTES + 1), entry["reason"])
        self.assertIn(str(_TEST_GATE_BYTES), entry["reason"])

        # 5. Never silent: the operator sees it on stderr too.
        self.assertIn("AUTO-ROTATING", stderr)
        self.assertIn("size threshold", stderr)

        # 6. Same continuity contract the crash path already honours: the pin
        #    is re-written to the fresh session, a session_rotated event is
        #    emitted, and the old transcript is left on disk untouched.
        pin = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(pin.session_id, result.session_id)
        rotated_events = self._outbox_events(engine_events.EventType.SESSION_ROTATED)
        self.assertEqual(len(rotated_events), 1)
        self.assertEqual(rotated_events[0]["old_session_id"], old_session_id)
        self.assertTrue(transcript.is_file())
        self.assertEqual(transcript.stat().st_size, _TEST_GATE_BYTES + 1)

    def test_rotation_log_entry_has_the_full_recorded_shape(self) -> None:
        old_session_id = f"{self.automation.slug}-20260801T000000Z-cccccc"
        self._pin_session_with_transcript(
            old_session_id, size_bytes=_TEST_GATE_BYTES * 3
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
        self.assertIn(str(_TEST_GATE_BYTES * 3), entry["reason"])


class TestUnderThresholdNeverRotates(_SizeRotationFixture):
    """Negative control. Without it, the test above could pass for the wrong
    reason ("every resumed run rotates") instead of the right one ("a session
    OVER the gate rotates")."""

    def test_under_threshold_session_is_resumed_untouched(self) -> None:
        session_id = f"{self.automation.slug}-20260802T000000Z-bbbbbb"
        self._pin_session_with_transcript(session_id, size_bytes=_TEST_GATE_BYTES - 1)

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
        self._pin_session_with_transcript(session_id, size_bytes=_TEST_GATE_BYTES)

        result, _, _ = self._run()

        self.assertEqual(result.session_id, session_id)
        self.assertTrue(result.session_resumed)
        self.assertEqual(self._rotation_lines(), [])


class TestDefaultThresholdIsTheMeasuredOne(unittest.TestCase):
    """The default is a measured claim (see this module's docstring), so it is
    pinned here rather than left to drift silently."""

    def test_default_is_five_million_bytes(self) -> None:
        self.assertEqual(runner._DEFAULT_SESSION_ROTATE_BYTES, 5_000_000)

    def test_unset_env_yields_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"DRUMBEAT_SESSION_ROTATE_BYTES": ""}):
            self.assertEqual(
                runner._session_rotate_bytes(), runner._DEFAULT_SESSION_ROTATE_BYTES
            )

    def test_positive_override_is_honoured(self) -> None:
        with mock.patch.dict(
            os.environ, {"DRUMBEAT_SESSION_ROTATE_BYTES": "123456789"}
        ):
            self.assertEqual(runner._session_rotate_bytes(), 123456789)

    def test_unusable_override_falls_back_loudly(self) -> None:
        """FAIL LOUD: an unusable value must not be silently honoured as
        "no gate" -- that would disable the mechanism by typo."""
        for raw in ("not-a-number", "0", "-1", "5e6"):
            with self.subTest(raw=raw):
                buf = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"DRUMBEAT_SESSION_ROTATE_BYTES": raw}),
                    redirect_stderr(buf),
                ):
                    value = runner._session_rotate_bytes()
                self.assertEqual(value, runner._DEFAULT_SESSION_ROTATE_BYTES)
                self.assertIn("DRUMBEAT_SESSION_ROTATE_BYTES", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
