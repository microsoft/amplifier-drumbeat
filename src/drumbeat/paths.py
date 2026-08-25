"""Shared filesystem path helpers used by more than one entry point."""

from __future__ import annotations

import os
from pathlib import Path


def amplifier_agent_home() -> Path:
    """Mirror amplifier-agent's own home-directory resolution.

    Default ``~/.amplifier-agent``, override via ``$AMPLIFIER_AGENT_HOME`` --
    the same two rules amplifier-agent itself uses. Promoted here (out of
    ``runner.py``, where it started as a private helper) once a second,
    independent consumer needed it: reading a session's ``transcript.jsonl``
    directly for the chat-history fallback (see ``transcript_reader.py``) --
    the two-implementation rule for promoting a helper out of a single
    module. ``runner.py``'s own ``_amplifier_agent_home`` now delegates here,
    same pattern as ``_derive_workspace_slug`` delegating to
    ``derive_workspace_slug`` below.
    """
    override = os.environ.get("AMPLIFIER_AGENT_HOME")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home).expanduser() / ".amplifier-agent"


def derive_workspace_slug(cwd: Path) -> str:
    """Mirror amplifier-agent's cwd -> workspace-slug derivation exactly.

    amplifier-agent resolves the workspace bucket that sessions are stored
    under as: ``--workspace`` argv flag > ``$AMPLIFIER_AGENT_WORKSPACE`` env
    > this cwd-derived fallback. We never pass ``--workspace`` ourselves, so
    only the env override and the cwd fallback apply here. The cwd-derived
    algorithm itself (verbatim, no hashing, full path encoded as the slug)
    is reproduced from amplifier-agent's own
    ``amplifier_agent_lib.persistence.derive_workspace_from_cwd`` -- the
    engine only shells out to the ``amplifier-agent`` CLI, it does not
    depend on that library, so this is intentionally reimplemented rather
    than imported.

    Shared by ``runner.py`` (session directory lookups) and ``ci_events.py``
    (tagging the engine's own orchestration events with the SAME workspace slug
    the amplifier-agent hook uses) -- one implementation, not two that could
    drift apart.
    """
    env_value = os.environ.get("AMPLIFIER_AGENT_WORKSPACE", "").strip()
    if env_value:
        return env_value
    slug = str(cwd.resolve()).replace("/", "-").replace("\\", "-").replace(":", "")
    if not slug.startswith("-"):
        slug = "-" + slug
    return slug


def workspace_for_automations_dir(automations_dir: Path) -> Path:
    """The workspace an ``automations/`` directory belongs to. **Never resolved.**

    ``.parent`` on the path AS GIVEN, with ``expanduser`` only. The missing
    ``.resolve()`` is the entire point of this function existing, so read
    this before "tidying" one in:

    A workspace's ``automations/`` is very often a SYMLINK to policy that
    lives somewhere else -- e.g. a workspace's ``automations/`` symlinked to
    a shared policy checkout, and so may ``guidance/``, ``prompts/`` and
    ``drumpacks.txt``. Resolving before taking the parent silently walks OUT
    of the workspace and lands in the policy repo, so every path derived from
    it -- above all ``bin/``, and therefore the entire turn PATH -- addresses
    a directory that is not the workspace.

    That is not hypothetical. It shipped: ``capabilities.resolve_tools``
    resolved first, computed the turn PATH against the symlink target's
    ``bin/`` instead of the workspace's own ``bin/``, and reported four
    installed-and-running tools as unresolvable to the client -- while the
    runner, which derives the workspace this way, found them and ran them
    fine. A card that lies about what the agent can run is the exact defect
    the capabilities endpoint exists to prevent.

    One implementation, used by BOTH the runner-side derivation
    (``management_api.EngineContext.workspace``) and the reporting-side one
    (``capabilities``), so the two cannot drift apart again -- the
    two-implementation promotion rule that put ``amplifier_agent_home``
    here.
    """
    return Path(automations_dir).expanduser().parent


__all__ = [
    "amplifier_agent_home",
    "derive_workspace_slug",
    "workspace_for_automations_dir",
]
