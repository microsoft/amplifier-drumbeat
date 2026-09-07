"""Shared filesystem path helpers used by more than one entry point."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# The directory under the engine's data dir that holds every pinned session's
# agent storage. One SUBDIRECTORY per session (see ``agent_session_storage``),
# never one shared root: the subdirectory's existence is the whole session
# probe, and drumbeat owns that path without ever reading inside it.
AGENT_STORAGE_DIRNAME = "agent-storage"

# The session-id shape the agent library accepts, checked at creation
# (contracts/agent-binding.v1.md section 6). An id outside it is refused with
# ``session_id_invalid``, so every id crossing the seam is translated first.
_AGENT_SESSION_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{7,63}\Z")
_AGENT_SESSION_ID_MAX = 64
_AGENT_SESSION_ID_MIN = 8
# Length of the digest suffix a translated id carries. Long enough that two
# distinct drumbeat ids that sanitize identically cannot collide in practice.
_AGENT_SESSION_ID_DIGEST = 8


def agent_storage_root(runs_dir: Path) -> Path:
    """The directory every pinned session's agent storage lives under.

    ``<runs_dir>/agent-storage``. Inside the workspace on purpose: a renamed or
    moved project carries its sessions with it, so nothing is orphaned by a
    rename (contracts/agent-binding.v1.md section 6).
    """
    return Path(runs_dir).expanduser() / AGENT_STORAGE_DIRNAME


def agent_session_storage(session_id: str, *, runs_dir: Path) -> Path:
    """This session's OWN agent storage root, passed as ``AgentOptions.storage``.

    One root per pinned session, keyed by the TRANSLATED id (so the directory
    name and the id the library was handed always agree). drumbeat owns this
    path and stats it; it never reads inside, because the library declares its
    internal layout non-contractual.
    """
    return agent_storage_root(runs_dir) / agent_session_id(session_id)


def agent_session_id(session_id: str) -> str:
    """Translate a drumbeat session id into one the agent library accepts.

    The library requires ``[a-z0-9][a-z0-9-]{7,63}``; drumbeat's own ids do not
    match it (they carry uppercase and ``T``/``Z`` stamps, e.g.
    ``channels-check-20260804T221148Z``).

    Two rules, and they are what make the translation safe rather than merely
    convenient:

    * an id that ALREADY matches is returned **verbatim** -- no rewriting, so an
      id a human reads in a pin file is the id the library stores;
    * anything else is lowercased, non-conforming characters become ``-``, and a
      ``-<sha256[:8] of the ORIGINAL id>`` suffix is appended. The digest is what
      keeps two distinct drumbeat ids (``Foo_Bar`` and ``Foo-Bar``) from
      collapsing onto one agent session -- which would silently merge two
      automations' conversations, the worst outcome this seam can produce.

    Pure and deterministic: the same drumbeat id always yields the same agent id,
    in any process, so a resume in a later process finds the same session.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if _AGENT_SESSION_ID_RE.match(session_id):
        return session_id

    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[
        :_AGENT_SESSION_ID_DIGEST
    ]
    sanitized = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    sanitized = sanitized.lstrip("-")
    if not sanitized:
        sanitized = "s"
    # Reserve room for "-<digest>" so the result always fits the ceiling.
    keep = _AGENT_SESSION_ID_MAX - _AGENT_SESSION_ID_DIGEST - 1
    translated = f"{sanitized[:keep].rstrip('-')}-{digest}"
    if len(translated) < _AGENT_SESSION_ID_MIN:  # pragma: no cover - digest is 8 chars
        translated = translated.ljust(_AGENT_SESSION_ID_MIN, "0")
    return translated


def derive_workspace_slug(cwd: Path) -> str:
    """The workspace bucket an automation's pins are recorded under.

    **drumbeat's own identity for a workspace**, not the agent library's. The
    library separates stored sessions by its own ``workspace`` key, which
    drumbeat never sets -- separation there is achieved by giving each pinned
    session its own storage root (``agent_session_storage``). This slug is what
    drumbeat records on a pin (``session_workspace``) so a pin copied between
    workspaces is detectable rather than silently resumed against the wrong
    conversation.

    ``$AMPLIFIER_AGENT_WORKSPACE`` overrides it, deliberately sharing the name
    the library uses for the same concept: an operator who buckets one, buckets
    both. Otherwise the full resolved path is encoded as the slug -- no hashing,
    so the slug is readable in a pin file.

    Shared by ``runner.py`` (pin bookkeeping), ``session_health.py`` and
    ``ci_events.py`` (tagging orchestration events with the same bucket) -- one
    implementation, not two that could drift apart.
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
    two-implementation promotion rule that put ``derive_workspace_slug``
    here.
    """
    return Path(automations_dir).expanduser().parent


__all__ = [
    "AGENT_STORAGE_DIRNAME",
    "agent_session_id",
    "agent_session_storage",
    "agent_storage_root",
    "derive_workspace_slug",
    "workspace_for_automations_dir",
]
