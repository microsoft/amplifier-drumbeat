"""9h5 end-to-end: the resolver and provider rotation wired into ``runner.run``.

These drive the REAL ``runner.run`` (mocking only ``_submit_turn`` -- the same
SDK-turn seam ``test_auto_rotation_and_failure_push`` uses) and
assert real on-disk side effects. They prove the two load-bearing acceptance
criteria the unit tests can only prove in pieces:

  * an automation whose ``agent_config:`` sets a model X over the workspace
    default Y runs amplifier-agent with a ``--config`` that carries X, and the
    persisted run record carries the config's sha (and path);
  * a pinned session whose effective provider module CHANGES auto-rotates on
    the next run -- exactly once, then self-heals with a fresh pin;
  * and the back-compat floor: a plain automation with no config anywhere
    threads NO ``--config`` and writes a run record whose config fields are
    null (argv identical to pre-feature behavior).
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

from drumbeat import agent_config, runner, session_health, session_pins
from drumbeat.automation import load
from drumbeat.paths import derive_workspace_slug


class _RunnerFixture(unittest.TestCase):
    """Shared workspace/runs_dir/agent-home wiring. House style: drive the REAL
    ``runner.run``; the only mock is ``runner._submit_turn`` (see
    ``runner._TurnOutcome``). ``AMPLIFIER_AGENT_CONFIG`` is pinned empty so a
    host value cannot leak into the resolver's base layer and perturb these
    assertions."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        self.workspace = self.tmp_path / "workspace"
        for sub in ("automations", "prompts"):
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
        self.prompts_dir = self.workspace / "prompts"

        self.runs_dir = self.tmp_path / "runs"
        self.runs_dir.mkdir()

        self.agent_home = self.tmp_path / "agent-home"
        self.workspace_slug = derive_workspace_slug(self.workspace)

        env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFIER_AGENT_HOME": str(self.agent_home),
                "AMPLIFIER_AGENT_WORKSPACE": "",
                "AMPLIFIER_AGENT_CONFIG": "",
                "CONTEXT_INTELLIGENCE_PERSONAL": "",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _write_automation(self, text: str, filename: str = "demo.md") -> None:
        (self.workspace / "automations" / filename).write_text(text, encoding="utf-8")
        self.automation = load(self.workspace / "automations" / filename)

    def _pin_real_session(self, session_id: str, *, provider: str | None) -> None:
        """Pin a session AND create the on-disk session dir it probes as EXISTS,
        recording the contract fingerprint + provider it was created under."""
        session_pins.upsert(
            self.automation.slug,
            session_id=session_id,
            session_workspace=self.workspace_slug,
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=self.runs_dir,
        )
        session_health.record_contract(
            session_id=session_id,
            automation_slug=self.automation.slug,
            fingerprint=session_health.contract_fingerprint(self.automation.steps),
            recorded_at="2026-08-25T00:00:00Z",
            runs_dir=self.runs_dir,
            provider_module=provider,
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

    def _rotation_lines(self) -> list[dict]:
        path = self.runs_dir / "session_rotations.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _result_json(self, run_id: str) -> dict:
        path = self.runs_dir / self.automation.slug / run_id / "result.json"
        return json.loads(path.read_text(encoding="utf-8"))


_MODEL_PIN = """---
automation:
  name: Model Pin
  enabled: true
  trigger:
    type: manual
  notify: never
  agent_config:
    provider:
      config:
        default_model: model-X
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""

_PLAIN = """---
automation:
  name: Plain One
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""

_PROVIDER_OPENAI = """---
automation:
  name: Teams Check
  enabled: true
  trigger:
    type: manual
  notify: never
  agent_config:
    provider:
      module: openai
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""


class TestRunRecordCarriesResolvedConfig(_RunnerFixture):
    """Acceptance: agent_config sets model X over workspace default Y -> the
    materialized --config carries X, and the run record carries the config sha."""

    def test_materialized_config_carries_X_and_run_record_carries_sha(self) -> None:
        # Workspace default says model-Y; the automation says model-X.
        (self.workspace / "agent-config.yaml").write_text(
            "default:\n  provider:\n    config:\n      default_model: model-Y\n",
            encoding="utf-8",
        )
        self._write_automation(_MODEL_PIN)

        result = self._run(outcome=runner._TurnOutcome(reply="ok", tokens_in=3, tokens_out=3, duration_ms=5))
        self.assertFalse(result.failed)

        record = self._result_json(result.run_id)
        # The run record carries the effective config path + sha.
        self.assertIsNotNone(record["effective_config_sha"])
        self.assertIsNotNone(record["effective_config_path"])

        # The materialized --config document actually carries model-X
        # (automation over workspace default), and its sha matches the record.
        materialized = json.loads(
            Path(record["effective_config_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(materialized["provider"]["config"]["default_model"], "model-X")
        import hashlib

        content = json.dumps(materialized, indent=2) + "\n"
        self.assertEqual(
            record["effective_config_sha"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )


class TestPlainAutomationIsUnchanged(_RunnerFixture):
    """Back-compat floor: no config anywhere -> no --config, and the run record's
    config fields are null (argv identical to pre-feature behavior)."""

    def test_no_config_threads_no_config_and_records_nulls(self) -> None:
        self._write_automation(_PLAIN)
        result = self._run(outcome=runner._TurnOutcome(reply="ok", tokens_in=3, tokens_out=3, duration_ms=5))
        self.assertFalse(result.failed)

        record = self._result_json(result.run_id)
        self.assertIsNone(record["effective_config_path"])
        self.assertIsNone(record["effective_config_sha"])

        # Nothing was materialized at all.
        self.assertFalse((self.runs_dir / agent_config.MATERIALIZED_DIRNAME).exists())


class TestProviderChangeAutoRotatesOnceThenSelfHeals(_RunnerFixture):
    """Acceptance: the provider module changes for a pinned slug -> the pin
    auto-rotates. The session was created under 'anthropic'; the automation's
    agent_config now selects provider module 'openai'."""

    def test_provider_change_rotates_once_and_next_run_gets_a_fresh_pin(
        self,
    ) -> None:
        self._write_automation(_PROVIDER_OPENAI)
        old_session_id = f"{self.automation.slug}-20260801T000000Z-aaaaaa"
        self._pin_real_session(old_session_id, provider="anthropic")

        # ---- A: first run under the changed provider rotates the stale pin ----
        result_a = self._run(outcome=runner._TurnOutcome(reply="ok", tokens_in=3, tokens_out=3, duration_ms=5))
        self.assertFalse(result_a.failed)

        rotations = self._rotation_lines()
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["old_session_id"], old_session_id)
        self.assertTrue(rotations[0]["reason"].startswith("auto:"))
        self.assertIn("provider change", rotations[0]["reason"])

        # The run created a FRESH session (not the abandoned one).
        pin = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertNotEqual(pin.session_id, old_session_id)
        self.assertEqual(pin.session_id, result_a.session_id)

        # The fresh session records the NEW provider it was created under.
        self.assertEqual(
            session_health.read_provider(pin.session_id, runs_dir=self.runs_dir),
            "openai",
        )

        # ---- B: the very next run does NOT rotate again (same provider) ----
        result_b = self._run(outcome=runner._TurnOutcome(reply="ok", tokens_in=3, tokens_out=3, duration_ms=5))
        self.assertFalse(result_b.failed)
        self.assertEqual(len(self._rotation_lines()), 1)

    def test_same_provider_pin_is_left_alone(self) -> None:
        """Negative control: a pin created under the SAME provider the config
        resolves to is never rotated -- proving the check can fail to fire."""
        self._write_automation(_PROVIDER_OPENAI)
        session_id = f"{self.automation.slug}-20260802T000000Z-bbbbbb"
        self._pin_real_session(session_id, provider="openai")

        result = self._run(outcome=runner._TurnOutcome(reply="ok", tokens_in=3, tokens_out=3, duration_ms=5))
        self.assertFalse(result.failed)
        self.assertEqual(self._rotation_lines(), [])

        pin = session_pins.get(self.automation.slug, runs_dir=self.runs_dir)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(pin.session_id, session_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
