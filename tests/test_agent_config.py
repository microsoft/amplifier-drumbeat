"""9h5: layered per-automation agent config -> ONE materialized host config.

These tests drive the REAL resolver, the REAL frontmatter parse, and the REAL
provider-change rotation primitives -- nothing here re-implements the mechanism.
Every test is red-provable: delete the rule it names and it fails.

Coverage maps to the item's acceptance criteria:

  * merge semantics (dicts recurse, scalars/lists replace, inputs unmutated);
  * null values REFUSED recursively (no v1 deletion semantics);
  * credentials REFUSED recursively, naming the full path -- with
    ``provider.config.api_key`` (the canonical attack) proven by name;
  * the CLOSED top-level vocabulary, with ``approval`` / ``allowProtocolSkew``
    refused by name;
  * materialization + sha, and the empty-merge back-compat (no file, no
    ``--config``);
  * ``enable_prompt_caching`` reaching the materialized config via a direct
    ``agent_config:`` block (the retired ``prompt_caching`` sugar's replacement);
  * the ``$AMPLIFIER_AGENT_CONFIG`` base layer folding in and being overridden;
  * the automation-frontmatter ``agent_config:`` parse surfacing bad blocks
    through ``load_all_tolerant`` -> doctor;
  * the provider-change rotation decision (flock-claimed, rotate-once).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from drumbeat import agent_config, session_health
from drumbeat.agent_config import AgentConfigError
from drumbeat.automation import (
    AutomationError,
    load_all_tolerant,
    load_from_text,
)

# --------------------------------------------------------------------------- #
# merge semantics                                                             #
# --------------------------------------------------------------------------- #


def test_merge_recurses_dicts_and_replaces_scalars() -> None:
    base = {"provider": {"config": {"default_model": "Y", "max_tokens": 100}}}
    overlay = {"provider": {"config": {"default_model": "X"}}}
    merged = agent_config.merge_config(base, overlay)
    assert merged == {"provider": {"config": {"default_model": "X", "max_tokens": 100}}}


def test_merge_replaces_lists_wholesale() -> None:
    base = {"skills": {"enabled": ["a", "b", "c"]}}
    overlay = {"skills": {"enabled": ["z"]}}
    assert agent_config.merge_config(base, overlay) == {"skills": {"enabled": ["z"]}}


def test_merge_does_not_mutate_its_inputs() -> None:
    base = {"provider": {"config": {"default_model": "Y"}}}
    overlay = {"provider": {"config": {"default_model": "X"}}}
    agent_config.merge_config(base, overlay)
    assert base == {"provider": {"config": {"default_model": "Y"}}}
    assert overlay == {"provider": {"config": {"default_model": "X"}}}


# --------------------------------------------------------------------------- #
# null refusal (recursive, no deletion semantics in v1)                        #
# --------------------------------------------------------------------------- #


def test_null_value_is_refused_naming_the_path() -> None:
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer(
            {"provider": {"config": {"default_model": None}}}, source="x"
        )
    msg = str(exc.value)
    assert "null" in msg.lower()
    assert "provider.config.default_model" in msg


def test_null_inside_a_list_is_refused() -> None:
    with pytest.raises(AgentConfigError):
        agent_config.validate_config_layer({"providers": [None]}, source="x")


# --------------------------------------------------------------------------- #
# recursive credential refusal -- the canonical attack                         #
# --------------------------------------------------------------------------- #


def test_api_key_nested_under_provider_config_fails_loud_naming_full_path() -> None:
    """Acceptance: api_key nested under provider.config -> load fails loud
    naming the full path."""
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer(
            {"provider": {"config": {"api_key": "sk-secret"}}}, source="x"
        )
    assert "provider.config.api_key" in str(exc.value)


def test_credential_key_variants_refused_at_any_depth() -> None:
    for key in ("apiKey", "token", "secret", "authorization", "API_KEY", "Token"):
        with pytest.raises(AgentConfigError):
            agent_config.validate_config_layer(
                {"mcp": {"servers": {"s": {key: "v"}}}}, source="x"
            )


def test_credential_inside_a_list_element_refused() -> None:
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer(
            {"providers": [{"config": {"token": "t"}}]}, source="x"
        )
    assert "token" in str(exc.value)


# --------------------------------------------------------------------------- #
# closed top-level vocabulary + named refusals                                 #
# --------------------------------------------------------------------------- #


def test_unknown_top_level_key_refused() -> None:
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer({"nonsense": 1}, source="x")
    assert "nonsense" in str(exc.value)


def test_approval_refused_by_name() -> None:
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer({"approval": {"mode": "auto"}}, source="x")
    assert "approval" in str(exc.value)


def test_allow_protocol_skew_refused_by_name() -> None:
    with pytest.raises(AgentConfigError) as exc:
        agent_config.validate_config_layer({"allowProtocolSkew": True}, source="x")
    assert "allowProtocolSkew" in str(exc.value)


def test_every_allowed_top_level_key_accepted() -> None:
    data = {"provider": {}, "providers": [], "mcp": {}, "skills": {}, "debug": {}}
    assert agent_config.validate_config_layer(dict(data), source="x") == data


# --------------------------------------------------------------------------- #
# effective provider module (drives rotation)                                  #
# --------------------------------------------------------------------------- #


def test_effective_provider_module_reads_provider_module() -> None:
    assert (
        agent_config.effective_provider_module({"provider": {"module": "openai"}})
        == "openai"
    )


def test_effective_provider_module_defaults_to_bundle_sentinel() -> None:
    assert (
        agent_config.effective_provider_module(
            {"provider": {"config": {"default_model": "x"}}}
        )
        == agent_config.BUNDLE_DEFAULT_PROVIDER
    )
    assert (
        agent_config.effective_provider_module({})
        == agent_config.BUNDLE_DEFAULT_PROVIDER
    )


# --------------------------------------------------------------------------- #
# resolve(): empty -> no materialization (back-compat by construction)         #
# --------------------------------------------------------------------------- #


def test_resolve_empty_materializes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        resolved = agent_config.resolve(
            runs_dir=root / "runs",
            slug="demo",
            workspace=root,
            automation_config=None,
            env={},
        )
        assert resolved.path is None
        assert resolved.sha is None
        assert resolved.config == {}
        assert resolved.provider_module == agent_config.BUNDLE_DEFAULT_PROVIDER
        assert not (root / "runs" / agent_config.MATERIALIZED_DIRNAME).exists()


# --------------------------------------------------------------------------- #
# resolve(): automation overrides workspace default, and records the sha       #
# --------------------------------------------------------------------------- #


def test_resolve_automation_overrides_workspace_default_and_records_sha() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "default:\n  provider:\n    config:\n      default_model: model-Y\n",
            encoding="utf-8",
        )
        resolved = agent_config.resolve(
            runs_dir=root / "runs",
            slug="demo",
            workspace=root,
            automation_config={"provider": {"config": {"default_model": "model-X"}}},
            env={},
        )
        assert resolved.path is not None
        materialized = json.loads(resolved.path.read_text(encoding="utf-8"))
        assert materialized["provider"]["config"]["default_model"] == "model-X"
        expected_sha = hashlib.sha256(
            (json.dumps(resolved.config, indent=2) + "\n").encode("utf-8")
        ).hexdigest()
        assert resolved.sha == expected_sha


# --------------------------------------------------------------------------- #
# $AMPLIFIER_AGENT_CONFIG base layer                                            #
# --------------------------------------------------------------------------- #


def test_env_base_layer_folds_in_and_is_overridden() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env_file = root / "operator.yaml"
        env_file.write_text(
            "debug:\n  verbose: true\n"
            "provider:\n  config:\n    default_model: base-model\n",
            encoding="utf-8",
        )
        resolved = agent_config.resolve(
            runs_dir=root / "runs",
            slug="demo",
            workspace=root,
            automation_config={"provider": {"config": {"default_model": "auto-model"}}},
            env={agent_config.ENV_CONFIG_VAR: str(env_file)},
        )
        # The operator's own key survives (this layer exists precisely so
        # --config does not silently defeat an operator debug config)...
        assert resolved.config["debug"] == {"verbose": True}
        # ...but the automation, being higher precedence, wins the model.
        assert resolved.config["provider"]["config"]["default_model"] == "auto-model"


def test_env_config_pointing_at_a_missing_file_fails_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with pytest.raises(AgentConfigError) as exc:
            agent_config.resolve(
                runs_dir=root / "runs",
                slug="demo",
                workspace=root,
                automation_config=None,
                env={agent_config.ENV_CONFIG_VAR: str(root / "nope.yaml")},
            )
        assert "not found" in str(exc.value)


def test_env_file_credential_is_refused_through_resolve() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env_file = root / "operator.yaml"
        env_file.write_text(
            "provider:\n  config:\n    api_key: leak\n", encoding="utf-8"
        )
        with pytest.raises(AgentConfigError) as exc:
            agent_config.resolve(
                runs_dir=root / "runs",
                slug="demo",
                workspace=root,
                automation_config=None,
                env={agent_config.ENV_CONFIG_VAR: str(env_file)},
            )
        assert "provider.config.api_key" in str(exc.value)


# --------------------------------------------------------------------------- #
# workspace agent-config.yaml vocabulary                                        #
# --------------------------------------------------------------------------- #


def test_workspace_unknown_top_level_key_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text("bogus: 1\n", encoding="utf-8")
        with pytest.raises(AgentConfigError) as exc:
            agent_config.resolve(
                runs_dir=root / "runs",
                slug="demo",
                workspace=root,
                automation_config=None,
                env={},
            )
        assert "bogus" in str(exc.value)


def test_workspace_profiles_key_tolerated_only_default_consumed() -> None:
    """`profiles:` is reserved for the named-profile lane; this module reads
    only `default:` and must not choke on a profiles block being present."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "default:\n  provider:\n    module: anthropic\n"
            "profiles:\n  fast:\n    provider:\n      module: openai\n",
            encoding="utf-8",
        )
        resolved = agent_config.resolve(
            runs_dir=root / "runs",
            slug="demo",
            workspace=root,
            automation_config=None,
            env={},
        )
        assert resolved.provider_module == "anthropic"


# --------------------------------------------------------------------------- #
# the profile seam (interactive/API turns lane hooks in here)                  #
# --------------------------------------------------------------------------- #


def test_profile_layer_sits_between_workspace_and_automation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "agent-config.yaml").write_text(
            "default:\n  provider:\n    config:\n      default_model: Y\n",
            encoding="utf-8",
        )
        resolved = agent_config.resolve(
            runs_dir=root / "runs",
            slug="demo",
            workspace=root,
            automation_config=None,
            profile={"provider": {"config": {"default_model": "P"}}},
            env={},
        )
        # profile overrides the workspace default...
        assert resolved.config["provider"]["config"]["default_model"] == "P"


def test_profile_layer_is_validated() -> None:
    with tempfile.TemporaryDirectory() as tmp, pytest.raises(AgentConfigError):
        agent_config.resolve(
            runs_dir=Path(tmp),
            slug="demo",
            workspace=Path(tmp),
            automation_config=None,
            profile={"provider": {"config": {"secret": "x"}}},
            env={},
        )


# --------------------------------------------------------------------------- #
# automation frontmatter agent_config: parse + fail-loud -> doctor             #
# --------------------------------------------------------------------------- #


def _auto(block: str = "", *, name: str = "Demo") -> str:
    return (
        "---\n"
        "automation:\n"
        f"  name: {name}\n"
        "  trigger:\n"
        "    type: manual\n"
        f"{block}"
        "---\n"
        "\n"
        "1. Do it.\n"
    )


def test_automation_agent_config_absent_is_none() -> None:
    automation = load_from_text(Path("d.md"), _auto())
    assert automation.agent_config is None


def test_automation_agent_config_is_parsed() -> None:
    block = "  agent_config:\n    provider:\n      config:\n        default_model: m\n"
    automation = load_from_text(Path("d.md"), _auto(block))
    assert automation.agent_config == {"provider": {"config": {"default_model": "m"}}}


def test_automation_agent_config_api_key_is_a_loud_refusal() -> None:
    block = "  agent_config:\n    provider:\n      config:\n        api_key: leak\n"
    with pytest.raises(AutomationError) as exc:
        load_from_text(Path("d.md"), _auto(block))
    assert "provider.config.api_key" in exc.value.problem


def test_automation_agent_config_unknown_key_is_a_loud_refusal() -> None:
    with pytest.raises(AutomationError):
        load_from_text(Path("d.md"), _auto("  agent_config:\n    bogus: 1\n"))


def test_automation_agent_config_non_mapping_is_a_loud_refusal() -> None:
    with pytest.raises(AutomationError):
        load_from_text(Path("d.md"), _auto("  agent_config: notamap\n"))


# --------------------------------------------------------------------------- #
# enable_prompt_caching via the direct agent_config: path (retired-sugar        #
# replacement) + the retired prompt_caching alias refused loudly                #
# --------------------------------------------------------------------------- #


def test_enable_prompt_caching_via_agent_config_threads_to_materialized_config() -> (
    None
):
    """The direct path that replaces the retired ``prompt_caching`` sugar: an
    automation setting ``provider.config.enable_prompt_caching: false`` in its
    OWN ``agent_config:`` block resolves to a materialized host config carrying
    exactly that -- proving the opt-out still reaches ``--config`` on every turn
    through the ordinary merge, with no special-case alias in the resolver."""
    block = (
        "  agent_config:\n"
        "    provider:\n"
        "      config:\n"
        "        enable_prompt_caching: false\n"
    )
    automation = load_from_text(Path("d.md"), _auto(block))
    assert automation.agent_config == {
        "provider": {"config": {"enable_prompt_caching": False}}
    }
    with tempfile.TemporaryDirectory() as tmp:
        resolved = agent_config.resolve(
            runs_dir=Path(tmp),
            slug=automation.slug,
            workspace=Path(tmp),
            automation_config=automation.agent_config,
            env={},
        )
        assert resolved.path is not None
        cfg = json.loads(resolved.path.read_text(encoding="utf-8"))
        assert cfg["provider"]["config"]["enable_prompt_caching"] is False


def test_retired_prompt_caching_key_is_refused_loudly() -> None:
    """The deprecated ``prompt_caching:`` alias is GONE: an automation that
    still carries it is REFUSED at parse time (never tolerated-and-ignored),
    and the refusal points the author at the direct ``enable_prompt_caching``
    path. A key that read as meaningful while the engine silently dropped it is
    exactly the fail-quiet shape this parser refuses."""
    with pytest.raises(AutomationError) as exc:
        load_from_text(Path("d.md"), _auto("  prompt_caching: false\n"))
    assert "prompt_caching" in exc.value.problem
    assert "enable_prompt_caching" in exc.value.problem


def test_bad_agent_config_surfaces_through_load_all_tolerant(tmp_path: Path) -> None:
    """A broken agent_config block must not take the whole fleet down: it is
    isolated as a load failure (which the scheduler names on every tick and
    `drumbeat doctor` surfaces), while good automations still load."""
    d = tmp_path / "automations"
    d.mkdir()
    (d / "good.md").write_text(_auto(name="Good One"), encoding="utf-8")
    (d / "bad.md").write_text(
        _auto(
            "  agent_config:\n    provider:\n      config:\n        api_key: leak\n",
            name="Bad One",
        ),
        encoding="utf-8",
    )
    automations, failures = load_all_tolerant(d)
    assert [a.name for a in automations] == ["Good One"]
    assert len(failures) == 1
    assert failures[0].path.name == "bad.md"
    assert "provider.config.api_key" in failures[0].problem


# --------------------------------------------------------------------------- #
# provider-change rotation primitives (session_health)                         #
# --------------------------------------------------------------------------- #


def _record(session_id: str, *, provider: str | None, runs_dir: Path) -> None:
    session_health.record_contract(
        session_id=session_id,
        automation_slug="demo",
        fingerprint="fp",
        recorded_at="2026-08-25T00:00:00Z",
        runs_dir=runs_dir,
        provider_module=provider,
    )


def test_claim_rotation_unknown_session_never_rotates(tmp_path: Path) -> None:
    changed, prev = session_health.claim_provider_rotation(
        session_id="nope", current_provider="openai", runs_dir=tmp_path
    )
    assert changed is False
    assert prev is None


def test_claim_rotation_first_observation_backfills_without_rotating(
    tmp_path: Path,
) -> None:
    _record("s1", provider=None, runs_dir=tmp_path)
    changed, prev = session_health.claim_provider_rotation(
        session_id="s1", current_provider="anthropic", runs_dir=tmp_path
    )
    assert changed is False
    assert prev is None
    assert session_health.read_provider("s1", runs_dir=tmp_path) == "anthropic"


def test_claim_rotation_change_rotates_exactly_once(tmp_path: Path) -> None:
    _record("s1", provider="anthropic", runs_dir=tmp_path)
    changed, prev = session_health.claim_provider_rotation(
        session_id="s1", current_provider="openai", runs_dir=tmp_path
    )
    assert changed is True
    assert prev == "anthropic"
    # The concurrent-trigger guard: a second caller sees the advanced provider
    # and does NOT rotate again for the same transition.
    changed2, _ = session_health.claim_provider_rotation(
        session_id="s1", current_provider="openai", runs_dir=tmp_path
    )
    assert changed2 is False


def test_claim_rotation_same_provider_is_not_a_change(tmp_path: Path) -> None:
    _record("s1", provider="anthropic", runs_dir=tmp_path)
    changed, prev = session_health.claim_provider_rotation(
        session_id="s1", current_provider="anthropic", runs_dir=tmp_path
    )
    assert changed is False
    assert prev == "anthropic"


def test_provider_drift_is_read_only(tmp_path: Path) -> None:
    _record("s1", provider="anthropic", runs_dir=tmp_path)
    changed, prev = session_health.provider_drift(
        session_id="s1", current_provider="openai", runs_dir=tmp_path
    )
    assert changed is True
    assert prev == "anthropic"
    # Read-only: the store was not advanced.
    assert session_health.read_provider("s1", runs_dir=tmp_path) == "anthropic"
