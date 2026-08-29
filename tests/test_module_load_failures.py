"""Regression proofs for session-init module-load degradation visibility.

Measured on a real deployment, 2026-08-28: 96 of 96 runs in one morning
carried a module-validation failure in stderr.log while every ``result.json``
recorded ``"failed": false, "error": null`` -- an independent watcher
scanning stderr correctly flagged the last 8 runs as failed; drumbeat's own
verdict never did.

Root cause: amplifier-core's own per-turn session init
(``amplifier_core._session_init.initialize_session``) treats a
provider/tool/hook that raises during load/validate as NON-FATAL -- it logs
``"Failed to load <type> '<module_id>': <exc>"`` at WARNING level and keeps
booting with a reduced module set. Nothing raises back to this worker, so
there is no exception for ``agent_worker.main``'s crash handling to catch
(see ``tests/test_agent_worker.py`` for that -- separate, already-correct --
seam). The WARNING line is the run's only trace, landing on stderr via
Python logging's "handler of last resort".

These tests drive the real ``runner._detect_module_load_failures`` /
``runner.run`` / ``runner._persist_run`` path (``runner._submit_turn`` is the
established fake seam every test in this suite uses -- see
``_TurnOutcome``'s own docstring), proving:

  1. The stderr pattern is parsed into deduped, sorted ``module_failures``.
  2. A run that degrades but still replies is NOT marked failed -- the
     honest minimum is visibility, not a manufactured failure.
  3. ``module_failures`` reaches ``StepResult``, ``RunResult``, the
     persisted ``result.json``, and the ``RUN_COMPLETED`` event.
  4. A genuinely failed run (a real error at session init) still gets
     ``failed: true`` with a real ``error`` -- unaffected by this change,
     and independent of whether that same run's stderr also carried a
     module-load warning.
  5. An ordinary run with no module-load warning in stderr carries an empty
     ``module_failures`` tuple -- no false positives.
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

from drumbeat.automation import load

from drumbeat import runner

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

# The exact shape amplifier-core's initialize_session logs (WARNING +
# exc_info=True, see amplifier_core/_session_init.py), captured on a turn's
# stderr the way runner._submit_turn actually sees it.
_REAL_TOOL_LOAD_FAILURE_STDERR = (
    "INFO:amplifier_core._session_init:Loading tool: tool-ledger-items\n"
    "WARNING:amplifier_core._session_init:Failed to load tool 'tool-ledger-items': "
    "Module 'tool-ledger-items' failed validation: 1 error. "
    "Errors: missing_mount: mount() function not found\n"
    "Traceback (most recent call last):\n"
    '  File ".../loader.py", line 662, in _validate_module\n'
    "    raise ModuleValidationError(\n"
    "amplifier_core.loader.ModuleValidationError: Module 'tool-ledger-items' "
    "failed validation: 1 error. Errors: missing_mount: mount() function not found\n"
)


class TestDetectModuleLoadFailures(unittest.TestCase):
    """Unit coverage for ``runner._detect_module_load_failures``."""

    def test_empty_stderr_yields_no_failures(self) -> None:
        self.assertEqual(runner._detect_module_load_failures(""), ())

    def test_ordinary_stderr_with_no_warning_yields_no_failures(self) -> None:
        stderr = "INFO:amplifier_core._session_init:Loading tool: tool-bash\n"
        self.assertEqual(runner._detect_module_load_failures(stderr), ())

    def test_real_warning_line_is_parsed(self) -> None:
        result = runner._detect_module_load_failures(_REAL_TOOL_LOAD_FAILURE_STDERR)
        self.assertEqual(result, ("tool:tool-ledger-items",))

    def test_multiple_distinct_failures_are_deduped_and_sorted(self) -> None:
        stderr = (
            "WARNING:...:Failed to load tool 'tool-b': boom\n"
            "WARNING:...:Failed to load hook 'hook-a': boom\n"
            "WARNING:...:Failed to load tool 'tool-b': boom (retry)\n"
        )
        result = runner._detect_module_load_failures(stderr)
        self.assertEqual(result, ("hook:hook-a", "tool:tool-b"))

    def test_provider_failures_are_also_recognized(self) -> None:
        stderr = "WARNING:...:Failed to load provider 'provider-anthropic': boom\n"
        result = runner._detect_module_load_failures(stderr)
        self.assertEqual(result, ("provider:provider-anthropic",))


class _RunFixture(unittest.TestCase):
    """Minimal real workspace/runs_dir wiring to drive ``runner.run`` --
    the same harness ``test_silent_automation_failures._RunFixture`` uses.
    ``runner._submit_turn`` is the only mocked seam; everything else is the
    real code path (``_execute_turn`` -> ``_run_body`` -> ``_persist_run``).
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


class TestDegradedRunIsVisibleNotFailed(_RunFixture):
    """A run whose init degraded (some module failed to load/validate) but
    still produced a real reply must NOT be marked ``failed`` -- the honest
    minimum is a visible ``module_failures`` field (see
    docs/AUTOMATIONS.md, "Session-init module failures").
    """

    def test_module_failures_surfaces_without_flipping_failed(self) -> None:
        self._write_automation(
            _STEP_AUTOMATION.format(name="Ledger Check"), "ledger-check.md"
        )
        result = self._run(
            outcome=runner._TurnOutcome(
                reply="nothing new",
                tokens_in=10,
                tokens_out=4,
                duration_ms=100,
                stderr_text=_REAL_TOOL_LOAD_FAILURE_STDERR,
                # In production _submit_turn computes this itself (see
                # TestDetectModuleLoadFailures above for that logic in
                # isolation); here it stands in for what a real worker
                # invocation would have already derived from the same stderr.
                module_failures=runner._detect_module_load_failures(
                    _REAL_TOOL_LOAD_FAILURE_STDERR
                ),
            )
        )

        self.assertFalse(result.failed)
        self.assertIsNone(result.error)
        self.assertEqual(result.module_failures, ("tool:tool-ledger-items",))
        self.assertEqual(result.steps[0].module_failures, ("tool:tool-ledger-items",))

        run_dir = self.runs_dir / self.automation.slug / result.run_id
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(persisted["failed"])
        self.assertEqual(persisted["module_failures"], ["tool:tool-ledger-items"])

    def test_clean_run_has_empty_module_failures(self) -> None:
        """Negative control: no warning in stderr -> no false positives."""
        self._write_automation(
            _STEP_AUTOMATION.format(name="Clean Check"), "clean-check.md"
        )
        result = self._run(
            outcome=runner._TurnOutcome(
                reply="all good", tokens_in=3, tokens_out=3, duration_ms=5
            )
        )
        self.assertFalse(result.failed)
        self.assertEqual(result.module_failures, ())

        run_dir = self.runs_dir / self.automation.slug / result.run_id
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["module_failures"], [])


class TestSessionInitCrashStillFailsLoudly(_RunFixture):
    """Unaffected by this change: a turn that genuinely fails at session
    init (the worker reported a real error -- see ``tests/test_agent_worker.py``
    for the worker-level half of this contract) still produces
    ``failed: true`` with a real, non-null ``error`` -- never a defaulted
    success. A degraded-but-answering run (above) and a genuinely crashed
    run must never be confused with each other.
    """

    def test_crash_error_reaches_top_level_and_result_json(self) -> None:
        self._write_automation(
            _STEP_AUTOMATION.format(name="Crash Check"), "crash-check.md"
        )
        result = self._run(
            outcome=runner._TurnOutcome(
                error=(
                    "ModuleValidationError: Module 'provider-anthropic' failed "
                    "validation: 1 error. Errors: missing_mount: mount() "
                    "function not found"
                ),
                # This run's stderr ALSO happens to carry a (different)
                # module-load warning -- visibility and the failure verdict
                # are independent signals, and both must surface.
                stderr_text=_REAL_TOOL_LOAD_FAILURE_STDERR,
                module_failures=runner._detect_module_load_failures(
                    _REAL_TOOL_LOAD_FAILURE_STDERR
                ),
            )
        )

        self.assertTrue(result.failed)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("ModuleValidationError", result.error)
        self.assertEqual(result.module_failures, ("tool:tool-ledger-items",))

        run_dir = self.runs_dir / self.automation.slug / result.run_id
        persisted = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(persisted["failed"])
        self.assertIsNotNone(persisted["error"])
        self.assertIn("ModuleValidationError", persisted["error"])
        self.assertEqual(persisted["module_failures"], ["tool:tool-ledger-items"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
