"""Proof that a crashed notify-capable run is distinguishable from a quiet one.

THE DEFECT. A notify-capable automation crashed mid-pass (``ContextLengthError``
at step 5 of 8) and produced ``notified: false`` -- byte-for-byte the observable
a healthy run produces when it judges that nothing needs the owner. From
outside, "nothing needs you" and "the thing that decides whether anything needs
you crashed" were the same thing. The vision names that enemy directly (*the
successful-looking run that did nothing*), as does the consumer's own guidance
(*silence from a starved aggregate is indistinguishable from a calm day*).

TWO HALVES, BOTH PROVEN HERE:

1. a standing, machine-readable surface -- ``<data-dir>/failed_passes.json`` --
   that answers "is this automation's most recent run a crash?" in ONE read,
   with no run-directory walk and no outbox cursor;
2. the NEXT run of the same automation is told, in plain language, on its first
   turn, so the gap is accounted for as UNCHECKED rather than quiet.

House style (see ``test_auto_rotation_and_failure_push.py``): drive the REAL
production functions and check their real on-disk side effects. Only
``runner._submit_turn`` is mocked -- and it is mocked precisely so the TEXT of
the first turn can be read back, which is the only way to prove the successor
was actually told rather than merely that a record exists somewhere.
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

from drumbeat import failed_passes, runner, session_pins
from drumbeat.automation import load
from drumbeat.paths import derive_workspace_slug

# The owner's own measured failure shape.
CRASH_ERROR = "Execution failed: ContextLengthError: prompt is too long"

_AUTOMATION = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: manual
  notify: {notify}
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""


class _NotifyRunFixture(unittest.TestCase):
    NOTIFY = "always"

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

        automation_path = self.workspace / "automations" / "daily-rollup.md"
        automation_path.write_text(
            _AUTOMATION.format(name="Daily Rollup", notify=self.NOTIFY),
            encoding="utf-8",
        )
        self.automation = load(automation_path)
        self.workspace_slug = derive_workspace_slug(self.workspace)

    # ---- helpers -----------------------------------------------------

    def _sessions_dir(self) -> Path:
        return (
            self.agent_home / "state" / "workspaces" / self.workspace_slug / "sessions"
        )

    def _materialize_session(self, session_id: str) -> None:
        """Create the on-disk session a real turn would have left behind.

        ``_submit_turn`` is mocked, so nothing actually writes the session
        directory. Without this, the SECOND run's pinned-session probe reports
        a workspace mismatch and aborts -- and every test here depends on a
        real resumed second run, not an aborted one.
        """
        session_dir = self._sessions_dir() / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        transcript = session_dir / "transcript.jsonl"
        if not transcript.is_file():
            transcript.write_text("{}\n", encoding="utf-8")

    def _run(
        self, *, outcome: runner._TurnOutcome
    ) -> tuple[runner.RunResult, mock.MagicMock]:
        with (
            mock.patch.object(
                runner, "_submit_turn", return_value=outcome
            ) as submit_turn,
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )
        self._materialize_session(result.session_id)
        return result, submit_turn

    def _crash_run(self) -> tuple[runner.RunResult, mock.MagicMock]:
        return self._run(
            outcome=runner._TurnOutcome(error=CRASH_ERROR, stderr_text=CRASH_ERROR)
        )

    def _good_run(self) -> tuple[runner.RunResult, mock.MagicMock]:
        return self._run(
            outcome=runner._TurnOutcome(
                reply="all clear", tokens_in=3, tokens_out=3, duration_ms=5
            )
        )

    def _store_raw(self) -> dict:
        path = failed_passes.store_path(self.runs_dir)
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _first_turn_text(self, submit_turn: mock.MagicMock) -> str:
        self.assertTrue(submit_turn.call_args_list, "the run executed no turns")
        return submit_turn.call_args_list[0].kwargs["text"]

    def _all_turn_text(self, submit_turn: mock.MagicMock) -> str:
        return "\n".join(c.kwargs["text"] for c in submit_turn.call_args_list)


class TestCrashedRunIsVisibleAndTheSuccessorIsTold(_NotifyRunFixture):
    def test_crash_records_the_surface_then_the_next_run_is_told(self) -> None:
        # ---- A: the crashing run --------------------------------------
        crashed, _ = self._crash_run()
        self.assertTrue(crashed.failed)

        # 1. The MACHINE-READABLE SURFACE. One file, one read -- no walking
        #    runs/<slug>/<run_id>/ and no outbox cursor.
        raw = self._store_raw()
        self.assertEqual(raw["failed_passes_format"], failed_passes.STORE_FORMAT)
        entry = raw["failed_passes"][self.automation.slug]
        self.assertEqual(entry["run_id"], crashed.run_id)
        self.assertEqual(entry["automation"], self.automation.name)
        self.assertEqual(entry["session_id"], crashed.session_id)
        self.assertIn("ContextLengthError", entry["error"])
        self.assertTrue(entry["failed_at"])

        # ...and the typed read agrees with the file.
        record = failed_passes.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.run_id, crashed.run_id)

        # 2. THE SUCCESSOR IS TOLD, on its FIRST turn.
        recovered, submit_turn = self._good_run()
        first_turn = self._first_turn_text(submit_turn)
        self.assertIn("[drumbeat] Notice:", first_turn)
        self.assertIn("did not complete", first_turn)
        self.assertIn(crashed.run_id, first_turn)
        self.assertIn("ContextLengthError", first_turn)
        self.assertIn("UNCHECKED", first_turn)
        self.assertIn("its silence was not a verdict", first_turn)
        # Never fabricate what the crashed pass would have said.
        self.assertIn("do not attempt to reconstruct", first_turn)
        # The automation's own step prompt still rides in the same turn --
        # the notice is a preamble, not a stolen turn.
        self.assertIn("Do the thing.", first_turn)

        # 3. Recovery clears the flag: a crash surface that never clears is a
        #    stuck alarm, which is its own untrustworthy signal.
        self.assertFalse(recovered.failed)
        self.assertIsNone(
            failed_passes.get(self.automation.slug, runs_dir=self.runs_dir)
        )

        # 4. ...and the run AFTER the recovery is told nothing.
        _, quiet_submit = self._good_run()
        self.assertNotIn("[drumbeat] Notice:", self._all_turn_text(quiet_submit))

    def test_notice_rides_the_first_turn_even_when_that_is_not_step_one(self) -> None:
        """The first turn varies by run (system prompt / requirements / inject /
        step 1). The notice is claimed by whichever actually runs first, so it
        can never be silently skipped because a different turn came first."""
        (self.workspace / "guidance" / "policy.md").write_text(
            "HOUSE POLICY: be careful.", encoding="utf-8"
        )
        self.automation.path.write_text(
            _AUTOMATION.format(name="Daily Rollup", notify=self.NOTIFY).replace(
                "  steps:", "  requires:\n    - guidance/policy.md\n  steps:"
            ),
            encoding="utf-8",
        )
        self.automation = load(self.automation.path)

        crashed, _ = self._crash_run()
        self.assertTrue(crashed.failed)

        _, submit_turn = self._good_run()
        texts = [c.kwargs["text"] for c in submit_turn.call_args_list]
        # The requirements turn ran first (before step 1) AND carries the
        # notice -- step 1's own turn is a later one.
        self.assertIn("Required guidance files:", texts[0])
        self.assertIn("guidance/policy.md", texts[0])
        self.assertIn("[drumbeat] Notice:", texts[0])
        self.assertNotIn("Do the thing.", texts[0])
        # ...and no later turn repeats it.
        self.assertEqual(
            sum(1 for t in texts if "[drumbeat] Notice:" in t),
            1,
            "the notice must be delivered exactly once",
        )


class TestQuietPassCarriesNoNotice(_NotifyRunFixture):
    """Negative control. Without it, the test above could pass for the wrong
    reason ("every run gets a notice") instead of the right one ("a run whose
    predecessor crashed gets a notice")."""

    def test_healthy_runs_never_write_the_surface_or_the_notice(self) -> None:
        first, first_submit = self._good_run()
        second, second_submit = self._good_run()

        self.assertFalse(first.failed)
        self.assertFalse(second.failed)
        self.assertNotIn("[drumbeat] Notice:", self._all_turn_text(first_submit))
        self.assertNotIn("[drumbeat] Notice:", self._all_turn_text(second_submit))
        self.assertFalse(failed_passes.store_path(self.runs_dir).is_file())


class TestNotifyNeverAutomationsAreUnaffected(_NotifyRunFixture):
    """``notify: never`` runs cannot produce the ambiguity this closes -- their
    silence was never going to be read as "nothing needs you" -- so they are
    deliberately absent from the store and never carry a notice. Their failures
    are still loud everywhere they already were (failures.log, the
    automation_error event); this test asserts the SCOPE, not a downgrade."""

    NOTIFY = "never"

    def test_crash_writes_no_record_and_the_next_run_gets_no_notice(self) -> None:
        crashed, _ = self._crash_run()
        self.assertTrue(crashed.failed)

        self.assertIsNone(
            failed_passes.get(self.automation.slug, runs_dir=self.runs_dir)
        )
        self.assertFalse(failed_passes.store_path(self.runs_dir).is_file())

        # The failure is still loud where it always was.
        failures_log = (self.runs_dir / "failures.log").read_text(encoding="utf-8")
        self.assertIn(crashed.run_id, failures_log)

        _, submit_turn = self._good_run()
        self.assertNotIn("[drumbeat] Notice:", self._all_turn_text(submit_turn))


class TestConsecutiveFailuresDoNotStack(_NotifyRunFixture):
    def test_two_crashes_leave_one_record_naming_the_latest(self) -> None:
        first_crash, _ = self._crash_run()
        second_crash, second_submit = self._crash_run()

        # The second run WAS told about the first (one notice)...
        self.assertEqual(
            sum(
                1
                for c in second_submit.call_args_list
                if "[drumbeat] Notice:" in c.kwargs["text"]
            ),
            1,
        )
        self.assertIn(first_crash.run_id, self._first_turn_text(second_submit))

        # ...and the store still holds exactly ONE record, now naming the
        # LATEST failure. Two crashes must not become two notices.
        entries = self._store_raw()["failed_passes"]
        self.assertEqual(list(entries), [self.automation.slug])
        self.assertEqual(entries[self.automation.slug]["run_id"], second_crash.run_id)

        # The third run hears about the second failure only -- never both.
        _, third_submit = self._crash_run()
        third_first_turn = self._first_turn_text(third_submit)
        self.assertIn(second_crash.run_id, third_first_turn)
        self.assertNotIn(first_crash.run_id, third_first_turn)
        self.assertEqual(
            sum(
                1
                for c in third_submit.call_args_list
                if "[drumbeat] Notice:" in c.kwargs["text"]
            ),
            1,
        )


class TestStoreNeverTakesDownARun(unittest.TestCase):
    """Posture check, deliberately opposite to ``session_pins``. That store
    refuses to be read as empty because reading-as-empty there means a silent
    mass rotation. Here, raising would take down healthy runs to protect a
    notice -- so this store reports loudly and returns nothing instead."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.runs_dir.mkdir()

    def _read_loudly(self) -> tuple[dict, str]:
        buf = io.StringIO()
        with redirect_stderr(buf):
            records = failed_passes.read_all(self.runs_dir)
        return records, buf.getvalue()

    def test_absent_store_is_empty_and_silent(self) -> None:
        records, stderr = self._read_loudly()
        self.assertEqual(records, {})
        self.assertEqual(stderr, "")

    def test_damaged_stores_are_empty_but_never_silent(self) -> None:
        path = failed_passes.store_path(self.runs_dir)
        cases = {
            "unparseable": "{not json at all",
            "torn": "",
            "wrong-type": '["a", "list"]',
            "wrong-format": '{"failed_passes": {}, "failed_passes_format": 99}',
            "bad-entries": '{"failed_passes": 7, "failed_passes_format": 1}',
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                path.write_text(content, encoding="utf-8")
                records, stderr = self._read_loudly()
                self.assertEqual(records, {})
                self.assertIn("failed_passes", stderr)
                self.assertIn(str(path), stderr)

    def test_a_record_naming_no_run_is_dropped_loudly(self) -> None:
        """A record that names no run reads as "there was a crash" while
        pointing at nothing anyone can go look at -- worse than no record."""
        failed_passes.store_path(self.runs_dir).write_text(
            json.dumps(
                {
                    "failed_passes": {"ghost": {"automation": "Ghost"}},
                    "failed_passes_format": 1,
                }
            ),
            encoding="utf-8",
        )
        records, stderr = self._read_loudly()
        self.assertEqual(records, {})
        self.assertIn("no usable run_id", stderr)

    def test_the_damaged_store_is_replaced_by_the_next_write(self) -> None:
        failed_passes.store_path(self.runs_dir).write_text("garbage", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            stored = failed_passes.record(
                slug="fleet-check",
                automation="Fleet Check",
                run_id="20260901T000000Z-abcdef",
                session_id="fleet-check-1",
                error="boom",
                runs_dir=self.runs_dir,
            )
            self.assertIsNotNone(stored)
            back = failed_passes.get("fleet-check", runs_dir=self.runs_dir)
        self.assertIsNotNone(back)
        assert back is not None
        self.assertEqual(back.run_id, "20260901T000000Z-abcdef")

    def test_clear_on_a_slug_with_no_record_is_a_no_op(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(failed_passes.clear("nobody", runs_dir=self.runs_dir))


class TestSurfaceSurvivesAnUnexpectedCrash(_NotifyRunFixture):
    """The escaped-exception path (``runner._persist_escaped_failure``) reaches
    the same persistence choke point, so an unexpected crash -- the shape most
    likely to look like silence -- is recorded on the surface too."""

    def test_an_escaped_exception_still_marks_the_pass_as_crashed(self) -> None:
        session_pins.upsert(
            self.automation.slug,
            session_id="daily-rollup-preexisting",
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
            / "daily-rollup-preexisting"
        )
        session_dir.mkdir(parents=True)
        (session_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

        with (
            mock.patch.object(
                runner, "_submit_turn", side_effect=RuntimeError("worker exploded")
            ),
            redirect_stderr(io.StringIO()),
            self.assertRaises(RuntimeError),
        ):
            runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )

        record = failed_passes.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIn("worker exploded", record.error)


if __name__ == "__main__":
    unittest.main()
