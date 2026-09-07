"""drumbeat-166: `model_class`, `reasoning_effort`, and the model deny-list.

Three knobs, one module (``drumbeat.agent_config``), all fail-loud:

  * ``provider.config.model_class: fast | standard`` -- a CLOSED vocabulary
    resolved AT MATERIALIZATION into a concrete ``default_model`` per
    ``provider.module``, from the engine table (overridable by the workspace
    ``agent-config.yaml`` ``models.classes:`` table). An explicit
    ``default_model`` WINS, and the shadowed ``model_class`` is named in a
    warning rather than silently dropped.
  * ``provider.config.reasoning_effort`` -- validated at LOAD against the
    closed set, then passed through UNTOUCHED to the provider config.
  * a model DENY-LIST (default ``gpt-5.6-sol``, overridable via
    ``models.deny:``) checked against the RESOLVED model at load, so a denied
    automation is rejected the same way every other config-lint error is and
    NEVER RUNS.

Every test here is red-provable: delete the rule it names and it fails.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from drumbeat import agent_config
from drumbeat.agent_config import AgentConfigError
from drumbeat.automation import (
    AutomationError,
    load_all_tolerant,
    load_from_text,
)

# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _auto(block: str = "", *, name: str = "Demo") -> str:
    return (
        "---\n"
        "automation:\n"
        f"  name: {name}\n"
        "  trigger:\n"
        "    type: manual\n"
        f"{block}"
        "  steps:\n"
        "    - id: do-it\n"
        "      prompt: Do it.\n"
        "---\n"
    )


def _resolve(root: Path, config: dict, **kwargs):
    return agent_config.resolve(
        runs_dir=root / "runs",
        slug="demo",
        workspace=root,
        automation_config=config,
        env={},
        **kwargs,
    )


def _materialized(resolved) -> dict:
    assert resolved.path is not None
    return json.loads(resolved.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# (1) model_class -> a concrete default_model, per provider module            #
# --------------------------------------------------------------------------- #


def test_openai_model_class_fast_materializes_luna_and_names_the_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved = _resolve(
            root,
            {"provider": {"module": "openai", "config": {"model_class": "fast"}}},
        )
        assert resolved.default_model == "gpt-5.6-luna"
        assert resolved.model_source == "model_class:fast"
        config = _materialized(resolved)["provider"]["config"]
        assert config["default_model"] == "gpt-5.6-luna"
        # The shorthand is a DRUMBEAT key, not an amplifier-agent host-config
        # field: it is resolved away, never forwarded to the engine.
        assert "model_class" not in config


def test_anthropic_model_class_fast_materializes_haiku() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved = _resolve(
            root,
            {"provider": {"module": "anthropic", "config": {"model_class": "fast"}}},
        )
        assert resolved.default_model == "claude-haiku-4-5-20251001"
        assert resolved.model_source == "model_class:fast"
        assert (
            _materialized(resolved)["provider"]["config"]["default_model"]
            == "claude-haiku-4-5-20251001"
        )


def test_model_class_standard_resolves_per_provider() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert (
            _resolve(
                root,
                {
                    "provider": {
                        "module": "openai",
                        "config": {"model_class": "standard"},
                    }
                },
            ).default_model
            == "gpt-5.6-terra"
        )
        assert (
            _resolve(
                root,
                {
                    "provider": {
                        "module": "anthropic",
                        "config": {"model_class": "standard"},
                    }
                },
            ).default_model
            == "claude-sonnet-4-6"
        )


def test_explicit_default_model_wins_and_the_warning_names_model_class() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved = _resolve(
            root,
            {
                "provider": {
                    "module": "openai",
                    "config": {"default_model": "gpt-5.6-terra", "model_class": "fast"},
                }
            },
        )
        assert resolved.default_model == "gpt-5.6-terra"
        assert resolved.model_source == "default_model"
        assert resolved.warnings, "a shadowed model_class must WARN, never be silent"
        joined = " ".join(resolved.warnings)
        assert "model_class" in joined
        assert "default_model" in joined
        assert "model_class" not in _materialized(resolved)["provider"]["config"]


def test_no_model_keys_leaves_the_config_alone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved = _resolve(root, {"provider": {"module": "openai"}})
        assert resolved.default_model is None
        assert resolved.model_source is None
        assert resolved.warnings == ()
        assert "config" not in _materialized(resolved)["provider"]


def test_model_class_needs_a_provider_module_and_fails_loud_without_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(AgentConfigError) as exc:
            _resolve(root, {"provider": {"config": {"model_class": "fast"}}})
        msg = str(exc.value)
        assert "model_class" in msg
        assert "provider.module" in msg


def test_model_class_on_an_unknown_provider_module_names_the_known_ones() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(AgentConfigError) as exc:
            _resolve(
                root,
                {"provider": {"module": "wat", "config": {"model_class": "fast"}}},
            )
        msg = str(exc.value)
        assert "wat" in msg
        assert "anthropic" in msg and "openai" in msg


def test_unknown_model_class_value_is_refused_at_load_naming_the_vocabulary() -> None:
    block = "  agent_config:\n    provider:\n      config:\n        model_class: turbo\n"
    with pytest.raises(AutomationError) as exc:
        load_from_text(Path("d.md"), _auto(block))
    problem = exc.value.problem
    assert "model_class" in problem
    assert "fast" in problem and "standard" in problem


def test_model_class_outside_provider_config_is_refused_not_forwarded() -> None:
    """A key that validates and then silently does nothing is the worst shape
    this module has -- `model_class` only resolves at provider.config."""
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer(
            {"mcp": {"openai": {"env": {"model_class": "fast"}}}},
            source="x",
        )
    msg = str(exc.value)
    assert "mcp.openai.env.model_class" in msg
    assert "provider.config.model_class" in msg


def test_workspace_classes_table_overrides_the_engine_table() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "models:\n"
            "  classes:\n"
            "    openai:\n"
            "      fast: gpt-5.6-mercury\n",
            encoding="utf-8",
        )
        resolved = _resolve(
            root,
            {"provider": {"module": "openai", "config": {"model_class": "fast"}}},
        )
        assert resolved.default_model == "gpt-5.6-mercury"
        # A PARTIAL override replaces only what it names.
        assert (
            _resolve(
                root,
                {
                    "provider": {
                        "module": "openai",
                        "config": {"model_class": "standard"},
                    }
                },
            ).default_model
            == "gpt-5.6-terra"
        )


def test_model_class_resolves_for_interactive_turns_too() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "default:\n"
            "  provider:\n"
            "    module: anthropic\n"
            "    config:\n"
            "      model_class: standard\n",
            encoding="utf-8",
        )
        resolved = agent_config.resolve_turn(
            runs_dir=root / "runs", workspace=root, key="t1", profile=None, env={}
        )
        assert resolved.default_model == "claude-sonnet-4-6"
        assert resolved.model_source == "model_class:standard"


def test_workspace_models_block_vocabulary_is_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "models:\n  bogus: 1\n", encoding="utf-8"
        )
        with pytest.raises(AgentConfigError) as exc:
            _resolve(root, {"provider": {"module": "openai"}})
        assert "bogus" in str(exc.value)


# --------------------------------------------------------------------------- #
# (2) reasoning_effort -- validated at load, passed through untouched         #
# --------------------------------------------------------------------------- #


def test_reasoning_effort_reaches_the_provider_config_untouched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved = _resolve(
            root,
            {
                "provider": {
                    "module": "openai",
                    "config": {"model_class": "fast", "reasoning_effort": "high"},
                }
            },
        )
        config = _materialized(resolved)["provider"]["config"]
        # Passed through UNTOUCHED, and it survives model_class resolution
        # rewriting the very mapping it lives in.
        assert config["reasoning_effort"] == "high"
        assert config["default_model"] == "gpt-5.6-luna"
        assert "model_class" not in config


def test_every_reasoning_effort_in_the_set_is_accepted() -> None:
    assert tuple(agent_config.VALID_REASONING_EFFORTS) == (
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    for value in agent_config.VALID_REASONING_EFFORTS:
        agent_config.validate_config_layer(
            {"provider": {"config": {"reasoning_effort": value}}}, source="x"
        )


def test_reasoning_effort_turbo_is_refused_at_load_naming_the_set() -> None:
    block = (
        "  agent_config:\n"
        "    provider:\n"
        "      config:\n"
        "        reasoning_effort: turbo\n"
    )
    with pytest.raises(AutomationError) as exc:
        load_from_text(Path("d.md"), _auto(block))
    problem = exc.value.problem
    assert "reasoning_effort" in problem
    assert "turbo" in problem
    for allowed in ("minimal", "low", "medium", "high", "xhigh"):
        assert allowed in problem


# --------------------------------------------------------------------------- #
# (3) the model deny-list -- rejected at load, never runs                     #
# --------------------------------------------------------------------------- #


def test_denied_model_is_rejected_at_load_by_default() -> None:
    block = (
        "  agent_config:\n"
        "    provider:\n"
        "      config:\n"
        "        default_model: gpt-5.6-sol\n"
    )
    with pytest.raises(AutomationError) as exc:
        load_from_text(Path("d.md"), _auto(block))
    assert "gpt-5.6-sol" in exc.value.problem
    assert "deny" in exc.value.problem.lower()


def test_denied_model_via_the_override_table_never_loads_so_it_never_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "models:\n  deny:\n    - gpt-5.6-luna\n", encoding="utf-8"
        )
        automations_dir = root / "automations"
        automations_dir.mkdir()
        block = (
            "  agent_config:\n"
            "    provider:\n"
            "      config:\n"
            "        default_model: gpt-5.6-luna\n"
        )
        (automations_dir / "denied.md").write_text(_auto(block), encoding="utf-8")
        (automations_dir / "ok.md").write_text(
            _auto(name="Fine"), encoding="utf-8"
        )
        automations, failures = load_all_tolerant(automations_dir)
        # The denied automation never becomes runnable...
        assert [a.name for a in automations] == ["Fine"]
        # ...and its rejection is reported through the ordinary config-lint path.
        assert len(failures) == 1
        assert failures[0].path.name == "denied.md"
        assert "gpt-5.6-luna" in failures[0].problem


def test_deny_override_replaces_the_default_list_wholesale() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "models:\n  deny:\n    - gpt-5.6-luna\n", encoding="utf-8"
        )
        policy = agent_config.load_model_policy(root)
        assert policy.deny == frozenset({"gpt-5.6-luna"})


def test_denied_model_reached_via_model_class_is_refused_at_materialization() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "models:\n"
            "  classes:\n"
            "    openai:\n"
            "      fast: gpt-5.6-sol\n",
            encoding="utf-8",
        )
        with pytest.raises(AgentConfigError) as exc:
            _resolve(
                root,
                {"provider": {"module": "openai", "config": {"model_class": "fast"}}},
            )
        msg = str(exc.value)
        assert "gpt-5.6-sol" in msg
        assert "model_class:fast" in msg


def test_denied_model_in_the_workspace_default_layer_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "default:\n  provider:\n    config:\n      default_model: gpt-5.6-sol\n",
            encoding="utf-8",
        )
        with pytest.raises(AgentConfigError) as exc:
            _resolve(root, None)
        assert "gpt-5.6-sol" in str(exc.value)


def test_default_deny_list_names_sol() -> None:
    assert "gpt-5.6-sol" in agent_config.DEFAULT_MODEL_POLICY.deny
