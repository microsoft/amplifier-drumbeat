"""Proof that a provider input-size refusal names its cause AND self-heals.

contracts/agent-binding.v1.md section 11, and the 2026-09-07 incident it was
written from. A single oversized tool result entered ``teams-check``'s pinned
session; every later turn re-sent it and OpenAI refused with
``invalid_request_error`` / ``string_above_max_length: input[37].output >
10,485,760``. Three things then went wrong at once, and each has a test here:

1. **The run did not name its cause.** The library surfaced only the lossy
   ``provider_failed``; the structured provider code sat unread in the worker's
   ``stderr.log``. ``run_completed.error_details`` now carries it.
2. **The pin was not rotated.** ``session_health.detect_ceiling_hit`` matches
   one provider's PROSE form only, and the incident arrived as a structured
   code, so Trigger 1 never fired.
3. **The pre-emptive token gate could not help.** A refused turn records no
   usage, so there was nothing for the gate to measure -- which is why the
   refusal has to be its own trigger rather than a tuning problem.

The identical run failed again four hours later on the same pinned session.
Manual ``drumbeat rotate-session`` was the only remedy.

House style (see ``test_auto_rotation_and_failure_push.py``): drive the REAL
``runner.run()`` and check its real on-disk and in-outbox side effects. The
only mocked thing is ``runner._submit_turn`` -- the established seam for faking
one turn -- so the stderr a real provider refusal would produce can be fed in.
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

from drumbeat import engine_events, runner, session_health, session_pins
from drumbeat.automation import load
from drumbeat.paths import agent_session_storage, derive_workspace_slug

# The measured incident, verbatim in shape: the provider's structured refusal
# as it reaches the worker's stderr.
INCIDENT_STDERR = (
    "openai.BadRequestError: Error code: 400 - {'error': {'message': "
    "\"Invalid 'input[37].output': string too long. Expected a string with "
    "maximum length 10485760, but got a string with length 46464072 instead.\", "
    "'type': 'invalid_request_error', 'param': 'input[37].output', "
    "'code': 'string_above_max_length'}}\n"
)

_AUTOMATION = """---
automation:
  name: Teams Check
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: sweep
      prompt: Sweep the channels.
---
"""


class TestDetection(unittest.TestCase):
    """The pure predicate. Every code, the prose form, and the negatives."""

    def test_each_provider_code_is_detected(self) -> None:
        for code in session_health.INPUT_SIZE_REFUSAL_CODES:
            with self.subTest(code=code):
                refusal = session_health.detect_input_size_refusal(
                    f"provider blew up: {code} (request id abc)"
                )
                self.assertIsNotNone(refusal)
                assert refusal is not None
                self.assertEqual(refusal.code, code)

    def test_the_measured_incident_stderr_is_detected(self) -> None:
        refusal = session_health.detect_input_size_refusal(INCIDENT_STDERR)
        self.assertIsNotNone(refusal)
        assert refusal is not None
        self.assertEqual(refusal.code, "string_above_max_length")
        # No numbers in this shape -- honestly absent, never fabricated.
        self.assertIsNone(refusal.prompt_tokens)
        self.assertIsNone(refusal.limit_tokens)

    def test_the_prose_form_still_works_and_keeps_its_numbers(self) -> None:
        refusal = session_health.detect_input_size_refusal(
            "prompt is too long: 219685 tokens > 200000 maximum"
        )
        assert refusal is not None
        self.assertEqual(refusal.code, session_health.InputSizeRefusal.PROSE_CODE)
        self.assertEqual(refusal.prompt_tokens, 219_685)
        self.assertEqual(refusal.limit_tokens, 200_000)

    def test_it_is_a_strict_superset_of_the_prose_only_detector(self) -> None:
        prose = "prompt is too long: 210347 tokens > 200000 maximum"
        self.assertIsNotNone(session_health.detect_ceiling_hit(prose))
        self.assertIsNotNone(session_health.detect_input_size_refusal(prose))
        # ... and it sees the shape the prose-only detector cannot.
        self.assertIsNone(session_health.detect_ceiling_hit(INCIDENT_STDERR))
        self.assertIsNotNone(session_health.detect_input_size_refusal(INCIDENT_STDERR))

    def test_an_unrelated_failure_is_never_reported_as_a_refusal(self) -> None:
        """Rotating a healthy session costs a real conversation's memory."""
        for text in (
            "",
            "amplifier-agent exited 1",
            "ConnectionResetError: [Errno 104] Connection reset by peer",
            "engine_unavailable: no credential for provider 'openai'",
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNone(session_health.detect_input_size_refusal(text))

    def test_a_code_inside_a_longer_identifier_does_not_match(self) -> None:
        """Strict boundary: a substring is not an occurrence."""
        for text in (
            "my_request_too_large_flag = False",
            "context_length_exceededness",
            "xstring_above_max_length",
        ):
            with self.subTest(text=text):
                self.assertIsNone(session_health.detect_input_size_refusal(text))

    def test_a_structured_code_wins_over_the_prose_form(self) -> None:
        """The provider's own name for what happened needs no interpreting."""
        refusal = session_health.detect_input_size_refusal(
            "prompt is too long: 210347 tokens > 200000 maximum\n"
            "code: context_length_exceeded\n"
        )
        assert refusal is not None
        self.assertEqual(refusal.code, "context_length_exceeded")
        # The numbers the prose supplied are still carried, not discarded.
        self.assertEqual(refusal.prompt_tokens, 210_347)
        self.assertEqual(refusal.limit_tokens, 200_000)

    def test_error_details_omits_absent_numbers_rather_than_padding_null(self) -> None:
        details = session_health.InputSizeRefusal(
            code="request_too_large"
        ).as_error_details()
        self.assertEqual(
            details, {"provider_code": "request_too_large", "source": "stderr"}
        )
        self.assertNotIn("prompt_tokens", details)


class _RunFixture(unittest.TestCase):
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
            {"AMPLIFIER_AGENT_WORKSPACE": "", "CONTEXT_INTELLIGENCE_PERSONAL": ""},
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        path = self.workspace / "automations" / "teams-check.md"
        path.write_text(_AUTOMATION, encoding="utf-8")
        self.automation = load(path)
        self.workspace_slug = derive_workspace_slug(self.workspace)

    def _pin_real_session(self, session_id: str) -> None:
        session_pins.upsert(
            self.automation.slug,
            session_id=session_id,
            session_workspace=self.workspace_slug,
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=self.runs_dir,
        )
        agent_session_storage(session_id, runs_dir=self.runs_dir).mkdir(parents=True)

    def _run(self, *, stderr_text: str) -> runner.RunResult:
        outcome = runner._TurnOutcome(
            error="amplifier-agent exited 1", stderr_text=stderr_text
        )
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

    def _events(self, event_type: engine_events.EventType) -> list[dict]:
        events, _ = engine_events.read_since(self.runs_dir, 0)
        return [e.data for e in events if e.event_type is event_type]

    def _rotations(self) -> list[dict]:
        path = self.runs_dir / "session_rotations.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class TestRefusalNamesItsCauseAndRotates(_RunFixture):
    """The whole incident, end to end, against the real runner."""

    def test_run_record_and_event_name_the_provider_code(self) -> None:
        old_session_id = f"{self.automation.slug}-20260907T000000Z-aaaaaa"
        self._pin_real_session(old_session_id)

        result = self._run(stderr_text=INCIDENT_STDERR)

        self.assertTrue(result.failed)
        self.assertEqual(
            result.error_details,
            {"provider_code": "string_above_max_length", "source": "stderr"},
        )

        # The run RECORD on disk carries it -- what `doctor` and the runs API read.
        run_dir = self.runs_dir / self.automation.slug / result.run_id
        record = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(
            record["error_details"]["provider_code"], "string_above_max_length"
        )

        # And so does the outbox event.
        completed = self._events(engine_events.EventType.RUN_COMPLETED)
        self.assertEqual(len(completed), 1)
        self.assertEqual(
            completed[0]["error_details"]["provider_code"], "string_above_max_length"
        )

    def test_the_pinned_session_is_rotated_with_the_code_in_the_reason(self) -> None:
        old_session_id = f"{self.automation.slug}-20260907T000000Z-bbbbbb"
        self._pin_real_session(old_session_id)

        self._run(stderr_text=INCIDENT_STDERR)

        rotations = self._rotations()
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["old_session_id"], old_session_id)
        self.assertIn("string_above_max_length", rotations[0]["reason"])

        # The pin is gone, so the next run cannot resume the poisoned session.
        self.assertNotIn(self.automation.slug, session_pins.read_all(self.runs_dir))

        rotated = self._events(engine_events.EventType.SESSION_ROTATED)
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0]["old_session_id"], old_session_id)
        self.assertIn("string_above_max_length", rotated[0]["reason"])

    def test_the_next_run_self_heals_on_a_fresh_session(self) -> None:
        """The measured defect: the identical run failed again 4h later.

        Rotation caps the blast radius at the one run that already failed.
        """
        old_session_id = f"{self.automation.slug}-20260907T000000Z-cccccc"
        self._pin_real_session(old_session_id)

        first = self._run(stderr_text=INCIDENT_STDERR)
        self.assertEqual(first.session_id, old_session_id)

        second = self._run(stderr_text="")
        self.assertNotEqual(second.session_id, old_session_id)
        # Exactly one rotation across both runs: the second did not re-rotate.
        self.assertEqual(len(self._rotations()), 1)

    def test_a_failure_that_is_not_a_refusal_neither_rotates_nor_invents_details(
        self,
    ) -> None:
        old_session_id = f"{self.automation.slug}-20260907T000000Z-dddddd"
        self._pin_real_session(old_session_id)

        result = self._run(stderr_text="ConnectionResetError: [Errno 104] reset\n")

        self.assertTrue(result.failed)
        # None, never {} -- an empty dict would read as "we looked, it was fine".
        self.assertIsNone(result.error_details)
        self.assertEqual(self._rotations(), [])
        self.assertIn(self.automation.slug, session_pins.read_all(self.runs_dir))

    def test_a_successful_run_is_never_given_a_manufactured_cause(self) -> None:
        self._pin_real_session(f"{self.automation.slug}-20260907T000000Z-eeeeee")
        outcome = runner._TurnOutcome(reply="all good", stderr_text=INCIDENT_STDERR)
        with (
            mock.patch.object(runner, "_submit_turn", return_value=outcome),
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )
        self.assertFalse(result.failed)
        self.assertIsNone(result.error_details)


if __name__ == "__main__":
    unittest.main()
