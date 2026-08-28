"""Layered per-automation agent config -> ONE materialized host config per turn.

Every turn runs under a single host config -- the authoritative source for
provider selection, model, MCP servers, skills, and debug knobs. This module
resolves the ONE host config each automation turn is handed, by merging up to
four layers, lowest precedence first:

  1. ``$AMPLIFIER_AGENT_CONFIG`` -- the operator's own debug/host config file,
     folded in as the BASE. This layer is load-bearing: the host config a turn
     is handed outranks the environment inside amplifier-agent, so without
     folding this file in the feature would SILENTLY defeat an operator who set
     that variable to debug a run. Absent/empty variable contributes nothing.
  2. the workspace ``agent-config.yaml`` ``default:`` block -- the owner's
     baseline for every automation in this workspace.
  3. a named ``profile`` (interactive/API turns) -- supplied by the caller;
     automation runs pass ``None``. The profile SOURCE (named profiles in
     ``agent-config.yaml``) is a separate lane; this module only provides the
     merge slot so that lane can drop a resolved profile in without touching
     the merge engine.
  4. the automation's own ``agent_config:`` frontmatter block -- the most
     specific policy an author can express, and the HIGHEST precedence layer.

Merge rules, deliberately boring so an author can predict the result:

  * two dicts at the same key recurse;
  * a scalar or a list REPLACES wholesale (no list concatenation, no scalar
    coercion);
  * a ``null`` value ANYWHERE is REFUSED, not honored -- v1 has no deletion
    semantics, and a key that reads as "unset this" while silently doing
    nothing is exactly the fail-quietly shape this project refuses.

Validation is fail-loud and applies to every file/profile layer BEFORE it is
merged:

  * the top-level vocabulary is CLOSED to
    ``provider | providers | mcp | skills | debug``. ``approval`` and
    ``allowProtocolSkew`` are refused by name (the engine always passes ``-y``,
    so an ``approval`` block is a silent no-op; ``allowProtocolSkew`` is not a
    knob an automation gets to flip).
  * credential-bearing keys (``api_key`` / ``apiKey`` / ``token`` / ``secret``
    / ``authorization``, case-insensitive) are refused at ANY depth, naming the
    full dotted path. ``provider.config.api_key`` is the canonical attack: a
    committed config that leaks a secret AND is silently ignored by
    amplifier-agent (which re-asserts credentials from the environment). The
    refusal is RECURSIVE, not top-level, precisely because the dangerous
    placement is nested.

The empty case is by construction: when every layer is empty, the merged config
is ``{}``, ``resolve()`` materializes NOTHING and returns a ``path`` of ``None``
-- so the turn is handed no host config and runs on the engine's own defaults.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
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
_WORKSPACE_ALLOWED_KEYS = frozenset({"default", "profiles"})

# Where the merged host config is materialized under ``runs_dir``. Same
# directory (and, for the caching-only case, same bytes) the retired
# ``runner._automation_host_config_path`` used, so nothing downstream moves.
MATERIALIZED_DIRNAME = "automation_host_configs"

# Where an interactive/API turn's merged host config is materialized under
# ``runs_dir``, keyed by turn id. DELIBERATELY distinct from
# ``MATERIALIZED_DIRNAME`` (scheduled automation runs, keyed by slug): an
# interactive turn's config must never overwrite -- or be overwritten by -- a
# scheduled run's materialized file mid-flight, and turn ids are unique so two
# concurrent turns can never collide either.
TURN_MATERIALIZED_DIRNAME = "turn_host_configs"

# The CLOSED top-level vocabulary a config layer may declare.
ALLOWED_TOP_LEVEL_KEYS = frozenset({"provider", "providers", "mcp", "skills", "debug"})

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
}

# Credential-bearing key names (compared case-insensitively) refused at any
# depth. ``provider.config.api_key`` is the canonical attack.
_CREDENTIAL_KEYS = frozenset({"api_key", "apikey", "token", "secret", "authorization"})

# The recorded "effective provider module" for a turn that names no provider
# module of its own -- i.e. it runs on whatever provider the bundle mounts.
# Stored beside the contract fingerprint so a later change to an EXPLICIT
# provider module is detectable; a run that stays on the bundle default never
# rotates against this sentinel.
BUNDLE_DEFAULT_PROVIDER = "<bundle-default>"


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
    or ``BUNDLE_DEFAULT_PROVIDER`` -- because provider-change rotation needs it
    whether or not anything was materialized. ``config`` is the merged mapping.
    """

    path: Path | None
    sha: str | None
    provider_module: str
    config: dict[str, Any]


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


def validate_config_layer(data: Any, *, source: str) -> dict[str, Any]:
    """Fully validate one config layer, or raise ``AgentConfigError``.

    Checks: the layer is a mapping; its top-level keys are within the closed
    vocabulary (with ``approval``/``allowProtocolSkew`` refused by name); and no
    credential key or null value appears at ANY depth. Returns the same mapping
    on success so a caller can ``validate_config_layer(...)`` inline.
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
    """The provider module a merged config selects, or ``BUNDLE_DEFAULT_PROVIDER``.

    Reads ``provider.module`` (amplifier-agent's own field). ``providers``
    (plural catalog) does not set the pin's provider identity in v1; a turn that
    names no explicit ``provider.module`` records the bundle-default sentinel.
    """
    provider = config.get("provider")
    if isinstance(provider, dict):
        module = provider.get("module")
        if isinstance(module, str) and module.strip():
            return module.strip()
    return BUNDLE_DEFAULT_PROVIDER


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


def _load_env_layer(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Layer 1: the ``$AMPLIFIER_AGENT_CONFIG`` file, or ``None`` if unset."""
    raw = env.get(ENV_CONFIG_VAR)
    if not raw or not raw.strip():
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise AgentConfigError(
            f"{ENV_CONFIG_VAR}={raw!r}: file not found -- unset the variable or "
            "point it at a real amplifier-agent config file (it is folded in as "
            "the base layer of every automation's agent config)"
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
        return None
    return validate_config_layer(data, source=source)


def _load_workspace_default(workspace: Path) -> dict[str, Any] | None:
    """Layer 2: the workspace ``agent-config.yaml`` ``default:`` block, or ``None``."""
    path = Path(workspace).expanduser() / WORKSPACE_CONFIG_FILENAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"{path}: cannot read: {exc}") from exc
    data = _parse_mapping(text, source=str(path))
    if not data:
        return None
    unknown = set(data) - _WORKSPACE_ALLOWED_KEYS
    if unknown:
        raise AgentConfigError(
            f"{path}: unknown top-level key(s) {sorted(unknown)} -- only "
            f"{sorted(_WORKSPACE_ALLOWED_KEYS)} are recognized ('default:' is "
            "the base layer merged into every automation; 'profiles:' holds "
            "named profiles for interactive/API turns)"
        )
    default = data.get("default")
    if default is None:
        return None
    return validate_config_layer(default, source=f"{path} (default:)")


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

    Merges the four layers (see the module docstring) and, when the result is
    non-empty, writes it atomically to
    ``<runs_dir>/automation_host_configs/<slug>.json`` (rewritten each run, so
    it can never drift from the sources on disk) and returns its path + sha.
    When the merged config is empty, returns a ``ResolvedAgentConfig`` with
    ``path``/``sha`` ``None`` -- the turn is handed no host config.

    ``automation_config`` is trusted (already validated at parse time). Every
    other file/profile layer is validated here and a failure raises
    ``AgentConfigError``.
    """
    env = os.environ if env is None else env

    layers: list[Mapping[str, Any]] = []

    env_layer = _load_env_layer(env)
    if env_layer:
        layers.append(env_layer)

    workspace_layer = _load_workspace_default(workspace)
    if workspace_layer:
        layers.append(workspace_layer)

    if profile is not None:
        layers.append(validate_config_layer(profile, source="named profile"))

    if automation_config:
        # Already validated at parse time (drumbeat.automation), so it is
        # trusted here -- re-raising a parse-time error at run time would report
        # the same fault against the wrong surface.
        layers.append(automation_config)

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = merge_config(merged, layer)

    provider_module = effective_provider_module(merged)

    if not merged:
        return ResolvedAgentConfig(
            path=None, sha=None, provider_module=provider_module, config={}
        )

    path, sha = _materialize(runs_dir, MATERIALIZED_DIRNAME, slug, merged)
    return ResolvedAgentConfig(
        path=path, sha=sha, provider_module=provider_module, config=merged
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
    path = Path(workspace).expanduser() / WORKSPACE_CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"{path}: cannot read: {exc}") from exc
    data = _parse_mapping(text, source=str(path))
    if not data:
        return {}
    unknown = set(data) - _WORKSPACE_ALLOWED_KEYS
    if unknown:
        raise AgentConfigError(
            f"{path}: unknown top-level key(s) {sorted(unknown)} -- only "
            f"{sorted(_WORKSPACE_ALLOWED_KEYS)} are recognized ('default:' is "
            "the base layer merged into every automation; 'profiles:' holds "
            "named profiles for interactive/API turns)"
        )
    raw = data.get("profiles")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentConfigError(
            f"{path} (profiles:): must be a mapping of profile name -> config "
            f"layer, got {type(raw).__name__}"
        )
    profiles: dict[str, dict[str, Any]] = {}
    for name, layer in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise AgentConfigError(
                f"{path} (profiles:): profile name {name!r} must be a non-empty string"
            )
        profiles[name] = validate_config_layer(
            layer, source=f"{path} (profiles.{name}:)"
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

    The layers, lowest precedence first: the ``$AMPLIFIER_AGENT_CONFIG`` base,
    the workspace ``agent-config.yaml`` ``default:`` block, and -- when the turn
    names one -- a ``profile`` looked up in that same file's ``profiles:`` block
    (layer 3 of the shared merge). ``profile=None`` merges only the base and
    ``default:`` layers: a profile-less turn uses the default, exactly as the
    spec requires. An unknown profile name fails loud (``select_profile``),
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

    layers: list[Mapping[str, Any]] = []

    env_layer = _load_env_layer(env)
    if env_layer:
        layers.append(env_layer)

    workspace_layer = _load_workspace_default(workspace)
    if workspace_layer:
        layers.append(workspace_layer)

    if profile is not None:
        layers.append(select_profile(workspace, profile))

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = merge_config(merged, layer)

    provider_module = effective_provider_module(merged)

    if not merged:
        return ResolvedAgentConfig(
            path=None, sha=None, provider_module=provider_module, config={}
        )

    path, sha = _materialize(runs_dir, TURN_MATERIALIZED_DIRNAME, key, merged)
    return ResolvedAgentConfig(
        path=path, sha=sha, provider_module=provider_module, config=merged
    )


__all__ = [
    "ALLOWED_TOP_LEVEL_KEYS",
    "BUNDLE_DEFAULT_PROVIDER",
    "ENV_CONFIG_VAR",
    "MATERIALIZED_DIRNAME",
    "TURN_MATERIALIZED_DIRNAME",
    "WORKSPACE_CONFIG_FILENAME",
    "AgentConfigError",
    "ResolvedAgentConfig",
    "effective_provider_module",
    "load_profiles",
    "merge_config",
    "resolve",
    "resolve_turn",
    "select_profile",
    "validate_config_layer",
]
