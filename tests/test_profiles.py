"""Open-vocabulary profiles replace the retired per-modality model policy.

An interactive/API turn may carry ``profile: <name>``; the named profile is
looked up in the workspace ``agent-config.yaml`` ``profiles:`` block and folded
in as a layer of the shared agent-config merge (``default:`` -> profile ->
automation ``agent_config:``). The
vocabulary of NAMES is OPEN -- the owner picks them -- but each profile's config
is held to the SAME closed top-level vocabulary and recursive credential/null
refusal as every other layer.

Every test here is red-provable: delete the mechanism and the test fails.

The load-bearing end-to-end pin is
``test_quick_profile_runs_the_worker_with_that_provider_model``: a real
``resume_turn`` over a real ``agent-config.yaml`` profile, asserting the turn
hands the worker the profile's model BOTH ways it can reach the agent library:
in the task spec (which becomes ``AgentOptions.model``) and in the host config
the worker is pointed at via ``$AMPLIFIER_AGENT_CONFIG``. Its companions prove a profile-less
turn uses the ``default:`` layer, an unknown profile fails loud LISTING the
available profiles, and credentials in a profile are refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        "default:\n"
        "  provider:\n"
        "    module: anthropic\n"
        "    config:\n"
        "      model_class: standard\n",
    )
    resolved = agent_config.resolve_turn(
        runs_dir=tmp_path / "runs", workspace=tmp_path, key="t-2", profile=None, env={}
    )
    assert resolved.path is not None
    written = json.loads(resolved.path.read_text(encoding="utf-8"))
    assert written["provider"]["config"]["default_model"] == (
        agent_config.MODEL_CLASS_TABLE["anthropic"]["standard"]
    )


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


def test_local_provider_profile_selects_the_local_provider(tmp_path: Path) -> None:
    # A profile can point a turn at a LOCAL box by naming the provider id and
    # the model it serves. The ENDPOINT and the credential are both environment
    # concerns -- an endpoint written here would be validated, materialized, and
    # read by nothing, which is why the vocabulary refuses it.
    _write_agent_config(
        tmp_path,
        "profiles:\n"
        "  local:\n"
        "    provider:\n"
        "      module: ollama\n"
        "      config:\n"
        f"        default_model: {LOCAL_MODEL}\n",
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
        "provider": {"module": "ollama", "config": {"default_model": LOCAL_MODEL}}
    }
    assert resolved.provider_module == "ollama"

    # And it reaches the library as a provider id + a model, nothing else.
    assert resolved.host_config_path is not None
    host = json.loads(resolved.host_config_path.read_text(encoding="utf-8"))
    assert host == {"provider": "ollama", "model": LOCAL_MODEL}


def test_an_endpoint_in_a_profile_is_refused_not_shipped_inert(tmp_path: Path) -> None:
    _write_agent_config(
        tmp_path,
        "profiles:\n"
        "  local:\n"
        "    provider:\n"
        "      module: ollama\n"
        "      config:\n"
        f"        base_url: {LOCAL_BASE_URL}\n",
    )
    with pytest.raises(agent_config.AgentConfigError) as exc:
        agent_config.resolve_turn(
            runs_dir=tmp_path / "runs",
            workspace=tmp_path,
            key="t-5b",
            profile="local",
            env={},
        )
    assert "base_url" in str(exc.value)


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


class _FakeWorkerProc:
    """Stand-in for the worker ``subprocess.Popen`` handle. Captures the
    task-spec JSON written to stdin (which carries the resolved provider/model)
    and replays one successful terminal envelope."""

    def __init__(self, captured: dict) -> None:
        self.pid = 424243
        sink: list[str] = []
        captured["stdin_sink"] = sink
        self.stdin = _Sink(sink)
        term = {"drumbeat_result": {"ok": True, "reply": "ok", "error": None, "code": None,
                                    "tokens_in": 1, "tokens_out": 1, "cost_usd": None,
                                    "cache_read_tokens": None, "cache_write_tokens": None}}
        self.stdout = iter([json.dumps(term) + "\n"])
        self.stderr = iter([])

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _Sink:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, data: str) -> None:
        self._sink.append(data)

    def close(self) -> None:
        pass


def _capture_spawn(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the worker spawn seam (``runner.subprocess.Popen``); capture both
    the task spec written to the worker's stdin AND the environment the worker
    is spawned with -- the two channels a resolved config reaches the agent
    library through -- and succeed with a fixed reply.
    """
    captured: dict = {}

    def fake_popen(args, **kwargs):  # noqa: ANN001, ANN002
        captured["env"] = dict(kwargs.get("env") or {})
        return _FakeWorkerProc(captured)

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    return captured


def _spec(captured: dict) -> dict:
    return json.loads("".join(captured["stdin_sink"]))


def _spec_agent_config_path(captured: dict) -> str | None:
    return captured["env"].get(agent_config.ENV_CONFIG_VAR)


def test_quick_profile_runs_the_worker_with_that_provider_model(
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

    resolved = agent_config.resolve_turn(
        runs_dir=runs, workspace=workspace, key="t-e2e", profile="quick", env={}
    )
    assert resolved.path is not None

    captured = _capture_spawn(monkeypatch)
    runner.resume_turn(
        "sess-quick",
        "what's on my calendar?",
        cwd=workspace,
        runs_dir=runs,
        resolved_config=resolved,
    )

    # Channel 1: the task spec, which becomes AgentOptions.model.
    assert _spec(captured)["model"] == QUICK_MODEL

    # Channel 2: the host config file the worker is pointed at. Both must
    # agree -- they come from the one ResolvedAgentConfig, so they cannot
    # disagree about which policy the turn ran on.
    config_path = _spec_agent_config_path(captured)
    assert config_path is not None, (
        "a profiled turn must point the worker at a host config"
    )
    written = json.loads(Path(config_path).read_text(encoding="utf-8"))
    assert written["model"] == QUICK_MODEL


def test_turn_without_profile_or_config_uses_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    # No agent-config.yaml at all: a profile-less turn resolves to nothing and
    # runs unchanged, on the agent library's own default model.
    resolved = agent_config.resolve_turn(
        runs_dir=runs, workspace=workspace, key="t-none", profile=None, env={}
    )
    assert resolved.path is None
    assert resolved.host_config_path is None

    captured = _capture_spawn(monkeypatch)
    runner.resume_turn(
        "sess-default",
        "hello",
        cwd=workspace,
        runs_dir=runs,
        resolved_config=resolved,
    )

    assert _spec(captured)["model"] is None
    assert _spec(captured)["provider"] is None
    assert _spec_agent_config_path(captured) is None, (
        "a turn with no profile and no config must run unchanged, on the "
        "library's own default -- and must not inherit a stale config file"
    )
