"""The BYO tool-drumpack contract (see docs/DRUMPACKS.md).

A **drumpack** is a directory -- normally a git checkout::

    drumpack.md      frontmatter: pack_format (required), name, description,
                     tools (the EXHAUSTIVE list of bin/ entries), and the
                     optional `activity:` map (subcommand -> narration label).
                     body: the card -- what exists, invocation shapes, quirks,
                     completeness conventions, negative space.
    bin/             executables; each MUST self-document via --help
    guidance/        optional: exemplar policy files for this source
    automations/     optional: exemplar automations requiring this drumpack's tools

That is the entire contract: **PATH + card + fail-loud load rules.** There is
no plugin protocol, no registry, no version solver, no install step, no
per-tool JSON schema, and no semver constraint. Drumpacks are checkouts; git is
the version mechanism; a consumer pins by submodule. Each of those was
considered and rejected as a contract surface nothing consumes yet -- the
`requires:`-was-theater lesson at repo scale.

What this module owns, all fail-loud:

1. **Load** -- every way a card can lie is a refusal, in both directions. A
   declared tool that is missing, or present but not executable, is class-2
   theater with a file extension; an executable in ``bin/`` that the card
   never declares is the same lie pointed the other way. A duplicate tool
   name across two drumpacks refuses to start and names BOTH drumpacks, because
   a silent precedence order is the silent-fallback ban applied to namespaces.
2. **PATH** -- the turn PATH is the declared drumpack ``bin/`` dirs + the
   workspace ``bin/`` **prepended to a pinned base**, captured once per
   process and never re-read. See ``pin_base_path``.
3. **Card resolution** -- which drumpack owns a tool an automation `requires:`,
   so ``runner`` can inject that drumpack's card verbatim beside the guidance.
   The card is structurally inseparable from the binary: you cannot get the
   tool on PATH without the knowledge that makes it usable.

The ``activity:`` map is the mechanism, not policy, half of progress
narration: the engine shows a human phrase while a tool runs, and each
consumer drumpack owns the phrasing for its OWN tools' subcommands rather than
the engine hardcoding any consumer's vocabulary. Undeclared tools fall back to
generic narration in ``runner``.

No caching, deliberately. Loading a drumpack is three small file reads and a
handful of ``stat`` calls, against turns that take minutes -- and a cache
here would be a second copy of the truth that can disagree with the disk,
which is precisely the running-code-vs-disk failure class this project keeps
paying for. Editing ``drumpack.md`` therefore takes effect on the next turn,
with no restart, exactly like every other markdown in this system.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# The file a consumer workspace uses to hand the engine its ORDERED list of
# drumpack directories (the drumpack contract's founding sentence: "The consumer
# hands the engine a workspace:
# automations/, guidance/, prompts/, an ordered list of drumpack directories,
# and a data dir"). One path per line, relative to the workspace or absolute;
# blank lines and `#` comments ignored.
#
# Order is for deterministic REPORTING only -- it never decides tool
# resolution, because a duplicate tool name across drumpacks refuses to load
# rather than letting position pick a winner.
PACK_LIST_FILENAME = "drumpacks.txt"

# The per-drumpack manifest filename: frontmatter + the card body.
MANIFEST_FILENAME = "drumpack.md"

# Unknown versions are refused loudly rather than best-effort parsed: a
# future drumpack format will mean something this code cannot know, and
# guessing is how a card starts lying.
SUPPORTED_PACK_FORMATS = frozenset({1})

_FRONTMATTER_DELIMITER = "---"


class PackError(Exception):
    """A drumpack could not be loaded. Always names the drumpack directory."""


@dataclass(frozen=True)
class Pack:
    """One loaded drumpack. Every field is verified against the disk, not trusted."""

    name: str
    directory: Path
    bin_dir: Path
    tools: tuple[str, ...]
    description: str
    card: str  # the drumpack.md body, verbatim -- what reaches the agent
    pack_format: int
    # Optional consumer-declared progress narration: {subcommand: label}. Empty
    # when the drumpack declares no `activity:` map -- undeclared tools narrate
    # via runner's generic fallback. Mechanism, not policy: the phrasing is the
    # consumer's, never the engine's.
    activity: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PackList:
    """The consumer's declared drumpack list, and whether it declared one at all.

    ``declared`` distinguishes "this workspace has no drumpacks" from "somebody
    deleted drumpacks.txt" at the reporting surface. Both are legal -- a tool
    that then fails to resolve aborts the run loudly at the requirements
    gate, naming the tool -- but they are different facts and are never
    conflated.
    """

    paths: tuple[Path, ...]
    source: Path
    declared: bool


# ---- the pinned base PATH (drumpack contract, requirement 2) ----
#
# Council amendment 7, rewritten to reality. The CONTRACT is "drumpack bins +
# workspace bin + the pinned base list". Raw os.environ inheritance is never
# the contract -- what cut-over ships is prepend-to-inherited as the
# IMPLEMENTATION of the base, captured exactly once per process and logged.
#
# What that structurally removes is the class-15 defect as it actually
# occurred twice in this project's record: undeclared dependence on whoever
# happened to start the process. The base is now pinned, echoed in
# /api/capabilities, and identical for every turn regardless of launch path.
#
# What it does NOT yet do is hermetic isolation, and the reason is brutal and
# worth stating rather than aspiring past: every current bin/ tool across the
# engine's test drumpack and both real drumpacks is a `#!/usr/bin/env bash`
# shim. A hermetic PATH shipped today kills 100% of tools at the shebang. Full
# hermeticism is a named post-topology commit, gated on a run-every-tool
# verification -- not a today decision.

_base_path_lock = threading.Lock()
_pinned_base_path: str | None = None


def pin_base_path(value: str | None = None) -> str:
    """Capture the base PATH once for this process. Idempotent.

    The first call wins and is never re-read: a later mutation of
    ``os.environ["PATH"]`` (by us, by a library, by anything) cannot change
    what turns see. ``value`` is for tests and for an explicit pin at service
    startup; omitted, the current environment is captured.
    """
    global _pinned_base_path
    with _base_path_lock:
        if _pinned_base_path is None:
            _pinned_base_path = (
                value if value is not None else os.environ.get("PATH", "")
            )
        return _pinned_base_path


def base_path() -> str:
    """The pinned base PATH, capturing it now if nothing has yet."""
    return pin_base_path()


def reset_base_path_for_tests() -> None:
    """Drop the pin so a test can pin a different base. Tests only."""
    global _pinned_base_path
    with _base_path_lock:
        _pinned_base_path = None


# ---- loading ----


def _split_frontmatter(path: Path, text: str) -> tuple[dict, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        raise PackError(
            f"{path}: missing YAML frontmatter (the file must start with a "
            f"'{_FRONTMATTER_DELIMITER}' delimited block)"
        )
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_DELIMITER:
            raw = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            break
    else:
        raise PackError(f"{path}: frontmatter block is never closed")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PackError(f"{path}: invalid YAML frontmatter: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PackError(f"{path}: frontmatter must be a YAML mapping")
    return data, body


def _parse_activity(path: Path, raw: object) -> dict[str, str]:
    """Parse the optional ``activity:`` map: {subcommand: narration label}.

    Consumer-owned progress narration -- mechanism, not policy. A drumpack
    declares, per subcommand of its CLIs, the human phrase the engine shows
    while that subcommand runs; ``runner`` consumes it. Absent is fine
    (undeclared tools fall back to generic narration). Present-but-malformed
    is a refusal, the same fail-loud discipline every other field gets: a
    narration map that lies about its own shape is theater like any other.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PackError(
            f"{path}: `activity` must be a mapping of subcommand -> narration "
            f"label (got {type(raw).__name__})"
        )
    labels: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise PackError(
                f"{path}: every `activity` key must be a non-empty subcommand string"
            )
        if not isinstance(value, str) or not value.strip():
            raise PackError(
                f"{path}: the `activity` label for {key.strip()!r} must be a "
                "non-empty string"
            )
        labels[key.strip()] = value.strip()
    return labels


def load_pack(directory: Path) -> Pack:
    """Load and fully verify one drumpack directory. Raises ``PackError`` on any lie.

    Every check below refuses rather than degrades. The card is the agent's
    only documentation for a tool it has never seen; a card that overstates
    (names a tool that is absent or not executable) sends the agent after
    something that cannot work, and a card that understates (a binary on PATH
    that nothing documents) hands it an undocumented weapon. Both are the
    same defect -- the card and ``bin/`` disagreeing -- so both refuse.
    """
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise PackError(f"drumpack directory does not exist: {directory}")
    directory = directory.resolve()

    card_path = directory / MANIFEST_FILENAME
    if not card_path.is_file():
        raise PackError(f"{directory}: no {MANIFEST_FILENAME} -- refusing to load")
    try:
        text = card_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackError(f"{card_path}: cannot read: {exc}") from exc

    frontmatter, card = _split_frontmatter(card_path, text)

    if "pack_format" not in frontmatter:
        raise PackError(
            f"{card_path}: `pack_format` is required with no default "
            f"(supported: {sorted(SUPPORTED_PACK_FORMATS)})"
        )
    pack_format = frontmatter["pack_format"]
    if pack_format not in SUPPORTED_PACK_FORMATS:
        raise PackError(
            f"{card_path}: unknown pack_format {pack_format!r} "
            f"(this engine supports {sorted(SUPPORTED_PACK_FORMATS)}) -- "
            "refusing rather than guessing what a future format means"
        )

    name = frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PackError(
            f"{card_path}: `name` is required and must be a non-empty string"
        )
    name = name.strip()

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PackError(
            f"{card_path}: `description` is required and must be a non-empty string"
        )

    declared = frontmatter.get("tools")
    if not isinstance(declared, list) or not declared:
        raise PackError(
            f"{card_path}: `tools` is required and must be a non-empty list of "
            "the executables in bin/ (exhaustive -- a card that lies in either "
            "direction is theater)"
        )
    # Two accepted shapes, deliberately: a bare string (the original card
    # shape, still live in the field) and the pack-card.v1 mapping
    # {name, bin, description}. The loader reads the NAME out of either --
    # everything else on the mapping is documentation the card contract owns,
    # not something the loader is entitled to reinterpret. Accepting both is
    # what lets a drumpack migrate its card without a flag day that takes every
    # other drumpack down with it.
    tools: list[str] = []
    for entry in declared:
        if isinstance(entry, str):
            if not entry.strip():
                raise PackError(
                    f"{card_path}: every `tools` entry must be a non-empty string"
                )
            tools.append(entry.strip())
            continue
        if isinstance(entry, dict):
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                raise PackError(
                    f"{card_path}: a `tools` entry given as a mapping must carry a "
                    "non-empty `name` (pack-card.v1 shape: {name, bin, description})"
                )
            tools.append(name.strip())
            continue
        raise PackError(
            f"{card_path}: every `tools` entry must be a non-empty string, or a "
            "pack-card.v1 mapping with a `name` -- got "
            f"{type(entry).__name__}"
        )
    duplicates = sorted({t for t in tools if tools.count(t) > 1})
    if duplicates:
        raise PackError(f"{card_path}: `tools` lists duplicates: {duplicates}")

    activity = _parse_activity(card_path, frontmatter.get("activity"))

    if not card.strip():
        raise PackError(
            f"{card_path}: the card body is empty -- a drumpack whose card says "
            "nothing puts the tool on PATH with no knowledge attached, which "
            "is the one thing the card exists to prevent"
        )

    bin_dir = directory / "bin"
    if not bin_dir.is_dir():
        raise PackError(f"{directory}: no bin/ directory -- refusing to load")

    for tool in tools:
        candidate = bin_dir / tool
        if not candidate.exists():
            raise PackError(
                f"{card_path}: declares tool {tool!r} but {candidate} does not exist"
            )
        if not candidate.is_file():
            raise PackError(
                f"{card_path}: declares tool {tool!r} but {candidate} is not a file"
            )
        if not os.access(candidate, os.X_OK):
            raise PackError(
                f"{card_path}: declares tool {tool!r} but {candidate} is NOT "
                "executable (exec bit unset) -- a card that names a tool the "
                "agent cannot run is theater with a file extension"
            )

    present = {
        entry.name
        for entry in bin_dir.iterdir()
        if entry.is_file() and os.access(entry, os.X_OK)
    }
    undeclared = sorted(present - set(tools))
    if undeclared:
        raise PackError(
            f"{card_path}: {bin_dir} holds executable(s) the card never "
            f"declares: {undeclared} -- a card that lies in either direction "
            "is theater. Declare them in `tools:` (and document them in the "
            "card) or move them out of bin/."
        )

    return Pack(
        name=name,
        directory=directory,
        bin_dir=bin_dir,
        tools=tuple(tools),
        description=description.strip(),
        card=card,
        pack_format=int(pack_format),
        activity=activity,
    )


def load_packs(directories: list[Path] | tuple[Path, ...]) -> tuple[Pack, ...]:
    """Load every declared drumpack, refusing on any duplicate name or tool name.

    Duplicates refuse to start and name BOTH drumpacks. There is deliberately
    no precedence rule: silently picking a winner is the silent-fallback ban
    applied to namespaces, and the loser's card would still be injected --
    documentation for a binary that never runs.
    """
    packs: list[Pack] = []
    for directory in directories:
        packs.append(load_pack(directory))

    by_name: dict[str, Pack] = {}
    for pack in packs:
        clash = by_name.get(pack.name)
        if clash is not None:
            raise PackError(
                f"two drumpacks both call themselves {pack.name!r}: {clash.directory} "
                f"and {pack.directory} -- refusing to start rather than picking one"
            )
        by_name[pack.name] = pack

    owner: dict[str, Pack] = {}
    for pack in packs:
        for tool in pack.tools:
            clash = owner.get(tool)
            if clash is not None:
                raise PackError(
                    f"tool {tool!r} is declared by BOTH drumpack {clash.name!r} "
                    f"({clash.directory}) and drumpack {pack.name!r} ({pack.directory}) "
                    "-- refusing to start rather than letting drumpack order silently "
                    "decide which binary the agent gets"
                )
            owner[tool] = pack

    return tuple(packs)


def read_pack_list(workspace: Path) -> PackList:
    """Read the consumer's ordered drumpack-directory list from ``drumpacks.txt``."""
    workspace = Path(workspace).expanduser().resolve()
    source = workspace / PACK_LIST_FILENAME
    if not source.is_file():
        return PackList(paths=(), source=source, declared=False)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackError(f"{source}: cannot read the drumpack list: {exc}") from exc

    paths: list[Path] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        candidate = Path(line).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            candidate = candidate.resolve()
        except OSError as exc:
            raise PackError(
                f"{source}:{lineno}: cannot resolve {line!r}: {exc}"
            ) from exc
        if candidate in paths:
            raise PackError(
                f"{source}:{lineno}: {line!r} resolves to {candidate}, which is "
                "already declared earlier in this file"
            )
        paths.append(candidate)
    return PackList(paths=tuple(paths), source=source, declared=True)


def load_workspace_packs(workspace: Path) -> tuple[Pack, ...]:
    """Every drumpack this workspace declares, loaded and verified. Fail-loud."""
    return load_packs(read_pack_list(workspace).paths)


# ---- PATH construction ----


def path_entries(workspace: Path, packs: tuple[Pack, ...] | None = None) -> list[str]:
    """The prepended entries, in order: each drumpack's bin/, then workspace bin/.

    Drumpack bins come first so a consumer's own ``bin/`` cannot shadow a
    drumpack tool the drumpack's card documents -- the card and the binary
    must stay the same thing. The workspace bin follows, and the pinned base
    follows that.
    """
    workspace = Path(workspace).expanduser().resolve()
    if packs is None:
        packs = load_workspace_packs(workspace)
    entries = [str(pack.bin_dir) for pack in packs]
    workspace_bin = workspace / "bin"
    if workspace_bin.is_dir():
        entries.append(str(workspace_bin))
    return entries


def turn_path(workspace: Path, packs: tuple[Pack, ...] | None = None) -> str:
    """The exact PATH every turn (and every `inject:` tool) runs with.

    Duplicates against the pinned base are dropped so the resulting string
    stays readable in ``/api/capabilities`` -- resolution order is unchanged
    either way, since the prepended entries always win.
    """
    prepended = path_entries(workspace, packs)
    seen = set(prepended)
    tail = [
        entry for entry in base_path().split(os.pathsep) if entry and entry not in seen
    ]
    return os.pathsep.join([*prepended, *tail])


def resolve_tool(
    name: str, workspace: Path, packs: tuple[Pack, ...] | None = None
) -> str | None:
    """Where ``name`` resolves on the turn PATH, or None. Never guesses."""
    return shutil.which(name, path=turn_path(workspace, packs))


# ---- card resolution (drumpack contract, requirement 3) ----


def pack_for_tool(packs: tuple[Pack, ...], tool: str) -> Pack | None:
    """Which drumpack declares ``tool``. None for a tool no drumpack provides."""
    for pack in packs:
        if tool in pack.tools:
            return pack
    return None


def cards_for_tools(packs: tuple[Pack, ...], tools: list[str]) -> list[Pack]:
    """The drumpacks owning ``tools``, in declaration order, each at most once.

    Declaration order rather than mention order so two automations naming the
    same drumpacks always produce the same card sequence -- a turn's text
    should not depend on how somebody happened to sort a `requires:` block.
    """
    wanted = {t for t in tools}
    return [pack for pack in packs if wanted & set(pack.tools)]


def activity_by_tool(packs: tuple[Pack, ...]) -> dict[str, dict[str, str]]:
    """Map each declared tool name to its drumpack's ``activity`` map.

    Every tool of a drumpack shares that drumpack's ``activity`` map -- the
    runner looks the invoked program up by name, then the subcommand within.
    Tools whose drumpack declares no ``activity:`` map are simply absent here
    and narrate via the runner's generic fallback.
    """
    mapping: dict[str, dict[str, str]] = {}
    for pack in packs:
        if not pack.activity:
            continue
        for tool in pack.tools:
            mapping[tool] = pack.activity
    return mapping


__all__ = [
    "MANIFEST_FILENAME",
    "PACK_LIST_FILENAME",
    "SUPPORTED_PACK_FORMATS",
    "Pack",
    "PackError",
    "PackList",
    "activity_by_tool",
    "base_path",
    "cards_for_tools",
    "load_pack",
    "load_packs",
    "load_workspace_packs",
    "pack_for_tool",
    "path_entries",
    "pin_base_path",
    "read_pack_list",
    "reset_base_path_for_tests",
    "resolve_tool",
    "turn_path",
]
