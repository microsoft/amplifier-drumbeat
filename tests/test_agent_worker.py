"""Regression proofs for the agent-worker crash-to-verdict seam.

Session-init crash -> ``ok: False`` with a real error, never a defaulted
success. ``agent_worker.main``'s outer ``except BaseException`` around
``_run_turn`` is the seam that catches a genuinely UNHANDLED exception raised
anywhere during session init or the turn itself (including one that somehow
escapes amplifier-core's own per-module catch -- see
``drumbeat.runner._detect_module_load_failures``'s module docstring for the
far more common case where a module-load failure does NOT raise at all).
This file proves that seam holds: it always produces a terminal envelope with
``ok: False`` and a non-empty, real ``error`` -- never a silently-defaulted
success -- and that a turn which actually succeeds is unaffected.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import agent_worker


class _FakeModuleValidationError(Exception):
    """Stand-in for ``amplifier_core.loader.ModuleValidationError`` -- a
    plain ``Exception`` subclass with no special ``.code``/``.message``
    attributes, the same shape ``agent_worker._error_payload`` must handle
    without any special-casing (drumbeat does not import amplifier-core's
    exception type; it must survive an OPAQUE exception just as well).
    """


def _run_worker_capturing_stdout(argv: list[str]) -> tuple[int, str]:
    """Run ``agent_worker.main(argv)`` with the REAL fd-1 redirection dance
    it performs internally (``_redirect_stdout_to_stderr``), and capture
    whatever lands on the ORIGINAL fd 1 -- exactly what
    ``runner._submit_turn`` reads from the worker's real stdout pipe in
    production.

    A raw fd swap is used (not ``capsys``) because ``_emit_result`` writes
    through an ``os.fdopen`` handle on the duplicated original fd, bypassing
    ``sys.stdout`` entirely -- the whole point of the redirection dance is
    that a stray ``print``/library write to "stdout" must land elsewhere.
    ``sys.stdout`` is restored to its original object afterward: ``main()``
    reassigns the process-global ``sys.stdout`` internally, which is
    harmless in production (a worker process exits right after) but must
    not leak into this test process.
    """
    original_stdout = sys.stdout
    with tempfile.TemporaryFile(mode="w+b") as tmp:
        saved_fd1 = os.dup(1)
        try:
            os.dup2(tmp.fileno(), 1)
            exit_code = agent_worker.main(argv)
        finally:
            os.dup2(saved_fd1, 1)
            os.close(saved_fd1)
            sys.stdout = original_stdout
        tmp.seek(0)
        return exit_code, tmp.read().decode("utf-8", errors="replace")


class TestSessionInitCrashProducesLoudFailure(unittest.TestCase):
    """An exception raised anywhere inside ``_run_turn`` (session init or the
    turn itself) must reach the parent as ``ok: False`` with a real error --
    never a silently-defaulted ``ok: True``.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        spec = {
            "session_id": "s-test",
            "cwd": self._tmp.name,
            "prompt": "hello",
            "resume": False,
        }
        self.spec_path = Path(self._tmp.name) / "spec.json"
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def test_module_validation_error_during_init_yields_ok_false(self) -> None:
        async def _boom(spec, out):
            raise _FakeModuleValidationError(
                "Module 'tool-xyz' failed validation: missing mount()"
            )

        with mock.patch.object(agent_worker, "_run_turn", _boom):
            exit_code, stdout_text = _run_worker_capturing_stdout(
                ["--spec-file", str(self.spec_path)]
            )

        # The worker process itself always exits 0 (main()'s own contract) --
        # the failure is communicated through the protocol envelope, not the
        # exit code, so the parent's classification cannot be confused by a
        # nonzero-exit heuristic that doesn't exist.
        self.assertEqual(exit_code, 0)
        lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
        self.assertEqual(
            len(lines), 1, f"expected exactly one protocol line, got: {lines!r}"
        )
        envelope = json.loads(lines[0])
        payload = envelope[agent_worker.RESULT_ENVELOPE_KEY]

        # THE CONTRACT: never a defaulted success.
        self.assertFalse(payload["ok"])
        self.assertIsNotNone(payload["error"])
        self.assertIn("Module 'tool-xyz' failed validation", payload["error"])
        self.assertEqual(payload["code"], "_FakeModuleValidationError")
        self.assertEqual(payload["reply"], "")

    def test_normal_success_is_unaffected(self) -> None:
        """Negative control: the crash-handling path must not manufacture a
        failure for a turn that actually completes.
        """

        async def _ok(spec, out):
            agent_worker._emit_result(
                out,
                {
                    "ok": True,
                    "reply": "all good",
                    "error": None,
                    "code": None,
                    "tokens_in": 3,
                    "tokens_out": 3,
                    "cost_usd": None,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                },
            )

        with mock.patch.object(agent_worker, "_run_turn", _ok):
            exit_code, stdout_text = _run_worker_capturing_stdout(
                ["--spec-file", str(self.spec_path)]
            )

        self.assertEqual(exit_code, 0)
        lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])[agent_worker.RESULT_ENVELOPE_KEY]
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["reply"], "all good")


class TestErrorPayloadShape(unittest.TestCase):
    """Unit coverage for ``_error_payload`` -- the exact shape
    ``runner._submit_turn`` reads (via the terminal envelope) to derive
    ``StepResult.error``.
    """

    def test_plain_exception_uses_str_and_class_name(self) -> None:
        payload = agent_worker._error_payload(_FakeModuleValidationError("boom"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "boom")
        self.assertEqual(payload["code"], "_FakeModuleValidationError")

    def test_exception_with_code_and_message_attrs_is_preferred(self) -> None:
        exc = RuntimeError("fallback text")
        exc.code = "custom_code"  # type: ignore[attr-defined]
        exc.message = "the real reason"  # type: ignore[attr-defined]
        payload = agent_worker._error_payload(exc)
        self.assertEqual(payload["error"], "the real reason")
        self.assertEqual(payload["code"], "custom_code")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
