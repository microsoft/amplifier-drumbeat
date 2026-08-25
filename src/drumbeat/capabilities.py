"""Best-effort discovery of the tool executables and environment facts the
mobile authoring UI needs to build its closed-set pickers.

This module never decides policy -- it only reports facts about what is
actually resolvable on this machine right now (``shutil.which``), so the
requires picker and the keyboard accessory bar can render a real, current
answer instead of the app guessing or the user typing from memory.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from drumbeat import packs
from drumbeat.automation import AutomationError, load_all
from drumbeat.paths import workspace_for_automations_dir

# A floor so the picker has something to show in a workspace that declares no
# packs and references no tools yet. `gh` is a deliberate example of a tool
# that is NOT provided by any pack -- it either resolves off the pinned base
# PATH or honestly reports itself unresolvable. A tool that no pack declares
# and that resolves on no PATH must surface as unresolvable, never be silently
# assumed present.
#
# Everything else that used to be hardcoded here is gone (2026-08-11). This
# list once carried a handful of consumer- and pack-specific tool names from
# the era before packs existed. Every one of them is now declared by a pack,
# which means the two automatic sources below already produce them, and
# keeping them here made the engine appear to know one specific consumer's
# tool vocabulary. Verified against a real workspace: the resolved candidate
# set is byte-identical with the list emptied, because the packs supply it.
#
# Two sources fold in automatically, so a newly-authored automation or a
# newly-declared pack is picked up the next time this is called with no code
# change:
#   * every non-file entry in every automation's `requires:`
#   * every tool every declared pack declares
_BASELINE_TOOLS = ("gh",)

_HELP_TIMEOUT_SECONDS = 3


def _candidate_tool_names(
    automations_dir: Path, loaded: tuple[packs.Pack, ...]
) -> list[str]:
    names: set[str] = set(_BASELINE_TOOLS)
    for pack in loaded:
        names.update(pack.tools)
    try:
        for automation in load_all(automations_dir):
            for req in automation.requires:
                if not req.endswith(".md"):
                    names.add(req)
    except AutomationError:
        # A broken automation file is already logged loudly by
        # AutomationError itself (see drumbeat.error_log) -- the capabilities
        # picker should still answer with the baseline set rather than
        # failing this whole endpoint over one unrelated broken file.
        pass
    return sorted(names)


def _one_line_help(name: str) -> str | None:
    """First non-empty line of ``name --help``, or None on any failure.

    Best-effort only: not every tool supports ``--help`` the same way, and a
    hanging or misbehaving executable must never block this endpoint --
    bounded by a short timeout, with every failure mode (missing binary,
    non-zero exit, timeout) treated identically as "no help available".
    """
    try:
        result = subprocess.run(
            [name, "--help"],
            capture_output=True,
            text=True,
            timeout=_HELP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    if not output:
        return None
    return output.splitlines()[0].strip()


def resolve_tools(automations_dir: Path) -> list[dict[str, Any]]:
    """Every tool name worth showing in the requires picker, resolved against PATH.

    A tool not currently resolvable still appears (``resolved: false``) --
    the picker renders it greyed with "not found on server" rather than
    hiding it, per the design's "visible-broken beats un-authorable" trade.

    Resolution happens against **the exact PATH a turn gets** -- declared
    pack bins + workspace bin + the pinned base (``packs.turn_path``). Any
    other search path here would let this endpoint report a tool as
    available that the agent's own bash tool cannot actually find, which is
    a card that lies with extra steps. Each tool also carries the pack that
    provides it (``pack: null`` for one resolved off the base PATH or the
    workspace's own bin/).

    The workspace is derived with ``workspace_for_automations_dir`` -- the
    same one-line derivation the runner side uses
    (``management_api.EngineContext.workspace``) and, critically, WITHOUT
    resolving symlinks first. See that function for why: resolving walked
    this endpoint out of the workspace and made it report four live,
    running tools as unresolvable.
    """
    workspace = workspace_for_automations_dir(automations_dir)
    loaded = packs.load_workspace_packs(workspace)
    search_path = packs.turn_path(workspace, loaded)
    tools = []
    for name in _candidate_tool_names(automations_dir, loaded):
        resolved_path = shutil.which(name, path=search_path)
        owner = packs.pack_for_tool(loaded, name)
        tools.append(
            {
                "name": name,
                "resolved": resolved_path is not None,
                "path": resolved_path,
                "help": _one_line_help(name) if resolved_path else None,
                "pack": owner.name if owner is not None else None,
            }
        )
    return tools


def resolve_packs(automations_dir: Path) -> dict[str, Any]:
    """The pack half of ``/api/capabilities``: what is declared and loaded.

    Reports the declared list, each loaded pack (name, directory, tools,
    description, card length), the prepended PATH entries, the pinned base,
    and the resulting turn PATH -- section 5's "packs loaded, executables
    resolved ON THE TURN PATH, cards", and section 7.2's requirement that
    the pinned base be echoed here rather than merely logged once at
    startup where nobody will ever look at it again.

    The card BODY is deliberately not inlined: cards run to hundreds of
    lines each and this endpoint is polled by a phone. ``card_bytes`` plus
    the pack directory is enough to answer "is the card what I think it is"
    without shipping it over the wire on every poll.

    Workspace derivation is the shared, never-resolved one -- see
    ``resolve_tools`` above and ``workspace_for_automations_dir``. Getting
    it wrong here mis-reports ``pack_list``, ``path_prepended`` and
    ``turn_path`` in exactly the same direction, which is how a wrong turn
    PATH stays invisible: the endpoint that would expose it is computing it
    the same wrong way.
    """
    workspace = workspace_for_automations_dir(automations_dir)
    declared = packs.read_pack_list(workspace)
    loaded = packs.load_packs(declared.paths)
    return {
        "declared": declared.declared,
        "pack_list": str(declared.source),
        "packs": [
            {
                "name": pack.name,
                "pack_format": pack.pack_format,
                "directory": str(pack.directory),
                "bin": str(pack.bin_dir),
                "tools": list(pack.tools),
                "description": pack.description,
                "card_bytes": len(pack.card.encode("utf-8")),
            }
            for pack in loaded
        ],
        "path_prepended": packs.path_entries(workspace, loaded),
        "path_base_pinned": packs.base_path(),
        "turn_path": packs.turn_path(workspace, loaded),
    }


def server_timezone() -> str:
    """Best-effort IANA timezone name; falls back to the platform's short name.

    Never fabricated: if neither source is available, the honest answer is
    the (potentially unhelpful) platform default rather than a guess.
    """
    # /etc/localtime is the authority the C library actually uses; /etc/timezone
    # is a hint that can disagree with it. Observed live 2026-08-03: the file
    # said "Etc/UTC" while the process was really running at UTC-7
    # (America/Los_Angeles). That made every "local hour" wrong by 7 hours and
    # silently inverted the quiet-hours window -- it treated his evening as
    # night and left his real 00:00-05:59 unflagged, so the mechanism was blind
    # exactly when it mattered. Resolve the symlink to a real IANA name first.
    localtime = Path("/etc/localtime")
    if localtime.exists():
        try:
            resolved = localtime.resolve()
            parts = resolved.parts
            if "zoneinfo" in parts:
                name = "/".join(parts[parts.index("zoneinfo") + 1 :])
                if name:
                    return name
        except OSError:
            pass
    tz_file = Path("/etc/timezone")
    if tz_file.is_file():
        try:
            name = tz_file.read_text(encoding="utf-8").strip()
            if name:
                return name
        except OSError:
            pass
    return time.tzname[0] if time.tzname and time.tzname[0] else "UTC"


__all__ = ["resolve_tools", "server_timezone"]
