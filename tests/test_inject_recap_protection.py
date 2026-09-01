"""Defect 1 fix (docs/designs/ledger-injection-compaction-fault.md, section
3a/3b): compaction evicts or truncates a run's own inject: result before a
later step in the SAME run has finished reading it.

The agent runtime's own context-compaction policy is invisible to this
engine and cannot be controlled from here -- but an automation's validated
``inject:`` text can be carried forward on every subsequent turn of the SAME
run (``runner._run_body``'s ``inject_recap_blocks``), riding the one slot
compaction can never evict: the turn's own prompt, which is always the most
recent message. This closes the failure mode without needing to detect or
reason about compaction at all.

House style (see test_auto_rotation_and_failure_push.py): drive the REAL
production functions; mock only ``runner._submit_turn`` (the established
fake seam for the SDK-driven turn). The inject tool is a REAL executable on
disk, run through the real ``subprocess.run`` in ``_run_inject_tool`` --
never mocked.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import runner
from drumbeat.automation import load_from_text
from drumbeat.paths import derive_workspace_slug

_AUTOMATION_WITH_INJECT_TWO_STEPS = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: manual
  notify: never
  inject:
    - argv: ["{tool}"]
      label: "state"
      expect_prefix: "STATE:"
  steps:
    - id: step-one
      prompt: "Step one: report what you see."
    - id: step-two
      prompt: "Step two: report again, using only what you already have."
---
"""

_AUTOMATION_NO_INJECT_TWO_STEPS = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: step-one
      prompt: "Step one."
    - id: step-two
      prompt: "Step two."
---
"""


def _write_tool(directory: Path, body: str, *, name: str = "tool.sh") -> Path:
    tool = directory / name
    tool.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


class _RunnerFixture(unittest.TestCase):
    """Shared workspace/runs_dir/agent-home wiring -- mirrors
    test_auto_rotation_and_failure_push.py's ``_RunnerFixture``.
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

        self.workspace_slug = derive_workspace_slug(self.workspace)

    def _load(self, template: str, *, name: str, tool: str | None = None):
        text = template.format(name=name, tool=tool or "")
        path = self.workspace / "automations" / f"{name.lower().replace(' ', '-')}.md"
        path.write_text(text, encoding="utf-8")
        return load_from_text(path, text)

    def _state_tool(self, stdout: str = "STATE: 42 open items\n") -> Path:
        return _write_tool(self.workspace, f"printf %s '{stdout}'")


class TestInjectRecapProtectsSubsequentSteps(_RunnerFixture):
    """The injected state is now carried on every subsequent turn of the
    same run, not just the one turn that first delivered it.
    """

    def test_every_step_after_inject_carries_the_injected_text(self) -> None:
        tool = self._state_tool("STATE: 108 open items\n")
        automation = self._load(
            _AUTOMATION_WITH_INJECT_TWO_STEPS, name="Ledger Check", tool=str(tool)
        )

        captured_texts: list[str] = []

        def _fake_submit_turn(**kwargs):
            captured_texts.append(kwargs["text"])
            return runner._TurnOutcome(reply="ok")

        with (
            mock.patch.object(runner, "_submit_turn", side_effect=_fake_submit_turn),
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run(
                automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )

        self.assertFalse(result.failed, result.error)
        # Turn order: requirements turn is absent (no requires:), so turn 0
        # is the inject turn itself, turns 1 and 2 are automation.steps.
        self.assertEqual(len(captured_texts), 3)
        inject_turn_text, step_one_text, step_two_text = captured_texts

        # The inject turn's own text is the raw injected content -- it does
        # not recap itself.
        self.assertIn("STATE: 108 open items", inject_turn_text)

        # Both automation steps -- not just the one immediately after the
        # inject turn -- carry the injected content forward, because
        # compaction could have evicted the ORIGINAL inject turn by the
        # time either of these runs.
        for step_text, step_name in (
            (step_one_text, "step one"),
            (step_two_text, "step two"),
        ):
            with self.subTest(step=step_name):
                self.assertIn(
                    "STATE: 108 open items",
                    step_text,
                    f"{step_name}'s turn text should carry the injected "
                    "state forward as a preamble, protected against "
                    "compaction evicting the original inject turn",
                )
                self.assertIn(
                    "state",
                    step_text.lower(),
                    "the recap should name the inject's own label",
                )

    def test_automation_with_no_inject_is_unaffected(self) -> None:
        """Zero cost / zero behavior change for automations that declare no
        inject: -- the recap tuple is empty and nothing is prepended.
        """
        automation = self._load(_AUTOMATION_NO_INJECT_TWO_STEPS, name="Plain Run")

        captured_texts: list[str] = []

        def _fake_submit_turn(**kwargs):
            captured_texts.append(kwargs["text"])
            return runner._TurnOutcome(reply="ok")

        with (
            mock.patch.object(runner, "_submit_turn", side_effect=_fake_submit_turn),
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run(
                automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )

        self.assertFalse(result.failed, result.error)
        self.assertEqual(len(captured_texts), 2)
        for text in captured_texts:
            self.assertNotIn("[drumbeat] Reminder", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
