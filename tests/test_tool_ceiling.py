"""Proof that a tool result is bounded BEFORE it can enter the conversation.

contracts/agent-binding.v1.md section 10. The incident these tests guard: a
single 46,464,072-byte tool result entered a pinned session, every later turn
re-sent it in full, and the provider then refused every request
(``string_above_max_length``). A tool result is not a transient display
artifact -- it is transcript, forever -- so bounding it AFTER the fact is not a
thing that exists.

House style: drive the real production functions and check their real on-disk
side effects. Nothing here is mocked; ``apply_ceiling`` writes real files into
real temporary directories, and ``run_command`` runs real shell commands.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import agent_config, tool_ceiling


class TestDefaultIsTheDocumentedOne(unittest.TestCase):
    """The default is a contract number, not an implementation detail.

    Asserted separately from every test that shrinks the ceiling for speed, so
    "the default is whatever the test happens to pass" can never quietly become
    true.
    """

    def test_default_is_262144_bytes(self) -> None:
        self.assertEqual(tool_ceiling.DEFAULT_CEILING_BYTES, 262_144)

    def test_no_declaration_and_no_env_uses_the_default(self) -> None:
        with mock.patch.dict(os.environ, {tool_ceiling.ENV_CEILING_VAR: ""}):
            self.assertEqual(
                tool_ceiling.ceiling_bytes(None), tool_ceiling.DEFAULT_CEILING_BYTES
            )


class TestCeilingApplied(unittest.TestCase):
    """Over the ceiling: truncated, noted, and the whole thing written out."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "runs" / "fleet-check" / "run-1"

    def test_oversized_result_is_truncated_noted_and_written_in_full(self) -> None:
        payload = "x" * 20_000
        bounded = tool_ceiling.apply_ceiling(
            payload, limit_bytes=1_000, call_id="call-abc", output_dir=self.run_dir
        )

        self.assertTrue(bounded.truncated)
        self.assertEqual(bounded.total_bytes, 20_000)
        self.assertEqual(bounded.kept_bytes, 1_000)

        # What actually enters the conversation: the kept prefix, then the note.
        self.assertTrue(bounded.text.startswith("x" * 1_000))
        self.assertIn(
            "[drumbeat: output truncated -- kept 1000 of 20000 bytes; full output: ",
            bounded.text,
        )
        # And it is genuinely small -- the whole point.
        self.assertLess(len(bounded.text.encode("utf-8")), 1_400)

        # The full output survives, at the contracted path, byte-for-byte.
        expected = self.run_dir / tool_ceiling.TOOL_OUTPUT_DIRNAME / "call-abc.txt"
        self.assertEqual(bounded.full_output_path, expected)
        self.assertTrue(expected.is_file())
        self.assertEqual(expected.read_text(encoding="utf-8"), payload)
        # The note names the file a human can actually open.
        self.assertIn(str(expected), bounded.text)

    def test_truncation_cuts_on_a_utf8_character_boundary(self) -> None:
        """A partial multi-byte sequence is DROPPED, never emitted broken.

        Mojibake in a tool result is worse than truncation: the model cannot
        tell it from data and reasons from it.
        """
        # Each snowman is 3 bytes; a 10-byte ceiling lands mid-character.
        bounded = tool_ceiling.apply_ceiling(
            "\u2603" * 100, limit_bytes=10, call_id="c", output_dir=self.run_dir
        )
        kept = bounded.text.split("\n[drumbeat:")[0]
        self.assertEqual(kept, "\u2603" * 3)  # 9 bytes, not 10 with a broken tail
        self.assertEqual(bounded.kept_bytes, 9)
        # Round-trips: it is valid UTF-8 by construction.
        self.assertEqual(kept.encode("utf-8").decode("utf-8"), kept)

    def test_call_id_is_sanitized_so_it_cannot_escape_the_run_dir(self) -> None:
        bounded = tool_ceiling.apply_ceiling(
            "y" * 500, limit_bytes=10, call_id="../../etc/passwd", output_dir=self.run_dir
        )
        assert bounded.full_output_path is not None
        self.assertEqual(
            bounded.full_output_path.parent,
            self.run_dir / tool_ceiling.TOOL_OUTPUT_DIRNAME,
        )
        self.assertNotIn("..", bounded.full_output_path.name)


class TestUnderCeilingIsUntouched(unittest.TestCase):
    """At or below the ceiling: byte-identical, no note, no file."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run"

    def test_small_result_passes_through_verbatim(self) -> None:
        payload = "the quick brown fox\nexit code: 0\n"
        bounded = tool_ceiling.apply_ceiling(
            payload, limit_bytes=1_000, call_id="c", output_dir=self.run_dir
        )
        self.assertFalse(bounded.truncated)
        self.assertEqual(bounded.text, payload)
        self.assertIsNone(bounded.full_output_path)
        self.assertNotIn("drumbeat: output truncated", bounded.text)
        # Nothing was written -- an ordinary run leaves no overflow litter.
        self.assertFalse((self.run_dir / tool_ceiling.TOOL_OUTPUT_DIRNAME).exists())

    def test_exactly_at_the_ceiling_is_not_truncated(self) -> None:
        bounded = tool_ceiling.apply_ceiling(
            "z" * 100, limit_bytes=100, call_id="c", output_dir=self.run_dir
        )
        self.assertFalse(bounded.truncated)
        self.assertEqual(bounded.text, "z" * 100)


class TestNeverFailsOpen(unittest.TestCase):
    """An unwritable overflow file must not become an unbounded result.

    Losing the overflow is an inconvenience. Letting 46 MB reach the provider
    is the incident. So the write failure is reported, loudly, and the
    truncation still happens.
    """

    def test_unwritable_run_dir_still_truncates_and_says_why(self) -> None:
        with TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "run"
            # A FILE where the run directory should be: mkdir(parents=True)
            # cannot succeed under it.
            blocked.write_text("not a directory", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                bounded = tool_ceiling.apply_ceiling(
                    "q" * 5_000, limit_bytes=100, call_id="c", output_dir=blocked
                )

        self.assertTrue(bounded.truncated)
        self.assertEqual(bounded.kept_bytes, 100)
        self.assertIsNone(bounded.full_output_path)
        self.assertIsNotNone(bounded.write_error)
        self.assertIn("full output: unavailable -- ", bounded.text)
        # Fail loud: the operator is told the overflow was lost.
        self.assertIn("could", stderr.getvalue())
        self.assertIn("NOT be written", stderr.getvalue())

    def test_no_run_directory_still_truncates(self) -> None:
        bounded = tool_ceiling.apply_ceiling(
            "q" * 5_000, limit_bytes=100, call_id="c", output_dir=None
        )
        self.assertTrue(bounded.truncated)
        self.assertIn("full output: unavailable -- no run directory", bounded.text)


class TestPerAutomationOverride(unittest.TestCase):
    """``agent_config.tool_result_ceiling_bytes`` -- the automation's own knob."""

    def test_declared_value_wins_over_the_env_and_the_default(self) -> None:
        merged = {"tool_result_ceiling_bytes": 4_096}
        override = agent_config.tool_result_ceiling_bytes(merged)
        self.assertEqual(override, 4_096)
        with mock.patch.dict(os.environ, {tool_ceiling.ENV_CEILING_VAR: "999999"}):
            self.assertEqual(tool_ceiling.ceiling_bytes(override), 4_096)

    def test_absent_key_defers_to_the_deployment_env_knob(self) -> None:
        self.assertIsNone(agent_config.tool_result_ceiling_bytes({}))
        with mock.patch.dict(os.environ, {tool_ceiling.ENV_CEILING_VAR: "512"}):
            self.assertEqual(tool_ceiling.ceiling_bytes(None), 512)

    def test_the_override_actually_changes_what_is_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            limit = tool_ceiling.ceiling_bytes(
                agent_config.tool_result_ceiling_bytes(
                    {"tool_result_ceiling_bytes": 64}
                )
            )
            bounded = tool_ceiling.apply_ceiling(
                "w" * 10_000, limit_bytes=limit, call_id="c", output_dir=Path(tmp)
            )
        self.assertEqual(bounded.kept_bytes, 64)

    def test_zero_is_refused_at_load_not_read_as_unlimited(self) -> None:
        """The one value that must never mean "no ceiling"."""
        with self.assertRaises(agent_config.AgentConfigError) as caught:
            agent_config.validate_config_layer(
                {"tool_result_ceiling_bytes": 0}, source="automation fleet-check.md"
            )
        message = str(caught.exception)
        self.assertIn("tool_result_ceiling_bytes", message)
        self.assertIn("does not mean 'unlimited'", message)
        # And the remedy names the engine default.
        self.assertIn(str(tool_ceiling.DEFAULT_CEILING_BYTES), message)

    def test_non_integer_is_refused_by_name(self) -> None:
        for bad in ("256k", -1, 1.5, True, None):
            with (
                self.subTest(value=bad),
                self.assertRaises(agent_config.AgentConfigError),
            ):
                agent_config.validate_config_layer(
                    {"tool_result_ceiling_bytes": bad}, source="automation x.md"
                )

    def test_the_key_is_inside_the_closed_vocabulary(self) -> None:
        self.assertIn("tool_result_ceiling_bytes", agent_config.ALLOWED_TOP_LEVEL_KEYS)
        # ... and the vocabulary is still closed to everything else.
        with self.assertRaises(agent_config.AgentConfigError):
            agent_config.validate_config_layer(
                {"tool_result_ceiling_ohno": 1}, source="automation x.md"
            )


class TestBadEnvValueFailsLoudNotOpen(unittest.TestCase):
    def test_unusable_env_value_is_reported_and_the_default_is_used(self) -> None:
        for bad in ("nope", "0", "-5"):
            with self.subTest(value=bad):
                stderr = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {tool_ceiling.ENV_CEILING_VAR: bad}),
                    redirect_stderr(stderr),
                ):
                    resolved = tool_ceiling.ceiling_bytes(None)
                self.assertEqual(resolved, tool_ceiling.DEFAULT_CEILING_BYTES)
                self.assertIn(tool_ceiling.ENV_CEILING_VAR, stderr.getvalue())


class TestRunCommand(unittest.TestCase):
    """The shell half: same shape as the built-in tool, real subprocesses."""

    def test_captures_stdout_and_exit_code(self) -> None:
        text, code = tool_ceiling.run_command("echo hello", cwd=Path.cwd())
        self.assertEqual(code, 0)
        self.assertIn("hello", text)
        self.assertIn("exit code: 0", text)

    def test_nonzero_exit_is_reported_not_swallowed(self) -> None:
        text, code = tool_ceiling.run_command("echo bad >&2; exit 3", cwd=Path.cwd())
        self.assertEqual(code, 3)
        self.assertIn("exit code: 3", text)
        self.assertIn("bad", text)

    def test_runs_in_the_given_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            text, _ = tool_ceiling.run_command("pwd", cwd=tmp)
            self.assertIn(str(Path(tmp).resolve()), text)

    def test_timeout_reports_what_it_had_and_says_the_effect_may_have_landed(
        self,
    ) -> None:
        text, code = tool_ceiling.run_command("sleep 5", cwd=Path.cwd(), timeout_s=1)
        self.assertIsNone(code)
        self.assertIn("timed out after 1s", text)
        self.assertIn("may already have happened", text)

    def test_timeout_is_clamped_to_the_library_bounds(self) -> None:
        self.assertEqual(tool_ceiling.coerce_timeout(None), tool_ceiling.DEFAULT_TIMEOUT_S)
        self.assertEqual(tool_ceiling.coerce_timeout(0), tool_ceiling.MIN_TIMEOUT_S)
        self.assertEqual(tool_ceiling.coerce_timeout(9_999), tool_ceiling.MAX_TIMEOUT_S)
        self.assertEqual(tool_ceiling.coerce_timeout(45), 45)

    def test_a_huge_command_output_is_bounded_end_to_end(self) -> None:
        """The DTU proof's shape, in miniature and without a provider.

        A command emitting far more than the ceiling produces a small result
        and a full file -- which is the whole claim.
        """
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            text, _ = tool_ceiling.run_command(
                "python3 -c \"print('x'*2000000)\"", cwd=tmp, timeout_s=60
            )
            self.assertGreater(len(text.encode("utf-8")), 2_000_000)
            bounded = tool_ceiling.apply_ceiling(
                text, limit_bytes=4_096, call_id="big", output_dir=run_dir
            )
        self.assertTrue(bounded.truncated)
        self.assertLess(len(bounded.text.encode("utf-8")), 4_500)
        self.assertGreater(bounded.total_bytes, 2_000_000)


if __name__ == "__main__":
    unittest.main()
