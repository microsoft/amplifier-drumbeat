"""The `inject:` hybrid-sentinel contract (docs/ARCHITECTURE.md section 6).

Every classification row of the council's contract, red-proven:

- timeout -> abort, voiced
- non-zero exit -> abort, voiced
- exit 0 + byte-exact INJECT_IDLE (whole stripped stdout) -> skip, recorded
- exit 0 + bare-empty stdout -> abort, loud
- anything else -> inject stdout verbatim

The forced-idle and forced-empty rows are the section-12 named negative
drills; they also run LIVE against the real system in the step-2 gate
battery -- these unit forms are the red-provability half.

Also covers: the frontmatter parser's fail-loud rules for `inject:` and the
engine test pack's public exemplar (section 9: consumers must be able to
COPY the pattern).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from drumbeat.automation import AutomationError, InjectSpec, load, load_from_text

from drumbeat import runner

_PACK_DIR = Path(__file__).resolve().parent / "packs" / "minimal"


def _write_tool(directory: Path, body: str) -> Path:
    tool = directory / "tool.sh"
    tool.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


class TestInjectClassification(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _spec(self, tool: Path) -> InjectSpec:
        return InjectSpec(argv=(str(tool),), label="test state")

    def test_nonzero_exit_aborts_voiced(self) -> None:
        tool = _write_tool(self.cwd, "echo 'real reason' >&2; exit 3")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        assert outcome.abort_reason is not None
        self.assertIn("exited 3", outcome.abort_reason)
        self.assertIn("real reason", outcome.abort_reason)
        self.assertFalse(outcome.idle)
        self.assertIsNone(outcome.text)

    def test_forced_idle_sentinel_skips_never_aborts(self) -> None:
        tool = _write_tool(self.cwd, "echo INJECT_IDLE")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        self.assertIsNone(outcome.abort_reason)
        self.assertTrue(outcome.idle)
        self.assertIsNone(outcome.text)

    def test_sentinel_match_is_whole_stdout_not_prefix(self) -> None:
        # A tool that says INJECT_IDLE and then keeps talking is NOT idle --
        # byte-exact whole-stripped-stdout match, never a prefix.
        tool = _write_tool(self.cwd, "printf 'INJECT_IDLE but also this'")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        self.assertIsNone(outcome.abort_reason)
        self.assertFalse(outcome.idle)
        self.assertEqual(outcome.text, "INJECT_IDLE but also this")

    def test_forced_empty_stdout_aborts_loud(self) -> None:
        tool = _write_tool(self.cwd, "exit 0")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        assert outcome.abort_reason is not None
        self.assertIn("EMPTY stdout", outcome.abort_reason)
        self.assertIn(runner.INJECT_IDLE, outcome.abort_reason)

    def test_whitespace_only_stdout_is_empty_not_content(self) -> None:
        tool = _write_tool(self.cwd, "printf '  \\n\\t\\n'")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        assert outcome.abort_reason is not None
        self.assertIn("EMPTY stdout", outcome.abort_reason)

    def test_real_output_injected_verbatim(self) -> None:
        tool = _write_tool(self.cwd, "printf 'line one\\nline two\\n'")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        self.assertIsNone(outcome.abort_reason)
        self.assertEqual(outcome.text, "line one\nline two\n")

    def test_undeclared_expect_prefix_keeps_verbatim_trust(self) -> None:
        """No `expect_prefix` declared (the default, `None`) -- an inject tool
        that never opted in keeps today's unchanged verbatim-trust behavior,
        even for content that looks nothing like ledger truth. Backward
        compatibility for every existing automation that never sets it.
        """
        tool = _write_tool(self.cwd, "printf 'anything at all, unchecked'")
        outcome = runner._run_inject_tool(
            self._spec(tool), cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        self.assertIsNone(outcome.abort_reason)
        self.assertEqual(outcome.text, "anything at all, unchecked")

    def test_expect_prefix_match_injects_verbatim(self) -> None:
        """A declared `expect_prefix` that the real stdout satisfies changes
        nothing about the outcome -- still injected verbatim, full text.
        """
        tool = _write_tool(
            self.cwd,
            "printf 'The state attention ledger currently has 2 OPEN item(s)\\n'",
        )
        spec = InjectSpec(
            argv=(str(tool),),
            label="open items",
            expect_prefix="The state attention ledger",
        )
        outcome = runner._run_inject_tool(
            spec, cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        self.assertIsNone(outcome.abort_reason)
        self.assertEqual(
            outcome.text, "The state attention ledger currently has 2 OPEN item(s)\n"
        )

    def test_expect_prefix_mismatch_aborts_loud_not_fed_to_model(self) -> None:
        """The core regression: a tool that exits 0
        with non-empty stdout that is NOT the declared payload shape -- a
        stray diagnostic sentence, not ledger content -- must abort loud,
        exactly like the empty-stdout row, never reach ``InjectOutcome.text``
        (the only path that becomes a trusted turn).
        """
        tool = _write_tool(
            self.cwd,
            "printf 'The injection that should have arrived ahead of step 2 "
            "is missing from this run.\\n'",
        )
        spec = InjectSpec(
            argv=(str(tool),),
            label="open items",
            expect_prefix="The state attention ledger",
        )
        outcome = runner._run_inject_tool(
            spec, cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        assert outcome.abort_reason is not None
        self.assertIn("expect_prefix", outcome.abort_reason)
        self.assertIn("The state attention ledger", outcome.abort_reason)
        self.assertIn(
            "injection that should have arrived ahead of step 2", outcome.abort_reason
        )
        self.assertFalse(outcome.idle)
        self.assertIsNone(outcome.text)

    def test_expect_prefix_mismatch_message_truncates_long_content(self) -> None:
        """The mismatched content is quoted in the abort reason for
        diagnosability, but capped -- an inject tool that goes rogue and
        emits megabytes must not blow up the abort message (and, downstream,
        the persisted run record / automation_error event) with it.
        """
        tool = _write_tool(self.cwd, "python3 -c \"print('x' * 5000)\"")
        spec = InjectSpec(
            argv=(str(tool),),
            label="open items",
            expect_prefix="The state attention ledger",
        )
        outcome = runner._run_inject_tool(
            spec, cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        assert outcome.abort_reason is not None
        self.assertLess(len(outcome.abort_reason), 1000)

    def test_timeout_aborts_voiced(self) -> None:
        tool = _write_tool(self.cwd, "sleep 5")
        spec = self._spec(tool)
        # Classification order is FIXED: timeout is checked first. Shrink the
        # budget rather than waiting a real minute.
        with mock.patch.object(runner, "_INJECT_TIMEOUT_SECONDS", 0.2):
            outcome = runner._run_inject_tool(
                spec, cwd=self.cwd, runs_dir=self.cwd / "runs"
            )
        assert outcome.abort_reason is not None
        self.assertIn("timed out", outcome.abort_reason)

    def test_unspawnable_tool_aborts(self) -> None:
        spec = InjectSpec(argv=(str(self.cwd / "does-not-exist"),), label="ghost")
        outcome = runner._run_inject_tool(
            spec, cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        assert outcome.abort_reason is not None
        self.assertIn("could not be executed", outcome.abort_reason)

    def test_tool_runs_with_project_bin_on_path(self) -> None:
        bin_dir = self.cwd / "bin"
        bin_dir.mkdir()
        _write_tool(bin_dir, "printf 'from project bin'").rename(bin_dir / "state-tool")
        spec = InjectSpec(argv=("state-tool",), label="path check")
        outcome = runner._run_inject_tool(
            spec, cwd=self.cwd, runs_dir=self.cwd / "runs"
        )
        self.assertEqual(outcome.text, "from project bin")


class TestInjectFrontmatter(unittest.TestCase):
    def _load(self, inject_yaml: str):
        text = (
            "---\n"
            "automation:\n"
            "  name: T\n"
            "  trigger:\n"
            "    type: manual\n"
            "  notify: never\n"
            f"{inject_yaml}"
            "  steps:\n"
            "    - id: do-the-thing\n"
            "      prompt: Do the thing.\n"
            "---\n"
        )
        return load_from_text(Path("t.md"), text)

    def test_absent_inject_is_empty_tuple(self) -> None:
        automation = self._load("")
        self.assertEqual(automation.inject, ())

    def test_valid_inject_parses(self) -> None:
        automation = self._load(
            "  inject:\n"
            '    - argv: ["items-cli", "inject-turn"]\n'
            '      label: "open items"\n'
        )
        self.assertEqual(
            automation.inject,
            (InjectSpec(argv=("items-cli", "inject-turn"), label="open items"),),
        )

    def test_missing_label_fails_loud(self) -> None:
        with self.assertRaises(AutomationError) as ctx:
            self._load('  inject:\n    - argv: ["tool"]\n')
        self.assertIn("label", str(ctx.exception))

    def test_missing_argv_fails_loud(self) -> None:
        with self.assertRaises(AutomationError):
            self._load('  inject:\n    - label: "x"\n')

    def test_empty_argv_fails_loud(self) -> None:
        with self.assertRaises(AutomationError):
            self._load('  inject:\n    - argv: []\n      label: "x"\n')

    def test_unknown_key_fails_loud(self) -> None:
        with self.assertRaises(AutomationError) as ctx:
            self._load(
                '  inject:\n    - argv: ["tool"]\n      label: "x"\n      timeout: 5\n'
            )
        self.assertIn("unknown key", str(ctx.exception))

    def test_non_list_inject_fails_loud(self) -> None:
        with self.assertRaises(AutomationError):
            self._load("  inject: yes\n")

    def test_valid_inject_with_expect_prefix_parses(self) -> None:
        """The optional shape-guard field: declaring it parses into
        ``InjectSpec.expect_prefix``, distinct from the absent-by-default
        case covered by ``test_valid_inject_parses`` above.
        """
        automation = self._load(
            "  inject:\n"
            '    - argv: ["items-cli", "inject-turn"]\n'
            '      label: "open items"\n'
            '      expect_prefix: "The attention ledger"\n'
        )
        self.assertEqual(
            automation.inject,
            (
                InjectSpec(
                    argv=("items-cli", "inject-turn"),
                    label="open items",
                    expect_prefix="The attention ledger",
                ),
            ),
        )

    def test_expect_prefix_absent_defaults_to_none(self) -> None:
        automation = self._load('  inject:\n    - argv: ["tool"]\n      label: "x"\n')
        self.assertIsNone(automation.inject[0].expect_prefix)

    def test_expect_prefix_empty_string_fails_loud(self) -> None:
        """An empty ``expect_prefix`` would match any stdout and silently
        defeat the shape check it exists to add -- reject it at parse time
        rather than shipping a no-op guard.
        """
        with self.assertRaises(AutomationError) as ctx:
            self._load(
                '  inject:\n    - argv: ["tool"]\n      label: "x"\n'
                '      expect_prefix: ""\n'
            )
        self.assertIn("expect_prefix", str(ctx.exception))

    def test_expect_prefix_wrong_type_fails_loud(self) -> None:
        with self.assertRaises(AutomationError) as ctx:
            self._load(
                '  inject:\n    - argv: ["tool"]\n      label: "x"\n'
                "      expect_prefix: 5\n"
            )
        self.assertIn("expect_prefix", str(ctx.exception))


class TestMinimalPackExemplar(unittest.TestCase):
    """The engine's minimal test pack carries the PUBLIC inject: exemplar
    (section 9) -- prove the copyable artifact actually parses and its tool
    honors the contract."""

    def test_exemplar_automation_parses_with_inject(self) -> None:
        automation = load(_PACK_DIR / "automations" / "inject-exemplar.md")
        self.assertEqual(
            automation.inject,
            (InjectSpec(argv=("minimal-state",), label="minimal state"),),
        )

    def test_pack_tool_honors_the_sentinel_contract(self) -> None:
        tool = _PACK_DIR / "bin" / "minimal-state"
        # idle -> byte-exact sentinel
        idle = subprocess.run(
            [str(tool)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "MINIMAL_STATE_IDLE": "1"},
            check=True,
        )
        self.assertEqual(idle.stdout.strip(), runner.INJECT_IDLE)
        # failure -> non-zero, stderr only
        failed = subprocess.run(
            [str(tool)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "MINIMAL_STATE_FAIL": "1"},
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        self.assertTrue(failed.stderr.strip())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
