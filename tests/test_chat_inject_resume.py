"""Defect 2 fix (docs/designs/ledger-injection-compaction-fault.md, section
3c / section 6 item 3): a resume/replay path that re-enters the step
sequence must re-run the one-shot inject:.

``runner.run_chat_message`` -- the chat/reply turn machinery, which resumes
an automation's pinned session exactly like a scheduled ``run()`` does --
never executed the automation's declared ``inject:`` tools at all, on
either a brand-new or a resumed session: it fires identity turns (new
session only) and the requirements turn (new session only), then the
message turn, with no reference anywhere to ``automation.inject``. Any chat
message that resumes a pinned session an automation ALSO uses for scheduled
runs saw none of that automation's live injected state.

Closed by firing ``inject:`` on every chat turn (new session or resumed),
through the same ``runner._run_inject_tool`` hybrid-sentinel contract
(timeout -> exit code -> stdout) scheduled runs already enforce.

House style (see test_auto_rotation_and_failure_push.py): drive the REAL
production functions; mock only ``runner._submit_turn``. The inject tool is
a REAL executable on disk, run through the real ``subprocess.run`` in
``_run_inject_tool`` -- never mocked, so the hybrid-sentinel contract is
exercised for real.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import runner, session_pins
from drumbeat.automation import load_from_text
from drumbeat.paths import derive_workspace_slug

_CHAT_AUTOMATION_WITH_INJECT = """---
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
    - id: identity
      prompt: "You are the assistant. This is not the host repo."
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

    def _pin_real_session(self, slug: str, session_id: str) -> None:
        session_pins.upsert(
            slug,
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
        (session_dir / "transcript.jsonl").write_text("{}\n", encoding="utf-8")

    def _state_tool(self, stdout: str = "STATE: 42 open items\n") -> Path:
        return _write_tool(self.workspace, f"printf %s '{stdout}'")


class TestChatFiresInjectOnEveryTurn(_RunnerFixture):
    """``run_chat_message`` -- a resume/replay path that re-enters the
    session -- now re-runs inject: on every turn, matching run()'s "before
    every use of this session" discipline.
    """

    def test_brand_new_chat_session_fires_inject(self) -> None:
        tool = self._state_tool("STATE: fresh chat state\n")
        automation = self._load(
            _CHAT_AUTOMATION_WITH_INJECT, name="Chat Ledger", tool=str(tool)
        )

        captured_texts: list[str] = []

        def _fake_submit_turn(**kwargs):
            captured_texts.append(kwargs["text"])
            return runner._TurnOutcome(reply="ok")

        with (
            mock.patch.object(runner, "_submit_turn", side_effect=_fake_submit_turn),
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run_chat_message(
                automation,
                "what's on my plate?",
                cwd=self.workspace,
                runs_dir=self.runs_dir,
            )

        self.assertFalse(result.failed, result.error)
        # identity turn (1) + inject turn (1) + message turn (1)
        self.assertEqual(len(captured_texts), 3)
        self.assertTrue(
            any("STATE: fresh chat state" in t for t in captured_texts),
            "the inject tool's real output never reached any turn -- "
            f"captured texts: {captured_texts!r}",
        )

    def test_resumed_chat_session_fires_inject_again(self) -> None:
        """The literal defect: a message that RESUMES an existing pinned
        session -- the common case, every message after the first -- must
        still re-run inject:, not just the brand-new-session path.
        """
        tool = self._state_tool("STATE: resumed chat state\n")
        automation = self._load(
            _CHAT_AUTOMATION_WITH_INJECT, name="Chat Ledger Resume", tool=str(tool)
        )
        session_id = f"{automation.slug}-existing"
        self._pin_real_session(automation.slug, session_id)

        captured_texts: list[str] = []

        def _fake_submit_turn(**kwargs):
            captured_texts.append(kwargs["text"])
            return runner._TurnOutcome(reply="ok")

        with (
            mock.patch.object(runner, "_submit_turn", side_effect=_fake_submit_turn),
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run_chat_message(
                automation,
                "anything new?",
                cwd=self.workspace,
                runs_dir=self.runs_dir,
            )

        self.assertFalse(result.failed, result.error)
        self.assertEqual(result.session_id, session_id)
        # No identity/requirements turns on a resumed session -- just the
        # (new) inject turn, then the message turn.
        self.assertEqual(len(captured_texts), 2)
        inject_text, _message_text = captured_texts
        self.assertIn("STATE: resumed chat state", inject_text)

    def test_inject_abort_aborts_the_chat_turn_loud(self) -> None:
        """The hybrid-sentinel contract holds on the chat path too: a
        non-zero exit aborts the whole turn rather than proceeding without
        the declared state.

        Uses an already-pinned (resumed) session so the only turn ever
        submitted would be the message turn -- isolating the assertion to
        "did the message turn fire after inject aborted" without also
        counting the brand-new-session identity turn.
        """
        tool = _write_tool(self.workspace, "echo 'boom' >&2; exit 3")
        automation = self._load(
            _CHAT_AUTOMATION_WITH_INJECT, name="Chat Ledger Abort", tool=str(tool)
        )
        self._pin_real_session(automation.slug, f"{automation.slug}-existing")

        with (
            mock.patch.object(runner, "_submit_turn") as mocked_submit,
            redirect_stderr(io.StringIO()),
        ):
            mocked_submit.return_value = runner._TurnOutcome(reply="should not run")
            result = runner.run_chat_message(
                automation,
                "hello",
                cwd=self.workspace,
                runs_dir=self.runs_dir,
            )

        self.assertTrue(result.failed)
        assert result.error is not None
        self.assertIn("exited 3", result.error)
        self.assertIn("boom", result.error)
        # The message turn must never fire once inject aborts -- the model
        # never sees a turn built on missing declared state.
        mocked_submit.assert_not_called()

    def test_idle_sentinel_skips_without_aborting(self) -> None:
        tool = _write_tool(self.workspace, "echo INJECT_IDLE")
        automation = self._load(
            _CHAT_AUTOMATION_WITH_INJECT, name="Chat Ledger Idle", tool=str(tool)
        )

        captured_texts: list[str] = []

        def _fake_submit_turn(**kwargs):
            captured_texts.append(kwargs["text"])
            return runner._TurnOutcome(reply="ok")

        with (
            mock.patch.object(runner, "_submit_turn", side_effect=_fake_submit_turn),
            redirect_stderr(io.StringIO()),
        ):
            result = runner.run_chat_message(
                automation,
                "hello",
                cwd=self.workspace,
                runs_dir=self.runs_dir,
            )

        self.assertFalse(result.failed, result.error)
        # identity turn + message turn only -- inject skipped, not injected,
        # never aborted.
        self.assertEqual(len(captured_texts), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
