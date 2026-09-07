"""Proofs for the drumbeat <-> amplifier-agent seam (contracts/agent-binding.v1.md).

Every test here is red-provable: delete the mechanism and the test fails.

The fakes stop at exactly one place -- ``amplifier_agent.create_agent``. Every
record the worker touches (``AgentOptions``, ``TurnInput``, ``Event``,
``TurnResult``, ``Usage``, ``AgentError``) is the REAL one, imported from the
installed library, so a change to the library's own shapes surfaces here rather
than being absorbed by a hand-rolled stand-in. What is faked is only the engine
behind them, because a unit test must not call a provider.

The opt-in live test at the bottom is the other half: one real turn against a
real provider, skipped without ``$OPENAI_API_KEY``.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

from drumbeat import agent_config, agent_worker, paths, runner


# --------------------------------------------------------------------------- #
# the fake engine (one seam: create_agent)                                     #
# --------------------------------------------------------------------------- #


class _FakeTurn:
    def __init__(self, events):
        self._events = events

    def events(self):
        async def _iter():
            for event in self._events:
                yield event

        return _iter()


class _FakeSession:
    def __init__(self, recorder, events):
        self._recorder = recorder
        self._events = events

    async def start_turn(self, turn_input):
        self._recorder["turn_input"] = turn_input
        return _FakeTurn(self._events)

    async def close(self) -> None:
        self._recorder["session_closed"] = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class _FakeAgent:
    def __init__(self, recorder, events, *, resume_error=None):
        self._recorder = recorder
        self._events = events
        self._resume_error = resume_error

    async def create_session(self, options=None):
        self._recorder["created"] = options
        return _FakeSession(self._recorder, self._events)

    async def resume_session(self, session_id):
        if self._resume_error is not None:
            raise self._resume_error
        self._recorder["resumed"] = session_id
        return _FakeSession(self._recorder, self._events)

    async def close(self) -> None:
        self._recorder["agent_closed"] = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def _events(*, state="success", text="the answer", usage=None):
    """A contract-shaped event stream: turn_started, deltas, usage, terminal."""
    from amplifier_agent import (
        Event,
        OutputDelta,
        Selection,
        TextPart,
        TurnResult,
        TurnStarted,
        Usage,
        UsageEntry,
        UsageEvent,
    )

    usage = usage or Usage(
        entries=[
            UsageEntry(
                provider="openai",
                model="gpt-5.6-luna",
                tokens_in=2446,
                tokens_out=6,
                cache_read_tokens=2443,
                cache_write_tokens=8796,
            )
        ]
    )

    def _event(seq, type_, payload):
        return Event(
            contract_version="turn-events/1",
            session_id="sess",
            turn_id="turn",
            sequence=seq,
            type=type_,
            payload=payload,
        )

    return [
        _event(
            1,
            "turn_started",
            TurnStarted(
                continuation="fresh",
                primary_actual=Selection(provider="openai", model="gpt-5.6-luna"),
            ),
        ),
        _event(2, "output_delta", OutputDelta(content=[TextPart(text)])),
        _event(3, "usage", UsageEvent(snapshot=usage)),
        _event(
            4,
            "terminal",
            TurnResult(
                state=state,
                content=[TextPart(text)] if state == "success" else None,
                usage=usage,
            ),
        ),
    ]


def _run_worker(spec: dict, *, agent) -> tuple[list[dict], dict | None]:
    """Drive ``agent_worker._run_turn`` and split its protocol stream.

    Returns ``(display_events, terminal_envelope)``. The stream is read from a
    real text buffer, exactly as the parent reads the worker's stdout pipe.
    """
    import asyncio

    out = io.StringIO()

    async def _create_agent(options):
        agent._recorder["options"] = options
        return agent

    with mock.patch("amplifier_agent.create_agent", _create_agent):
        asyncio.run(agent_worker._run_turn(spec, out))

    events: list[dict] = []
    terminal: dict | None = None
    for line in out.getvalue().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if agent_worker.RESULT_ENVELOPE_KEY in obj:
            terminal = obj[agent_worker.RESULT_ENVELOPE_KEY]
        else:
            events.append(obj)
    return events, terminal


def _spec(tmp: Path, **overrides) -> dict:
    spec = {
        "prompt": "what is on my calendar?",
        "session_id": "fleet-check-20260801T000000Z",
        "agent_session_id": paths.agent_session_id("fleet-check-20260801T000000Z"),
        "turn_id": "turn-abc",
        "cwd": str(tmp),
        "storage": str(tmp / "runs" / "agent-storage" / "s"),
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "skills": [],
        "mcp": [],
        "resume": False,
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------- #
# 1. the options object a turn is assembled from                               #
# --------------------------------------------------------------------------- #


class TestAgentOptionsAssembly(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_provider_model_storage_skills_and_approvals_reach_the_library(self) -> None:
        recorder: dict = {}
        agent = _FakeAgent(recorder, _events())
        agent._recorder = recorder
        skills = self.tmp / "skills"
        skills.mkdir()

        _run_worker(
            _spec(self.tmp, skills=[str(skills)]),
            agent=agent,
        )

        options = recorder["options"]
        self.assertEqual(options.provider, "openai")
        self.assertEqual(options.model, "gpt-5.6-luna")
        self.assertEqual(options.skills, [str(skills)])
        self.assertEqual(str(options.storage), str(self.tmp / "runs" / "agent-storage" / "s"))
        # Unattended by construction: there is nobody to ask, and the library
        # fails a turn loudly rather than proceeding with no policy at all.
        self.assertEqual(options.approvals, "allow")
        # A model probing a path that does not exist must not end an otherwise
        # healthy turn as a failure (contracts/agent-binding.v1.md section 7).
        self.assertEqual(options.tool_error_policy, "continue")

    def test_mcp_block_becomes_real_mcp_server_records(self) -> None:
        from amplifier_agent import McpServer

        recorder: dict = {}
        agent = _FakeAgent(recorder, _events())
        _run_worker(
            _spec(
                self.tmp,
                mcp=[
                    {
                        "name": "ledger",
                        "transport": "stdio",
                        "command": "ledger-mcp",
                        "args": ["--quiet"],
                    }
                ],
            ),
            agent=agent,
        )
        servers = recorder["options"].mcp_servers
        assert servers is not None
        self.assertEqual(len(servers), 1)
        self.assertIsInstance(servers[0], McpServer)
        self.assertEqual(servers[0].name, "ledger")
        self.assertEqual(servers[0].command, "ledger-mcp")

    def test_fresh_turn_wipes_the_storage_root_and_creates_the_session(self) -> None:
        storage = self.tmp / "storage"
        storage.mkdir()
        stale = storage / "stale-marker"
        stale.write_text("previous conversation", encoding="utf-8")

        recorder: dict = {}
        agent = _FakeAgent(recorder, _events())
        _run_worker(_spec(self.tmp, storage=str(storage), resume=False), agent=agent)

        # THE CONTRACT: a fresh turn can never resume a stale conversation.
        self.assertFalse(stale.exists())
        self.assertTrue(storage.is_dir())
        self.assertIn("created", recorder)
        self.assertNotIn("resumed", recorder)
        self.assertEqual(recorder["created"].persistence, "durable")

    def test_resumed_turn_resumes_by_id_and_leaves_storage_alone(self) -> None:
        storage = self.tmp / "storage"
        storage.mkdir()
        keep = storage / "keep-me"
        keep.write_text("the conversation", encoding="utf-8")

        recorder: dict = {}
        agent = _FakeAgent(recorder, _events())
        spec = _spec(self.tmp, storage=str(storage), resume=True)
        _run_worker(spec, agent=agent)

        self.assertTrue(keep.is_file())
        self.assertEqual(recorder["resumed"], spec["agent_session_id"])
        self.assertNotIn("created", recorder)


# --------------------------------------------------------------------------- #
# 2. what comes back                                                           #
# --------------------------------------------------------------------------- #


class TestTurnOutcome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_events_are_translated_into_drumbeats_display_vocabulary(self) -> None:
        recorder: dict = {}
        events, _ = _run_worker(_spec(self.tmp), agent=_FakeAgent(recorder, _events()))
        methods = [e["method"] for e in events]
        self.assertEqual(
            methods, ["progress", "result/delta", "usage", "result/final"]
        )
        # Every translated method must be one the parent's tracker recognizes;
        # an event it drops is an event the operator never sees.
        for method in methods:
            self.assertIn(method, runner._CANONICAL_NDJSON_METHODS)

    def test_usage_is_summed_from_the_terminal_snapshot(self) -> None:
        from amplifier_agent import Usage, UsageEntry

        recorder: dict = {}
        usage = Usage(
            entries=[
                UsageEntry(provider="openai", model="a", tokens_in=10, tokens_out=1),
                UsageEntry(provider="openai", model="b", tokens_in=5, tokens_out=2),
            ]
        )
        _, terminal = _run_worker(
            _spec(self.tmp), agent=_FakeAgent(recorder, _events(usage=usage))
        )
        assert terminal is not None
        self.assertTrue(terminal["ok"])
        self.assertEqual(terminal["reply"], "the answer")
        self.assertEqual(terminal["tokens_in"], 15)
        self.assertEqual(terminal["tokens_out"], 3)

    def test_a_counter_the_library_did_not_report_stays_none(self) -> None:
        """VISION section 4: honestly absent, never a fabricated 0."""
        from amplifier_agent import Usage, UsageEntry

        recorder: dict = {}
        usage = Usage(entries=[UsageEntry(provider="openai", model="a")])
        _, terminal = _run_worker(
            _spec(self.tmp), agent=_FakeAgent(recorder, _events(usage=usage))
        )
        assert terminal is not None
        self.assertIsNone(terminal["tokens_in"])
        self.assertIsNone(terminal["cost_usd"])

    def test_cost_is_an_exact_decimal_string_not_a_float(self) -> None:
        from decimal import Decimal

        from amplifier_agent import Usage, UsageEntry

        recorder: dict = {}
        usage = Usage(
            entries=[
                UsageEntry(
                    provider="openai",
                    model="a",
                    tokens_in=1,
                    tokens_out=1,
                    cost={"USD": Decimal("0.00225566")},
                )
            ]
        )
        _, terminal = _run_worker(
            _spec(self.tmp), agent=_FakeAgent(recorder, _events(usage=usage))
        )
        assert terminal is not None
        self.assertEqual(terminal["cost_usd"], "0.00225566")

    def test_a_non_success_terminal_is_a_failure(self) -> None:
        recorder: dict = {}
        _, terminal = _run_worker(
            _spec(self.tmp), agent=_FakeAgent(recorder, _events(state="failure"))
        )
        assert terminal is not None
        self.assertFalse(terminal["ok"])
        self.assertEqual(terminal["reply"], "")


class TestLibraryErrorsReachTheOperatorVerbatim(unittest.TestCase):
    """A provider drumbeat cannot reach must say so in the LIBRARY's own words.

    Rewording it, or -- worse -- letting the run succeed with an apologetic
    reply, is the dead-brain defect this seam exists to close.
    """

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_engine_unavailable_carries_message_and_remedy_verbatim(self) -> None:
        import asyncio

        from amplifier_agent import AgentError

        error = AgentError(
            "engine_unavailable",
            "lifecycle",
            "The openai connection is not configured.",
            "Set OPENAI_API_KEY before constructing the agent.",
        )

        async def _create_agent(options):
            raise error

        out = io.StringIO()
        with (
            mock.patch("amplifier_agent.create_agent", _create_agent),
            self.assertRaises(AgentError),
        ):
            asyncio.run(agent_worker._run_turn(_spec(self.tmp), out))

        # main() is the seam that converts it to a terminal envelope.
        payload = agent_worker._error_payload(error)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "engine_unavailable")
        self.assertIn("The openai connection is not configured.", payload["error"])
        self.assertIn(
            "Set OPENAI_API_KEY before constructing the agent.", payload["error"]
        )


# --------------------------------------------------------------------------- #
# 3. session-id translation                                                    #
# --------------------------------------------------------------------------- #


_AGENT_SESSION_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{7,63}\Z")


class TestAgentSessionIdTranslation(unittest.TestCase):
    def test_an_already_valid_id_is_used_verbatim(self) -> None:
        """No gratuitous rewriting: an id a human reads in a pin file is the id
        the library stores."""
        self.assertEqual(paths.agent_session_id("fleet-check-1"), "fleet-check-1")

    def test_a_real_drumbeat_id_is_translated_into_the_libraries_shape(self) -> None:
        translated = paths.agent_session_id("channels-check-20260804T221148Z")
        self.assertRegex(translated, _AGENT_SESSION_ID_RE)
        self.assertTrue(translated.startswith("channels-check-20260804t221148z-"))

    def test_translation_is_deterministic_across_processes(self) -> None:
        first = paths.agent_session_id("Daily_Rollup-20260805T202538Z")
        second = paths.agent_session_id("Daily_Rollup-20260805T202538Z")
        self.assertEqual(first, second)

    def test_two_ids_that_sanitize_alike_do_not_collapse(self) -> None:
        """The collision that would silently MERGE two automations' conversations
        -- the worst outcome this seam can produce."""
        a = paths.agent_session_id("Foo_Bar-20260101T000000Z")
        b = paths.agent_session_id("Foo-Bar-20260101T000000Z")
        self.assertNotEqual(a, b)
        self.assertRegex(a, _AGENT_SESSION_ID_RE)
        self.assertRegex(b, _AGENT_SESSION_ID_RE)

    def test_a_very_long_id_still_fits_the_ceiling(self) -> None:
        translated = paths.agent_session_id("x" * 300 + "-20260101T000000Z")
        self.assertLessEqual(len(translated), 64)
        self.assertRegex(translated, _AGENT_SESSION_ID_RE)

    def test_a_short_id_still_meets_the_floor(self) -> None:
        translated = paths.agent_session_id("A")
        self.assertRegex(translated, _AGENT_SESSION_ID_RE)

    def test_storage_root_is_keyed_by_the_translated_id(self) -> None:
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            storage = paths.agent_session_storage("Foo_Bar-1", runs_dir=runs)
            self.assertEqual(storage.parent, paths.agent_storage_root(runs))
            self.assertEqual(storage.name, paths.agent_session_id("Foo_Bar-1"))


# --------------------------------------------------------------------------- #
# 4. reasoning effort reaches the wire the only way it can                     #
# --------------------------------------------------------------------------- #


class TestReasoningEffortMaterialization(unittest.TestCase):
    """The library has no options field and no environment form for reasoning
    effort -- a config FILE is the only channel (measured on the wire:
    evidence/aa-v1-eval/FINDINGS-raw.md T1b)."""

    def _resolve(self, root: Path, block: dict):
        return agent_config.resolve(
            runs_dir=root / "runs",
            slug="fleet-check",
            workspace=root,
            automation_config=block,
            env={},
        )

    def test_effort_is_written_scoped_to_the_resolved_provider(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolved = self._resolve(
                root,
                {
                    "provider": {
                        "module": "openai",
                        "config": {
                            "default_model": "gpt-5.6-luna",
                            "reasoning_effort": "low",
                        },
                    }
                },
            )
            assert resolved.host_config_path is not None
            host = json.loads(
                resolved.host_config_path.read_text(encoding="utf-8")
            )
            self.assertEqual(host["provider"], "openai")
            self.assertEqual(host["model"], "gpt-5.6-luna")
            self.assertEqual(
                host["extra_request_params"]["openai"]["reasoning"]["effort"], "low"
            )
            # The file speaks the LIBRARY's vocabulary only -- a drumbeat key
            # here would be refused by the library, by name.
            self.assertLessEqual(set(host), set(agent_config.HOST_CONFIG_KEYS))

    def test_effort_without_a_provider_is_refused_not_shipped_inert(self) -> None:
        """extra_request_params is keyed BY PROVIDER, so an unscoped effort
        would be written and silently ignored."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with pytest.raises(agent_config.AgentConfigError) as exc:
                self._resolve(
                    root,
                    {"provider": {"config": {"reasoning_effort": "low"}}},
                )
            self.assertIn("provider.module", str(exc.value))

    def test_no_effort_declared_writes_no_request_params(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolved = self._resolve(
                root,
                {"provider": {"module": "openai", "config": {"default_model": "m"}}},
            )
            assert resolved.host_config_path is not None
            host = json.loads(
                resolved.host_config_path.read_text(encoding="utf-8")
            )
            self.assertNotIn("extra_request_params", host)


# --------------------------------------------------------------------------- #
# 5. a turn with no working brain is a failure                                 #
# --------------------------------------------------------------------------- #


class TestDeadBrainVerdict(unittest.TestCase):
    """Measured on the originating deployment: 34 runs between 20:30Z and
    23:44Z recorded ``failed: false, error: null`` while their reply was the
    engine reporting it had no provider."""

    def test_a_provider_unavailability_reply_fails_the_turn(self) -> None:
        for reply in (
            "Error: No providers available",
            "error: no provider available",
            "**No providers available**",
            "- Error: No providers available. Please configure one.",
        ):
            with self.subTest(reply=reply):
                error = runner._dead_brain_error(runner._TurnOutcome(reply=reply))
                self.assertIsNotNone(error)
                assert error is not None
                self.assertIn("provider unavailability", error)

    def test_a_reply_that_merely_mentions_the_phrase_is_not_failed(self) -> None:
        """Anchored, never a substring search: an automation reporting on its
        own fleet's health legitimately quotes this phrase, and failing it
        would be a gate that breaks healthy runs."""
        outcome = runner._TurnOutcome(
            reply="Two automations reported 'No providers available' overnight."
        )
        self.assertIsNone(runner._dead_brain_error(outcome))

    def test_an_ordinary_reply_is_untouched(self) -> None:
        self.assertIsNone(
            runner._dead_brain_error(runner._TurnOutcome(reply="nothing new"))
        )

    def test_a_degraded_module_set_fails_the_turn_naming_the_modules(self) -> None:
        error = runner._dead_brain_error(
            runner._TurnOutcome(reply="all good", module_failures=("tool:ledger",))
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertIn("tool:ledger", error)


# --------------------------------------------------------------------------- #
# 6. the worker env is the only channel the host config travels on             #
# --------------------------------------------------------------------------- #


class TestWorkerEnvironment(unittest.TestCase):
    def test_the_host_config_is_set_on_the_child_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            env = runner._worker_env(
                root,
                runs_dir=root / "runs",
                session_id="s",
                agent_host_config=root / "host.json",
            )
            self.assertEqual(
                env[agent_config.ENV_CONFIG_VAR], str(root / "host.json")
            )
            self.assertNotIn(agent_config.ENV_CONFIG_VAR, os.environ)

    def test_an_inherited_stale_config_is_removed_when_this_turn_has_none(self) -> None:
        """A turn must run on the policy resolved FOR IT, never on a leftover
        file from an earlier one."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runs").mkdir()
            with mock.patch.dict(
                os.environ, {agent_config.ENV_CONFIG_VAR: "/stale/from/earlier.json"}
            ):
                env = runner._worker_env(
                    root, runs_dir=root / "runs", agent_host_config=None
                )
            self.assertNotIn(agent_config.ENV_CONFIG_VAR, env)


# --------------------------------------------------------------------------- #
# 7. opt-in: one REAL turn against a real provider                             #
# --------------------------------------------------------------------------- #


LIVE_MODEL = "gpt-5.6-luna"


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="live provider test -- set OPENAI_API_KEY to run it",
)
def test_live_openai_turn_returns_a_real_reply_and_real_usage(tmp_path: Path) -> None:
    """The one test that proves the whole seam against a real provider.

    Deliberately opt-in: a unit suite that silently needs a credential is a
    suite that is green for the wrong reason on any machine that happens to
    have one. Everything above proves the wiring; this proves the wiring is
    connected to something real.
    """
    storage = tmp_path / "agent-storage" / "live"
    spec = {
        "prompt": "Reply with the single word OK and nothing else.",
        "session_id": "drumbeat-live-check",
        "agent_session_id": paths.agent_session_id("drumbeat-live-check"),
        "turn_id": "turn-live",
        "cwd": str(tmp_path),
        "storage": str(storage),
        "provider": "openai",
        "model": LIVE_MODEL,
        "skills": [],
        "mcp": [],
        "resume": False,
    }

    import asyncio

    out = io.StringIO()
    with redirect_stderr(io.StringIO()):
        asyncio.run(agent_worker._run_turn(spec, out))

    terminal = None
    for line in out.getvalue().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if agent_worker.RESULT_ENVELOPE_KEY in obj:
            terminal = obj[agent_worker.RESULT_ENVELOPE_KEY]
    assert terminal is not None, "the worker produced no terminal envelope"
    assert terminal["ok"], terminal["error"]
    assert terminal["reply"].strip(), "a live turn must return a non-empty reply"
    assert isinstance(terminal["tokens_in"], int)
    assert terminal["tokens_in"] > 0
    # The session is durable and lives exactly where drumbeat put it.
    assert storage.is_dir()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
