"""Layered per-automation agent config -> ONE materialized policy per turn.

Every turn runs under a single resolved agent config -- the authoritative source
for provider selection, model, reasoning effort, MCP servers, and skills. This
module resolves the ONE config each automation turn is handed, by merging up to
three layers, lowest precedence first:

  1. the workspace ``agent-config.yaml`` ``default:`` block -- the owner's
     baseline for every automation in this workspace.
  2. a named ``profile`` (interactive/API turns) -- supplied by the caller;
     automation runs pass ``None``. The profile SOURCE (named profiles in
     ``agent-config.yaml``) is a separate lane; this module only provides the
     merge slot so that lane can drop a resolved profile in without touching
     the merge engine.
  3. the automation's own ``agent_config:`` frontmatter block -- the most
     specific policy an author can express, and the HIGHEST precedence layer.

The operator's own ``$AMPLIFIER_AGENT_CONFIG`` file is deliberately NOT one of
these layers. It belongs to the agent library and speaks the library's five-key
host-config vocabulary, not this one; it is folded in as the BASE of the HOST
config below (``operator_host_config`` -> ``build_host_config``), where the two
speak the same language. drumbeat's own per-automation policy still wins.

Merge rules, deliberately boring so an author can predict the result:

  * two dicts at the same key recurse;
  * a scalar or a list REPLACES wholesale (no list concatenation, no scalar
    coercion);
  * a ``null`` value ANYWHERE is REFUSED, not honored -- v1 has no deletion
    semantics, and a key that reads as "unset this" while silently doing
    nothing is exactly the fail-quietly shape this project refuses.

Validation is fail-loud and applies to every file/profile layer BEFORE it is
merged:

  * the top-level vocabulary is CLOSED to ``provider | mcp | skills``.
    ``approval``, ``allowProtocolSkew``, ``debug`` and ``providers`` are refused
    BY NAME, each with its own reason -- a key with nothing behind it is the
    "enabled, validated, inert" shape this module exists to prevent.
  * ``provider`` is CLOSED to ``module | config``, and ``provider.config`` to
    ``default_model | model_class | reasoning_effort`` -- those three are
    exactly what reaches the agent library, so anything else would be
    validated, written, and never read.
  * ``provider.config.model_class`` (``fast|standard``) and
    ``provider.config.reasoning_effort``
    (``minimal|low|medium|high|xhigh``) are CLOSED value vocabularies; an
    unknown value is refused naming the set. ``model_class`` is drumbeat's own
    shorthand, resolved AT MATERIALIZATION into a concrete ``default_model``
    per ``provider.module`` (see ``_apply_model_policy``).
  * the RESOLVED model is checked against a deny-list. Both the tier table and
    the deny-list come from ``load_model_policy`` -- the engine defaults, with
    the workspace ``agent-config.yaml`` ``models:`` block folded over them.
    Because the check runs on every layer at validation time, an automation
    naming a denied model is rejected at LOAD (config lint -> ``doctor``) and
    never runs at all, rather than failing when its first turn starts.
  * credential-bearing keys (``api_key`` / ``apiKey`` / ``token`` / ``secret``
    / ``authorization``, case-insensitive) are refused at ANY depth, naming the
    full dotted path. ``provider.config.api_key`` is the canonical attack: a
    committed config that leaks a secret AND is silently ignored by the agent
    library, which takes credentials from the environment only. The refusal is
    RECURSIVE, not top-level, precisely because the dangerous placement is
    nested.

TWO artifacts come out of one resolution, and the split is deliberate
(contracts/agent-binding.v1.md section 4):

  * the MERGED config -- drumbeat's own vocabulary, written to
    ``<runs_dir>/automation_agent_configs/<slug>.json``. Its sha is the run
    record's fingerprint of the policy a run used, and its ``provider.module``
    drives provider-change rotation.
  * the HOST config -- the agent library's own five-key vocabulary, written to
    ``<runs_dir>/agent_host_configs/<slug>.json`` and handed to the turn's
    worker as ``$AMPLIFIER_AGENT_CONFIG``, with the operator's own file as its
    base. This is the ONLY channel through which reasoning effort can reach a
    provider request: the library accepts ``extra_request_params`` from a file
    and from nowhere else.

``skills`` and ``mcp`` appear in NEITHER file: the library takes both in code
(``AgentOptions.skills`` / ``.mcp_servers``) and its host-config vocabulary has
no key for either, so a file entry would be refused by name.

The empty case is by construction: when every layer is empty, the merged config
is ``{}``, ``resolve()`` materializes NOTHING and returns a ``path`` of ``None``
-- so the turn is handed no config and runs on the library's own defaults.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from drumbeat import fsutil

# The operator debug/host config, folded in as the merge BASE (layer 1).
ENV_CONFIG_VAR = "AMPLIFIER_AGENT_CONFIG"

# The workspace-root policy file whose ``default:`` block is layer 2. Named
# profiles (layer 3) also live here under ``profiles:`` -- reserved here, owned
# by a separate lane; this module reads only ``default:``.
WORKSPACE_CONFIG_FILENAME = "agent-config.yaml"
_WORKSPACE_ALLOWED_KEYS = frozenset({"default", "profiles", "models"})

# Where a scheduled automation's MERGED config (drumbeat's own vocabulary) is
# materialized under ``runs_dir``, keyed by slug. Its sha is the run record's
# fingerprint of the policy that run used.
MATERIALIZED_DIRNAME = "automation_agent_configs"

# Where an interactive/API turn's merged host config is materialized under
# ``runs_dir``, keyed by turn id. DELIBERATELY distinct from
# ``MATERIALIZED_DIRNAME`` (scheduled automation runs, keyed by slug): an
# interactive turn's config must never overwrite -- or be overwritten by -- a
# scheduled run's materialized file mid-flight, and turn ids are unique so two
# concurrent turns can never collide either.
TURN_MATERIALIZED_DIRNAME = "turn_agent_configs"

# Where the derived HOST config -- the agent library's own five-key vocabulary
# -- is materialized, for both scheduled and interactive turns. Separate from
# the two directories above because it is a different vocabulary for a
# different reader: drumbeat reads the merged config, the library reads this
# one (via ``$AMPLIFIER_AGENT_CONFIG`` on the turn's worker).
HOST_CONFIG_DIRNAME = "agent_host_configs"

# The CLOSED top-level vocabulary a config layer may declare.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"provider", "mcp", "skills"})

# Top-level keys refused BY NAME, with the reason shown in the refusal. Both
# would otherwise be caught by the closed-vocab check, but a named refusal
# tells the author WHY rather than just "unknown key".
_REFUSED_TOP_LEVEL_KEYS: dict[str, str] = {
    "approval": (
        "the engine always runs amplifier-agent non-interactively (`-y`), so an "
        "`approval` block here is a silent no-op -- remove it"
    ),
    "allowProtocolSkew": (
        "`allowProtocolSkew` is not an automation-tunable knob; it must not be "
        "flipped from a per-automation config -- remove it"
    ),
    "debug": (
        "raw provider request/response capture has no equivalent in the agent "
        "library, which exposes turn events rather than wire payloads -- there "
        "is nothing behind this key, so it is refused rather than accepted and "
        "ignored (see contracts/agent-binding.v1.md, \"Known gap\"); remove it"
    ),
    "providers": (
        "the agent library ships every provider in-process and selects exactly "
        "one by id, so a provider CATALOG selects nothing -- name the one "
        "provider under `provider.module` instead; remove it"
    ),
}

# Credential-bearing key names (compared case-insensitively) refused at any
# depth. ``provider.config.api_key`` is the canonical attack.
_CREDENTIAL_KEYS = frozenset({"api_key", "apikey", "token", "secret", "authorization"})

# --------------------------------------------------------------------------- #
# Model policy: the ``model_class`` shorthand, ``reasoning_effort``, deny-list #
# --------------------------------------------------------------------------- #

# The CLOSED value vocabulary for ``provider.config.model_class`` -- a
# provider-independent tier an author picks INSTEAD of naming a model string.
# Same discipline as ``notify:`` / ``conversation:`` / ``priority:``: an unknown
# value is refused loudly naming the vocabulary, never coerced or ignored.
VALID_MODEL_CLASSES = ("fast", "standard")

# The ENGINE table: tier -> concrete model, per ``provider.module``. This is
# the knowledge an author should not have to carry -- model ids rotate, tiers
# do not. Overridable per workspace via ``agent-config.yaml``'s ``models:``
# block (see ``load_model_policy``) so an owner can re-point a tier without an
# engine release.
MODEL_CLASS_TABLE: dict[str, dict[str, str]] = {
    "anthropic": {
        "fast": "claude-haiku-4-5-20251001",
        "standard": "claude-sonnet-4-6",
    },
    "openai": {
        "fast": "gpt-5.6-luna",
        "standard": "gpt-5.6-terra",
    },
}

# The CLOSED value vocabulary for ``provider.config.reasoning_effort``. The
# value itself is amplifier-agent's own field and is passed through UNTOUCHED;
# drumbeat only refuses a value the provider would not understand, at load,
# rather than letting it fail mid-turn.
VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# Models this engine refuses to run, checked against the RESOLVED model.
# Overridable (wholesale) via ``agent-config.yaml``'s ``models.deny:``.
DEFAULT_DENIED_MODELS = ("gpt-5.6-sol",)

# The workspace ``agent-config.yaml`` block that overrides both tables above.
# Added to the CLOSED workspace vocabulary rather than smuggled inside
# ``default:`` -- ``default:`` is a config LAYER (held to the closed top-level
# ``provider|providers|mcp|skills|debug`` vocabulary), and engine policy is not
# a layer. Same allow-list discipline, one more registered name.
WORKSPACE_MODELS_KEY = "models"
_MODELS_ALLOWED_KEYS = frozenset({"classes", "deny"})


@dataclass(frozen=True)
class ModelPolicy:
    """The resolved model tables for one workspace.

    ``classes`` maps ``provider.module`` -> ``model_class`` -> concrete model
    (the engine table, with any workspace override folded in per module).
    ``deny`` is the set of models this engine refuses to run.
    """

    classes: dict[str, dict[str, str]]
    deny: frozenset[str]


DEFAULT_MODEL_POLICY = ModelPolicy(
    classes=copy.deepcopy(MODEL_CLASS_TABLE),
    deny=frozenset(DEFAULT_DENIED_MODELS),
)


@dataclass(frozen=True)
class ModelResolution:
    """One resolved model and WHERE it came from.

    ``source`` is ``"default_model"`` (an explicit model string) or
    ``"model_class:<class>"`` (resolved from the tier table). ``shadowed`` names
    the key that lost when both were set -- so the loss is reported, never
    silent.
    """

    model: str
    source: str
    shadowed: str | None = None


# The recorded "effective provider" for a turn that names no provider of its
# own -- i.e. it runs on whatever the agent library defaults to. Stored beside
# the contract fingerprint so a later change to an EXPLICIT provider is
# detectable; a run that stays on the library default never rotates against
# this sentinel.
LIBRARY_DEFAULT_PROVIDER = "<library-default>"


class AgentConfigError(Exception):
    """A config layer could not be read, parsed, or validated.

    Always names the source (env var, file path, or ``automation.agent_config``)
    and the specific problem. The parser (``drumbeat.automation``) catches this
    when validating an automation's frontmatter block and re-raises it as an
    ``AutomationError`` so a bad block surfaces through ``load_all_tolerant`` ->
    ``doctor`` like every other authoring mistake; ``resolve()`` lets it
    propagate for the env/workspace layers, which no parser ever sees.
    """


@dataclass(frozen=True)
class ResolvedAgentConfig:
    """The single host config resolved for one automation's turns.

    ``path`` is ``None`` when the merged config is empty -- the turn is handed
    no host config and runs on the engine defaults. When non-empty, ``path``
    points at the materialized file and
    ``sha`` is the sha256 of its exact bytes (recorded in the run record).
    ``provider_module`` is always populated -- the explicit ``provider.module``
    or ``LIBRARY_DEFAULT_PROVIDER`` -- because provider-change rotation needs it
    whether or not anything was materialized. ``config`` is the merged mapping.

    ``default_model``/``model_source`` name the model this config actually runs
    on and WHERE it came from -- ``"default_model"`` (an explicit model string)
    or ``"model_class:<class>"`` (resolved from the tier table). Both ``None``
    when the config names no model at all. ``warnings`` carries any non-fatal
    policy note raised while resolving (today: an explicit ``default_model``
    shadowing a ``model_class``); the resolver also prints each one to stderr,
    so a shadowed key is never silently dropped.
    """

    path: Path | None
    sha: str | None
    provider_module: str
    config: dict[str, Any]
    default_model: str | None = None
    model_source: str | None = None
    warnings: tuple[str, ...] = ()
    # The DERIVED agent-library host config (``build_host_config``), written
    # beside the merged one and handed to the turn's worker as
    # ``$AMPLIFIER_AGENT_CONFIG``. ``None`` when the projection is empty --
    # i.e. this config names no provider, no model and no reasoning effort, so
    # there is nothing for the library to read and the turn runs on its
    # defaults. Separate from ``path`` because the two files speak different
    # vocabularies to different readers (see the module docstring).
    host_config_path: Path | None = None


def _scan_forbidden(obj: Any, *, source: str, path: str) -> None:
    """Recursively refuse credential keys and null values, naming the path."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in _CREDENTIAL_KEYS:
                raise AgentConfigError(
                    f"{source}: credential-bearing key {child!r} is refused -- "
                    "credentials must come from the engine environment, never a "
                    "config file (a committed value would leak, and amplifier-agent "
                    "re-asserts credentials from the environment and ignores it "
                    "anyway)"
                )
            if value is None:
                raise AgentConfigError(
                    f"{source}: null value at {child!r} is refused -- v1 has no "
                    "deletion semantics; omit the key or give it a concrete value"
                )
            _scan_forbidden(value, source=source, path=child)
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            child = f"{path}[{i}]"
            if item is None:
                raise AgentConfigError(
                    f"{source}: null value at {child!r} is refused -- v1 has no "
                    "deletion semantics; omit the entry or give it a concrete value"
                )
            _scan_forbidden(item, source=source, path=child)


def _provider_config(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """``provider.config`` when it is a mapping, else ``None``."""
    provider = config.get("provider")
    if not isinstance(provider, dict):
        return None
    provider_config = provider.get("config")
    if not isinstance(provider_config, dict):
        return None
    return provider_config


_MODEL_CLASS_PATH = "provider.config.model_class"

# The CLOSED vocabularies INSIDE ``provider``. Closed for the same reason the
# top level is: ``build_host_config`` projects exactly three things onto the
# library's host config (provider id, model, reasoning effort), and the library
# takes nothing else from a file. A key here that is not one of these validates,
# lands in the materialized merged config, and reaches nothing -- the "enabled,
# validated, inert" shape this module exists to prevent. Refused by name, with
# the vocabulary shown, rather than shipped dead.
_PROVIDER_ALLOWED_KEYS = frozenset({"module", "config"})
_PROVIDER_CONFIG_ALLOWED_KEYS = frozenset(
    {"default_model", "model_class", "reasoning_effort"}
)


def _validate_provider_block(config: Mapping[str, Any], *, source: str) -> None:
    """Hold ``provider`` and ``provider.config`` to their closed vocabularies."""
    provider = config.get("provider")
    if provider is None:
        return
    if not isinstance(provider, dict):
        raise AgentConfigError(
            f"{source}: `provider` must be a mapping of "
            f"{sorted(_PROVIDER_ALLOWED_KEYS)} -> value, got "
            f"{type(provider).__name__}"
        )
    unknown = sorted(set(provider) - _PROVIDER_ALLOWED_KEYS)
    if unknown:
        raise AgentConfigError(
            f"{source}: unknown key(s) {unknown} under `provider` -- the "
            f"vocabulary is closed to {sorted(_PROVIDER_ALLOWED_KEYS)}"
        )
    provider_config = provider.get("config")
    if provider_config is None:
        return
    if not isinstance(provider_config, dict):
        raise AgentConfigError(
            f"{source}: `provider.config` must be a mapping, got "
            f"{type(provider_config).__name__}"
        )
    unknown = sorted(set(provider_config) - _PROVIDER_CONFIG_ALLOWED_KEYS)
    if unknown:
        raise AgentConfigError(
            f"{source}: unknown key(s) {unknown} under `provider.config` -- the "
            f"vocabulary is closed to {sorted(_PROVIDER_CONFIG_ALLOWED_KEYS)}. "
            "Anything else here would be validated, written, and never read: "
            "the agent library takes a provider id, a model, and request "
            "parameters from a config file, and nothing else. An endpoint or a "
            "credential is an ENVIRONMENT concern"
        )


def _refuse_misplaced_model_class(obj: Any, *, source: str, path: str) -> None:
    """Refuse a ``model_class`` written anywhere but ``provider.config``.

    ``model_class`` is DRUMBEAT's key, resolved only at ``provider.config``.
    Written anywhere else -- most plausibly under the ``providers:`` plural
    catalog -- it would validate, forward verbatim into the host config, and do
    NOTHING, since amplifier-agent has never heard of it. That is the
    "enabled, validated, inert" shape this module exists to prevent, so it is a
    loud refusal naming the path rather than a silent pass-through.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if key == "model_class" and child != _MODEL_CLASS_PATH:
                raise AgentConfigError(
                    f"{source}: {child!r} is not a recognized field -- "
                    f"'model_class' is drumbeat's own shorthand and is only "
                    f"resolved at {_MODEL_CLASS_PATH!r}. Anywhere else it would "
                    "be forwarded to the engine, which does not know it, and "
                    "silently do nothing."
                )
            _refuse_misplaced_model_class(value, source=source, path=child)
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _refuse_misplaced_model_class(item, source=source, path=f"{path}[{i}]")


def _validate_reasoning_effort(config: Mapping[str, Any], *, source: str) -> None:
    """Refuse a ``reasoning_effort`` outside the closed set, naming the set.

    The value is otherwise UNTOUCHED -- it is amplifier-agent's own field and is
    forwarded verbatim. Validating here, at load, is what keeps a typo from
    surfacing as a provider error mid-turn (or, worse, being silently dropped).
    """
    provider_config = _provider_config(config)
    if provider_config is None or "reasoning_effort" not in provider_config:
        return
    value = provider_config["reasoning_effort"]
    if value not in VALID_REASONING_EFFORTS:
        raise AgentConfigError(
            f"{source}: provider.config.reasoning_effort must be one of "
            f"{list(VALID_REASONING_EFFORTS)}, got {value!r}"
        )


def resolve_model(
    config: Mapping[str, Any],
    *,
    policy: ModelPolicy | None = None,
    provider_module: str | None = None,
    source: str,
    require_module: bool = False,
) -> ModelResolution | None:
    """The model a config selects, and where it came from -- or ``None``.

    Reads ``provider.config.default_model`` (an explicit model string) and
    ``provider.config.model_class`` (a tier resolved through the policy table
    for ``provider.module``). An explicit ``default_model`` WINS; the shadowed
    ``model_class`` is reported on the result so the caller can warn rather than
    drop it silently.

    ``require_module`` distinguishes the two call sites. At MATERIALIZATION the
    merged config is final, so a ``model_class`` with no ``provider.module`` to
    resolve it against is unresolvable and fails loud. While validating ONE
    LAYER it is not: the module legitimately arrives from a different layer, so
    an unresolvable ``model_class`` is simply left for the merged check.
    """
    policy = DEFAULT_MODEL_POLICY if policy is None else policy
    provider_config = _provider_config(config)
    if provider_config is None:
        return None

    explicit = provider_config.get("default_model")
    if explicit is not None and (not isinstance(explicit, str) or not explicit.strip()):
        raise AgentConfigError(
            f"{source}: provider.config.default_model must be a non-empty "
            f"string, got {explicit!r}"
        )

    model_class = provider_config.get("model_class")
    if model_class is not None and model_class not in VALID_MODEL_CLASSES:
        raise AgentConfigError(
            f"{source}: provider.config.model_class must be one of "
            f"{list(VALID_MODEL_CLASSES)}, got {model_class!r}"
        )

    if explicit is not None:
        return ModelResolution(
            model=explicit.strip(),
            source="default_model",
            shadowed=("provider.config.model_class" if model_class else None),
        )

    if model_class is None:
        return None

    module = provider_module or effective_provider_module(config)
    if module == LIBRARY_DEFAULT_PROVIDER:
        if not require_module:
            return None
        raise AgentConfigError(
            f"{source}: provider.config.model_class {model_class!r} cannot be "
            "resolved without provider.module -- a model class is a tier "
            "WITHIN a provider, so name the provider module (one of "
            f"{sorted(policy.classes)}) or set provider.config.default_model "
            "explicitly"
        )
    tiers = policy.classes.get(module)
    if tiers is None:
        raise AgentConfigError(
            f"{source}: provider.config.model_class {model_class!r} has no "
            f"model table for provider module {module!r} -- known modules: "
            f"{sorted(policy.classes)}. Add a "
            f"'{WORKSPACE_MODELS_KEY}.classes.{module}' table to the workspace "
            f"{WORKSPACE_CONFIG_FILENAME}, or set provider.config.default_model "
            "explicitly"
        )
    model = tiers.get(model_class)
    if model is None:
        raise AgentConfigError(
            f"{source}: provider module {module!r} has no {model_class!r} model "
            f"-- it defines {sorted(tiers)}"
        )
    return ModelResolution(model=model, source=f"model_class:{model_class}")


def _refuse_denied_model(
    resolution: ModelResolution, *, source: str, policy: ModelPolicy
) -> None:
    """Refuse a DENIED model, naming it, its source, and where the list lives."""
    if resolution.model not in policy.deny:
        return
    raise AgentConfigError(
        f"{source}: model {resolution.model!r} (from {resolution.source}) is on "
        f"this workspace's model deny-list {sorted(policy.deny)} -- pick another "
        f"model, or edit '{WORKSPACE_MODELS_KEY}.deny' in the workspace "
        f"{WORKSPACE_CONFIG_FILENAME}"
    )


def validate_config_layer(
    data: Any, *, source: str, policy: ModelPolicy | None = None
) -> dict[str, Any]:
    """Fully validate one config layer, or raise ``AgentConfigError``.

    Checks: the layer is a mapping; its top-level keys are within the closed
    vocabulary (with ``approval``/``allowProtocolSkew`` refused by name); no
    credential key or null value appears at ANY depth; ``reasoning_effort`` and
    ``model_class`` are within their closed value vocabularies; and the model
    this layer resolves to -- when it resolves to one at all -- is not on the
    deny-list. Returns the same mapping on success so a caller can
    ``validate_config_layer(...)`` inline.
    """
    if not isinstance(data, dict):
        raise AgentConfigError(
            f"{source}: must be a mapping of "
            f"{sorted(ALLOWED_TOP_LEVEL_KEYS)} -> value, got "
            f"{type(data).__name__}"
        )
    for key in data:
        if not isinstance(key, str):
            raise AgentConfigError(f"{source}: top-level key {key!r} must be a string")
        if key in _REFUSED_TOP_LEVEL_KEYS:
            raise AgentConfigError(
                f"{source}: top-level key {key!r} is refused -- "
                f"{_REFUSED_TOP_LEVEL_KEYS[key]}"
            )
        if key not in ALLOWED_TOP_LEVEL_KEYS:
            raise AgentConfigError(
                f"{source}: unknown top-level key {key!r} -- the vocabulary is "
                f"closed to {sorted(ALLOWED_TOP_LEVEL_KEYS)}"
            )
    _scan_forbidden(data, source=source, path="")
    policy = DEFAULT_MODEL_POLICY if policy is None else policy
    _validate_provider_block(data, source=source)
    _refuse_misplaced_model_class(data, source=source, path="")
    _validate_reasoning_effort(data, source=source)
    resolution = resolve_model(data, policy=policy, source=source)
    if resolution is not None:
        _refuse_denied_model(resolution, source=source, policy=policy)
    return data


def merge_config(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base``: dicts recurse, scalars/lists replace.

    Neither input is mutated. Replaced values are deep-copied so the result
    shares no mutable state with a caller's inputs (an automation's frontmatter
    dict is shared across runs and must never be written through).
    """
    result: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = merge_config(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def effective_provider_module(config: Mapping[str, Any]) -> str:
    """The provider module a merged config selects, or ``LIBRARY_DEFAULT_PROVIDER``.

    Reads ``provider.module``, which carries a provider ID (``openai``,
    ``anthropic``, ``azure-openai``, ...) and is handed to the library as
    ``AgentOptions.provider`` unchanged. A turn that names no provider records
    the library-default sentinel.
    """
    provider = config.get("provider")
    if isinstance(provider, dict):
        module = provider.get("module")
        if isinstance(module, str) and module.strip():
            return module.strip()
    return LIBRARY_DEFAULT_PROVIDER


# The library's CLOSED host-config vocabulary (docs/configuration.md: "Five,
# and no more"). drumbeat writes at most three of them: ``storage`` is passed in
# code so a relative path can never be re-anchored by the child's working
# directory, and ``workspace`` is never written at all (each pinned session gets
# its own storage root instead -- contracts/agent-binding.v1.md section 6).
HOST_CONFIG_KEYS = ("provider", "model", "storage", "workspace", "extra_request_params")


def build_host_config(
    merged: Mapping[str, Any],
    *,
    provider_module: str,
    model: str | None,
    source: str,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a merged config onto the agent library's host-config vocabulary.

    Three keys, at most: ``provider``, ``model``, and -- only when the config
    declares a ``reasoning_effort`` -- ``extra_request_params``. ``base`` is the
    operator's own file (``operator_host_config``); drumbeat's per-automation
    policy overrides it key by key, and ``extra_request_params`` MERGES per
    provider rather than replacing, so an operator's unrelated request setting
    survives an automation setting an effort.

    Reasoning effort is the whole reason this file exists. The library has no
    ``AgentOptions`` field for it and no environment form; a config FILE is the
    only channel that reaches the provider request (measured on the wire:
    evidence/aa-v1-eval/FINDINGS-raw.md T1b). And ``extra_request_params`` is
    keyed BY PROVIDER, so an effort declared with no ``provider.module`` to
    scope it under would validate, be written, and do nothing -- refused loudly
    here rather than shipped inert.
    """
    out: dict[str, Any] = copy.deepcopy(dict(base or {}))
    if provider_module != LIBRARY_DEFAULT_PROVIDER:
        out["provider"] = provider_module
    if model:
        out["model"] = model

    provider_config = _provider_config(merged) or {}
    effort = provider_config.get("reasoning_effort")
    if effort is None:
        return out
    if provider_module == LIBRARY_DEFAULT_PROVIDER:
        raise AgentConfigError(
            f"{source}: provider.config.reasoning_effort {effort!r} cannot be "
            "applied without provider.module -- the agent library keys request "
            "parameters BY PROVIDER, so an unscoped effort would be written and "
            "silently ignored; name the provider (e.g. provider.module: openai)"
        )
    out["extra_request_params"] = merge_config(
        out.get("extra_request_params") or {},
        {provider_module: {"reasoning": {"effort": effort}}},
    )
    return out


def skills_dirs(merged: Mapping[str, Any], *, workspace: Path) -> tuple[str, ...]:
    """The ``skills:`` block projected onto ``AgentOptions.skills``.

    A list of directories, each holding skill subdirectories. Relative entries
    resolve against the workspace, so an automation can name ``skills`` and mean
    the one beside its own automations. Passed in CODE, never through the host
    config file -- the library's file vocabulary has no key for skills.
    """
    raw = merged.get("skills")
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise AgentConfigError(
            "agent config: `skills` must be a list of directory paths, got "
            f"{type(raw).__name__}"
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise AgentConfigError(
                f"agent config: skills entry {entry!r} must be a non-empty "
                "directory path"
            )
        path = Path(entry).expanduser()
        out.append(str(path if path.is_absolute() else (workspace / path)))
    return tuple(out)


_MCP_ALLOWED_KEYS = frozenset(
    {"transport", "command", "args", "env", "url", "headers"}
)


def mcp_servers(merged: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The ``mcp:`` block projected onto ``AgentOptions.mcp_servers`` entries.

    A mapping of server name -> ``{transport, command?, args?, env?, url?,
    headers?}``. The inner vocabulary is CLOSED, same discipline as every other
    block here: an unknown key is refused naming it, because the library's
    ``McpServer`` would simply not carry it.
    """
    raw = merged.get("mcp")
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise AgentConfigError(
            f"agent config: `mcp` must be a mapping of server name -> settings, "
            f"got {type(raw).__name__}"
        )
    out: list[dict[str, Any]] = []
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            raise AgentConfigError(
                f"agent config: mcp.{name} must be a mapping, got "
                f"{type(entry).__name__}"
            )
        unknown = sorted(set(entry) - _MCP_ALLOWED_KEYS)
        if unknown:
            raise AgentConfigError(
                f"agent config: mcp.{name} has unknown key(s) {unknown} -- the "
                f"vocabulary is closed to {sorted(_MCP_ALLOWED_KEYS)}"
            )
        server: dict[str, Any] = {"name": str(name)}
        server.update(entry)
        server.setdefault("transport", "stdio")
        out.append(server)
    return tuple(out)


def _materialize(
    runs_dir: Path, dirname: str, key: str, merged: Mapping[str, Any]
) -> tuple[Path, str]:
    """Write ``merged`` as an amplifier-agent host config, return (path, sha256).

    Rewritten atomically each call so the file can never drift from the sources
    on disk. ``dirname`` selects the materialization directory under
    ``runs_dir`` (per-slug automation configs vs per-turn interactive configs);
    ``key`` names the file within it.
    """
    content = json.dumps(dict(merged), indent=2) + "\n"
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    target_dir = Path(runs_dir).expanduser() / dirname
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{key}.json"
    fsutil.atomic_write(path, content)
    return path, sha


def _parse_mapping(text: str, *, source: str) -> dict[str, Any]:
    """Parse YAML/JSON text into a mapping (empty text -> ``{}``)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{source}: invalid YAML/JSON: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AgentConfigError(
            f"{source}: must be a mapping, got {type(data).__name__}"
        )
    return data


def operator_host_config(env: Mapping[str, str]) -> dict[str, Any]:
    """The operator's own ``$AMPLIFIER_AGENT_CONFIG`` file, or ``{}`` if unset.

    This file belongs to the AGENT LIBRARY, not to drumbeat: it speaks the
    library's five-key host-config vocabulary, not the ``agent_config:``
    authoring vocabulary, so it is NOT one of the merge layers. It is folded in
    as the BASE of the host config drumbeat materializes
    (``build_host_config``), where the two speak the same language.

    Folding it in at all is load-bearing. drumbeat sets that same variable on
    every turn's worker, pointing at the file it wrote; without this, an
    operator who set it to change a provider request would be SILENTLY
    overridden -- the exact fail-quietly shape this module exists to prevent.
    drumbeat's own resolved provider/model/effort still WIN, because they are
    per-automation policy and this is a process-wide default.

    Fail-loud on a variable pointing at nothing, an unreadable or non-object
    file, a key outside the library's closed vocabulary, a credential-bearing
    key at any depth, or a null.
    """
    raw = env.get(ENV_CONFIG_VAR)
    if not raw or not raw.strip():
        return {}
    path = Path(raw).expanduser()
    if not path.is_file():
        raise AgentConfigError(
            f"{ENV_CONFIG_VAR}={raw!r}: file not found -- unset the variable or "
            "point it at a real agent host-config file (it is folded in as the "
            "base of the host config every turn is handed)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(
            f"{ENV_CONFIG_VAR} ({path}): cannot read: {exc}"
        ) from exc
    source = f"{ENV_CONFIG_VAR} ({path})"
    data = _parse_mapping(text, source=source)
    if not data:
        return {}
    unknown = sorted(set(data) - set(HOST_CONFIG_KEYS))
    if unknown:
        raise AgentConfigError(
            f"{source}: unknown host-config key(s) {unknown} -- the agent "
            f"library's vocabulary is closed to {list(HOST_CONFIG_KEYS)}. This "
            "file is the LIBRARY's config, not drumbeat's `agent_config:` block; "
            "per-automation policy belongs in the automation file or the "
            f"workspace {WORKSPACE_CONFIG_FILENAME}"
        )
    _scan_forbidden(data, source=source, path="")
    return data


def _read_workspace_config(workspace: Path) -> tuple[Path, dict[str, Any]]:
    """Read + top-level-validate the workspace ``agent-config.yaml``.

    Returns ``(path, data)``; ``data`` is ``{}`` for a missing or empty file.
    ONE implementation of the closed workspace vocabulary check, shared by all
    three readers of this file (``default:``, ``profiles:``, ``models:``) so a
    newly registered key can never be recognized by one reader and refused by
    another.
    """
    path = Path(workspace).expanduser() / WORKSPACE_CONFIG_FILENAME
    if not path.is_file():
        return path, {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"{path}: cannot read: {exc}") from exc
    data = _parse_mapping(text, source=str(path))
    if not data:
        return path, {}
    unknown = set(data) - _WORKSPACE_ALLOWED_KEYS
    if unknown:
        raise AgentConfigError(
            f"{path}: unknown top-level key(s) {sorted(unknown)} -- only "
            f"{sorted(_WORKSPACE_ALLOWED_KEYS)} are recognized ('default:' is "
            "the base layer merged into every automation; 'profiles:' holds "
            "named profiles for interactive/API turns; 'models:' holds the "
            "model_class table and the model deny-list)"
        )
    return path, data


def load_model_policy(workspace: Path) -> ModelPolicy:
    """The workspace's model tables: the ``models:`` block over the engine's own.

    ``models.classes`` overrides the engine ``model_class`` table PER PROVIDER
    MODULE and per class -- a partial override replaces only the tiers it names,
    so pointing ``openai.fast`` somewhere new leaves ``openai.standard`` and
    every other provider alone. ``models.deny`` REPLACES the default deny-list
    wholesale (the same "a list replaces, never concatenates" rule the config
    merge itself uses), so an owner can widen OR narrow it deliberately.

    A missing file, or a file with no ``models:`` block, yields
    ``DEFAULT_MODEL_POLICY``. A malformed block is a loud ``AgentConfigError``
    naming the file and the offending path -- never a silent fallback to the
    engine defaults, which would run turns under a policy nobody chose.
    """
    path, data = _read_workspace_config(workspace)
    raw = data.get(WORKSPACE_MODELS_KEY)
    if raw is None:
        return DEFAULT_MODEL_POLICY
    source = f"{path} ({WORKSPACE_MODELS_KEY}:)"
    if not isinstance(raw, dict):
        raise AgentConfigError(
            f"{source}: must be a mapping of "
            f"{sorted(_MODELS_ALLOWED_KEYS)} -> value, got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - _MODELS_ALLOWED_KEYS)
    if unknown:
        raise AgentConfigError(
            f"{source}: unknown key(s) {unknown} -- the vocabulary is closed to "
            f"{sorted(_MODELS_ALLOWED_KEYS)} ('classes:' overrides the "
            "model_class table per provider module; 'deny:' replaces the model "
            "deny-list)"
        )

    classes = copy.deepcopy(MODEL_CLASS_TABLE)
    raw_classes = raw.get("classes")
    if raw_classes is not None:
        if not isinstance(raw_classes, dict):
            raise AgentConfigError(
                f"{source}: 'classes' must be a mapping of provider module -> "
                f"{{{' | '.join(VALID_MODEL_CLASSES)}}} -> model, got "
                f"{type(raw_classes).__name__}"
            )
        for module, tiers in raw_classes.items():
            if not isinstance(module, str) or not module.strip():
                raise AgentConfigError(
                    f"{source}: provider module {module!r} must be a non-empty string"
                )
            if not isinstance(tiers, dict):
                raise AgentConfigError(
                    f"{source}: classes.{module} must be a mapping of "
                    f"{sorted(VALID_MODEL_CLASSES)} -> model, got "
                    f"{type(tiers).__name__}"
                )
            for tier, model in tiers.items():
                if tier not in VALID_MODEL_CLASSES:
                    raise AgentConfigError(
                        f"{source}: classes.{module}.{tier} is not a model class -- "
                        f"the vocabulary is closed to {sorted(VALID_MODEL_CLASSES)}"
                    )
                if not isinstance(model, str) or not model.strip():
                    raise AgentConfigError(
                        f"{source}: classes.{module}.{tier} must be a non-empty "
                        f"model string, got {model!r}"
                    )
            classes.setdefault(module.strip(), {})
            classes[module.strip()].update(
                {tier: model.strip() for tier, model in tiers.items()}
            )

    deny = frozenset(DEFAULT_DENIED_MODELS)
    raw_deny = raw.get("deny")
    if raw_deny is not None:
        if not isinstance(raw_deny, list):
            raise AgentConfigError(
                f"{source}: 'deny' must be a list of model strings, got "
                f"{type(raw_deny).__name__}"
            )
        for entry in raw_deny:
            if not isinstance(entry, str) or not entry.strip():
                raise AgentConfigError(
                    f"{source}: deny entry {entry!r} must be a non-empty string"
                )
        deny = frozenset(entry.strip() for entry in raw_deny)

    return ModelPolicy(classes=classes, deny=deny)


def _load_workspace_default(
    workspace: Path, *, policy: ModelPolicy | None = None
) -> dict[str, Any] | None:
    """Layer 2: the workspace ``agent-config.yaml`` ``default:`` block, or ``None``."""
    path, data = _read_workspace_config(workspace)
    if not data:
        return None
    default = data.get("default")
    if default is None:
        return None
    return validate_config_layer(
        default, source=f"{path} (default:)", policy=policy
    )


def _emit_warning(message: str) -> None:
    """Print one non-fatal policy warning to stderr.

    A warning that lives only in a returned dataclass is a warning nobody sees;
    a warning that only prints is a warning nothing can assert on. Both, so the
    operator reads it in the service log AND the caller can carry it.
    """
    print(f"drumbeat: agent-config warning: {message}", file=sys.stderr)


def _apply_model_policy(
    merged: dict[str, Any],
    *,
    provider_module: str,
    policy: ModelPolicy,
    source: str,
) -> tuple[dict[str, Any], ModelResolution | None, tuple[str, ...]]:
    """Resolve ``model_class`` into a concrete ``default_model``, or refuse.

    Runs at MATERIALIZATION, against the fully merged config, so the tier is
    resolved against the provider module the turn actually runs on -- wherever
    in the layer stack that module was declared. The shorthand is then REMOVED
    from the materialized bytes: ``model_class`` is a drumbeat key, not an
    amplifier-agent host-config field, and forwarding it would hand the engine
    a key it does not know. The deny-list is checked against the RESOLVED
    model, which is the only model that ever reaches a provider.
    """
    resolution = resolve_model(
        merged,
        policy=policy,
        provider_module=provider_module,
        source=source,
        require_module=True,
    )
    if resolution is None:
        return merged, None, ()

    _refuse_denied_model(resolution, source=source, policy=policy)

    warnings: tuple[str, ...] = ()
    if resolution.shadowed is not None:
        message = (
            f"{source}: provider.config.default_model ({resolution.model!r}) "
            f"wins over {resolution.shadowed} -- the model class is ignored; "
            "remove one of the two so the intent is unambiguous"
        )
        _emit_warning(message)
        warnings = (message,)

    out = copy.deepcopy(merged)
    provider_config = out["provider"]["config"]
    provider_config.pop("model_class", None)
    provider_config["default_model"] = resolution.model
    return out, resolution, warnings


def resolve(
    *,
    runs_dir: Path,
    slug: str,
    workspace: Path,
    automation_config: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedAgentConfig:
    """Resolve and materialize one automation's per-turn host config.

    Merges the three layers (see the module docstring) and, when the result is
    non-empty, writes it atomically to
    ``<runs_dir>/automation_host_configs/<slug>.json`` (rewritten each run, so
    it can never drift from the sources on disk) and returns its path + sha.
    When the merged config is empty, returns a ``ResolvedAgentConfig`` with
    ``path``/``sha`` ``None`` -- the turn is handed no host config.

    ``automation_config`` is trusted (already validated at parse time). Every
    other file/profile layer is validated here and a failure raises
    ``AgentConfigError``.

    A ``provider.config.model_class`` surviving the merge is resolved into a
    concrete ``default_model`` here, at materialization, against the workspace
    model policy (see ``_apply_model_policy``); the resolved model is checked
    against the deny-list before anything is written.
    """
    env = os.environ if env is None else env

    policy = load_model_policy(workspace)

    operator_base = operator_host_config(env)

    layers: list[Mapping[str, Any]] = []

    workspace_layer = _load_workspace_default(workspace, policy=policy)
    if workspace_layer:
        layers.append(workspace_layer)

    if profile is not None:
        layers.append(
            validate_config_layer(profile, source="named profile", policy=policy)
        )

    if automation_config:
        # Already validated at parse time (drumbeat.automation), so it is
        # trusted here -- re-raising a parse-time error at run time would report
        # the same fault against the wrong surface.
        layers.append(automation_config)

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = merge_config(merged, layer)

    provider_module = effective_provider_module(merged)

    source = f"agent config for automation {slug!r}"

    if not merged:
        # No drumbeat-side policy at all: nothing to materialize as a MERGED
        # config, and nothing recorded on the run record. The operator's own
        # host config still reaches the turn, unchanged -- overriding it here
        # would be the silent defeat this seam exists to prevent.
        return ResolvedAgentConfig(
            path=None,
            sha=None,
            provider_module=provider_module,
            config={},
            host_config_path=(
                _materialize(runs_dir, HOST_CONFIG_DIRNAME, slug, operator_base)[0]
                if operator_base
                else None
            ),
        )

    merged, resolution, warnings = _apply_model_policy(
        merged,
        provider_module=provider_module,
        policy=policy,
        source=source,
    )

    path, sha = _materialize(runs_dir, MATERIALIZED_DIRNAME, slug, merged)
    host_config = build_host_config(
        merged,
        provider_module=provider_module,
        model=(resolution.model if resolution else None),
        source=source,
        base=operator_base,
    )
    host_config_path = (
        _materialize(runs_dir, HOST_CONFIG_DIRNAME, slug, host_config)[0]
        if host_config
        else None
    )
    return ResolvedAgentConfig(
        path=path,
        sha=sha,
        provider_module=provider_module,
        config=merged,
        default_model=(resolution.model if resolution else None),
        model_source=(resolution.source if resolution else None),
        warnings=warnings,
        host_config_path=host_config_path,
    )


def load_profiles(workspace: Path) -> dict[str, dict[str, Any]]:
    """The workspace ``agent-config.yaml`` ``profiles:`` block (layer-3 source).

    Returns a mapping of profile NAME -> its validated config layer. A missing
    file, or a present file with no ``profiles:`` block, returns ``{}`` (no
    named profiles). The vocabulary of NAMES is OPEN -- the owner picks them
    (``quick``, ``local``, ``deep``, whatever fits) -- but each profile's config
    is held to the SAME closed top-level vocabulary and recursive
    credential/null refusal as every other layer (``validate_config_layer``).
    A malformed ``profiles:`` block, or a malformed individual profile, is a
    loud ``AgentConfigError`` naming the file and the profile.
    """
    path, data = _read_workspace_config(workspace)
    if not data:
        return {}
    raw = data.get("profiles")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentConfigError(
            f"{path} (profiles:): must be a mapping of profile name -> config "
            f"layer, got {type(raw).__name__}"
        )
    policy = load_model_policy(workspace)
    profiles: dict[str, dict[str, Any]] = {}
    for name, layer in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise AgentConfigError(
                f"{path} (profiles:): profile name {name!r} must be a non-empty string"
            )
        profiles[name] = validate_config_layer(
            layer, source=f"{path} (profiles.{name}:)", policy=policy
        )
    return profiles


def select_profile(workspace: Path, name: str) -> dict[str, Any]:
    """Resolve one named profile to its validated config layer, or FAIL LOUD.

    An unknown ``name`` raises ``AgentConfigError`` that LISTS the available
    profile names -- never a silent fallback to the default, which would run a
    turn on the wrong provider/model without anyone noticing. The refusal names
    what IS available so the caller can correct the request in one step.
    """
    profiles = load_profiles(workspace)
    layer = profiles.get(name)
    if layer is None:
        available = sorted(profiles)
        if available:
            raise AgentConfigError(
                f"unknown profile {name!r} -- available profiles: {available}"
            )
        raise AgentConfigError(
            f"unknown profile {name!r} -- the workspace agent-config.yaml "
            f"defines no profiles (add a 'profiles:' block naming {name!r})"
        )
    return layer


def resolve_turn(
    *,
    runs_dir: Path,
    workspace: Path,
    key: str,
    profile: str | None,
    env: Mapping[str, str] | None = None,
) -> ResolvedAgentConfig:
    """Resolve one interactive/API turn's host config from the layered merge.

    The layers, lowest precedence first: the workspace ``agent-config.yaml``
    ``default:`` block, and -- when the turn names one -- a ``profile`` looked
    up in that same file's ``profiles:`` block. ``profile=None`` merges only
    ``default:``: a profile-less turn uses the default, exactly as the spec
    requires. An unknown profile name fails loud (``select_profile``),
    listing the available profiles.

    **Deliberately no ``automation_config`` parameter, unlike ``resolve()``.**
    Every interactive/API turn -- a fresh ``automation_slug`` turn or a
    ``session_id`` reply -- runs through this function, never ``resolve()``, so
    an automation's own ``agent_config:`` frontmatter (layer 4 of ``resolve()``)
    is silently NOT part of this merge. A ``trigger: manual`` automation (e.g. a
    chat-style conversational agent) is *only ever* invoked interactively, so its
    ``agent_config:`` block -- if it had one -- would never take effect. The
    model/provider knob for that class of automation is the named ``profile``
    the caller passes here, resolved against ``agent-config.yaml``'s
    ``profiles:`` block -- not a frontmatter block on the automation file.

    When every layer is empty (no env config, no ``default:`` block, no
    profile), the merged config is ``{}``: ``.path`` is ``None`` and the turn is
    handed no host config, running on the engine's own defaults. Otherwise the
    merged config is materialized to
    ``<runs_dir>/turn_host_configs/<key>.json`` and its path + sha returned.
    """
    env = os.environ if env is None else env

    policy = load_model_policy(workspace)

    operator_base = operator_host_config(env)

    layers: list[Mapping[str, Any]] = []

    workspace_layer = _load_workspace_default(workspace, policy=policy)
    if workspace_layer:
        layers.append(workspace_layer)

    if profile is not None:
        layers.append(select_profile(workspace, profile))

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = merge_config(merged, layer)

    provider_module = effective_provider_module(merged)

    source = f"agent config for turn {key!r}"

    if not merged:
        return ResolvedAgentConfig(
            path=None,
            sha=None,
            provider_module=provider_module,
            config={},
            host_config_path=(
                _materialize(runs_dir, HOST_CONFIG_DIRNAME, key, operator_base)[0]
                if operator_base
                else None
            ),
        )

    merged, resolution, warnings = _apply_model_policy(
        merged,
        provider_module=provider_module,
        policy=policy,
        source=source,
    )

    path, sha = _materialize(runs_dir, TURN_MATERIALIZED_DIRNAME, key, merged)
    host_config = build_host_config(
        merged,
        provider_module=provider_module,
        model=(resolution.model if resolution else None),
        source=source,
        base=operator_base,
    )
    host_config_path = (
        _materialize(runs_dir, HOST_CONFIG_DIRNAME, key, host_config)[0]
        if host_config
        else None
    )
    return ResolvedAgentConfig(
        path=path,
        sha=sha,
        provider_module=provider_module,
        config=merged,
        default_model=(resolution.model if resolution else None),
        model_source=(resolution.source if resolution else None),
        warnings=warnings,
        host_config_path=host_config_path,
    )


__all__ = [
    "ALLOWED_TOP_LEVEL_KEYS",
    "DEFAULT_DENIED_MODELS",
    "DEFAULT_MODEL_POLICY",
    "ENV_CONFIG_VAR",
    "HOST_CONFIG_DIRNAME",
    "HOST_CONFIG_KEYS",
    "LIBRARY_DEFAULT_PROVIDER",
    "MATERIALIZED_DIRNAME",
    "MODEL_CLASS_TABLE",
    "TURN_MATERIALIZED_DIRNAME",
    "VALID_MODEL_CLASSES",
    "VALID_REASONING_EFFORTS",
    "WORKSPACE_CONFIG_FILENAME",
    "WORKSPACE_MODELS_KEY",
    "AgentConfigError",
    "ModelPolicy",
    "ModelResolution",
    "ResolvedAgentConfig",
    "build_host_config",
    "effective_provider_module",
    "load_model_policy",
    "load_profiles",
    "mcp_servers",
    "merge_config",
    "operator_host_config",
    "resolve",
    "resolve_model",
    "resolve_turn",
    "select_profile",
    "skills_dirs",
    "validate_config_layer",
]
