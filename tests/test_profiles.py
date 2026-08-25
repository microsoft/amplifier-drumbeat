"""Open-vocabulary profiles replace the retired per-modality model policy.

An interactive/API turn may carry ``profile: <name>``; the named profile is
looked up in the workspace ``agent-config.yaml`` ``profiles:`` block and folded
in as layer 3 of the shared agent-config merge (base env -> ``default:`` ->
profile -> automation ``agent_config:``). The
vocabulary of NAMES is OPEN -- the owner picks them -- but each profile's config
is held to the SAME closed top-level vocabulary and recursive credential/null
refusal as every other layer.

Every test here is red-provable: delete the mechanism and the test fails.

The load-bearing end-to-end pin is
``test_quick_profile_runs_amplifier_agent_with_that_provider_model``: a real
``resume_turn`` over a real ``agent-config.yaml`` profile, asserting the real
argv carries ``--config`` pointing at a host config whose ``default_model`` is
the profile's model. Its companions prove a profile-less turn uses the
``default:`` layer, an unknown profile fails loud LISTING the available
profiles, and credentials in a profile are refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import pytest
from amplifier_agent_py import ResultEvent

from drumbeat import agent_config, runner, turns
from drumbeat.management_api import EngineContext

QUICK_MODEL = "claude-3-5-haiku-latest"
LOCAL_MODEL = "qwen3.6-35b-a3b"
LOCAL_BASE_URL = "http://192.168.1.7:8081/v1"


def _write_agent_config(workspace: Path, text: str) -> None:
    (workspace / agent_config.WORKSPACE_CONFIG_FILENAME).write_text(
        text, encoding="utf-8"
    )


# ---- parse_request: the profile field ----


def test_profile_is_optional_and_defaults_to_none() -> None:
    request = turns.parse_request({"text": "hi", "origin": "reply", "session_id": "s"})
    assert request.profile is None


def test_parse_request_accepts_a_profile_name() -> None:
    body = {"text": "hi", "origin": "reply", "session_id": "s", "profile": "quick"}
    assert turns.parse_request(body).profile == "quick"


def test_parse_request_strips_profile_whitespace() -> None:
    body = {"text": "hi", "origin": "reply", "session_id": "s", "profile": " quick "}
    assert turns.parse_request(body).profile == "quick"


def test_parse_request_refuses_empty_profile() -> None:
    body = {"text": "hi", "origin": "reply", "session_id": "s", "profile": "   "}
    with pytest.raises(turns.TurnError) as exc:
        turns.parse_request(body)
    assert exc.value.status == 400


# ---- load_profiles: fail loud on every malformed shape ----


def test_missing_file_has_no_profiles(tmp_path: Path) -> None:
    assert agent_config.load_profiles(tmp_path) == {}


def test_file_without_profiles_block_has_no_profiles(tmp_path: Path) -> None:
    _write_agent_config(tmp_path, "default:\n  provider:\n    module: anthropic\n")
    assert agent_config.load_profiles(tmp_path) == {}


def test_valid_profiles_parse(tmp_path: Path) -> None:
    _write_agent_config(
        tmp_path,
        "profiles:\n"
        "  quick:\n"
        "    provider:\n"
        "      config:\n"
        f"        default_model: {QUICK_MODEL}\n",
    )
    profiles = agent_config.load_profiles(tmp_path)
    assert set(profiles) == {"quick"}
    assert profiles["quick"]["provider"]["config"]["default_model"] == QUICK_MODEL


def test_profiles_block_must_be_a_mapping(tmp_path: Path) -> None:
    _write_agent_config(tmp_path, "profiles:\n  - quick\n")
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.load_profiles(tmp_path)
    assert "profiles" in str(exc.value)


def test_profile_config_is_held_to_closed_vocabulary(tmp_path: Path) -> None:
    # A profile's config obeys the SAME closed top-level vocabulary as every
    # other layer -- an unknown top-level key is refused, naming the profile.
    _write_agent_config(tmp_path, "profiles:\n  quick:\n    nonsense: true\n")
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.load_profiles(tmp_path)
    assert "nonsense" in str(exc.value)


def test_profile_credentials_are_refused(tmp_path: Path) -> None:
    # Credentials in a committed config never take effect (amplifier-agent
    # re-asserts them from the environment) and would leak -- refused at depth.
    _write_agent_config(
        tmp_path,
        "profiles:\n  quick:\n    provider:\n      config:\n        api_key: sk-leak\n",
    )
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.load_profiles(tmp_path)
    assert "api_key" in str(exc.value)


def test_unknown_workspace_top_level_key_is_refused(tmp_path: Path) -> None:
    _write_agent_config(tmp_path, "profiles:\n  quick: {}\nbogus: 1\n")
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.load_profiles(tmp_path)
    assert "bogus" in str(exc.value)


# ---- select_profile: unknown fails loud, LISTING the available profiles ----


def test_select_profile_returns_the_named_layer(tmp_path: Path) -> None:
    _write_agent_config(
        tmp_path,
        "profiles:\n"
        "  quick:\n"
        "    provider:\n"
        "      config:\n"
        f"        default_model: {QUICK_MODEL}\n",
    )
    layer = agent_config.select_profile(tmp_path, "quick")
    assert layer["provider"]["config"]["default_model"] == QUICK_MODEL


def test_unknown_profile_fails_loud_listing_available(tmp_path: Path) -> None:
    _write_agent_config(
        tmp_path,
        "profiles:\n  quick: {}\n  deep: {}\n",
    )
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.select_profile(tmp_path, "carrier-pigeon")
    message = str(exc.value)
    assert "carrier-pigeon" in message
    # The refusal LISTS what IS available so the caller can correct in one step.
    assert "quick" in message
    assert "deep" in message


def test_unknown_profile_with_no_profiles_defined_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.select_profile(tmp_path, "quick")
    assert "quick" in str(exc.value)


# ---- resolve_turn: (profile + layers) -> a real --config file, or None ----


def test_resolve_turn_without_profile_or_default_threads_no_config(
    tmp_path: Path,
) -> None:
    # No agent-config.yaml, no env config, no profile -> empty merge -> no
    # --config, byte-identical to pre-profile behavior.
    resolved = agent_config.resolve_turn(
        runs_dir=tmp_path / "runs", workspace=tmp_path, key="t-1", profile=None, env={}
    )
    assert resolved.path is None


def test_profile_less_turn_uses_default_layer(tmp_path: Path) -> None:
    _write_agent_config(
        tmp_path,
        "default:\n  provider:\n    config:\n      enable_prompt_caching: true\n",
    )
    resolved = agent_config.resolve_turn(
        runs_dir=tmp_path / "runs", workspace=tmp_path, key="t-2", profile=None, env={}
    )
    assert resolved.path is not None
    written = json.loads(resolved.path.read_text(encoding="utf-8"))
    assert written["provider"]["config"]["enable_prompt_caching"] is True


def test_resolve_turn_materializes_profile_provider_model(tmp_path: Path) -> None:
    _write_agent_config(
        tmp_path,
        "profiles:\n"
        "  quick:\n"
        "    provider:\n"
        "      config:\n"
        f"        default_model: {QUICK_MODEL}\n",
    )
    resolved = agent_config.resolve_turn(
        runs_dir=tmp_path / "runs",
        workspace=tmp_path,
        key="t-3",
        profile="quick",
        env={},
    )
    assert resolved.path is not None
    written = json.loads(resolved.path.read_text(encoding="utf-8"))
    assert written["provider"]["config"]["default_model"] == QUICK_MODEL


def test_profile_overlays_the_default_layer(tmp_path: Path) -> None:
    # default: sets provider.module; the profile overlays the model on top.
    _write_agent_config(
        tmp_path,
        "default:\n"
        "  provider:\n"
        "    module: anthropic\n"
        "profiles:\n"
        "  quick:\n"
        "    provider:\n"
        "      config:\n"
        f"        default_model: {QUICK_MODEL}\n",
    )
    resolved = agent_config.resolve_turn(
        runs_dir=tmp_path / "runs",
        workspace=tmp_path,
        key="t-4",
        profile="quick",
        env={},
    )
    assert resolved.path is not None
    written = json.loads(resolved.path.read_text(encoding="utf-8"))
    assert written["provider"]["module"] == "anthropic"
    assert written["provider"]["config"]["default_model"] == QUICK_MODEL


def test_local_provider_profile_reaches_a_local_endpoint(tmp_path: Path) -> None:
    # A profile can point a turn at a LOCAL, OpenAI-compatible box: provider
    # openai + a base_url folded into provider.config. The API key still comes
    # from the environment, never the file.
    _write_agent_config(
        tmp_path,
        "profiles:\n"
        "  local:\n"
        "    provider:\n"
        "      module: openai\n"
        "      config:\n"
        f"        default_model: {LOCAL_MODEL}\n"
        f"        base_url: {LOCAL_BASE_URL}\n"
        "        use_streaming: false\n"
        "        max_tokens: 512\n",
    )
    resolved = agent_config.resolve_turn(
        runs_dir=tmp_path / "runs",
        workspace=tmp_path,
        key="t-5",
        profile="local",
        env={},
    )
    assert resolved.path is not None
    written = json.loads(resolved.path.read_text(encoding="utf-8"))
    assert written == {
        "provider": {
            "module": "openai",
            "config": {
                "default_model": LOCAL_MODEL,
                "base_url": LOCAL_BASE_URL,
                "use_streaming": False,
                "max_tokens": 512,
            },
        }
    }
    assert resolved.provider_module == "openai"


def test_resolve_turn_reraises_for_unknown_profile(tmp_path: Path) -> None:
    _write_agent_config(tmp_path, "profiles:\n  quick: {}\n")
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.resolve_turn(
            runs_dir=tmp_path / "runs",
            workspace=tmp_path,
            key="t-6",
            profile="nope",
            env={},
        )
    assert "nope" in str(exc.value)
    assert "quick" in str(exc.value)


# ---- submit_turn: an unknown profile is refused SYNCHRONOUSLY, listing options ----


def _ctx(tmp_path: Path) -> EngineContext:
    (tmp_path / "automations").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "runs").mkdir()
    return EngineContext(
        automations_dir=tmp_path / "automations",
        prompts_dir=tmp_path / "prompts",
        runs_dir=tmp_path / "runs",
        cwd=tmp_path,
    )


def test_submit_turn_refuses_unknown_profile_400_listing_available(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    _write_agent_config(tmp_path, "profiles:\n  quick: {}\n  deep: {}\n")
    body = {
        "text": "hi",
        "origin": "reply",
        "session_id": "some-session",
        "profile": "carrier-pigeon",
    }
    with pytest.raises(turns.TurnError) as exc:
        turns.submit_turn(body, ctx)
    assert exc.value.status == 400
    message = exc.value.message
    assert "carrier-pigeon" in message
    assert "quick" in message and "deep" in message


# ---- end-to-end: a profiled turn runs amplifier-agent with that provider/model ----


class _FakeSpawnHandle:
    """Stand-in for the SDK's ``SyncSessionHandle`` (Strategy B fakes)."""

    def __init__(self, events: list[object], prompts: list[str]) -> None:
        self._events = events
        self._prompts = prompts

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def submit(self, prompt: str):
        self._prompts.append(prompt)
        return iter(self._events)


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the SDK spawn seam (``runner.spawn_agent_sync``); capture each
    call's kwargs -- notably ``config_path``, the ``--config`` a profiled turn
    threads through the SDK -- and succeed with a fixed reply.
    """
    captured: dict = {}
    prompts: list[str] = []

    def fake_spawn(**kwargs):
        captured.update(kwargs)
        return _FakeSpawnHandle([ResultEvent(text="ok")], prompts)

    monkeypatch.setattr(runner, "spawn_agent_sync", fake_spawn)
    return captured


def test_quick_profile_runs_amplifier_agent_with_that_provider_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_agent_config(
        workspace,
        "profiles:\n"
        "  quick:\n"
        "    provider:\n"
        "      config:\n"
        f"        default_model: {QUICK_MODEL}\n",
    )

    host_config_path = agent_config.resolve_turn(
        runs_dir=runs, workspace=workspace, key="t-e2e", profile="quick", env={}
    ).path
    assert host_config_path is not None

    captured = _capture_spawn(monkeypatch)
    runner.resume_turn(
        "sess-quick",
        "what's on my calendar?",
        cwd=workspace,
        runs_dir=runs,
        host_config_path=host_config_path,
    )

    config_path = captured.get("config_path")
    assert config_path is not None, (
        "a profiled turn must hand the SDK a host config (config_path)"
    )
    written = json.loads(Path(config_path).read_text(encoding="utf-8"))
    assert written["provider"]["config"]["default_model"] == QUICK_MODEL


def test_turn_without_profile_or_config_uses_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    # No agent-config.yaml at all: a profile-less turn resolves to nothing and
    # runs unchanged, on the bundle default model.
    host_config_path = agent_config.resolve_turn(
        runs_dir=runs, workspace=workspace, key="t-none", profile=None, env={}
    ).path
    assert host_config_path is None

    captured = _capture_spawn(monkeypatch)
    runner.resume_turn(
        "sess-default",
        "hello",
        cwd=workspace,
        runs_dir=runs,
        host_config_path=host_config_path,
    )

    assert captured.get("config_path") is None, (
        "a turn with no profile and no config must run unchanged, on the default model"
    )
