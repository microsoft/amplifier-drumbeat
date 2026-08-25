"""Execute one automation end to end.

An automation's steps are fed to ``amplifier-agent`` as sequential user turns
in ONE conversation: step 1 starts a fresh session, every subsequent step
(including the steps of *this* module, not the automation author's) resumes
that same session. One OS process per turn — that is the engine's contract,
not an implementation shortcut.

Fail loud: a step that errors, times out, or returns malformed output aborts
the run immediately. Later steps assume earlier ones succeeded; continuing
past a failure would produce confidently wrong output.
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from drumbeat import (
    agent_config,
    capabilities,
    ci_events,
    ci_upload,
    engine_events,
    error_log,
    packs,
    session_health,
    session_pins,
)
from drumbeat.automation import (
    DEFAULT_CONVERSATION_LIFECYCLE,
    DEFAULT_GUIDANCE_DELIVERY,
    VALID_GUIDANCE_DELIVERY,
    Automation,
    InjectSpec,
    Trigger,
)
from drumbeat.paths import amplifier_agent_home, derive_workspace_slug
from drumbeat.prompts import (
    DEFAULT_PROMPTS_DIR,
    PromptError,
    load_prompt,
    require_prompt,
)
from drumbeat.rotation_log import log_session_rotation

# Sentinel the agent must reply with, verbatim, when notify=auto and it has
# nothing worth telling the user. This is a protocol token, not policy — it
# stays a code constant. The *prompt text* that asks for it lives in
# prompts/auto-notify.md, which the user owns; see drumbeat.prompts.
NOTHING_TO_REPORT = "NOTHING_TO_REPORT"

_STEP_TIMEOUT_SECONDS = 900
_AGENT_COMMAND = "amplifier-agent"

# The engine executes every turn by shelling out to ``amplifier-agent``. If it
# cannot be resolved, every automation in the workspace dies at spawn -- and,
# before this, died invisibly: the FileNotFoundError propagated out of ``run()``
# before the run had persisted anything, so the scheduler's catch-all logged one
# line to a journal nobody tails and recorded ``run_id '<none -- raised>'`` in
# memory. No run record, no failures.log line, no automation_error event, and
# ``GET /api/automations/<slug>/runs`` still answered ``{"runs": [], "count":
# 0}``. Failure class 1, in the engine whose entire pitch is that a silence
# you cannot trust becomes a record you can.
#
# How it is resolved: SIBLING FIRST, then the turn PATH -- see
# ``_resolve_agent_command``. A single ``uv tool install git+<drumbeat repo>``
# co-installs amplifier-agent (a declared dependency) into the SAME tool venv
# as drumbeat, but uv exposes only the primary package's script (``drumbeat``)
# on the user PATH -- so the co-installed agent is found by locus (sibling of
# ``sys.executable``), not by name. Nothing else needs installing.
#
# Two defenses, deliberately overlapping. ``check_agent_command`` is the
# PREFLIGHT: ``serve`` refuses to start and ``doctor`` reports, both quoting
# the hint below, so the ordinary case is caught before a single automation is
# scheduled. ``_execute_turn`` catches the spawn failure anyway, because the
# preflight can be true at startup and false at 03:40 (an uninstall, a PATH
# edit, a pack that shadows the name) -- and a run that dies then must still
# land in the ledger.
#
# The hint is the fallback for a NON-tool-install setup (a dev checkout, a venv
# without the dependency synced). amplifier-agent is not on PyPI, so the git URL
# is the mechanism; it is unpinned to match the pyproject dependency.
AGENT_INSTALL_HINT = (
    f"{_AGENT_COMMAND} could not be resolved. The engine executes every turn "
    "by running it, so nothing can run until it resolves.\n"
    f"  A normal `uv tool install git+<drumbeat repo>` co-installs {_AGENT_COMMAND} "
    "into the same tool venv and the engine finds it there automatically; if "
    "you see this, drumbeat is likely running from a dev checkout or a venv "
    "that has not installed its dependencies.\n"
    "  To put it on PATH yourself (it is NOT on PyPI -- a bare `uv tool "
    "install amplifier-agent` will not resolve):\n"
    "    uv tool install git+https://github.com/microsoft/amplifier-agent\n"
    f"    {_AGENT_COMMAND} --version\n"
    "  Then make sure the directory it landed in (usually ~/.local/bin) is on "
    "the PATH this engine was started with."
)


def _sibling_agent_command() -> str | None:
    """``amplifier-agent`` installed alongside this interpreter, if present.

    A ``uv tool install git+<drumbeat repo>`` builds ONE venv holding drumbeat
    and its dependencies -- amplifier-agent among them, as an unpinned git dep
    -- but uv exposes only the PRIMARY package's script (``drumbeat``) on the
    user PATH (usually ~/.local/bin). amplifier-agent's own console script
    lands in the tool venv's ``bin/`` (the directory holding ``sys.executable``)
    and never reaches the turn PATH. Resolving it here, as a sibling of the
    running interpreter, is what makes the single-command install story work:
    the agent the engine shells out to is the one uv co-installed, with no
    second manual install and no PATH surgery.
    """
    candidate = Path(sys.executable).parent / _AGENT_COMMAND
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _resolve_agent_command(workspace: Path) -> str:
    """Absolute path to spawn ``amplifier-agent`` as; the bare name as last resort.

    Order, and why:
      1. Sibling of ``sys.executable`` (``_sibling_agent_command``) -- the agent
         co-installed into this engine's own uv tool venv. uv keeps it off the
         turn PATH, so it must be found by locus. This is the single-install
         path, and it is preferred so the co-installed agent (guaranteed to be
         the one resolved alongside this drumbeat) wins over any unrelated
         amplifier-agent a machine happens to also have on PATH.
      2. The turn PATH (``packs.turn_path``) -- a bring-your-own amplifier-agent
         the operator installed separately and put on PATH. Preserves the
         dev-checkout story.
      3. The bare name, when neither resolves -- so the spawn raises the same
         FileNotFoundError ``_execute_turn`` converts into a persisted run
         failure quoting AGENT_INSTALL_HINT, rather than this function having to
         return something a caller must special-case.
    """
    return (
        _sibling_agent_command()
        or shutil.which(_AGENT_COMMAND, path=packs.turn_path(Path(workspace)))
        or _AGENT_COMMAND
    )


def check_agent_command(workspace: Path) -> str | None:
    """Resolve ``amplifier-agent`` as the engine will spawn it; None when missing.

    The preflight for ``serve``/``doctor``. Mirrors ``_resolve_agent_command``'s
    first two sources -- the sibling of ``sys.executable`` (the tool-venv
    co-install), then the turn PATH -- so the preflight answers about the SAME
    binary a turn will actually run.

    The turn-PATH half is deliberately resolved against
    ``packs.turn_path(workspace)`` -- the exact PATH ``_turn_env`` hands every
    turn -- and not against ``os.environ``. Those two differ by construction
    (the base PATH is pinned at startup and pack ``bin/`` directories are
    prepended), so a preflight that consulted the launcher's environment could
    answer "present" about a PATH no turn ever runs with.

    Returns None -- not the bare name -- when neither source resolves, because
    its callers (``serve`` refusing to start, ``doctor`` printing MISSING)
    branch on None.
    """
    return _sibling_agent_command() or shutil.which(
        _AGENT_COMMAND, path=packs.turn_path(Path(workspace))
    )


class RunnerError(Exception):
    """Raised for runner-level failures that are not a specific step's fault."""


# Linux caps a SINGLE argv element at MAX_ARG_STRLEN = 32 * PAGE_SIZE. On every
# machine this engine has run on (4 KiB pages) that is 131072 bytes = 128 KiB.
# The engine passes the whole turn text as the last argv element to
# amplifier-agent (see ``_build_command``), so a turn whose text reaches this
# size fails ``execve`` with ``OSError(errno=E2BIG)`` -- BEFORE the agent boots,
# with no run record. Measured on this host: 131071 bytes execs, 131072 fails.
# This is the ceiling the reference-form guidance mechanism exists to stay far
# under, and the belt guard below is what turns a breach into a named,
# actionable failure instead of the kernel's opaque E2BIG.
MAX_ARG_STRLEN = 131072


class TurnTooLargeError(RunnerError):
    """One turn's text would exceed the OS per-argument ceiling (MAX_ARG_STRLEN).

    The belt in ``_build_command``: a turn whose text is this large would fail
    ``execve`` with E2BIG the moment it is spawned. Raising here instead lets
    ``_execute_turn`` convert it into a StepResult with ``.error`` set -- the
    SAME persisted-run / failures.log / automation_error path a spawn failure
    already takes -- so the breach lands in the ledger loudly rather than
    surfacing as an opaque OSError with no record. The remedy is almost always
    ``guidance_delivery: reference`` (the default): reference-form guidance
    keeps the turn a few hundred bytes no matter how large the files are.
    """

    def __init__(self, *, nbytes: int, limit: int = MAX_ARG_STRLEN) -> None:
        self.nbytes = nbytes
        self.limit = limit
        super().__init__(
            f"turn text is {nbytes} bytes, at or over the OS per-argument "
            f"ceiling of {limit} bytes (MAX_ARG_STRLEN); it would fail execve "
            f"with E2BIG before amplifier-agent could boot. Almost always this "
            f"is an automation using `guidance_delivery: inline` with a large "
            f"required guidance file -- switch it to the default "
            f"`guidance_delivery: reference` so the guidance is referenced by "
            f"path instead of inlined into argv."
        )


class SessionLockedError(RunnerError):
    """A session's advisory lock could not be acquired within the allotted wait.

    Fix 1: two OS processes -- the scheduler firing a scheduled run, and
    notify-serve handling an inbound reply/message -- can otherwise resume
    the SAME pinned session id at the same time. amplifier-agent replays
    and rewrites ``transcript.jsonl`` on resume, so two concurrent turns on
    one session can interleave writes and corrupt or truncate it --
    corrupting the very memory the pinned-session mechanism exists to
    protect. This is always caught inside ``_execute_turn`` and converted
    into a ``StepResult.error`` -- callers never see this exception type
    directly, they see it through the exact same error-handling path every
    other turn failure already uses.
    """

    def __init__(self, session_id: str, *, waited_seconds: float) -> None:
        self.session_id = session_id
        self.waited_seconds = waited_seconds
        super().__init__(
            f"session {session_id!r} is locked by another in-flight turn "
            f"(waited {waited_seconds:.1f}s)"
        )


# Machine-readable failure kinds for ``StepResult.error_kind``. The error
# STRING stays the human-facing truth; this is the bit a program is allowed
# to branch on, so nobody ever has to substring-match an error message to
# make a safety decision.
ERROR_KIND_SESSION_LOCKED = "session_locked"


@dataclass
class StepResult:
    """Outcome of a single turn (one automation step, or the auto-notify check)."""

    index: int
    text: str
    reply: str
    error: str | None
    duration_ms: int
    tokens_in: int
    tokens_out: int
    # ADDITIVE (decomposition step 5; see docs/ARCHITECTURE.md): a machine-readable
    # classification of WHY this turn failed, for the one decision that
    # genuinely depends on it -- "is it safe to resend the user's text
    # verbatim?".
    #
    # ``ERROR_KIND_SESSION_LOCKED`` is the only kind that answers yes, and it
    # answers yes for a hard reason, not a hopeful one: the lock was never
    # acquired, so no subprocess ever ran and no real action (send a chat
    # message, mark mail read) can have half-happened. It is the same
    # busy-signal the turn API reports synchronously as a 423 -- the only
    # difference is that the caller asked to wait first and the wait ran out.
    # Treating those two differently would mean the identical situation loses
    # the user's typed text or keeps it depending purely on a timing budget.
    #
    # ``None`` for every other failure, which is the conservative direction:
    # unknown never means resendable.
    error_kind: str | None = None
    # The declared automation step's ``id`` (contract automation-file.v1, rule
    # 4: step id appears in run records as identity, not control flow). Set only
    # for turns that ARE an automation step; ``None`` for the system-prompt,
    # requirements, inject, and auto-notify turns, which are engine-generated
    # and correspond to no declared step. Threaded into result.json and the
    # RUN_COMPLETED event so a run's turns can be tied back to the step that
    # produced them by identity, surviving a later reordering of the steps.
    step_id: str | None = None


@dataclass
class RunResult:
    """Outcome of an entire automation run (one full conversation)."""

    automation: str
    run_id: str
    session_id: str
    started_at: str  # ISO8601 UTC
    finished_at: str
    steps: list[StepResult]
    final_reply: str
    notified: bool
    failed: bool
    # Top-level abort reason for a run that never executed any turn at all
    # (currently only the "pinned session in an indeterminate state" abort
    # below). Distinct from a per-step ``StepResult.error``, which always
    # means at least one turn ran. ``None`` for every ordinary run.
    error: str | None = None
    # Observability for the pinned-session mechanism (see run()'s docstring):
    # never acted on by this module, recorded purely so a human can
    # correlate item loss/behavior against when compaction likely happened.
    session_resumed: bool = False
    session_transcript_bytes_at_start: int | None = None
    session_transcript_lines_at_start: int | None = None
    # (Fields ``suppressed_duplicate``/``suppressed_duplicate_of`` removed in
    # decomposition step 2: duplicate suppression happens at notification
    # mint, in the consumer's delivery worker -- see docs/ARCHITECTURE.md
    # section 2.1. Keeping engine-side fields that could only ever be False
    # would be failure class 13.)
    # Push demotion (notify: "urgent-only"): set when a would-be
    # notification was withheld from push because the automation is
    # marked "urgent-only" and the final reply carried no `URGENT:
    # <reason>` marker. ``notified`` is False whenever this is True.
    # Never means the work was lost: ``final_reply`` (persisted below,
    # same as any other run) still carries the full report.
    demoted: bool = False
    demoted_reason: str | None = None
    # Best-effort upload of this run's captured Context Intelligence events
    # to the local CI server (see ci_upload.py). ``ci_upload_attempted`` is
    # False for every "we didn't even try" case (no API key configured, the
    # CLI isn't installed) -- distinguished from an attempted-and-failed
    # upload (``ci_upload_attempted=True``, ``ci_upload_exit_code`` != 0)
    # so a silently-missing API key can never be mistaken for "everything's
    # fine, nothing to upload." ``None`` fields mean "no upload attempt has
    # been recorded yet" -- see run()'s call to _record_ci_upload_outcome,
    # which patches these in AFTER result.json is first written.
    ci_upload_attempted: bool | None = None
    ci_upload_exit_code: int | None = None
    ci_upload_error: str | None = None
    # Per-automation agent config (9h5): the materialized host config this run's
    # turns were handed via ``--config`` (its ``runs_dir``-relative-ish absolute
    # path) and the sha256 of its exact bytes. Both ``None`` when the resolved
    # config was empty -- i.e. no ``--config`` was threaded and the run used the
    # engine defaults, byte-identical to pre-feature behavior. Recorded so a run
    # can be tied back to the exact provider/model/skills policy it executed
    # under. Set on the normal run path (aborts before any turn leave them None).
    effective_config_path: str | None = None
    effective_config_sha: str | None = None


# The run id's shape is load-bearing in exactly two ways and opaque in every
# other: a fixed-width UTC-second prefix (so lexicographic order over run dir
# names is chronological order) followed by entropy (so identity is minted,
# not derived from a clock).
#
# The entropy is not decoration. Before it, two automations firing in the same
# wall-clock second minted the SAME run id, and the delivery seam's
# dedup-at-mint keys on run_id -- so the second automation's notification was
# suppressed against the first's, silently, carrying a reasoned suppression
# record that named an unrelated automation. Reproduced twice in production on
# 2026-08-10. That is failure class 5 (identity from a clock) manufacturing
# failure class 1 (ran, delivered nothing, told no one).
#
# Same shape as `turns.new_turn_id` (`t-<timestamp>-<hex4>`) -- house practice,
# not a new invention.
_RUN_ID_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_RUN_ID_ENTROPY_BYTES = 3  # -> 6 hex characters


def _run_id_now() -> str:
    stamp = datetime.now(UTC).strftime(_RUN_ID_TIMESTAMP_FORMAT)
    return f"{stamp}-{secrets.token_hex(_RUN_ID_ENTROPY_BYTES)}"


def _iso8601_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_now() -> datetime:
    """Current wall-clock time in the HOST's local timezone.

    The single clock seam the ``conversation: daily`` lifecycle reads -- both
    to stamp a new session's anchor day and to decide, on resume, whether the
    local calendar day has rolled over since. Deliberately its own function
    (not ``_iso8601_now``/``_run_id_now``, which are UTC and feed run ids) so a
    deterministic-clock test can patch the daily boundary without disturbing
    any run-id or artifact timestamp. Host-local because the daily boundary is
    a human-facing "since midnight where the operator is" line, matching the
    scheduler's own local-timezone ``daily at HH:MM`` handling.
    """
    return datetime.now().astimezone()


# ---- "what time is it" context (Fix: agent had no honest sense of "now") ----
#
# amplifier-agent's own bundle declares hooks-status-context with
# include_datetime=true, datetime_include_timezone=false: every turn already
# gets an ephemeral, untagged "Today's date: YYYY-MM-DD HH:MM:SS" line (local
# wall-clock, never persisted to transcript.jsonl -- see that hook's
# `ephemeral=True`). Verified directly (2026-08-04): a throwaway
# `amplifier-agent run` turn asked to state the date, time, and timezone
# replied with the correct date/time but "timezone cannot be determined from
# available context" -- exactly the gap this closes.
#
# That gap matters here specifically: a consumer's guidance files are full
# of freshness judgments (same-day/next-day, quiet hours, "how long ago",
# deadline proximity) computed by comparing an untagged local time against
# whatever timestamps its connectors return -- which, for the connector
# that surfaced this bug, are always UTC. (What a given connector's
# timestamps mean is that PACK's to say, on its card; the engine's job is
# only to make the local answer available at all.) A model with no
# timezone signal has every reason to assume
# its own untagged clock IS UTC -- which silently inverts every one of those
# comparisons by exactly the host's UTC offset. This is the same failure
# class as the quiet-hours bug capabilities.server_timezone() was written to
# fix (host's /etc/timezone said "Etc/UTC" while running at
# America/Los_Angeles); this closes the equivalent gap for the AGENT's own
# reasoning during a turn, not just the mechanical quiet-hours check.
#
# Mechanism, not policy: this only states the fact, every single turn, via
# capabilities.server_timezone() (already fixed to resolve /etc/localtime
# correctly). What to DO with the time -- quiet hours, urgency, deadline
# proximity -- stays entirely in guidance/ATTENTION.md. Deliberately applied
# to EVERY automation and every chat turn (not opt-in per automation):
# there is no automation for which "what time is it" is irrelevant, the
# cost is one short line of text, and an opt-in flag here would be exactly
# the kind of config surface IMPLEMENTATION_PHILOSOPHY.md warns against for
# something with no real cost and universal value.
#
# Applied at the single choke point every turn already passes through
# (``_execute_turn``) rather than at each of its ~15 call sites, so a future
# call site can never forget it -- see ``_execute_turn``'s docstring.


def _now_context_line() -> str:
    """One mechanically-generated line stating the current moment, local
    zone, and UTC equivalent -- see the module note above for why this
    exists. Never raises: ``capabilities.server_timezone()`` already
    guarantees an honest (never fabricated) answer and falls back to the
    platform default rather than raising.

    Prefix is ``[drumbeat]`` (renamed from an earlier consumer-specific
    prefix): the line is emitted by the engine on every turn of every
    consumer, so labelling it with one consumer's name was simply false for
    every other one.

    DOMAIN-FREE BY CONSTRUCTION (B4): this line used to carry four
    hardcoded sentences of one connector's timestamp advice. That is a
    pack's knowledge, not the engine's -- an engine with no mail-check pack
    installed was still lecturing the agent about that connector's API. The sentence now
    lives on the mail-check drumpack card (``drumbeat-pack-mail-check/drumpack.md``, section
    "Every timestamp this pack returns is UTC"), which is re-read verbatim
    on every run that declares the tool. What stays here is only what is
    true regardless of which packs exist: what time it is, in which zone,
    and its UTC equivalent.
    """
    local_now = datetime.now().astimezone()
    utc_now = datetime.now(UTC)
    tz_name = capabilities.server_timezone()
    return (
        "[drumbeat] Current date/time: "
        f"{local_now.strftime('%Y-%m-%d %H:%M:%S')} {tz_name} "
        f"(= {utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC)."
    )


def _prepend_now_context(text: str) -> str:
    """Prefix ``text`` with a fresh ``_now_context_line()``, same
    ``\\n\\n---\\n\\n`` separator convention already used to prepend the open
    items ledger onto a reply's turn text (see ``resume_turn``).
    """
    return f"{_now_context_line()}\n\n---\n\n{text}"


def _sanitize_run_id(run_id: str) -> str:
    """Defensive filesystem-safety pass — no ':' (Windows), no path separators."""
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", run_id)
    if not sanitized:
        raise RunnerError(f"run_id {run_id!r} sanitizes to empty string")
    return sanitized


class _SessionProbe(Enum):
    """Result of checking whether a pinned session id resolves to a real session."""

    EXISTS = "exists"
    MISSING = "missing"
    # Fix 3: the amplifier-agent WORKSPACE this session lives under could not
    # be confirmed for the current cwd -- either the recorded
    # ``session_workspace`` no longer matches what the current cwd derives
    # to, or (legacy automations with no recorded workspace) the workspace
    # directory itself does not exist at all. Both mean the project
    # directory was very likely renamed/moved, NOT that the session was
    # deleted -- treated as an abort, never as "safe to recreate".
    WORKSPACE_MISMATCH = "workspace_mismatch"
    UNKNOWN = "unknown"


def _amplifier_agent_home() -> Path:
    """Thin wrapper -- see ``drumbeat.paths.amplifier_agent_home`` for the real
    implementation and its docstring. Kept as a private name here so every
    existing call site in this module (``_session_dir``, ``_probe_session``,
    ...) is untouched by the move (same pattern as ``_derive_workspace_slug``
    delegating to ``paths.derive_workspace_slug``).
    """
    return amplifier_agent_home()


def _derive_workspace_slug(cwd: Path) -> str:
    """Thin wrapper -- see ``drumbeat.paths.derive_workspace_slug`` for the
    real implementation and its docstring. Kept as a private name here so
    every existing call site in this module (``_session_dir``,
    ``_probe_session``, ...) is untouched by the move.
    """
    return derive_workspace_slug(cwd)


def _session_dir(session_id: str, *, cwd: Path) -> Path:
    workspace = _derive_workspace_slug(cwd)
    return (
        _amplifier_agent_home()
        / "state"
        / "workspaces"
        / workspace
        / "sessions"
        / session_id
    )


def _probe_session(
    session_id: str, *, cwd: Path, recorded_workspace: str | None
) -> tuple[_SessionProbe, str]:
    """Determine whether a pinned session id resolves to a real, resumable session.

    Checked in this order:

    1. Fix 3 -- direct detection: if ``recorded_workspace`` (the workspace
       slug this session was pinned under, recorded in the automation's
       frontmatter -- see ``automation.write_session_id``) is set and
       differs from the workspace slug the CURRENT ``cwd`` derives to,
       that is unambiguous: the project directory has been renamed or
       moved since this session was pinned. ``WORKSPACE_MISMATCH`` --
       never treated as "safe to recreate", because doing so would
       silently abandon everything under the OLD workspace slug, which
       nothing will ever probe again.
    2. Fix 3 -- fallback heuristic for automations pinned before this field
       existed (``recorded_workspace is None``) or that happen to match:
       if the CURRENT workspace directory does not exist AT ALL, that is
       the same "moved/renamed" signal, inferred rather than recorded --
       also ``WORKSPACE_MISMATCH``.
    3. Only once the workspace itself is confirmed current: a confirmed-
       absent SESSION directory is ``MISSING`` (safe to recreate) --
       this is the ordinary, expected "session was deleted" case.

    Any I/O error while checking, or a session directory present without
    its transcript, is ``UNKNOWN`` -- the caller must abort rather than
    guess. This is the "defend the pin" requirement: a silently-recreated
    session here is the silent-drop defect this whole mechanism exists to
    fix, re-armed.
    """
    current_workspace = _derive_workspace_slug(cwd)
    if recorded_workspace is not None and recorded_workspace != current_workspace:
        return (
            _SessionProbe.WORKSPACE_MISMATCH,
            (
                f"automation was pinned under workspace {recorded_workspace!r} but "
                f"the current working directory ({cwd}) derives workspace "
                f"{current_workspace!r} -- the project directory was very likely "
                "renamed or moved"
            ),
        )

    workspace_dir = _amplifier_agent_home() / "state" / "workspaces" / current_workspace
    try:
        workspace_exists = workspace_dir.is_dir()
    except OSError as exc:
        return (
            _SessionProbe.UNKNOWN,
            f"cannot stat workspace directory {workspace_dir}: {exc}",
        )
    if not workspace_exists:
        return (
            _SessionProbe.WORKSPACE_MISMATCH,
            (
                f"workspace directory does not exist at all: {workspace_dir} -- "
                "the project directory was very likely renamed or moved (this "
                "automation has no recorded session_workspace to confirm "
                "directly -- it was pinned before that field existed)"
            ),
        )

    session_dir = _session_dir(session_id, cwd=cwd)
    try:
        dir_exists = session_dir.is_dir()
    except OSError as exc:
        return _SessionProbe.UNKNOWN, f"cannot stat {session_dir}: {exc}"
    if not dir_exists:
        other_sessions_note = ""
        sessions_dir = workspace_dir / "sessions"
        try:
            if sessions_dir.is_dir():
                other_count = sum(1 for _ in sessions_dir.iterdir())
                other_sessions_note = (
                    f" (workspace {workspace_dir} exists and contains "
                    f"{other_count} other session(s))"
                )
        except OSError:
            pass  # best-effort note only; doesn't change the MISSING determination
        return (
            _SessionProbe.MISSING,
            f"no directory at {session_dir}{other_sessions_note}",
        )

    transcript_path = session_dir / "transcript.jsonl"
    try:
        transcript_exists = transcript_path.is_file()
    except OSError as exc:
        return _SessionProbe.UNKNOWN, f"cannot stat {transcript_path}: {exc}"
    if not transcript_exists:
        return (
            _SessionProbe.UNKNOWN,
            f"{session_dir} exists but has no transcript.jsonl (ambiguous state)",
        )
    return _SessionProbe.EXISTS, f"found {transcript_path}"


def _transcript_stats(session_id: str, *, cwd: Path) -> dict[str, int | None]:
    """Best-effort transcript size (bytes, lines) at a point in time.

    Recorded on ``RunResult`` purely for observability -- see run()'s
    docstring -- never acted on by this module. ``None`` fields (not 0)
    when the transcript can't be read, so a missing/unreadable file is
    never confused with a genuinely empty one.
    """
    transcript_path = _session_dir(session_id, cwd=cwd) / "transcript.jsonl"
    try:
        size_bytes = transcript_path.stat().st_size
    except OSError:
        return {"bytes": None, "lines": None}
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = sum(1 for _ in f)
    except OSError:
        lines = None
    return {"bytes": size_bytes, "lines": lines}


# ---- automatic session rotation (see drumbeat.session_health) ----
#
# A rotation policy nobody evaluates is not a policy. These two call sites
# are the whole enforcement mechanism: the drift check below runs before
# the first turn of every resumed run, and the ceiling check at the end of
# run() fires the moment the provider refuses a prompt. Both go through
# this one function so a rotation can never happen without landing in
# runs/session_rotations.jsonl, and never happen silently.


def _auto_rotate(
    automation: Automation,
    *,
    old_session_id: str,
    reason: str,
    runs_dir: Path,
) -> bool:
    """Clear an automation's pinned session and record why. Never raises.

    FAIL LOUD: the stderr line is unconditional, and every outcome --
    success, nothing-to-clear, or failure -- is printed. A rotation that
    silently failed to write would leave a dead session pinned and looking
    healthy, which is strictly worse than not rotating at all.

    Returns:
        True if the pin was actually cleared, False otherwise.
    """
    print(
        f"[{automation.name}] AUTO-ROTATING pinned session {old_session_id!r}: {reason}",
        file=sys.stderr,
    )
    try:
        removed = session_pins.delete(automation.slug, runs_dir=runs_dir)
    except session_pins.PinStoreError as exc:
        print(
            f"[{automation.name}] AUTO-ROTATION FAILED for {old_session_id!r}: {exc} "
            f"-- the dead/stale session is STILL PINNED in "
            f"{session_pins.pins_path(runs_dir)}. Run `drumbeat rotate-session "
            f"{automation.slug} --workspace <dir> [--data-dir <dir>] --reason "
            f"'<why>'` by hand.",
            file=sys.stderr,
        )
        return False
    if removed is None:
        print(
            f"[{automation.name}] auto-rotation found no pinned session to clear "
            f"(expected {old_session_id!r}) -- nothing changed",
            file=sys.stderr,
        )
        return False
    cleared = removed.session_id
    log_session_rotation(
        automation_name=automation.name,
        automation_slug=automation.slug,
        automation_path=automation.path,
        old_session_id=cleared,
        reason=f"auto: {reason}",
        log_path=Path(runs_dir).expanduser() / "session_rotations.jsonl",
    )
    session_health.forget_contract(cleared, runs_dir=runs_dir)
    # Same fact, second home: session_rotations.jsonl is the human log, the
    # outbox event is the machine-readable one a consumer can act on without
    # tailing a second file. Not fsync'd (rotations are not deliveries) and
    # never fatal -- a rotation that happened must not be undone by a
    # bookkeeping failure.
    try:
        engine_events.append_event(
            runs_dir,
            engine_events.EventType.SESSION_ROTATED,
            {
                "automation": automation.name,
                "automation_slug": automation.slug,
                "old_session_id": cleared,
                "reason": f"auto: {reason}",
            },
        )
    except (engine_events.OutboxError, OSError) as exc:
        print(
            f"[{automation.name}] auto-rotation recorded on disk but the "
            f"session_rotated event could not be emitted: {exc}",
            file=sys.stderr,
        )
    print(
        f"[{automation.name}] auto-rotated: {cleared!r} unpinned and recorded in "
        f"{Path(runs_dir).expanduser() / 'session_rotations.jsonl'}. The transcript "
        "is left on disk untouched; the next run starts a fresh session and is "
        "re-seeded with the guidance files and the full open-items ledger.",
        file=sys.stderr,
    )
    return True


def _conversation_rotation_reason(
    automation: Automation,
    *,
    session_id: str,
    runs_dir: Path,
) -> str | None:
    """Why (if at all) this automation's ``conversation:`` lifecycle rotates now.

    Evaluated on a resumed run whose pinned session PROBES AS EXISTING, right
    beside the contract-drift check, and rotates through the exact same
    flock-guarded path (``_auto_rotate``). Returns a human-readable reason when
    the lifecycle demands a fresh conversation this run, or ``None`` to leave
    the pinned session resuming as-is.

    - ``continuous`` (default): never returns a reason -- returns ``None``
      before any clock read or store I/O, so a run with no ``conversation:``
      key behaves byte-for-byte as it did before this field existed. The only
      thing that abandons a continuous conversation is a health signal.
    - ``fresh``: always returns a reason. Every run leaves the last one behind.
    - ``daily``: returns a reason only on the first run whose HOST-LOCAL
      calendar day is later than the day the pinned session was anchored to.
      An unrecorded anchor (a session predating this feature, or a lifecycle
      write that failed) yields ``None`` -- "unknown" must not read as "yes"
      and silently abandon a live conversation, the same posture the drift
      check takes.
    """
    mode = automation.conversation
    if mode == "continuous":
        return None
    if mode == "fresh":
        return "conversation: fresh -- a new conversation is started on every run"
    if mode == "daily":
        anchor = session_health.read_lifecycle_anchor_day(session_id, runs_dir=runs_dir)
        if anchor is None:
            return None
        try:
            anchor_day = date.fromisoformat(anchor)
        except ValueError:
            # A malformed anchor is a store we cannot trust for THIS decision;
            # do not abandon the conversation on unreadable data.
            return None
        today = _local_now().date()
        if today > anchor_day:
            return (
                f"conversation: daily -- session anchored to {anchor_day.isoformat()}, "
                f"host-local day is now {today.isoformat()} (first run after "
                "local midnight)"
            )
        return None
    # Unreachable: the parser guarantees a closed vocabulary. Fail safe (no
    # rotation) rather than guess if a future value ever slips through.
    return None


def _build_command(
    *,
    session_id: str,
    fresh: bool,
    cwd: Path,
    text: str,
    ndjson: bool = False,
    host_config_path: Path | None = None,
) -> list[str]:
    cmd = [
        # SIBLING FIRST, then turn PATH -- the absolute path of the agent
        # co-installed into this engine's own tool venv (see
        # ``_resolve_agent_command``). An absolute argv[0] still carries the
        # ``amplifier-agent run --session-id`` substring drain/staleness match
        # against (see drain.AGENT_TURN_MARKER), so process detection is
        # unaffected. Falls back to the bare name only when nothing resolves,
        # so a genuinely-missing agent still raises the FileNotFoundError that
        # ``_execute_turn`` turns into a persisted, hinted run failure.
        _resolve_agent_command(Path(cwd)),
        "run",
        "--session-id",
        session_id,
        "--fresh" if fresh else "--resume",
        "--output",
        "json",
        "-y",
        "--cwd",
        str(cwd),
    ]
    if host_config_path is not None:
        # Per-turn / per-automation agent config. The host config carries this
        # turn's provider/model selection in amplifier-agent's OWN vocabulary --
        # e.g. its ``provider.config.default_model`` field, resolved from
        # ``--config``. drumbeat picks WHICH config (the layered agent-config
        # merge -- see ``drumbeat.agent_config``); amplifier-agent still does the
        # actual model resolution. Omitted (None) when the merge is empty, so the
        # turn keeps the bundle's default model exactly as before.
        cmd += ["--config", str(host_config_path)]
    if ndjson:
        # Chat/reply live-progress callers only (see ``_execute_turn``'s
        # ``progress_callback`` parameter): swaps amplifier-agent's stderr
        # display from human-readable text to one JSON-RPC notification per
        # line (`{"method": ..., "params": ...}`), independent of `--output
        # json` above, which governs stdout and is unaffected either way.
        # Scheduled automation runs never pass this -- their stderr.log
        # keeps its existing human-readable shape.
        cmd += ["--display", "ndjson"]
    # Belt: the turn text becomes a SINGLE argv element. If it reaches
    # MAX_ARG_STRLEN the OS rejects the spawn with an opaque E2BIG before the
    # agent boots (see the constant's note). Fail loud here, named, at the one
    # point every turn's argv is actually constructed -- so reference-form
    # guidance's whole promise (argv stays tiny) is enforced, not merely
    # intended, and any legacy inline turn that would breach is caught with a
    # remedy rather than a kernel errno. ``_execute_turn`` converts this into a
    # persisted run failure; a dry-run surfaces it directly.
    nbytes = len(text.encode("utf-8"))
    if nbytes >= MAX_ARG_STRLEN:
        raise TurnTooLargeError(nbytes=nbytes)
    # ``--`` terminates amplifier-agent's own option parsing so a prompt that
    # BEGINS with ``-`` is never mistaken for a flag. A turn-context injector
    # block leads with ``--- <label> ---`` (see injectors.collect_preamble), so
    # any interactive turn that carries an injector produced a prompt starting
    # with ``---`` that amplifier-agent's CLI parsed as an unknown option
    # ("Error: No such option '--- ...'", exit 2) -- the whole injector feature
    # was dead on the interactive-turn path. Guard EVERY prompt, not just
    # injector-led ones: any user/reply text may legitimately start with ``-``.
    # Verified against amplifier-agent run: ``-- <prompt>`` treats the prompt as
    # the positional PROMPT even when it begins with ``---``.
    cmd.append("--")
    cmd.append(text)
    return cmd


def _format_command(cmd: list[str]) -> str:
    return shlex.join(cmd)


def _pump_stream(
    stream,
    sink: list[str],
    *,
    echo_to: TextIO | None,
    on_line: Callable[[str], None] | None = None,
) -> None:
    """Read a subprocess stream line-by-line into sink, optionally echoing live
    and/or forwarding each line to ``on_line`` (used to parse live NDJSON
    progress off stderr -- see ``_TurnProgressTracker``). ``on_line`` must
    never raise; a broken progress sink must not interrupt draining the
    subprocess's actual stdout/stderr.
    """
    for line in stream:
        sink.append(line)
        if echo_to is not None:
            echo_to.write(line)
            echo_to.flush()
        if on_line is not None:
            try:
                on_line(line)
            except Exception as exc:  # noqa: BLE001 - progress parsing is best-effort only
                sys.stderr.write(
                    f"[runner] progress parsing failed for one line: {exc}\n"
                )


# ---- live progress (Change: chat "what is it doing right now") ----
#
# amplifier-agent already emits a stream of NDJSON events on stderr under
# `--display ndjson` (tool/started, tool/completed, thinking/delta,
# thinking/final, progress, result/delta, result/final, usage, error --
# the 9-type canonical taxonomy). Nothing previously consumed this stream
# for the engine's own turns; `_execute_turn` parsed only the single stdout
# JSON envelope. This section surfaces the stream, translated into
# human-safe phrases, for chat/reply turns only (see
# ``_execute_turn``'s ``progress_callback`` parameter) -- scheduled
# automation runs are untouched.


@dataclass(frozen=True)
class ProgressEvent:
    """One human-relevant tick of progress from a live, in-flight turn.

    ``step`` is a monotonically increasing count of every recognized
    NDJSON event line for this turn so far -- not just the ones that
    change ``activity`` -- so a caller can tell the turn is still alive
    even between activity changes (e.g. while token-by-token
    ``result/delta`` chunks stream in). ``activity`` is always safe,
    human-readable phrasing -- never a raw tool argument, session id, or
    command line (see ``_translate_tool_activity``). ``tool`` is the bare
    tool name (e.g. ``"bash"``), also always safe to display, or ``None``
    when the current activity isn't tool-shaped (e.g. "Thinking…").
    """

    step: int
    activity: str
    tool: str | None


ProgressCallback = Callable[[ProgressEvent], None]

# The fixed 9-type taxonomy amplifier-agent's NDJSON stream is contractually
# limited to (adapters translate; they don't invent new types). Anything
# else is an event type this module doesn't know about yet -- dropped
# rather than guessed at.
_CANONICAL_NDJSON_METHODS = frozenset(
    {
        "result/delta",
        "result/final",
        "tool/started",
        "tool/completed",
        "progress",
        "thinking/delta",
        "thinking/final",
        "usage",
        "error",
    }
)

# Generic built-in agent tools that may appear during identity/requirements
# turns (see run_chat_message). These are the ENGINE's own tools, generic to
# every consumer -- not any drumpack's CLIs -- so their phrasing lives here.
# Consumer-tool phrasing, by contrast, is NOT hardcoded in the engine: each
# drumpack declares an `activity:` map for its own tools' subcommands
# (see ``drumbeat.packs`` and ``_bash_activity`` below). Mechanism, not policy.
_GENERIC_TOOL_ACTIVITY: dict[str, str] = {
    "read_file": "Reading a file…",
    "write_file": "Writing a file…",
    "edit_file": "Editing a file…",
    "grep": "Searching files…",
    "glob": "Searching for files…",
    "web_fetch": "Fetching a web page…",
    "web_search": "Searching the web…",
}


def _bash_activity(
    command: str, activity_by_tool: dict[str, dict[str, str]] | None
) -> str:
    """Translate a bash tool's raw command into a human-safe activity phrase.

    NEVER returns any part of ``command`` itself -- only a phrase a drumpack
    declared for that program's subcommand (``activity_by_tool``, built from
    the loaded drumpacks' ``activity:`` maps), or a generic fallback. This is
    the one place a raw session id, chat id, or full command line could
    otherwise leak into the chat UI; every return path here is a fixed literal
    or a consumer-authored label, never any part of ``command``.

    The engine hardcodes NO consumer vocabulary: a drumpack owns the phrasing
    for its own tools. An undeclared tool (or an undeclared subcommand) gets
    the generic "Running a command…".
    """
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        parts = command.strip().split()
    if not parts:
        return "Running a command…"
    program = Path(parts[0]).name  # strip any bin/ path prefix
    subcommand = parts[1] if len(parts) > 1 else None

    if subcommand and activity_by_tool:
        labels = activity_by_tool.get(program)
        if labels:
            phrase = labels.get(subcommand)
            if phrase:
                return phrase
    return "Running a command…"


def _translate_tool_activity(
    name: str,
    args: dict[str, Any],
    activity_by_tool: dict[str, dict[str, str]] | None = None,
) -> str:
    """Human-readable present-tense phrase for one ``tool/started`` event.

    ``name``/``args`` are the raw NDJSON fields (amplifier-agent's
    ``ToolStartedNotification``: ``name`` + ``args`` dict). Never echoes
    any part of ``args`` into the returned phrase -- falls back to the
    plain tool name when nothing more specific is known, per the "never
    render a raw session id, chat id, or full command line" requirement.

    ``activity_by_tool`` carries the loaded drumpacks' consumer-declared
    narration (program name -> {subcommand: label}); it is consulted only for
    the bash tool, where a drumpack CLI runs.
    """
    if name == "bash":
        command = args.get("command")
        if isinstance(command, str) and command.strip():
            return _bash_activity(command, activity_by_tool)
        return "Running a command…"
    return _GENERIC_TOOL_ACTIVITY.get(name, f"Using {name}…")


def _load_activity_labels(cwd: Path) -> dict[str, dict[str, str]]:
    """Build {program -> {subcommand: label}} from the workspace's drumpacks.

    Best-effort, and deliberately so: progress narration is live-UI sugar for
    a turn whose subprocess is ALREADY running, and the authoritative fail-loud
    drumpack load already happened this same turn (``_turn_env`` -> ``turn_path``,
    and the requirements gate before it). A drumpack list that breaks between
    then and now degrades narration to generic phrasing rather than killing an
    in-flight turn -- the one place in this module a load failure is swallowed,
    and only because raising here cannot help a process already spawned.
    """
    try:
        loaded = packs.load_workspace_packs(cwd)
    except packs.PackError:
        return {}
    return packs.activity_by_tool(loaded)


class _TurnProgressTracker:
    """Accumulates one turn's NDJSON stderr lines into ``ProgressEvent``s and
    forwards each recognized one to a caller-supplied callback.

    ``step`` bumps on every recognized event line, including ones (e.g.
    ``usage``, ``result/delta``) that don't change ``activity`` -- this is
    the "elapsed time / step count is the only honest signal" mechanism:
    a client watching ``step`` can tell the turn is still alive even
    during a long tool call or a quiet thinking stretch, without this
    module ever fabricating a fake activity description.
    """

    def __init__(
        self,
        on_progress: ProgressCallback,
        activity_by_tool: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._on_progress = on_progress
        # Loaded drumpacks' consumer-declared narration: program name ->
        # {subcommand: label}. None/empty when no drumpack declares an
        # `activity:` map -- narration then falls back to generic phrases.
        self._activity_by_tool = activity_by_tool
        self._step = 0
        self._activity = "Working…"
        self._tool: str | None = None

    def observe_line(self, line: str) -> None:
        """Parse one stderr line; forward a ProgressEvent if it's a
        recognized NDJSON event. Never raises -- an unparseable line
        (blank, or genuinely not JSON) is silently ignored, exactly as
        stray non-protocol stderr output always has been.
        """
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(envelope, dict):
            return
        method = envelope.get("method")
        params = envelope.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method not in _CANONICAL_NDJSON_METHODS:
            return  # unrecognized event type -- don't count it, don't guess

        self._step += 1
        if method == "tool/started":
            name = params.get("name")
            if isinstance(name, str) and name:
                self._activity = _translate_tool_activity(
                    name, params.get("args") or {}, self._activity_by_tool
                )
                self._tool = name
        elif method in ("thinking/delta", "thinking/final"):
            self._activity = "Thinking…"
            self._tool = None
        elif method == "error":
            message = params.get("message")
            self._activity = f"Error: {message}" if message else "An error occurred"
            self._tool = None
        # tool/completed, result/delta, result/final, progress, usage:
        # step already bumped above; activity intentionally unchanged --
        # these don't describe a NEW activity, they close out or narrate
        # the one already reported.

        self._on_progress(
            ProgressEvent(step=self._step, activity=self._activity, tool=self._tool)
        )


# The connector CLIs (`mail-check`, `session-list`) live in the project's own bin/.
# The agent reaches them through its bash tool, which inherits OUR environment.
# If bin/ is not on PATH they simply do not exist for the agent -- and it will
# dutifully report "I could not do X" while the real cause is our launcher.
# Relying on whoever started the process to have exported PATH is exactly the
# silent-failure class this project keeps getting bitten by, so make it
# structural instead of incidental.
#
# DRUMBEAT_TURN_SESSION_ID (reply-on-item, item routing): the same mechanism
# now also carries this turn's OWN session id down to the agent's bash tool --
# and from there, transparently, to any consumer CLI the agent invokes (it
# inherits the same environment, exactly like PATH above). A consumer's
# ledger CLI reads this to stamp a `session_id` onto an item AT CREATION ONLY
# (never touched again, same discipline as `first_seen_at`) -- the
# mechanically true identity of whichever pinned session first reported it,
# with no string-matching or "source" inference involved. This is what makes
# reply-on-item safe: a reply resumes exactly the session that created the
# item, never a guess.
#
# PATH construction moved to `drumbeat.packs` at decomposition step 4 (see
# docs/ARCHITECTURE.md). What
# changed and what did not:
#
#   before: workspace bin/ prepended to whatever PATH this process happened
#           to inherit -- so what an agent could reach depended on who
#           started the service (the class-15 defect as it actually occurred,
#           twice: bb39f9e, 059099d).
#   after:  declared pack bin/ dirs + workspace bin/ prepended to a PINNED
#           base, captured once per process, logged at startup, and echoed in
#           /api/capabilities. Identical for every turn regardless of launch
#           path (section 7.2 item 2).
#
# `packs.PackError` is deliberately NOT caught here. A workspace whose pack
# list is broken must fail the run loudly at the requirements gate -- not
# quietly resolve tools off a degraded PATH and let the agent report "I could
# not do that" while the real cause is our loader.
TURN_SESSION_ID_ENV_VAR = "DRUMBEAT_TURN_SESSION_ID"
# DRUMBEAT_DATA_DIR: the engine's resolved
# data dir, exported into every turn so consumer CLIs the agent (or an inject:
# spec) invokes can find the engine-adjacent state store WITHOUT resolving it
# from the turn's cwd. The defect this closes: the consumer's ledger tools defaulted
# their store to ./runs -- correct for years only because the workspace and the
# data dir happened to share a root. The moment --workspace points at a policy
# checkout while --data-dir stays behind, a cwd-relative
# default silently reads an EMPTY store (inject-turn: false INJECT_IDLE over
# ~28 open items) and every in-turn write mints a shadow ledger inside the
# policy tree -- .gitignore'd, so invisible in git status too. Same seam and
# same expand/contract discipline as the session-id var above; the value is one
# the engine already owns. DO-NOT-SWEEP (design section 6): no rename or
# cleanup pass touches this var -- it is the ledger's address, and grazing it
# re-creates the fifth wall.
#
# Sixth wall: the engine's own log modules (error_log, rotation_log) joined
# the same resolution order, so the var's single definition moved to
# error_log (the lowest-level module in the chain -- no import cycle).
# Re-exported here because this module is where turn-env consumers look.
DATA_DIR_ENV_VAR = error_log.DATA_DIR_ENV_VAR


def _turn_env(
    cwd: Path,
    *,
    runs_dir: Path,
    session_id: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    if session_id:
        env[TURN_SESSION_ID_ENV_VAR] = session_id
    # Required, not optional, and resolved absolute: a turn env without the
    # data-dir address is exactly the state the fifth wall lived in, so the
    # signature refuses to let a call site forget it.
    env[DATA_DIR_ENV_VAR] = str(Path(runs_dir).expanduser().resolve())
    env["PATH"] = packs.turn_path(cwd)
    return env


# ---- requirements gate (Fix 2) ----
#
# `automation.requires` used to be parsed and shape-validated, then never
# consulted for meaning -- an automation could list `guidance/ATTENTION.md`
# and nothing would load it, or forget to list it and nothing would notice.
# Everything below is mechanical: no LLM call decides whether a requirement
# is satisfied, only local filesystem/PATH inspection. See ``run()`` for how
# an unsatisfied requirement aborts the run rather than proceeding degraded.


@dataclass(frozen=True)
class RequirementCheck:
    """Mechanical verification result for one ``automation.requires`` entry."""

    item: str
    kind: str  # "file" | "executable"
    satisfied: bool
    detail: str  # resolved path / which() result on success, reason on failure
    content: str | None = (
        None  # file content, only set when kind == "file" and satisfied
    )


def _classify_requirement(item: str) -> str:
    """'file' for anything path- or filename-shaped ('/' or a '.' present), else 'executable'."""
    return "file" if ("/" in item or "." in item) else "executable"


def check_requirements(
    requires: list[str], *, cwd: Path, runs_dir: Path
) -> list[RequirementCheck]:
    """Mechanically verify every ``automation.requires`` entry. No LLM involved.

    A file-shaped entry (e.g. ``guidance/TEAMS.md``, resolved relative to
    ``cwd``) must exist and be non-empty; its content is captured here for
    injection into the session (see ``format_requirements_turn``). An
    executable-shaped entry (e.g. ``mail-check``, ``session-list``) must resolve on
    the exact PATH the turn itself will use (``_turn_env``, which adds
    ``cwd/bin``) -- checking against our own inherited PATH would pass even
    when the agent's own bash tool can't actually find the binary.

    ``runs_dir`` exists only to build that exact turn env (the env now
    carries ``DRUMBEAT_DATA_DIR`` -- see ``_turn_env``); this function never
    reads or writes anything under it.

    Every entry is checked regardless of earlier failures, so the caller can
    report everything that's wrong in one shot rather than one-at-a-time.
    """
    cwd = Path(cwd).expanduser()
    env = _turn_env(cwd, runs_dir=runs_dir)
    checks: list[RequirementCheck] = []
    for item in requires:
        kind = _classify_requirement(item)
        if kind == "file":
            path = (cwd / item).resolve()
            if not path.is_file():
                checks.append(
                    RequirementCheck(item, kind, False, f"file not found: {path}")
                )
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                checks.append(
                    RequirementCheck(item, kind, False, f"cannot read {path}: {exc}")
                )
                continue
            if not content.strip():
                checks.append(
                    RequirementCheck(item, kind, False, f"file is empty: {path}")
                )
                continue
            checks.append(
                RequirementCheck(item, kind, True, str(path), content=content)
            )
        else:
            resolved = shutil.which(item, path=env.get("PATH"))
            if resolved is None:
                checks.append(
                    RequirementCheck(
                        item, kind, False, f"executable not found on PATH: {item!r}"
                    )
                )
            else:
                checks.append(RequirementCheck(item, kind, True, resolved))
    return checks


def resolve_requirement_cards(
    checks: list[RequirementCheck], *, cwd: Path
) -> list[packs.Pack]:
    """The pack cards this automation's `requires:` executables entitle it to.

    Section 7.2 item 3: "for each automation, the engine resolves every
    executable in `requires:` to its owning pack and injects that pack's
    card, verbatim, alongside guidance." No new `requires:` syntax is needed
    -- naming the tool IS the request for its documentation, which is what
    makes the card structurally inseparable from the binary.

    A required executable that no pack provides (`gh`, the consumer's own
    `bin/` tools) simply contributes no card. That is not a failure: the
    workspace `bin/` is on the turn PATH by contract. The failure case --
    a required tool that resolves NOWHERE -- was already caught by
    ``check_requirements`` before this is ever reached.
    """
    tools = [c.item for c in checks if c.kind == "executable"]
    if not tools:
        return []
    return packs.cards_for_tools(packs.load_workspace_packs(cwd), tools)


def format_requirements_turn(
    checks: list[RequirementCheck],
    cards: list[packs.Pack] | None = None,
    *,
    mode: str = DEFAULT_GUIDANCE_DELIVERY,
) -> str | None:
    """Build the turn text carrying required guidance and pack cards.

    ``mode`` controls how the file-kind ``requires:`` guidance reaches the
    agent (see ``automation.VALID_GUIDANCE_DELIVERY``):

    * ``"reference"`` (default, preferred): inject only the workspace-relative
      guidance PATHS plus a mandatory "read these first" preamble. The bodies
      are NOT embedded, so the turn text -- and therefore argv -- stays a few
      hundred bytes regardless of how large the guidance is. This is what
      kills the argv-inlining failure class at the root (a single argv element
      over Linux's 128 KiB ``MAX_ARG_STRLEN`` fails ``execve`` with E2BIG,
      silently, before the agent ever boots -- exactly how channels-check
      died). ``check_requirements`` has already read each file to fail loud on
      a missing/empty one BEFORE this is reached, so referencing (rather than
      inlining) loses none of that guarantee; the agent loads the current body
      itself with its file tools, which every turn has.
    * ``"inline"`` (legacy): embed each guidance body verbatim, as the engine
      always did. Kept working during migration. Subject to the runner's
      turn-size belt guard, which fails loud (named) before the kernel's opaque
      E2BIG would.

    Pack cards ride inline in BOTH modes -- they are pack documentation, not
    workspace files the agent can read by relative path, and have never been
    the argv-size culprit.

    Returns None only when there is genuinely nothing to inject -- no
    file-kind requirements AND no pack cards. Only ever called after every
    check has been confirmed satisfied -- an unsatisfied requirement aborts
    the run before this is reached (see ``run()``).
    """
    if mode not in VALID_GUIDANCE_DELIVERY:
        # Fail loud rather than silently defaulting: a typo'd mode that quietly
        # inlined 200 KiB of guidance would resurrect the exact E2BIG class
        # this parameter exists to prevent.
        raise RunnerError(
            f"format_requirements_turn mode must be one of "
            f"{sorted(VALID_GUIDANCE_DELIVERY)}, got {mode!r}"
        )
    file_checks = [c for c in checks if c.kind == "file" and c.content is not None]
    cards = cards or []
    if not file_checks and not cards:
        return None
    parts: list[str] = []

    if cards:
        parts.append(
            "The following are the tool cards for the executables this "
            "automation requires -- the documentation that travels with each "
            "tool, provided verbatim below. Each card describes what its tools "
            "can do, how they are invoked, the quirks that are not discoverable "
            "from `--help`, and what they deliberately cannot do. Read them "
            "before concluding a tool cannot do something.\n"
        )
        for pack in cards:
            parts.append(
                f"--- tool card: {pack.name} "
                f"(provides: {', '.join(pack.tools)}) ---\n"
                f"{pack.card.strip()}\n"
            )

    if file_checks and mode == "inline":
        parts.append(
            "The following files are this automation's required guidance, "
            "listed under `requires:` and provided verbatim below. Treat "
            "this content as authoritative and current for this run -- it "
            "may have changed since any earlier run or turn in this "
            "conversation.\n"
        )
        for c in file_checks:
            content = c.content or ""  # narrowed by the file_checks filter above
            parts.append(f"--- {c.item} ---\n{content.rstrip()}\n")
    elif file_checks:  # mode == "reference"
        # Paths, not bodies. The agent reads each file itself with its file
        # tools, resolving the relative path against its cwd (the workspace) --
        # the same base ``check_requirements`` used to verify them. The bodies
        # were verified present and non-empty at the gate; here we only name
        # them, so argv never carries their weight.
        listing = "\n".join(f"- {c.item}" for c in file_checks)
        parts.append(
            "The following files are this automation's required guidance, "
            "listed under `requires:`. Their contents are NOT included in this "
            "message. Before you do anything else this turn -- before step 1 -- "
            "you MUST read EVERY file listed below IN FULL using your file "
            "tools (e.g. read_file), resolving each path relative to your "
            "current working directory. Treat their contents as authoritative "
            "and current for this run -- they may have changed since any "
            "earlier run or turn in this conversation. Do not proceed until you "
            "have read them all.\n\n"
            "Required guidance files:\n"
            f"{listing}\n"
        )

    return "\n".join(parts)


def _unsatisfied_requirements_message(checks: list[RequirementCheck]) -> str:
    unsatisfied = [c for c in checks if not c.satisfied]
    details = "; ".join(f"{c.item} ({c.detail})" for c in unsatisfied)
    return f"unsatisfied requirements, refusing to run degraded: {details}"


# ---- `inject:` -- durable consumer state reaches every run, mechanically ----
#
# docs/ARCHITECTURE.md section 6, the hybrid-sentinel contract (council
# blocker B1, resolved 6-0). The engine executes each declared inject argv
# before step 1 of every run and classifies the result in FIXED order:
# timeout -> exit code -> stdout.
#
#   timeout                      -> abort the run, voiced (never silent)
#   non-zero exit                -> abort the run, voiced
#   exit 0 + stdout == INJECT_IDLE (whole, stripped, byte-exact)
#                                -> inject no turn; run proceeds; a reasoned,
#                                   actor-attributed inject_skipped event is
#                                   written -- the skip is a record, never an
#                                   inference
#   exit 0 + bare-empty stdout   -> abort, loud. A tool with nothing to say
#                                   must say INJECT_IDLE; silence is never a
#                                   contract value -- a crashed pipe and an
#                                   idle ledger must not share an observable
#   anything else                -> inject stdout verbatim as a turn
#
# Sentinel match is byte-exact on the whole stripped stdout -- not a prefix,
# not a regex: the `^\s*URGENT:` anchor that could not match what an agent
# actually writes is the precedent this pins against. The token is
# INJECT_IDLE, minted distinct from NOTHING_TO_REPORT on purpose: that one
# is a value the AGENT emits inside turns; a tool-side sentinel sharing it
# would invite one channel's value to be read by the other's parser
# (semantic overload -- this project's worst bug class).

INJECT_IDLE = "INJECT_IDLE"

# "Short timeout" (section 7.1): inject tools are local state reads (the
# exemplar, a ledger's `inject-turn`, is a subsecond store read plus
# interpreter startup). 60s is generous for any honest state read and small
# against the minutes a hung tool would otherwise silently cost every run.
_INJECT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class InjectOutcome:
    """Classified result of one inject tool execution.

    Exactly one of the three shapes: ``abort_reason`` set (abort the run,
    voiced), ``idle`` True (skip, recorded), or ``text`` set (inject
    verbatim). Never more than one.
    """

    abort_reason: str | None = None
    idle: bool = False
    text: str | None = None


def _run_inject_tool(spec: InjectSpec, *, cwd: Path, runs_dir: Path) -> InjectOutcome:
    """Execute one ``inject:`` argv and classify it (timeout -> exit -> stdout).

    Runs with the turn environment (constructed PATH, project bin/
    prepended -- the same environment the agent's own tool calls get), so an
    inject tool resolves exactly like a `requires:` tool does. ``runs_dir``
    rides into that env as ``DRUMBEAT_DATA_DIR`` (fifth wall): the exemplar
    inject tool IS a ledger read (a consumer's `inject-turn`), and a
    cwd-relative store default here read a false-empty ledger the moment the
    workspace moved away from the data dir.
    """
    argv = list(spec.argv)
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INJECT_TIMEOUT_SECONDS,
            cwd=str(cwd),
            env=_turn_env(cwd, runs_dir=runs_dir),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return InjectOutcome(
            abort_reason=(
                f"inject {spec.label!r} ({argv[0]}) timed out after "
                f"{_INJECT_TIMEOUT_SECONDS:.0f}s -- aborting rather than running "
                "without the state this automation declared it needs"
            )
        )
    except OSError as exc:
        # A tool that cannot even be spawned is classified with the same
        # severity as a non-zero exit: abort, voiced.
        return InjectOutcome(
            abort_reason=(
                f"inject {spec.label!r} ({argv[0]}) could not be executed: {exc} "
                "-- aborting rather than running without its declared state"
            )
        )

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-500:]
        return InjectOutcome(
            abort_reason=(
                f"inject {spec.label!r} ({argv[0]}) exited "
                f"{completed.returncode} -- aborting rather than running "
                f"without its declared state. stderr: {stderr_tail or '(empty)'}"
            )
        )

    stdout = completed.stdout or ""
    stripped = stdout.strip()
    if stripped == INJECT_IDLE:
        return InjectOutcome(idle=True)
    if not stripped:
        return InjectOutcome(
            abort_reason=(
                f"inject {spec.label!r} ({argv[0]}) exited 0 with EMPTY stdout "
                f"-- a tool with nothing to say must say {INJECT_IDLE}; silence "
                "is never a contract value (a crashed pipe and an idle source "
                "must not share an observable). Aborting loud."
            )
        )
    return InjectOutcome(text=stdout)


# ---- refusal detection (Fix 4a) ----
#
# A final reply that is itself a statement of inability ("I cannot make
# this determination...") is a run FAILURE, not a notification -- pushing
# it to the user's phone as if it were real signal is worse than silence.
# Deliberately a small, anchored (start-of-reply) pattern set, not a general
# sentiment classifier: it exists to catch the specific, observed failure
# shape (an agent honestly reporting it lacked the guidance it needed), not
# to second-guess every message that happens to contain the word "cannot".
_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^i\s+cannot\s+make\s+this\s+determination", re.IGNORECASE),
    re.compile(
        r"^i\s+(?:cannot|can't|am unable to|'m unable to|was unable to)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^i\s+(?:do not|don't)\s+have\s+(?:the|any|enough)?\s*"
        r"(?:guidance|information|access|context|data)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^i\s+lack\s+(?:the|any)?\s*(?:guidance|information|access|tools)\b",
        re.IGNORECASE,
    ),
    re.compile(r"^unfortunately,?\s+i\s+(?:cannot|can't)\b", re.IGNORECASE),
)

# Fix (2026-08-05): narrowed. A reply that OPENS with one of the patterns
# above used to fail the whole run regardless of what followed -- but some
# of his real open items are things the agent genuinely, correctly cannot
# read (encrypted MSRC mail, Purview-protected content), and an honest
# report that opens "I cannot access the MSRC body" and then goes on to
# report everything else it checked is exactly the behavior this project
# wants, not a refusal. Punishing it teaches the agent to omit the
# disclosure instead. The distinction that matters -- "refused the whole
# task" vs. "reported one obstacle and kept going" -- is measured
# mechanically, no LLM: does anything substantive follow the sentence
# containing the matched phrase? A genuine task refusal IS that sentence,
# nothing more; an obstacle report continues past it.
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")


def _looks_like_refusal(text: str) -> bool:
    """Cheap, mechanical check: is this reply, in its entirety, a refusal/inability statement?

    No LLM call. Matches one of ``_REFUSAL_PATTERNS`` at the start of the
    reply AND requires nothing substantive to follow the sentence carrying
    that phrase -- see the module note above for why a matching OPENER
    with a real report attached (e.g. "I cannot access the MSRC body due
    to encryption. Here's everything else I found: ...") must NOT be
    flagged: that is an honest obstacle disclosure, not a task refusal.
    """
    stripped = text.strip()
    if not stripped:
        return False
    for pattern in _REFUSAL_PATTERNS:
        match = pattern.search(stripped)
        if match is None:
            continue
        remainder = stripped[match.end() :]
        sentence_end = _SENTENCE_END_RE.search(remainder)
        if sentence_end:
            remainder = remainder[sentence_end.end() :]
        if remainder.strip():
            continue  # substantive content follows -- an obstacle report, not a refusal
        return True
    return False


def _invoke_turn(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    on_progress: ProgressCallback | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """Run one subprocess turn.

    Streams the child's stderr through to our stderr live (never swallowed)
    while also capturing it, and captures stdout (the single JSON envelope)
    in full. Both streams are pumped on background threads so neither pipe
    buffer filling up can deadlock the wait.

    ``on_progress``, when given (chat/reply turns only -- see
    ``_execute_turn``), receives a ``ProgressEvent`` for every recognized
    NDJSON line on stderr as it arrives -- the caller's command must
    already include ``--display ndjson`` for this to see anything (see
    ``_build_command``'s ``ndjson`` parameter). Both stderr behaviors
    (live echo to our own stderr, and progress parsing) run together;
    neither replaces the other.

    ``env``, when given, replaces the default subprocess.Popen behavior of
    inheriting our own os.environ verbatim. ``_execute_turn`` always passes
    ``_turn_env(cwd, runs_dir=..., session_id=...)`` -- a freshly-built COPY of os.environ
    per call, never a mutation of the shared global (this function runs
    concurrently from multiple threads -- scheduled runs and interactive
    replies both call it -- so mutating ``os.environ`` in place here would be
    a real cross-thread race; passing a private dict per call has none).

    Returns:
        (returncode, stdout_text, stderr_text, timed_out)
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    tracker = (
        _TurnProgressTracker(on_progress, _load_activity_labels(cwd))
        if on_progress is not None
        else None
    )

    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_thread = threading.Thread(
        target=_pump_stream, args=(proc.stdout, stdout_lines), kwargs={"echo_to": None}
    )
    stderr_thread = threading.Thread(
        target=_pump_stream,
        args=(proc.stderr, stderr_lines),
        kwargs={
            "echo_to": sys.stderr,
            "on_line": tracker.observe_line if tracker is not None else None,
        },
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    proc.stdout.close()
    proc.stderr.close()

    return proc.returncode, "".join(stdout_lines), "".join(stderr_lines), timed_out


# ---- per-session advisory lock (Fix 1) ----
#
# Two OS processes can otherwise resume the SAME pinned session id at the
# same time: the scheduler firing a scheduled run, and notify-serve
# handling an inbound /api/reply or /api/message. amplifier-agent replays
# and rewrites transcript.jsonl on resume, so two concurrent turns on one
# session can interleave writes and corrupt or truncate it -- corrupting
# the very memory the pinned-session mechanism exists to protect. This
# lock is what prevents that: one fcntl.flock per session id, held for
# exactly the duration of one turn's subprocess invocation.

_SESSION_LOCK_POLL_SECONDS = 0.5
_DEFAULT_INTERACTIVE_LOCK_WAIT_SECONDS = 5.0


def _interactive_lock_wait_seconds() -> float:
    """Bounded wait (seconds) an INTERACTIVE caller (reply/message) tolerates
    on session-lock contention before giving up and reporting an honest
    error. Override via ``$DRUMBEAT_SESSION_LOCK_WAIT_SECONDS`` for testing.
    """
    raw = os.environ.get("DRUMBEAT_SESSION_LOCK_WAIT_SECONDS")
    if not raw:
        return _DEFAULT_INTERACTIVE_LOCK_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_INTERACTIVE_LOCK_WAIT_SECONDS
    return value if value > 0 else _DEFAULT_INTERACTIVE_LOCK_WAIT_SECONDS


# Fix (reply-timeout defect): a /api/reply or /api/message turn used to run
# INSIDE the HTTP request/response cycle, so it inherited
# ``_interactive_lock_wait_seconds``'s brief 5s tolerance -- appropriate for
# a synchronous caller, wildly insufficient once a scheduled automation
# (3-14 minutes observed) holds the same pinned session. Now that these
# turns run in a background thread (see ``reply_service``) with no HTTP
# client waiting on this exact call, contention can be tolerated for far
# longer -- long enough to outlast an entire scheduled run.
_DEFAULT_BACKGROUND_LOCK_WAIT_SECONDS = 900.0  # 15 minutes


def background_lock_wait_seconds() -> float:
    """Bounded wait (seconds) a BACKGROUND-THREAD caller (async reply/message
    turns, run by ``reply_service`` after the HTTP handler has already
    returned 202) tolerates on session-lock contention before giving up and
    recording an honest failure. Override via
    ``$DRUMBEAT_BACKGROUND_LOCK_WAIT_SECONDS`` for testing.
    """
    raw = os.environ.get("DRUMBEAT_BACKGROUND_LOCK_WAIT_SECONDS")
    if not raw:
        return _DEFAULT_BACKGROUND_LOCK_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_BACKGROUND_LOCK_WAIT_SECONDS
    return value if value > 0 else _DEFAULT_BACKGROUND_LOCK_WAIT_SECONDS


def _session_lock_dir(runs_dir: Path) -> Path:
    return runs_dir / ".session-locks"


def _session_lock_path(session_id: str, *, runs_dir: Path) -> Path:
    # Defensive filesystem-safety pass, same discipline as _sanitize_run_id --
    # session ids are already slug-shaped in practice, but a lock filename
    # must never depend on that being true.
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "-", session_id) or "unknown-session"
    return _session_lock_dir(runs_dir) / f"{safe_name}.lock"


@contextlib.contextmanager
def _session_lock(session_id: str, *, runs_dir: Path, wait_seconds: float | None):
    """Acquire the per-session advisory lock for the duration of one turn.

    ``wait_seconds=None``: try once, non-blocking. Raises
    ``SessionLockedError`` immediately if another turn currently holds the
    lock -- used by scheduled/manual "run now" invocations (see ``run()``'s
    docstring): skip this occurrence rather than queue behind it or spawn a
    second session.

    ``wait_seconds=<float>``: poll for up to that many seconds before
    giving up and raising ``SessionLockedError`` -- used by interactive
    callers (``resume_turn``/``run_chat_message``) where a human is waiting
    on a reply and a brief wait is worth it before reporting an honest
    error.
    """
    lock_dir = _session_lock_dir(runs_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _session_lock_path(session_id, runs_dir=runs_dir)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    waited = 0.0
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if wait_seconds is None or waited >= wait_seconds:
                    raise SessionLockedError(
                        session_id, waited_seconds=waited
                    ) from None
                sleep_for = min(_SESSION_LOCK_POLL_SECONDS, wait_seconds - waited)
                time.sleep(sleep_for)
                waited += sleep_for
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---- public probes for the turn API (decomposition step 5; docs/ARCHITECTURE.md) ----
#
# ``POST /api/turns`` must answer two questions BEFORE it accepts a turn,
# and both of them are this module's knowledge, not the HTTP layer's:
# "is this a real session?" (404 if not -- never guess) and "is its lock
# held right now?" (423 if the caller did not ask to wait). Exposed as
# named functions rather than letting another module reach for the private
# ``_probe_session``/``_session_lock`` internals.

SessionProbe = _SessionProbe


def probe_session(session_id: str, *, cwd: Path) -> tuple[_SessionProbe, str]:
    """Does ``session_id`` resolve to a real, resumable session under ``cwd``?

    Thin public wrapper over ``_probe_session`` with no recorded workspace
    (an explicit, caller-supplied session id carries no automation
    frontmatter to cross-check against). Returns the probe verdict and a
    human-readable detail string -- the caller decides what to do with a
    non-``EXISTS`` verdict; this never guesses on its behalf.
    """
    return _probe_session(
        session_id, cwd=Path(cwd).expanduser(), recorded_workspace=None
    )


def session_lock_is_held(session_id: str, *, runs_dir: Path) -> bool:
    """Is another in-flight turn currently holding this session's flock?

    A NON-BLOCKING probe that never keeps the lock: it acquires and
    immediately releases, so it cannot itself become the contention it is
    reporting on. ``True`` means "held by somebody else right now".

    Inherently a point-in-time answer -- the lock can be taken in the
    instant after this returns False. That race is harmless by
    construction: the executor that follows re-acquires the lock properly
    (with the caller's own wait budget), so a lost race becomes an ordinary
    wait, never a corrupt interleave. This probe exists to give an honest
    *immediate* 423 to a caller that asked not to wait, nothing more.
    """
    runs_dir = Path(runs_dir).expanduser()
    lock_path = _session_lock_path(session_id, runs_dir=runs_dir)
    if not lock_path.exists():
        return False
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# Stale session-lock files (Fix 2, orphaned-reply defect): ``.session-locks/``
# has accumulated one file per session id ever locked, going back weeks, and
# nothing ever removes them. ``flock(2)`` locks are held by an OPEN FILE
# DESCRIPTION, not recorded in the file's bytes or its directory entry --
# when the process holding one exits (cleanly, killed, or crashed), the
# kernel releases the lock automatically. A ``.lock`` file's mere presence,
# or its age, says nothing about whether anyone currently holds it; a lock
# file from three weeks ago is exactly as "held" or "free" as one created a
# second ago -- it depends entirely on whether a live process's fd still
# references it, which this reaps by testing directly rather than guessing
# from a timestamp.
#
# DECISION: reap, don't just tolerate. These files are provably inert (see
# above -- an old, unheld lock file blocks nothing; a fresh ``os.open`` +
# ``flock(LOCK_EX | LOCK_NB)`` on it succeeds immediately), so leaving them
# alone would never break anything. But this directory is genuinely
# unbounded (one file per session id for the project's entire lifetime,
# never trimmed), so it is reaped at every ``notify-serve``/``serve``
# startup for tidiness -- not because tolerating them is unsafe.
#
# SAFETY (why this can't corrupt a lock a live process depends on): each
# candidate file is opened and an EXCLUSIVE, NON-BLOCKING lock is attempted
# on THIS process's own fd before anything is removed.
#   - If that lock attempt FAILS (``BlockingIOError``): some other process
#     currently holds it -- it is a live, in-use lock, not stale. Skipped,
#     untouched.
#   - If that lock attempt SUCCEEDS: by definition of ``flock``, no other
#     process holds a lock on this inode at this instant. The file is
#     unlinked from the directory *while this process's fd still holds the
#     lock* -- so if another process is, at this exact moment, calling
#     ``open()`` on the same path (racing to acquire it themselves), one of
#     two safe outcomes happens: either their ``open()`` resolves before the
#     ``unlink`` (same inode) and their subsequent ``flock`` call correctly
#     blocks/fails on OUR held lock (ordinary, correctly-reported
#     contention -- not corruption), or their ``open()`` resolves after
#     (fresh inode, since the old path is gone) and they get an uncontended
#     lock on a brand-new file, exactly as if this reap had never run.
#
# CORRECTION (decomposition step 2, pre-step-3 requirement; see
# docs/ARCHITECTURE.md): the original
# version of this note claimed "no interleaving exists" between reaper and
# waiter. That was WRONG: reaper A and reaper B can both open the same path;
# A acquires, unlinks, releases; B then acquires ITS fd (same, now-unlinked
# inode) while a waiter recreates the path -- B believes it validated a lock
# file that no longer exists at that path and could unlink a FRESH file some
# third process just locked-by-path. The fix below revalidates after
# acquisition: fstat the held fd vs stat the path, proceed only on inode
# match. Also note the 10-minute mtime guard is a NO-OP for detecting active
# use -- flock(2) never touches mtime -- it only skips recently-CREATED
# files; kept as cheap caution, not correctness.
_LOCK_REAP_MIN_AGE_SECONDS = 600.0  # 10 minutes -- see docstring below


def reap_stale_session_locks(
    runs_dir: Path, *, min_age_seconds: float = _LOCK_REAP_MIN_AGE_SECONDS
) -> tuple[list[str], list[str]]:
    """Remove session-lock files nobody currently holds; leave active ones alone.

    Call once at the startup of any process that uses ``_session_lock``
    (``notify-serve``, the scheduler) -- see the module-level note above this
    function for the full safety argument. ``min_age_seconds`` is a
    conservative extra margin on top of that argument (skip anything touched
    in the last 10 minutes without even attempting the lock) purely to avoid
    doing needless work on a lock file some other process is actively
    cycling through right now; it is not required for correctness.

    Returns ``(reaped, skipped_active)`` -- the session-id-derived filenames
    (not full paths) removed, and the filenames left alone because another
    process currently holds them. Never raises: a single file's stat/open
    failure is logged to stderr and that file is skipped, never fatal to the
    rest of the sweep.
    """
    lock_dir = _session_lock_dir(Path(runs_dir).expanduser())
    reaped: list[str] = []
    skipped_active: list[str] = []
    if not lock_dir.is_dir():
        return reaped, skipped_active

    now = time.time()
    for entry in sorted(lock_dir.glob("*.lock")):
        try:
            age = now - entry.stat().st_mtime
        except OSError as exc:
            print(f"[runner] lock reap: cannot stat {entry}: {exc}", file=sys.stderr)
            continue
        if age < min_age_seconds:
            continue  # too recent to risk touching; not a correctness requirement, just caution

        try:
            fd = os.open(entry, os.O_RDWR)
        except OSError as exc:
            print(f"[runner] lock reap: cannot open {entry}: {exc}", file=sys.stderr)
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                skipped_active.append(entry.name)
                continue
            try:
                # Revalidate after acquisition (see the CORRECTION note
                # above): the inode this fd holds locked must still be the
                # inode living at the path, or another reaper already
                # unlinked it and the path may now be a fresh file some
                # other process just locked. Proceed only on inode match.
                try:
                    held = os.fstat(fd)
                    on_disk = os.stat(entry)
                except OSError:
                    continue  # path gone or unreadable: nothing safe to do
                if (held.st_dev, held.st_ino) != (on_disk.st_dev, on_disk.st_ino):
                    continue
                entry.unlink()
                reaped.append(entry.name)
            except OSError as exc:
                print(
                    f"[runner] lock reap: cannot unlink {entry}: {exc}", file=sys.stderr
                )
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    return reaped, skipped_active


def _parse_envelope(stdout_text: str) -> dict:
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RunnerError(
            f"malformed JSON envelope on stdout: {exc}\nraw stdout: {stdout_text!r}"
        ) from exc
    if not isinstance(envelope, dict):
        raise RunnerError(
            f"expected a JSON object envelope, got: {type(envelope).__name__}"
        )
    return envelope


def _execute_turn(
    *,
    session_id: str,
    fresh: bool,
    cwd: Path,
    text: str,
    index: int,
    runs_dir: Path,
    wait_seconds: float | None,
    progress_callback: ProgressCallback | None = None,
    host_config_path: Path | None = None,
    preamble_blocks: tuple[str, ...] = (),
) -> tuple[StepResult, str]:
    """Execute one turn and return (StepResult, raw_stderr_text).

    A failed turn still returns a StepResult (with `error` populated) rather
    than raising — the caller decides whether to abort.

    Fix 1: the subprocess invocation happens under this session's advisory
    lock (see ``_session_lock``), held for exactly the duration of this one
    turn -- never across multiple turns of the same run. If the lock can't
    be acquired (another process is mid-turn on this exact session id right
    now), this returns a StepResult with ``.error`` set describing the
    contention, through the EXACT SAME abort path every other turn failure
    already uses -- no new control flow needed at any call site.

    ``wait_seconds=None``: fail immediately on contention (scheduled/manual
    "run now" semantics -- skip this occurrence rather than queue or
    double-run). ``wait_seconds=<float>``: poll for up to that many seconds
    first (interactive reply/message semantics -- worth a brief wait before
    reporting an honest error to a waiting user).

    ``progress_callback``, when given, switches this turn's command to
    ``--display ndjson`` and receives a ``ProgressEvent`` for every
    recognized event as it arrives (see ``_TurnProgressTracker``). Omitted
    by every scheduled-automation call site -- only chat/reply turns
    (``resume_turn``, ``run_chat_message``) ever pass one -- so this is a
    pure addition with no effect on existing automation runs.

    Every call is prefixed with a fresh ``_now_context_line()`` (see that
    function's module note above) -- the single choke point every turn
    already passes through, so no call site (present or future) can forget
    it. ``StepResult.text`` below reflects the combined, actually-sent
    text, same discipline ``resume_turn`` already uses for its ledger
    prefix.
    """
    text = _prepend_now_context(text)
    # Turn-context injectors: prepend each owner-declared preamble block above
    # the now-context and the person's own message. Blocks arrive already
    # rendered and labeled, in policy (file) order; prepending in reverse puts
    # the first-declared block at the very top and leaves the person's message
    # last (most salient). An empty tuple leaves the turn unchanged.
    for block in reversed(preamble_blocks):
        text = f"{block}\n\n{text}"
    try:
        with _session_lock(session_id, runs_dir=runs_dir, wait_seconds=wait_seconds):
            cmd = _build_command(
                session_id=session_id,
                fresh=fresh,
                cwd=cwd,
                text=text,
                ndjson=progress_callback is not None,
                host_config_path=host_config_path,
            )
            returncode, stdout_text, stderr_text, timed_out = _invoke_turn(
                cmd,
                cwd=cwd,
                timeout=_STEP_TIMEOUT_SECONDS,
                on_progress=progress_callback,
                env=_turn_env(
                    cwd,
                    runs_dir=runs_dir,
                    session_id=session_id,
                ),
            )
    except FileNotFoundError as exc:
        # The agent binary itself could not be spawned. Returned as a
        # StepResult with `.error` set -- exactly the shape SessionLockedError
        # already uses below -- so this rides the abort path every other turn
        # failure already takes: the caller sees `result.error`, marks the run
        # failed, and `_persist_run` writes the run record AND emits the
        # automation_error event. Raising here instead (the previous behavior,
        # by omission) skipped every one of those, which is what made a
        # dead engine indistinguishable from a quiet one. See
        # AGENT_INSTALL_HINT above.
        message = f"{exc.strerror or exc}: {exc.filename or _AGENT_COMMAND}\n{AGENT_INSTALL_HINT}"
        print(f"[runner] turn could not be spawned -- {message}", file=sys.stderr)
        ci_events.emit(
            "drumbeat:agent_command_missing",
            {"session_id": session_id, "command": _AGENT_COMMAND},
            cwd=cwd,
        )
        return (
            StepResult(
                index=index,
                text=text,
                reply="",
                error=message,
                duration_ms=0,
                tokens_in=0,
                tokens_out=0,
            ),
            "",
        )
    except TurnTooLargeError as exc:
        # The belt in _build_command tripped: this turn's text would fail
        # execve with E2BIG. Ride the same StepResult-with-.error abort path a
        # spawn failure takes, so the breach lands in the ledger (run record +
        # failures.log line + automation_error event via _persist_run) instead
        # of surfacing as an opaque OSError with no record.
        message = str(exc)
        print(f"[runner] turn too large to spawn -- {message}", file=sys.stderr)
        ci_events.emit(
            "drumbeat:turn_too_large",
            {
                "session_id": session_id,
                "turn_bytes": exc.nbytes,
                "limit_bytes": exc.limit,
            },
            cwd=cwd,
        )
        return (
            StepResult(
                index=index,
                text=text,
                reply="",
                error=message,
                duration_ms=0,
                tokens_in=0,
                tokens_out=0,
            ),
            "",
        )
    except SessionLockedError as exc:
        print(f"[runner] {exc}", file=sys.stderr)
        ci_events.emit(
            "drumbeat:session_lock_contention",
            {
                "session_id": exc.session_id,
                "waited_seconds": exc.waited_seconds,
            },
            cwd=cwd,
        )
        return (
            StepResult(
                index=index,
                text=text,
                reply="",
                error=str(exc),
                duration_ms=0,
                tokens_in=0,
                tokens_out=0,
                error_kind=ERROR_KIND_SESSION_LOCKED,
            ),
            "",
        )

    if timed_out:
        return (
            StepResult(
                index=index,
                text=text,
                reply="",
                error=f"step timed out after {_STEP_TIMEOUT_SECONDS}s",
                duration_ms=_STEP_TIMEOUT_SECONDS * 1000,
                tokens_in=0,
                tokens_out=0,
            ),
            stderr_text,
        )

    if returncode != 0:
        return (
            StepResult(
                index=index,
                text=text,
                reply="",
                error=f"amplifier-agent exited {returncode}",
                duration_ms=0,
                tokens_in=0,
                tokens_out=0,
            ),
            stderr_text,
        )

    try:
        envelope = _parse_envelope(stdout_text)
    except RunnerError as exc:
        return (
            StepResult(
                index=index,
                text=text,
                reply="",
                error=str(exc),
                duration_ms=0,
                tokens_in=0,
                tokens_out=0,
            ),
            stderr_text,
        )

    envelope_error = envelope.get("error")
    metadata = envelope.get("metadata") or {}
    reply = envelope.get("reply") or ""

    return (
        StepResult(
            index=index,
            text=text,
            reply=reply,
            error=str(envelope_error) if envelope_error else None,
            duration_ms=int(metadata.get("durationMs", 0)),
            tokens_in=int(metadata.get("tokensIn", 0)),
            tokens_out=int(metadata.get("tokensOut", 0)),
        ),
        stderr_text,
    )


def new_run_id() -> str:
    """Mint a fresh, filesystem-safe run id (public wrapper for external callers)."""
    return _sanitize_run_id(_run_id_now())


def resume_turn(
    session_id: str,
    text: str,
    *,
    cwd: Path,
    runs_dir: Path,
    wait_seconds: float | None = None,
    progress_callback: ProgressCallback | None = None,
    host_config_path: Path | None = None,
    preamble_blocks: tuple[str, ...] = (),
) -> StepResult:
    """Resume an existing session with one ad-hoc turn (not part of any automation).

    Used by ``reply_service`` to route a reply, or a follow-up message
    naming an existing session, into the correct running conversation.
    Reuses the exact subprocess invocation this module already uses for
    automation steps — there is only one code path that talks to
    ``amplifier-agent``.

    ``wait_seconds``: how long to tolerate session-lock contention before
    giving up (see ``_session_lock``). Defaults to
    ``_interactive_lock_wait_seconds()`` (a brief 5s wait) when omitted.
    ``reply_service`` runs this in a background thread -- with no HTTP
    client waiting on this exact call -- and passes
    ``background_lock_wait_seconds()`` explicitly, so contention with a
    scheduled automation run doesn't fail immediately. Never fabricates a
    reply: on contention that outlasts the wait, or any other turn failure,
    the returned ``StepResult.error`` carries the real reason.

    ``progress_callback``, when given, surfaces live NDJSON progress for
    this turn -- see ``_execute_turn``.

    The open-items ledger prefix that used to ride this call behind an
    ``include_open_items_ledger`` flag is now the CONSUMER's to compose
    (docs/ARCHITECTURE.md; decomposition step 2): the consumer's reply_service builds the ledger text
    itself and passes the already-composed turn ``text`` -- the engine does
    not know what an item is.
    """
    cwd = Path(cwd).expanduser()
    runs_dir = Path(runs_dir).expanduser()
    if wait_seconds is None:
        wait_seconds = _interactive_lock_wait_seconds()

    turn_text = text
    run_id = new_run_id()
    started_at = _iso8601_now()

    result, stderr_text = _execute_turn(
        session_id=session_id,
        fresh=False,
        cwd=cwd,
        text=turn_text,
        index=0,
        runs_dir=runs_dir,
        wait_seconds=wait_seconds,
        progress_callback=progress_callback,
        host_config_path=host_config_path,
        preamble_blocks=preamble_blocks,
    )
    if stderr_text:
        sys.stderr.write(stderr_text)

    # Persist this bare-session REPLY turn as a real run record through the SAME
    # choke point every chat/automation run uses, so a typed reply into an
    # existing session is retrievable exactly like a chat automation's run
    # instead of leaving only a turns/<id>.json the runs API never reads. See
    # _persist_session_turn.
    _persist_session_turn(
        session_id=session_id,
        run_id=run_id,
        started_at=started_at,
        result=result,
        stderr_text=stderr_text,
        runs_dir=runs_dir,
    )
    return result


def start_session(
    session_id: str, text: str, *, cwd: Path, runs_dir: Path
) -> StepResult:
    """Start a brand new session with one ad-hoc turn (``--fresh``).

    Used by the notification service for ``/api/message`` calls that don't
    name an existing session, and by the consumer CLI's ``notify-test`` verb. A brand-new,
    freshly-minted session id is never contended in practice (nothing else
    could know about it yet), so lock contention here fails immediately
    rather than waiting -- it would indicate a real bug, not a race worth
    tolerating.
    """
    cwd = Path(cwd).expanduser()
    runs_dir = Path(runs_dir).expanduser()
    result, stderr_text = _execute_turn(
        session_id=session_id,
        fresh=True,
        cwd=cwd,
        text=text,
        index=0,
        runs_dir=runs_dir,
        wait_seconds=None,
    )
    if stderr_text:
        sys.stderr.write(stderr_text)
    return result


# ---- push demotion (notify: "urgent-only") ----
#
# Measured: channels-check and reconcile together produced 9 notifications,
# 0 replies -- their output is status reporting ("swept 129 channels,
# nothing new", "closed 3 items"), never something the owner needed to answer.
# Their work and their judgment stay identical to notify: "auto" (same
# auto-notify turn, same NOTHING_TO_REPORT convention, same item-ledger
# bookkeeping) -- only the PUSH is withheld by default. Same discipline as
# quiet hours: a plain-text marker the agent writes when it judges this
# specific finding worth a phone interruption, mechanically parsed, never a
# scored gate. Unlike quiet hours (a record with no effect on delivery),
# this marker's absence DOES withhold the push -- but never silently: see
# _record_demotion below and run()'s persisted result.demoted field. A
# demoted run's own artifacts (result.json, step-NN.txt -- see
# _persist_run) are written exactly as they always are, regardless of this
# decision, so nothing is ever lost, only kept off the phone.

# Fix (2026-08-07): the original pattern was `^\s*URGENT:\s*(.+)$`, which
# requires the line to open with a bare, undecorated `URGENT:`. Every reply
# here is markdown, and the agent writes markdown -- so a genuinely urgent
# finding rendered as `**URGENT: ...**` (or `## URGENT: ...`) did not match
# and was silently demoted. Verified against a real withheld push:
# runs/session-growth-check/20260807T195155Z produced
#
#     **URGENT: agent-sessions-check at 41 MB -- 8 MB past proven 33 MB failure point**
#
# and was demoted for "no `URGENT: <reason>` marker" -- the automation did
# its job perfectly and the phone stayed quiet. That is the whole reason
# Session Growth Check had delivered exactly one notification in its
# lifetime while watching a fleet of sessions, one of which had been
# hard-failing every 90 minutes for 27 hours.
#
# The marker is a plain-text convention, so the parse must tolerate the
# plain-text decoration people and models actually write around it:
# optional list-bullet or blockquote/heading prefix, optional emphasis run
# either side of the word. Still anchored to line start and still requiring
# a literal colon -- widening it to a substring search would let any prose
# mention of the word ("this is not urgent:") force a push.
_URGENT_MARKER_RE = re.compile(
    r"^[ \t]*"  # leading indent
    r"(?:[-+*>#]+[ \t]+)?"  # bullet / blockquote / heading prefix ("- ", "## ")
    r"(?:[*_]{1,3})?[ \t]*"  # opening emphasis ("**", "__", "*")
    r"URGENT"
    r"[ \t]*(?:[*_]{1,3})?[ \t]*"  # emphasis closed before the colon
    r":[ \t]*"
    r"(?:[*_]{1,3})?[ \t]*"  # or opened again just after it
    r"(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _record_demotion(
    text: str, *, automation: str, run_id: str, reason: str, runs_dir: Path
) -> None:
    """Append one line to ``runs/demoted_notifications.log`` when a
    ``notify: urgent-only`` automation's report was withheld from push for
    lacking an ``URGENT: <reason>`` marker.

    FAIL LOUD, NEVER SILENT: this is the mechanically-greppable record that
    a push was intentionally withheld and why -- on top of (not instead
    of) the run's own artifacts, which already carry the full text
    regardless of this decision. A write failure here is printed to
    stderr; it must never fail the run that already completed.

    Fix (2026-08-05): the ``QUIET HOURS:`` marker has a real-time stderr
    backstop (see ``_record_quiet_hours_judgment``'s "UNRECORDED" branch)
    on top of its log line -- this function used to only write the log
    line, silently, with nothing visible unless someone went looking in
    ``demoted_notifications.log``. That asymmetry is exactly the failure
    this project cannot afford here: the ``URGENT:`` contract that governs
    it lives ONLY as prose in ``guidance/ATTENTION.md``, a file the agent
    is explicitly told to prune -- if that prose is ever pruned away, this
    automation goes permanently, silently dark (every run demoted,
    forever, indistinguishable from "nothing was ever urgent"). A loud
    stderr line on every demotion is the same visibility quiet hours
    already has, given to the twin case that didn't have it.
    """
    print(
        f"[urgent-demote] {automation} run {run_id}: push withheld -- {reason} "
        "(if guidance/ATTENTION.md's `URGENT:` convention has been pruned or "
        "forgotten, this automation goes dark permanently; see "
        "runs/demoted_notifications.log)",
        file=sys.stderr,
    )
    runs_dir = Path(runs_dir).expanduser()
    log_path = runs_dir / "demoted_notifications.log"
    preview = text.strip().replace("\n", " ")[:200]
    line = (
        f"{_iso8601_now()} automation={automation} run_id={run_id} "
        f"reason={reason!r} text_preview={preview!r}\n"
    )
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        print(f"[demote] failed to write {log_path}: {exc}", file=sys.stderr)


# ---- the delivery seam (docs/ARCHITECTURE.md section 7) ----
#
# THE ENGINE EVALUATES DELIVERY POLICY; IT NEVER PERFORMS DELIVERY.
#
# Everything above this line decides. Nothing above this line delivers.
# ``run()`` emits exactly one ``delivery_intent`` event per run -- verdict,
# the gate that decided it, and a reason that has no default -- into the
# durable outbox, and ``delivery_worker`` (inside notify-serve) is what
# actually mints notifications and pushes.
#
# The reason this is worth a boundary rather than a comment: THREE separate
# gates in this file could each silently zero a run's output, and the
# summary everyone repeated ("the three gates") did not even contain all of
# them. ``_looks_like_refusal`` is a real gate absent from that summary and
# would have vanished in an extraction that trusted it. ``engine_events.Gate``
# is enumerated from the code, not from the summary.
#
# Engine extraction, step 2 (docs/ARCHITECTURE.md section 7, "Outbox
# semantics"): the
# other unsummarized gate -- duplicate suppression against recently DELIVERED
# notifications -- moved to the consumer's delivery worker, at notification-
# mint time. "What was delivered" is transport knowledge; the engine now
# ALWAYS emits the deliver intent and the worker suppresses at mint with a
# reasoned record. The class-1 chain got stronger: no gate inside the engine
# can zero an intent for delivered-history reasons any more.


@dataclass(frozen=True)
class DeliveryIntent:
    """What the engine decided about one run's output, and why.

    Required-with-no-default is the whole point (failure class 13): there is
    no ``reason=""`` path, no ``gate=None`` path, and no way to persist a run
    without one of these -- ``_persist_run`` takes it as a required keyword,
    so a future call site cannot forget it and produce a run record with no
    delivery record beside it.
    """

    verdict: engine_events.Verdict
    gate: engine_events.Gate
    reason: str
    text: str


def _classify_delivery_intent(
    *,
    notify_policy: str,
    final_reply: str,
    failed: bool,
    refusal_detected: bool,
    demoted: bool,
    demoted_reason: str | None,
    notified: bool,
) -> DeliveryIntent:
    """Name the gate that actually decided this run's delivery.

    The branch order below mirrors ``run()``'s own order exactly, so the
    LAST gate to fire is the one recorded -- that is the one a human asking
    "why did nothing arrive?" needs, and recording an earlier one would be a
    true statement that answers the wrong question.

    Raises:
        RunnerError: the classification disagrees with ``run()``'s own
            ``notified`` flag. That can only mean this function and the code
            above it have drifted apart -- i.e. a gate exists that this
            function does not know about, which is precisely the failure
            this seam is built to make impossible. It fails loudly rather
            than shipping an intent that quietly contradicts the run.
    """
    stripped = final_reply.strip()

    if failed and refusal_detected:
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.WITHHOLD,
            gate=engine_events.Gate.REFUSAL_DETECTED,
            reason=(
                "the final reply is itself a refusal/inability statement with "
                "nothing substantive after it -- pushing 'I cannot determine "
                "this' as if it were signal is worse than silence, so this is "
                "recorded as a run FAILURE, not a notification"
            ),
            text=final_reply,
        )
    elif failed:
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.WITHHOLD,
            gate=engine_events.Gate.RUN_FAILED,
            reason=(
                "the run failed before it produced a deliverable result; the "
                "failure itself is reported separately as an automation_error "
                "event, which the delivery worker does push"
            ),
            text=final_reply,
        )
    elif demoted:
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.DEMOTE,
            gate=engine_events.Gate.URGENT_MARKER_MISSING,
            reason=(
                demoted_reason
                or (
                    "notify: urgent-only and no `URGENT: <reason>` marker in "
                    "the final reply -- push withheld, full text recorded"
                )
            ),
            text=final_reply,
        )
    elif notify_policy == "never":
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.WITHHOLD,
            gate=engine_events.Gate.POLICY_NEVER,
            reason=(
                "this automation's frontmatter says notify: never -- it runs "
                "for its side effects, and its output is never pushed"
            ),
            text=final_reply,
        )
    elif not stripped:
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.WITHHOLD,
            gate=engine_events.Gate.FINAL_REPLY_EMPTY,
            reason=(
                f"notify: {notify_policy} but the selected final reply was "
                "empty after stripping -- there is nothing to deliver"
            ),
            text=final_reply,
        )
    elif notify_policy == "always":
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.DELIVER,
            gate=engine_events.Gate.POLICY_ALWAYS,
            reason=(
                "notify: always and the final reply is non-empty -- no "
                "judgment gate applies"
            ),
            text=final_reply,
        )
    elif stripped == NOTHING_TO_REPORT:
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.WITHHOLD,
            gate=engine_events.Gate.AUTO_SENTINEL,
            reason=(
                f"notify: {notify_policy} and the auto-notify check replied "
                f"with the {NOTHING_TO_REPORT} sentinel -- the agent judged it "
                "had nothing worth interrupting for"
            ),
            text=final_reply,
        )
    elif notify_policy == "urgent-only":
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.DELIVER,
            gate=engine_events.Gate.URGENT_MARKER_PRESENT,
            reason=(
                "notify: urgent-only and the final reply carries an `URGENT:` "
                "marker -- the agent judged this specific finding worth a "
                "phone interruption"
            ),
            text=final_reply,
        )
    else:
        intent = DeliveryIntent(
            verdict=engine_events.Verdict.DELIVER,
            gate=engine_events.Gate.AUTO_SENTINEL,
            reason=(
                f"notify: {notify_policy} and the auto-notify check returned "
                "something other than the sentinel -- the agent judged this "
                "worth reporting"
            ),
            text=final_reply,
        )

    wants_delivery = intent.verdict is engine_events.Verdict.DELIVER
    if wants_delivery != notified:
        raise RunnerError(
            "delivery-intent classification disagrees with the run's own "
            f"notified flag (intent={intent.verdict.value}/{intent.gate.value}, "
            f"notified={notified}, notify={notify_policy!r}, failed={failed}, "
            f"refusal={refusal_detected}, demoted={demoted}). A gate exists that "
            "_classify_delivery_intent does not know about -- exactly the "
            "silent-zeroing failure this seam exists to prevent. Refusing to "
            "emit an intent that contradicts the run."
        )
    return intent


# Named so the two rules can never be spelled differently at two call sites.
FINAL_REPLY_RULE_ABORTED = "aborted-before-any-turn"
FINAL_REPLY_RULE_CHAT = "chat-message: the single message turn's own reply"
FINAL_REPLY_RULE_SESSION = "session-turn: the resumed session turn's own reply"


def _aborted_run_intent(abort_message: str) -> DeliveryIntent:
    """The intent for a run that aborted before executing a single turn.

    An abort still writes an intent. A run record with no delivery record
    beside it is an invalid run (section 6), and "it never got far enough to
    have an opinion" is itself the reason -- stated, not left blank.
    """
    return DeliveryIntent(
        verdict=engine_events.Verdict.WITHHOLD,
        gate=engine_events.Gate.RUN_FAILED,
        reason=abort_message,
        text="",
    )


def _chat_run_intent(final_reply: str) -> DeliveryIntent:
    """The intent for a chat/reply turn (``run_chat_message``).

    A real branch in this file zeroes delivery here -- ``notified=False,
    # chat replies return over HTTP; never pushed`` -- so it gets a real
    gate and a real reason rather than being the one run shape that
    mysteriously emits nothing. The answer travels back to the caller over
    HTTP and any push is ``reply_service``'s to make, not this path's.
    """
    return DeliveryIntent(
        verdict=engine_events.Verdict.WITHHOLD,
        gate=engine_events.Gate.CHAT_HTTP_REPLY,
        reason=(
            "chat/message run: the agent's answer returns to the caller "
            "synchronously over HTTP (POST /api/reply, POST /api/message) and "
            "any push is reply_service's to make -- this run path never "
            "delivers"
        ),
        text=final_reply,
    )


def _session_run_intent(final_reply: str) -> DeliveryIntent:
    """The intent for a bare-session (live/interactive) turn (``resume_turn``).

    Same delivery posture as ``_chat_run_intent``: the answer returns to the
    caller over the turn API (``GET /api/turns/{turn_id}``) and any push is
    the consumer's to make, so this run path never delivers. It records the
    real ``CHAT_HTTP_REPLY`` gate -- whose documented meaning is "a chat OR
    reply turn's answer travels back over HTTP and is never pushed" -- rather
    than being the one run shape that emits no delivery record. Reusing that
    gate (instead of minting a new one) keeps the outbox's closed Gate
    vocabulary stable for the delivery worker.
    """
    return DeliveryIntent(
        verdict=engine_events.Verdict.WITHHOLD,
        gate=engine_events.Gate.CHAT_HTTP_REPLY,
        reason=(
            "bare-session (live/interactive) turn: the agent's answer returns to the "
            "caller over the turn API (GET /api/turns/{turn_id}) and any push "
            "is the consumer's to make -- this run path never delivers"
        ),
        text=final_reply,
    )


def run(
    automation: Automation,
    *,
    cwd: Path,
    runs_dir: Path,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    dry_run: bool = False,
    run_id: str | None = None,
    trigger: str = "manual",
) -> RunResult:
    """Execute an automation end to end (one persistent conversation across runs).

    An automation is pinned to ONE amplifier-agent session across every run
    it ever has, not just within a single run: ``automation.session``
    (frontmatter) names the session id, resolved get-or-create on every
    firing, with create as the exception path. This mirrors Microsoft
    Scout's own per-automation session pinning (clean-room verified) and is
    the fix for the defect where every run started from an empty
    transcript, so "re-surface unresolved items" degraded into
    "re-derive from the current mailbox" and silently dropped items.

    Turn order within a run: an optional system prompt (``prompts/system.md``)
    goes first, using ``--fresh``, if and only if this run is the one
    CREATING the session -- a resumed session already has the system
    prompt in its transcript, so it is never resent. The automation's own
    steps follow, then the optional auto-notify check. Exactly one turn
    across the session's entire lifetime uses ``--fresh``; every other
    turn, in every run, ``--resume``s the same session.

    Defending the pin: if ``automation.session`` is set but its session
    directory cannot be positively confirmed to exist (I/O error, or an
    ambiguous partial state -- a directory with no transcript), the run
    ABORTS before executing any turn rather than risk silently recreating
    a session and losing whatever the prior transcript held. Only a
    confirmed-absent directory is treated as safe to recreate -- and when
    that happens, the new id is written back to the automation's
    frontmatter and logged loudly, never silently.

    This function does not rotate, trim, or summarize the transcript, and
    keeps no item-level state of its own: amplifier-agent already
    auto-compacts long-running sessions, and Scout's own persistent-session
    design carries no item-state store either. It does, however, record
    (on ``RunResult``, purely for observability) whether this run resumed
    or created the session, plus the transcript's size at run start, so
    compaction can be correlated against any future item-loss reports.

    Args:
        run_id: caller-supplied run id (e.g. minted up front by a caller
            that needs to return it before the run finishes, such as the
            management API's "run now" endpoint). Defaults to a fresh
            timestamp-based id when not supplied — every existing caller
            (CLI, scheduler) keeps generating its own as before. This is
            the *run* id, always fresh per invocation; it is distinct from
            the (usually reused across many runs) session id.
    """
    cwd = Path(cwd).expanduser()
    runs_dir = Path(runs_dir).expanduser()
    prompts_dir = Path(prompts_dir).expanduser()

    run_id = _sanitize_run_id(run_id if run_id is not None else _run_id_now())
    started_at = _iso8601_now()

    # Last-resort ledger guarantee: every failure path *inside*
    # the run body below already persists a failed result.json (see the many
    # _persist_run(..., failed=True) aborts). But an UNEXPECTED exception --
    # one that escapes all of that handling -- used to leave NO result.json at
    # all, and both callers that catch it then recorded the failure somewhere
    # the run-status read path never consults:
    #   * the scheduler only updated its in-memory state, and
    #   * the management API's "run now" wrote a status.json that the last_run
    #     computation (management_api._iter_run_records, result.json-ONLY)
    #     ignores.
    # Net effect: a run that died here left the automation's surfaced
    # "last run" pointing at its previous SUCCESS -- a failing automation
    # reading as healthy, its real failure time recorded nowhere anything
    # shows. Persist a canonical failed record here, at the one place run_id
    # and started_at are known, THEN re-raise: fail loud, the exception still
    # propagates exactly as before, we only stop losing the record of it.
    try:
        # Per-automation agent config (9h5): resolve the layered overlay
        # ($AMPLIFIER_AGENT_CONFIG base -> workspace agent-config.yaml default: ->
        # this automation's `agent_config:`) into ONE materialized amplifier-agent
        # host config handed to `--config` on EVERY turn. `.path` is None -- no
        # `--config`, byte-identical argv to pre-feature behavior -- when every
        # layer is empty. `.provider_module` drives built-in provider-change pin
        # rotation; `.sha`/`.path` are recorded in the run record. Resolved INSIDE
        # the try so a malformed operator env config or workspace
        # `agent-config.yaml` is recorded as a failed run (fail loud + persisted),
        # not lost. See drumbeat.agent_config.
        resolved_agent_config = agent_config.resolve(
            runs_dir=runs_dir,
            slug=automation.slug,
            workspace=cwd,
            automation_config=automation.agent_config,
        )
        return _run_body(
            automation,
            cwd=cwd,
            runs_dir=runs_dir,
            prompts_dir=prompts_dir,
            dry_run=dry_run,
            run_id=run_id,
            started_at=started_at,
            trigger=trigger,
            resolved_agent_config=resolved_agent_config,
        )
    except Exception as exc:  # record-then-reraise, never swallow
        if not dry_run:
            _persist_escaped_failure(
                automation=automation,
                run_id=run_id,
                started_at=started_at,
                runs_dir=runs_dir,
                error=exc,
            )
        raise


def _persist_escaped_failure(
    *,
    automation: Automation,
    run_id: str,
    started_at: str,
    runs_dir: Path,
    error: BaseException,
) -> None:
    """Persist a canonical failed run record for an exception that escaped
    ``_run_body``'s own fail-loud aborts, so ``last_run`` and run history
    surface THIS failure's time -- never a stale prior success.

    Best-effort and never raises: it runs on the way OUT of an
    already-failing run (``run`` is about to re-raise the original
    exception), so a failure to write the failure record must not mask the
    real error. ``session_id`` is resolved best-effort -- the run may well
    have died before it was resolved, and the pin lookup that resolves it is
    itself a candidate for having raised.
    """
    finished_at = _iso8601_now()
    message = f"unhandled exception during run: {error}"
    try:
        pin = session_pins.get(automation.slug, runs_dir=runs_dir)
        session_id = pin.session_id if pin else f"{automation.slug}-{run_id}"
    except Exception:  # noqa: BLE001 - best-effort; fall back to the mint form
        session_id = f"{automation.slug}-{run_id}"
    result = RunResult(
        automation=automation.name,
        run_id=run_id,
        session_id=session_id,
        started_at=started_at,
        finished_at=finished_at,
        steps=[],
        final_reply="",
        notified=False,
        failed=True,
        error=message,
        session_resumed=False,
        session_transcript_bytes_at_start=None,
        session_transcript_lines_at_start=None,
    )
    try:
        _persist_run(
            automation=automation,
            result=result,
            runs_dir=runs_dir,
            stderr_chunks=[],
            intent=_aborted_run_intent(message),
            final_reply_rule=FINAL_REPLY_RULE_ABORTED,
        )
    except Exception as persist_exc:  # noqa: BLE001 - never mask the original error
        print(
            f"[{automation.name}] CRITICAL: could not persist the failure "
            f"record for run {run_id}: {persist_exc} "
            f"(original error being re-raised: {error})",
            file=sys.stderr,
        )


def _run_body(
    automation: Automation,
    *,
    cwd: Path,
    runs_dir: Path,
    prompts_dir: Path,
    dry_run: bool,
    run_id: str,
    started_at: str,
    trigger: str,
    resolved_agent_config: agent_config.ResolvedAgentConfig | None = None,
) -> RunResult:
    """The full run implementation. Wrapped by ``run()`` so that any exception
    which escapes the fail-loud aborts below is still recorded as a failed
    run before it propagates -- see ``run()`` and ``_persist_escaped_failure``.
    ``cwd``/``runs_dir``/``prompts_dir`` arrive already expanded, and
    ``run_id``/``started_at`` already minted, by ``run()``.

    ``resolved_agent_config`` is the merged per-automation host config (9h5),
    resolved by ``run()``. Its ``.path`` is the ``--config`` threaded on every
    turn (``None`` -> no ``--config``, byte-identical argv to pre-feature
    behavior), its ``.provider_module`` drives built-in provider-change pin
    rotation below, and its ``.sha``/``.path`` are recorded in the run record.
    """
    host_config_path = (
        resolved_agent_config.path if resolved_agent_config is not None else None
    )
    # The effective provider module this run resolves to -- recorded beside the
    # contract fingerprint when a session is created, and compared on resume so
    # a provider change auto-rotates the pin (owner decision: always).
    effective_provider = (
        resolved_agent_config.provider_module
        if resolved_agent_config is not None
        else agent_config.BUNDLE_DEFAULT_PROVIDER
    )

    pin = session_pins.get(automation.slug, runs_dir=runs_dir)
    pinned_session_id = pin.session_id if pin else None
    is_new_session: bool
    session_id: str

    if pinned_session_id:
        probe_status, probe_detail = _probe_session(
            pinned_session_id,
            cwd=cwd,
            recorded_workspace=pin.session_workspace if pin else None,
        )
        if probe_status is _SessionProbe.EXISTS:
            # Trigger 0 -- CONVERSATION LIFECYCLE (`conversation:` frontmatter).
            # Evaluated first, and it shares the exact flock-guarded rotation
            # path the health triggers use, so any trigger -- lifecycle, drift,
            # provider change, or ceiling -- rotates through one place and lands
            # one record. `continuous` (the default, and every automation with no
            # `conversation:` key) returns None here with no clock read and no
            # store I/O, so its path below is byte-for-byte what it was before
            # this field existed. `fresh`/`daily` return a reason and rotate.
            lifecycle_reason = _conversation_rotation_reason(
                automation, session_id=pinned_session_id, runs_dir=runs_dir
            )
            if lifecycle_reason is not None and not dry_run:
                rotated = _auto_rotate(
                    automation,
                    old_session_id=pinned_session_id,
                    reason=lifecycle_reason,
                    runs_dir=runs_dir,
                )
                if rotated:
                    session_id = f"{automation.slug}-{run_id}"
                    is_new_session = True
                else:
                    session_id = pinned_session_id
                    is_new_session = False
            elif lifecycle_reason is not None:  # dry_run
                print(
                    f"[{automation.name}] dry run: pinned session "
                    f"{pinned_session_id!r} WOULD be auto-rotated "
                    f"({lifecycle_reason})",
                    file=sys.stderr,
                )
                session_id = pinned_session_id
                is_new_session = False
            else:
                # Two independent auto-rotation triggers, both checked before the
                # first turn so a stale pin costs zero bad runs rather than many:
                #
                #   * CONTRACT DRIFT (Trigger 2) -- the automation's STEPS changed
                #     since this session was created. A resumed session keeps obeying
                #     the old contract (the 15-run Daily Rollup failure of
                #     2026-08-07). The fingerprint covers the steps only, never the
                #     frontmatter. See session_health's module docstring.
                #   * PROVIDER CHANGE (9h5, owner decision: ALWAYS rotate) -- the
                #     resolved agent config now selects a different provider MODULE
                #     than the session was created under. The transcript carries
                #     provider-specific artifacts (thinking-block signatures, cache
                #     breakpoints, tokenization) the new provider can reject outright.
                #     The decision is flock-guarded (``claim_provider_rotation``) so
                #     two concurrent triggers cannot both rotate the same pin.
                drifted, recorded_fp = session_health.contract_drift(
                    session_id=pinned_session_id,
                    steps=automation.steps,
                    runs_dir=runs_dir,
                )
                if dry_run:
                    provider_changed, prev_provider = session_health.provider_drift(
                        session_id=pinned_session_id,
                        current_provider=effective_provider,
                        runs_dir=runs_dir,
                    )
                else:
                    provider_changed, prev_provider = (
                        session_health.claim_provider_rotation(
                            session_id=pinned_session_id,
                            current_provider=effective_provider,
                            runs_dir=runs_dir,
                        )
                    )

                rotate_reason: str | None = None
                if drifted:
                    rotate_reason = (
                        f"contract drift: {automation.path.name}'s steps changed "
                        f"since this session was created (recorded fingerprint "
                        f"{(recorded_fp or '?')[:12]}, current "
                        f"{session_health.contract_fingerprint(automation.steps)[:12]}). "
                        "A resumed session keeps obeying the instructions it was "
                        "given, so it would argue against the current automation "
                        "-- exactly the 15-run Daily Rollup failure of 2026-08-07."
                    )
                elif provider_changed:
                    rotate_reason = (
                        f"provider change: this session was created under provider "
                        f"module {prev_provider!r}, but the resolved agent config now "
                        f"selects {effective_provider!r}. A resumed conversation "
                        "carries provider-specific transcript state the new provider "
                        "can reject, so the pin is rotated and the next run starts "
                        "fresh."
                    )

                if rotate_reason is not None and not dry_run:
                    rotated = _auto_rotate(
                        automation,
                        old_session_id=pinned_session_id,
                        reason=rotate_reason,
                        runs_dir=runs_dir,
                    )
                    if rotated:
                        session_id = f"{automation.slug}-{run_id}"
                        is_new_session = True
                    else:
                        session_id = pinned_session_id
                        is_new_session = False
                else:
                    if drifted:
                        print(
                            f"[{automation.name}] dry run: pinned session "
                            f"{pinned_session_id!r} has drifted from "
                            f"{automation.path.name}'s current steps and WOULD be "
                            "auto-rotated",
                            file=sys.stderr,
                        )
                    if provider_changed:
                        print(
                            f"[{automation.name}] dry run: pinned session "
                            f"{pinned_session_id!r} was created under provider "
                            f"{prev_provider!r} but the resolved config selects "
                            f"{effective_provider!r}, so it WOULD be auto-rotated",
                            file=sys.stderr,
                        )
                    session_id = pinned_session_id
                    is_new_session = False
                    print(
                        f"[{automation.name}] resuming pinned session {session_id!r} "
                        f"({probe_detail})",
                        file=sys.stderr,
                    )
        elif probe_status is _SessionProbe.MISSING:
            session_id = f"{automation.slug}-{run_id}"
            is_new_session = True
            print(
                f"[{automation.name}] pinned session {pinned_session_id!r} confirmed "
                f"missing ({probe_detail}) -- creating new session {session_id!r} "
                "and re-pinning",
                file=sys.stderr,
            )
        else:
            if probe_status is _SessionProbe.WORKSPACE_MISMATCH:
                # Fix 3: this is the "project directory renamed/moved" case --
                # deliberately NOT treated as safe-to-recreate. Silently
                # creating a fresh session here would abandon every prior
                # turn under the OLD workspace slug, which nothing will ever
                # probe again -- the exact silent-drop defect this whole
                # mechanism exists to fix, re-armed.
                abort_message = (
                    f"pinned session {pinned_session_id!r} could not be safely "
                    f"resumed ({probe_detail}). ABORTING rather than silently "
                    "creating a fresh session, which would permanently discard "
                    "all accumulated memory. To recover: confirm the project's "
                    "real location, then either fix this automation's "
                    "pin by hand in the engine's session pin store "
                    f"({session_pins.pins_path(runs_dir)}), or run `drumbeat "
                    f"rotate-session {automation.slug} --workspace <dir> "
                    "[--data-dir <dir>] --reason '<why>'` to deliberately "
                    "abandon the old session and start fresh."
                )
            else:  # _SessionProbe.UNKNOWN
                abort_message = (
                    f"cannot determine whether pinned session {pinned_session_id!r} "
                    f"exists ({probe_detail}) -- aborting rather than risk silently "
                    "orphaning the pin or double-creating a session"
                )
            print(f"[{automation.name}] ABORTED: {abort_message}", file=sys.stderr)
            finished_at = _iso8601_now()
            result = RunResult(
                automation=automation.name,
                run_id=run_id,
                session_id=pinned_session_id,
                started_at=started_at,
                finished_at=finished_at,
                steps=[],
                final_reply="",
                notified=False,
                failed=True,
                error=abort_message,
                session_resumed=False,
                session_transcript_bytes_at_start=None,
                session_transcript_lines_at_start=None,
            )
            if not dry_run:
                _persist_run(
                    automation=automation,
                    result=result,
                    runs_dir=runs_dir,
                    stderr_chunks=[],
                    intent=_aborted_run_intent(abort_message),
                    final_reply_rule=FINAL_REPLY_RULE_ABORTED,
                )
            return result
    else:
        session_id = f"{automation.slug}-{run_id}"
        is_new_session = True
        print(
            f"[{automation.name}] no pinned session on {automation.path.name} -- "
            f"creating {session_id!r}",
            file=sys.stderr,
        )

    # Orchestration observability: this is the engine's own decision to fire
    # an automation -- the hook-context-intelligence capture (see ci_upload.py)
    # only sees INSIDE the amplifier-agent session this triggers, never the
    # trigger decision itself. See ci_events.py's module docstring.
    if not dry_run:
        ci_events.emit(
            "drumbeat:automation_triggered",
            {
                "automation": automation.name,
                "run_id": run_id,
                "session_id": session_id,
                "trigger": trigger,
                "is_new_session": is_new_session,
            },
            cwd=cwd,
        )

    # Fix 2: `requires:` is now a real pre-run gate, not parsed-and-ignored
    # theater. Every entry is mechanically verified BEFORE any turn runs --
    # a missing/empty guidance file or an unresolvable executable aborts the
    # run outright rather than letting the agent proceed and discover (or,
    # worse, not notice) the gap itself.
    requirement_checks = check_requirements(
        automation.requires, cwd=cwd, runs_dir=runs_dir
    )
    unsatisfied_requirements = [c for c in requirement_checks if not c.satisfied]
    if unsatisfied_requirements:
        abort_message = _unsatisfied_requirements_message(requirement_checks)
        print(f"[{automation.name}] ABORTED: {abort_message}", file=sys.stderr)
        if not dry_run:
            ci_events.emit(
                "drumbeat:requirements_gate_aborted",
                {
                    "automation": automation.name,
                    "run_id": run_id,
                    "session_id": session_id,
                    "unsatisfied": [c.item for c in unsatisfied_requirements],
                    "detail": abort_message,
                },
                cwd=cwd,
            )
        finished_at = _iso8601_now()
        result = RunResult(
            automation=automation.name,
            run_id=run_id,
            session_id=session_id,
            started_at=started_at,
            finished_at=finished_at,
            steps=[],
            final_reply="",
            notified=False,
            failed=True,
            error=abort_message,
            session_resumed=not is_new_session,
            session_transcript_bytes_at_start=None,
            session_transcript_lines_at_start=None,
        )
        if not dry_run:
            _persist_run(
                automation=automation,
                result=result,
                runs_dir=runs_dir,
                stderr_chunks=[],
                intent=_aborted_run_intent(abort_message),
                final_reply_rule=FINAL_REPLY_RULE_ABORTED,
            )
        return result

    # `inject:` (docs/ARCHITECTURE.md section 6): execute every declared
    # inject argv BEFORE any turn runs, same read-before-any-turn, fail-loud
    # discipline the requirements gate above uses and the built-in ledger
    # reads used to. Classification order is fixed -- timeout -> exit code ->
    # stdout -- and an abort here is voiced: the persisted failed run emits
    # an automation_error event, which the delivery worker pushes.
    inject_turns: list[tuple[InjectSpec, str]] = []
    inject_abort: str | None = None
    for spec in automation.inject:
        outcome = _run_inject_tool(spec, cwd=cwd, runs_dir=runs_dir)
        if outcome.abort_reason is not None:
            inject_abort = outcome.abort_reason
            break
        if outcome.idle:
            print(
                f"[{automation.name}] inject {spec.label!r}: tool reported "
                f"{INJECT_IDLE} -- no turn injected, run proceeds",
                file=sys.stderr,
            )
            if not dry_run:
                try:
                    engine_events.append_event(
                        runs_dir,
                        engine_events.EventType.INJECT_SKIPPED,
                        {
                            "run_id": run_id,
                            "automation": automation.name,
                            "automation_slug": automation.slug,
                            "session_id": session_id,
                            "tool": spec.argv[0],
                            "label": spec.label,
                            "reason": (
                                f"tool reported idle: exit 0 with stdout exactly "
                                f"{INJECT_IDLE} -- the skip is this record, never "
                                "an inference"
                            ),
                        },
                    )
                except (engine_events.OutboxError, OSError) as exc:
                    # OutboxError (NOT an OSError) as well as OSError: an
                    # inject_skipped write that fails field validation -- e.g.
                    # a blank/None run_id -- raises OutboxWriteError, which a
                    # bare ``except OSError`` lets escape and kill the whole
                    # turn. This bookkeeping write must never take down the run
                    # it is only annotating -- the same widening already
                    # guards the session_rotated emit at its own write site.
                    print(
                        f"[{automation.name}] failed to write inject_skipped "
                        f"event: {exc}",
                        file=sys.stderr,
                    )
            continue
        assert outcome.text is not None
        inject_turns.append((spec, outcome.text))

    if inject_abort is not None:
        abort_message = inject_abort
        print(f"[{automation.name}] ABORTED: {abort_message}", file=sys.stderr)
        ci_events.emit(
            "drumbeat:inject_tool_failed",
            {
                "automation": automation.name,
                "run_id": run_id,
                "session_id": session_id,
                "detail": abort_message,
            },
            cwd=cwd,
        )
        finished_at = _iso8601_now()
        result = RunResult(
            automation=automation.name,
            run_id=run_id,
            session_id=session_id,
            started_at=started_at,
            finished_at=finished_at,
            steps=[],
            final_reply="",
            notified=False,
            failed=True,
            error=abort_message,
            session_resumed=not is_new_session,
            session_transcript_bytes_at_start=None,
            session_transcript_lines_at_start=None,
        )
        if not dry_run:
            _persist_run(
                automation=automation,
                result=result,
                runs_dir=runs_dir,
                stderr_chunks=[],
                intent=_aborted_run_intent(abort_message),
                final_reply_rule=FINAL_REPLY_RULE_ABORTED,
            )
        return result

    needs_write_back = is_new_session
    if needs_write_back and not dry_run:
        # ABORT, never warn-and-proceed. The retired frontmatter write-back
        # logged a WARNING here and ran anyway; carried into the store that
        # posture turns a full disk into a silent fresh-session fork on
        # every run, forever -- each one abandoning the conversation the
        # previous one just created.
        try:
            session_pins.upsert(
                automation.slug,
                session_id=session_id,
                session_workspace=_derive_workspace_slug(cwd),
                created_by=session_pins.CREATED_BY_RUN,
                runs_dir=runs_dir,
            )
        except session_pins.PinStoreError as exc:
            abort_message = (
                f"failed to record the pinned session {session_id!r} in "
                f"{session_pins.pins_path(runs_dir)}: {exc} -- ABORTING before "
                "running any turn. Proceeding would do the work and then lose "
                "the conversation it happened in: the next run would find no "
                "pin and start over, silently, every time."
            )
            print(f"[{automation.name}] ABORTED: {abort_message}", file=sys.stderr)
            finished_at = _iso8601_now()
            result = RunResult(
                automation=automation.name,
                run_id=run_id,
                session_id=session_id,
                started_at=started_at,
                finished_at=finished_at,
                steps=[],
                final_reply="",
                notified=False,
                failed=True,
                error=abort_message,
                session_resumed=False,
                session_transcript_bytes_at_start=None,
                session_transcript_lines_at_start=None,
            )
            _persist_run(
                automation=automation,
                result=result,
                runs_dir=runs_dir,
                stderr_chunks=[],
                intent=_aborted_run_intent(abort_message),
                final_reply_rule=FINAL_REPLY_RULE_ABORTED,
            )
            return result

        print(
            f"[{automation.name}] pinned session {session_id!r} recorded in "
            f"{session_pins.pins_path(runs_dir).name}",
            file=sys.stderr,
        )
        # Remember the contract this session is being started under, so a
        # later rewrite of the automation's steps is DETECTABLE rather than
        # inferred -- see the drift check above and session_health's module
        # docstring.
        try:
            session_health.record_contract(
                session_id=session_id,
                automation_slug=automation.slug,
                fingerprint=session_health.contract_fingerprint(automation.steps),
                recorded_at=started_at,
                runs_dir=runs_dir,
                provider_module=effective_provider,
            )
        except OSError as exc:
            print(
                f"[{automation.name}] WARNING: pinned session {session_id!r} was "
                f"recorded, but its contract fingerprint could not be: {exc} -- "
                "the run proceeds; drift detection for this session will report "
                "'never recorded' until the next rotation.",
                file=sys.stderr,
            )

        # Anchor the conversation lifecycle for a `fresh`/`daily` automation.
        # Recorded ONLY for the non-default modes (a `continuous` automation
        # writes nothing here, so its session_contracts.json entry is unchanged
        # from before this field existed), and AFTER record_contract, which
        # replaces the entry -- record_lifecycle merges its namespaced sub-object
        # in additively. anchor_day is the host-local day this session begins,
        # the boundary `daily` compares against on resume. Cleaned up for free by
        # _auto_rotate -> forget_contract's whole-entry delete on rotation.
        if automation.conversation != DEFAULT_CONVERSATION_LIFECYCLE:
            session_health.record_lifecycle(
                session_id=session_id,
                mode=automation.conversation,
                anchor_day=_local_now().date().isoformat(),
                runs_dir=runs_dir,
            )

    transcript_stats: dict[str, int | None]
    if is_new_session:
        transcript_stats = {"bytes": None, "lines": None}
    else:
        transcript_stats = _transcript_stats(session_id, cwd=cwd)

    system_prompt = load_prompt("system", prompts_dir)
    # A system-prompt turn only ever fires on the run that CREATES the
    # session -- a resumed session already carries it in the transcript.
    has_system_turn = system_prompt is not None and is_new_session

    # Fix 2: the required guidance files' content is injected as its own
    # turn EVERY run (not just on session creation) -- guidance files are
    # living documents the agent itself edits, so a resumed session's
    # transcript only holds whatever content existed when it was first
    # loaded. Re-injecting fresh content every firing is cheap (local
    # reads, already verified above) and is what actually fixes the
    # documented failure (a required file silently never reaching the
    # agent at all).
    #
    # Step 4 adds the pack cards to this same turn (section 7.2 item 3):
    # every executable in `requires:` resolves to its owning pack, and that
    # pack's card rides verbatim beside the guidance. Re-resolved every run
    # for the same reason the guidance is re-read -- a card is markdown a
    # human edits, and a stale copy in a resumed transcript is exactly the
    # failure the per-run re-injection exists to close.
    requirement_cards = resolve_requirement_cards(requirement_checks, cwd=cwd)
    requirements_turn_text = format_requirements_turn(
        requirement_checks, requirement_cards, mode=automation.guidance_delivery
    )
    has_requirements_turn = requirements_turn_text is not None

    # `inject:` turns (section 7.1): same "inject fresh content every run"
    # discipline as the requirements turn above, built from the declared
    # inject tools already executed (and classified) at the top of run().
    # Injected verbatim -- the tool's stdout IS the turn; the only framing
    # added is the same now-context line every turn gets in _execute_turn.
    has_inject_turns = bool(inject_turns)

    # The very first turn of a brand-new session must use `--fresh`.
    # Priority for claiming that one slot: system prompt (if configured) ->
    # requirements turn (if any file-based `requires:`) -> first inject turn
    # (if any inject tool produced one) -> the automation's own step 1.
    # Exactly one turn across the session's entire lifetime uses `--fresh`.
    requirements_turn_is_fresh = (
        is_new_session and not has_system_turn and has_requirements_turn
    )
    first_inject_turn_is_fresh = (
        is_new_session
        and not has_system_turn
        and not has_requirements_turn
        and has_inject_turns
    )
    step_one_is_fresh = (
        is_new_session
        and not has_system_turn
        and not has_requirements_turn
        and not has_inject_turns
    )

    # Validated up front (before running anything) so a missing/emptied
    # auto-notify prompt fails loud immediately rather than after wasting a
    # real automation run.
    #
    # "Loud" now means what it means everywhere else in this function: the
    # abort goes through the SAME persisted-run path the requirements gate
    # above uses, so a missing prompt produces a run record, a failures.log
    # line, and an automation_error event. It used to raise straight out of
    # run(), which meant a fresh workspace's very first `notify: auto`
    # automation failed with nothing in the ledger at all -- the identical
    # silence the missing-agent case had, one gate later, and the one a new
    # user is most likely to hit (no prompt files ship by default; see
    # examples/prompts/).
    auto_notify_prompt: str | None = None
    if automation.notify in ("auto", "urgent-only"):
        try:
            auto_notify_prompt = require_prompt(
                "auto-notify",
                prompts_dir,
                reason=f"automation.notify is {automation.notify!r}",
            )
        except PromptError as exc:
            abort_message = (
                f"required prompt unavailable, refusing to run degraded: {exc}. "
                "Copy the exemplar from examples/prompts/auto-notify.md into "
                f"{prompts_dir}/ and edit it -- it is yours, and there is no "
                "built-in fallback text."
            )
            print(f"[{automation.name}] ABORTED: {abort_message}", file=sys.stderr)
            finished_at = _iso8601_now()
            result = RunResult(
                automation=automation.name,
                run_id=run_id,
                session_id=session_id,
                started_at=started_at,
                finished_at=finished_at,
                steps=[],
                final_reply="",
                notified=False,
                failed=True,
                error=abort_message,
                session_resumed=not is_new_session,
                session_transcript_bytes_at_start=None,
                session_transcript_lines_at_start=None,
            )
            if not dry_run:
                _persist_run(
                    automation=automation,
                    result=result,
                    runs_dir=runs_dir,
                    stderr_chunks=[],
                    intent=_aborted_run_intent(abort_message),
                    final_reply_rule=FINAL_REPLY_RULE_ABORTED,
                )
            return result

    if dry_run:
        print(
            f"# dry run: {automation.name} "
            f"(session {session_id}, {'new' if is_new_session else 'resumed'})"
        )
        if needs_write_back:
            print(
                f"# would pin session id {session_id!r} for {automation.slug!r} "
                f"in {session_pins.pins_path(runs_dir)}"
            )
        # Every command preview below wraps its text in _prepend_now_context
        # for parity with the real path -- _execute_turn (used by every
        # non-dry-run call site) applies this same prefix unconditionally,
        # see that function's docstring.
        if has_system_turn:
            assert system_prompt is not None
            cmd = _build_command(
                session_id=session_id,
                fresh=True,
                cwd=cwd,
                text=_prepend_now_context(system_prompt),
                host_config_path=host_config_path,
            )
            print(_format_command(cmd))
        if has_requirements_turn:
            assert requirements_turn_text is not None
            cmd = _build_command(
                session_id=session_id,
                fresh=requirements_turn_is_fresh,
                cwd=cwd,
                text=_prepend_now_context(requirements_turn_text),
                host_config_path=host_config_path,
            )
            print(_format_command(cmd))
        for i, (_spec, inject_text) in enumerate(inject_turns):
            cmd = _build_command(
                session_id=session_id,
                fresh=(first_inject_turn_is_fresh and i == 0),
                cwd=cwd,
                text=_prepend_now_context(inject_text),
                host_config_path=host_config_path,
            )
            print(_format_command(cmd))
        for i, step in enumerate(automation.steps, start=1):
            cmd = _build_command(
                session_id=session_id,
                fresh=(step_one_is_fresh and i == 1),
                cwd=cwd,
                text=_prepend_now_context(step.prompt),
                host_config_path=host_config_path,
            )
            print(_format_command(cmd))
        if auto_notify_prompt is not None:
            cmd = _build_command(
                session_id=session_id,
                fresh=False,
                cwd=cwd,
                text=_prepend_now_context(auto_notify_prompt),
                host_config_path=host_config_path,
            )
            print(_format_command(cmd))
        finished_at = _iso8601_now()
        return RunResult(
            automation=automation.name,
            run_id=run_id,
            session_id=session_id,
            started_at=started_at,
            finished_at=finished_at,
            steps=[],
            final_reply="",
            notified=False,
            failed=False,
            session_resumed=not is_new_session,
            session_transcript_bytes_at_start=transcript_stats["bytes"],
            session_transcript_lines_at_start=transcript_stats["lines"],
        )

    step_results: list[StepResult] = []
    stderr_chunks: list[tuple[int, str]] = []
    failed = False
    final_reply = ""
    # WHICH reply became final_reply, and by what rule. Recorded on the
    # run_completed event because "which of the step outputs did we actually
    # treat as the answer" was the first of the three gates that silently
    # killed Daily Rollup, and it previously left no record at all.
    final_reply_rule = "no turn produced a reply"
    notified = False
    # Every turn in this run gets a unique, monotonically increasing index
    # (used only for stderr/step debug filenames -- see _persist_run) --
    # decoupled from automation step *numbering* (used only in the
    # human-readable progress prints below), since the requirements turn
    # now occupies a slot that doesn't correspond to any automation step.
    turn_index = itertools.count()

    if has_system_turn:
        assert system_prompt is not None
        print(f"[{automation.name}] system prompt turn", file=sys.stderr)
        index = next(turn_index)
        system_result, system_stderr = _execute_turn(
            session_id=session_id,
            fresh=True,
            cwd=cwd,
            text=system_prompt,
            index=index,
            runs_dir=runs_dir,
            wait_seconds=None,
            host_config_path=host_config_path,
        )
        step_results.append(system_result)
        stderr_chunks.append((index, system_stderr))
        if system_result.error:
            print(
                f"[{automation.name}] system prompt turn FAILED: {system_result.error} — aborting run",
                file=sys.stderr,
            )
            failed = True

    if not failed and has_requirements_turn:
        assert requirements_turn_text is not None
        print(
            f"[{automation.name}] requirements turn "
            f"({sum(1 for c in requirement_checks if c.kind == 'file')} file(s) injected)",
            file=sys.stderr,
        )
        index = next(turn_index)
        requirements_result, requirements_stderr = _execute_turn(
            session_id=session_id,
            fresh=requirements_turn_is_fresh,
            cwd=cwd,
            text=requirements_turn_text,
            index=index,
            runs_dir=runs_dir,
            wait_seconds=None,
            host_config_path=host_config_path,
        )
        step_results.append(requirements_result)
        stderr_chunks.append((index, requirements_stderr))
        if requirements_result.error:
            print(
                f"[{automation.name}] requirements turn FAILED: {requirements_result.error} — aborting run",
                file=sys.stderr,
            )
            failed = True

    if not failed and has_inject_turns:
        for i, (spec, inject_text) in enumerate(inject_turns):
            if failed:
                break
            print(
                f"[{automation.name}] inject turn {spec.label!r} "
                f"({len(inject_text)} chars from {spec.argv[0]})",
                file=sys.stderr,
            )
            index = next(turn_index)
            inject_result, inject_stderr = _execute_turn(
                session_id=session_id,
                fresh=(first_inject_turn_is_fresh and i == 0),
                cwd=cwd,
                text=inject_text,
                index=index,
                runs_dir=runs_dir,
                wait_seconds=None,
                host_config_path=host_config_path,
            )
            step_results.append(inject_result)
            stderr_chunks.append((index, inject_stderr))
            if inject_result.error:
                print(
                    f"[{automation.name}] inject turn {spec.label!r} FAILED: "
                    f"{inject_result.error} — aborting run",
                    file=sys.stderr,
                )
                failed = True
                break
            if not dry_run:
                try:
                    engine_events.append_event(
                        runs_dir,
                        engine_events.EventType.TURN_COMPLETED,
                        {
                            "run_id": run_id,
                            "automation": automation.name,
                            "automation_slug": automation.slug,
                            "session_id": session_id,
                            "origin": "inject",
                            "label": spec.label,
                        },
                    )
                except OSError as exc:
                    print(
                        f"[{automation.name}] failed to write turn_completed "
                        f"event: {exc}",
                        file=sys.stderr,
                    )

    if not failed:
        for i, step in enumerate(automation.steps, start=1):
            print(
                f"[{automation.name}] step {i}/{len(automation.steps)} "
                f"({step.id}): {step.prompt.splitlines()[0][:80]}",
                file=sys.stderr,
            )
            index = next(turn_index)
            result, stderr_text = _execute_turn(
                session_id=session_id,
                fresh=(step_one_is_fresh and i == 1),
                cwd=cwd,
                text=step.prompt,
                index=index,
                runs_dir=runs_dir,
                wait_seconds=None,
                host_config_path=host_config_path,
            )
            # Identity, not control flow: tie this turn's record back to the
            # declared step by id (contract automation-file.v1, rule 4).
            result.step_id = step.id
            step_results.append(result)
            stderr_chunks.append((index, stderr_text))
            if result.error:
                print(
                    f"[{automation.name}] step {i} FAILED: {result.error} — aborting run",
                    file=sys.stderr,
                )
                failed = True
                final_reply = step_results[-2].reply if len(step_results) > 1 else ""
                final_reply_rule = (
                    f"step {i} failed: fell back to the previous turn's reply"
                    if len(step_results) > 1
                    else f"step {i} failed on the first turn: no earlier reply exists"
                )
                break

    if not failed:
        last_reply = step_results[-1].reply
        if automation.notify in ("always", "never"):
            final_reply = last_reply
            final_reply_rule = (
                f"notify: {automation.notify} -- the last automation step's "
                "reply; no auto-notify check runs"
            )
        elif automation.notify in ("auto", "urgent-only"):
            assert auto_notify_prompt is not None
            check_index = next(turn_index)
            check_result, check_stderr = _execute_turn(
                session_id=session_id,
                fresh=False,
                cwd=cwd,
                text=auto_notify_prompt,
                index=check_index,
                runs_dir=runs_dir,
                wait_seconds=None,
                host_config_path=host_config_path,
            )
            step_results.append(check_result)
            stderr_chunks.append((check_index, check_stderr))
            if check_result.error:
                print(
                    f"[{automation.name}] auto-notify check FAILED: {check_result.error} — treating as no notification",
                    file=sys.stderr,
                )
                final_reply = last_reply
                final_reply_rule = (
                    "auto-notify check FAILED -- fell back to the last "
                    "automation step's reply"
                )
            else:
                final_reply = check_result.reply
                final_reply_rule = "auto-notify check turn's own reply"
                if final_reply.strip() == NOTHING_TO_REPORT:
                    ci_events.emit(
                        "drumbeat:nothing_to_report",
                        {
                            "automation": automation.name,
                            "run_id": run_id,
                            "session_id": session_id,
                        },
                        cwd=cwd,
                    )
                    # The withheld-run record (runs/withheld_notifications.log,
                    # with its open_items_count) is now written by the
                    # CONSUMER's delivery worker from this run's withhold
                    # intent (gate=auto-sentinel) -- decomposition step 2,
                    # docs/ARCHITECTURE.md section 7: the count is
                    # the consumer's ledger knowledge, and the engine does not
                    # know what an item is. Same file, same line, one
                    # writer -- now the consumer.
        else:  # pragma: no cover - automation.py validates this, defensive only
            raise RunnerError(f"unknown notify policy: {automation.notify!r}")

        if automation.notify == "always":
            notified = bool(final_reply.strip())
        elif automation.notify in ("auto", "urgent-only"):
            notified = final_reply.strip() != NOTHING_TO_REPORT and bool(
                final_reply.strip()
            )
        # notify == "never" => notified stays False

    # Fix 4a: a final reply that is ITSELF a refusal/inability statement is
    # a run failure, not a notification -- pushing "I cannot determine
    # this" to the user's phone as if it were real signal is worse than
    # silence. Mechanical check, no LLM call; see _looks_like_refusal.
    refusal_detected = notified and _looks_like_refusal(final_reply)
    if refusal_detected:
        print(
            f"[{automation.name}] final reply looks like a refusal/inability "
            f"statement -- treating as a run FAILURE, not a notification: "
            f"{final_reply.strip()[:200]!r}",
            file=sys.stderr,
        )
        failed = True
        notified = False

    # Push demotion: notify: "urgent-only" ran the exact same auto-notify
    # judgment as "auto" above (same NOTHING_TO_REPORT convention, same
    # item-ledger bookkeeping the automation's own steps already did) --
    # but the push itself is withheld unless the agent judged THIS finding
    # urgent enough to mark with `URGENT: <reason>` (see _URGENT_MARKER_RE
    # above deliver()). Never silent: _record_demotion logs it, and the
    # run's own artifacts (result.json, step-NN.txt) are written exactly as
    # always by _persist_run below, regardless of this decision.
    demoted = False
    demoted_reason: str | None = None
    if (
        notified
        and automation.notify == "urgent-only"
        and not _URGENT_MARKER_RE.search(final_reply)
    ):
        demoted = True
        demoted_reason = (
            "notify: urgent-only and no `URGENT: <reason>` marker in the "
            "final reply -- push withheld, full text still recorded in "
            "this run's own artifacts"
        )
        _record_demotion(
            final_reply,
            automation=automation.name,
            run_id=run_id,
            reason=demoted_reason,
            runs_dir=runs_dir,
        )
        notified = False

    # Duplicate suppression (formerly "Fix 4b") moved to the consumer's
    # delivery worker at notification-mint time -- decomposition step 2,
    # docs/ARCHITECTURE.md section 7. The engine ALWAYS emits
    # its intent; "already delivered recently" is the deliverer's knowledge.

    finished_at = _iso8601_now()

    # THE SEAM. Every gate above has now fired or not; this is where the
    # engine's decision becomes a durable, reasoned record instead of a
    # function call into a transport. Computed BEFORE the RunResult so a
    # classification that disagrees with the run's own flags raises here,
    # loudly, rather than emitting an intent that contradicts the run.
    intent = _classify_delivery_intent(
        notify_policy=automation.notify,
        final_reply=final_reply,
        failed=failed,
        refusal_detected=refusal_detected,
        demoted=demoted,
        demoted_reason=demoted_reason,
        notified=notified,
    )

    # Loudness (silent-failure defect class): a run that failed for ANY
    # reason must carry a non-null ``error``, so result.json / the run record
    # / ``drumbeat doctor`` see WHY -- not just ``"failed": true, "error":
    # null``. Before this, ONLY ``refusal_detected`` set ``error``; a failing
    # system/requirements/inject/step turn -- whose ``StepResult.error`` was
    # already set (e.g. ``"amplifier-agent exited 1"``) and already logged to
    # failures.log via ``_write_failure_log`` -- left the TOP-LEVEL error
    # null. That is the exact silent shape measured on the owner's box:
    # session-list and mail-check runs with ``failed: true`` and a
    # step carrying ``"amplifier-agent exited 1"`` while ``error`` stayed
    # null. This mirrors the bare-session path ("A per-step error IS the
    # run's error here") and the chat path, which already surface it.
    if refusal_detected:
        run_error: str | None = (
            f"final reply looks like a refusal/inability statement: {final_reply.strip()[:500]!r}"
        )
    elif failed:
        run_error = next(
            (s.error for s in step_results if s.error),
            "run failed but no turn recorded an error -- see this run's "
            "stderr and step artifacts",
        )
    else:
        run_error = None

    result = RunResult(
        automation=automation.name,
        run_id=run_id,
        session_id=session_id,
        started_at=started_at,
        finished_at=finished_at,
        steps=step_results,
        final_reply=final_reply,
        notified=notified,
        failed=failed,
        error=run_error,
        session_resumed=not is_new_session,
        session_transcript_bytes_at_start=transcript_stats["bytes"],
        session_transcript_lines_at_start=transcript_stats["lines"],
        demoted=demoted,
        demoted_reason=demoted_reason,
        effective_config_path=(
            str(host_config_path) if host_config_path is not None else None
        ),
        effective_config_sha=(
            resolved_agent_config.sha if resolved_agent_config is not None else None
        ),
    )

    _persist_run(
        automation=automation,
        result=result,
        runs_dir=runs_dir,
        stderr_chunks=stderr_chunks,
        intent=intent,
        final_reply_rule=final_reply_rule,
    )

    # Trigger 1 -- CEILING HIT. Checked after the run record is safely on
    # disk, so the evidence outlives the rotation. A prompt the provider
    # refuses can only grow: amplifier-agent compacts at 240k and the
    # provider rejects at 200k, so a session in that window never compacts
    # its way out. Measured on this project's own history: 12 consecutive
    # failures for channels-check, 2 for agent-sessions-check, zero
    # recoveries. Rotating on the first hit costs exactly the run that had
    # already failed. See session_health's module docstring.
    if failed and not dry_run:
        ceiling = session_health.detect_ceiling_hit(
            "\n".join(text for _, text in stderr_chunks)
        )
        if ceiling is not None:
            ci_events.emit(
                "drumbeat:session_ceiling_hit",
                {
                    "automation": automation.name,
                    "run_id": run_id,
                    "session_id": session_id,
                    "prompt_tokens": ceiling.prompt_tokens,
                    "limit_tokens": ceiling.limit_tokens,
                },
                cwd=cwd,
            )
            _auto_rotate(
                automation,
                old_session_id=session_id,
                reason=(
                    f"context ceiling hit on run {run_id}: {ceiling.detail}. "
                    "This session cannot recover -- amplifier-agent compacts "
                    "above the provider's limit, so the prompt only grows."
                ),
                runs_dir=runs_dir,
            )

    if notified:
        # Kept verbatim, including the name: this event always fired
        # BEFORE the transport ran, so it always meant "decided to deliver,"
        # never "arrived." Post-seam it means the same thing at the same
        # moment. The transport half of the accounting is, and already was,
        # the store's `delivered`/`delivery_error` fields.
        ci_events.emit(
            "drumbeat:notification_delivered",
            {
                "automation": automation.name,
                "run_id": run_id,
                "session_id": session_id,
            },
            cwd=cwd,
        )
        # THE CUT. `deliver(...)` used to be called right here. It is now
        # the delivery worker's to call, from the delivery_intent event
        # _persist_run wrote above. This function no longer performs
        # delivery, and no code path below it does either.

    upload_outcome = ci_upload.upload_session(
        _session_dir(session_id, cwd=cwd) / "context-intelligence",
        job_id=f"{automation.slug}-{run_id}",
    )
    _record_ci_upload_outcome(
        runs_dir=runs_dir, automation=automation, result=result, outcome=upload_outcome
    )

    # Tail telemetry (loop metrics, notification-concentration, discharge-by-
    # source, stake distribution) is now the CONSUMER's delivery worker's to
    # compute and append, from this run's outbox events -- decomposition
    # step 2, docs/ARCHITECTURE.md section 7: all four are notify_store
    # reads, and the engine does not know what an item is. Same files, same
    # lines, one writer -- now the consumer, ~seconds later (named delta 7).

    return result


def run_chat_message(
    chat_automation: Automation,
    text: str,
    *,
    cwd: Path,
    runs_dir: Path,
    lock_wait_seconds: float | None = None,
    progress_callback: ProgressCallback | None = None,
    force_new: bool = False,
    host_config_path: Path | None = None,
    preamble_blocks: tuple[str, ...] = (),
) -> RunResult:
    """Route one chat message through the same automation machinery every
    scheduled automation uses -- pinned session, fail-loud requirements gate
    -- instead of the historical bare ``--fresh`` ad-hoc turn.

    ``force_new`` (chat persistence, ITEM 1 -- "New conversation" starts a
    FRESH amplifier-agent session, never a resume of the pinned one): when
    True, behaves exactly as if ``chat_automation.session`` were confirmed
    missing, REGARDLESS of what a probe would find -- a brand-new session
    is created and the pin is rewritten to point at it, same as the
    ordinary missing-pin recovery path below. The OLD pinned session is
    never deleted or modified on disk; its transcript and
    consumer-side conversation history remain
    independently readable forever -- ``force_new`` only changes what
    ``chat_automation``'s ``session:`` field points at going forward.

    ``lock_wait_seconds`` bounds how long EACH turn fired in this call waits
    on session-lock contention (see ``_session_lock``); defaults to
    ``_interactive_lock_wait_seconds()`` (5s) when omitted. ``reply_service``
    runs this in a background thread and passes
    ``background_lock_wait_seconds()`` explicitly.

    ``progress_callback``, when given, is threaded through EVERY turn fired
    in this call -- identity turns and the requirements turn (brand-new
    session only) as well as the actual message turn -- so a first-ever
    chat message shows live progress across its setup turns too, not just
    the final reply turn. See ``_execute_turn``.

    Fix 1: chat used to bypass ALL guidance. ``notify_service`` created an
    ad-hoc session with no automation object at all, so none of
    ``runner.run()``'s guidance-loading machinery ever ran -- the model saw
    only its default coding-assistant persona plus whatever it inspected in
    ``cwd`` (this repo's own git working tree), which is exactly how "what
    needs my attention?" turned into a ``git status`` report. Routing chat
    through a real ``Automation`` (``automations/chat.md``) with a real
    ``requires:`` list closes that gap using the exact same mechanism Fix 2
    made real for every other automation.

    On a brand-new (or confirmed-missing) pinned session: this fires each
    of ``chat_automation.steps`` as its own sequential turn (identity/scope
    framing -- who chat is, what this repo is NOT), then, if there are any
    file-based ``requires:``, injects their content as one more turn. Only
    the very first of these turns uses ``--fresh``. Every subsequent chat
    message resumes the SAME pinned session directly -- the identity and
    guidance turns are never repeated (they're already in the transcript),
    giving chat cross-conversation memory for free.

    Every message still re-verifies (cheap, local, no LLM call) that every
    ``requires:`` entry is satisfied before proceeding -- deliberately NOT
    re-injecting the file content on every message (unlike ``run()``, which
    fires at most every few minutes; chat fires per message, and paying an
    extra LLM turn for every single exchange would be a bad trade for
    content that's already in the transcript). A requirement that goes
    unsatisfied mid-conversation (e.g. a guidance file deleted) aborts that
    message with a clear error rather than proceeding degraded.

    Reuses ``run()``'s pinned-session defense verbatim: a confirmed-missing
    session id is safe to recreate (and is re-pinned); anything else
    indeterminate aborts rather than risk silently orphaning the pin.
    """
    cwd = Path(cwd).expanduser()
    runs_dir = Path(runs_dir).expanduser()
    run_id = new_run_id()
    started_at = _iso8601_now()
    resolved_wait_seconds = (
        lock_wait_seconds
        if lock_wait_seconds is not None
        else _interactive_lock_wait_seconds()
    )

    pin = session_pins.get(chat_automation.slug, runs_dir=runs_dir)
    pinned_session_id = pin.session_id if pin else None
    is_new_session: bool
    session_id: str

    if pinned_session_id and force_new:
        session_id = f"{chat_automation.slug}-{run_id}"
        is_new_session = True
        print(
            f"[{chat_automation.name}] force_new requested (New conversation) -- "
            f"leaving pinned session {pinned_session_id!r} untouched on disk and "
            f"creating {session_id!r} as the new default",
            file=sys.stderr,
        )
    elif pinned_session_id:
        probe_status, probe_detail = _probe_session(
            pinned_session_id,
            cwd=cwd,
            recorded_workspace=pin.session_workspace if pin else None,
        )
        if probe_status is _SessionProbe.EXISTS:
            session_id = pinned_session_id
            is_new_session = False
            print(
                f"[{chat_automation.name}] resuming pinned chat session "
                f"{session_id!r} ({probe_detail})",
                file=sys.stderr,
            )
        elif probe_status is _SessionProbe.MISSING:
            session_id = f"{chat_automation.slug}-{run_id}"
            is_new_session = True
            print(
                f"[{chat_automation.name}] pinned chat session "
                f"{pinned_session_id!r} confirmed missing ({probe_detail}) -- "
                f"creating new session {session_id!r} and re-pinning",
                file=sys.stderr,
            )
        else:
            if probe_status is _SessionProbe.WORKSPACE_MISMATCH:
                abort_message = (
                    f"pinned chat session {pinned_session_id!r} could not be "
                    f"safely resumed ({probe_detail}). ABORTING rather than "
                    "silently creating a fresh session, which would permanently "
                    "discard all accumulated memory. To recover: confirm the "
                    "project's real location, then either fix this "
                    "automation's pin by hand in the engine's session pin "
                    f"store ({session_pins.pins_path(runs_dir)}), or run "
                    f"`drumbeat rotate-session {chat_automation.slug} "
                    "--workspace <dir> [--data-dir <dir>] --reason '<why>'` to "
                    "deliberately abandon the old session and start fresh."
                )
            else:  # _SessionProbe.UNKNOWN
                abort_message = (
                    f"cannot determine whether pinned chat session "
                    f"{pinned_session_id!r} exists ({probe_detail}) -- aborting "
                    "rather than risk silently orphaning the pin or "
                    "double-creating a session"
                )
            print(f"[{chat_automation.name}] ABORTED: {abort_message}", file=sys.stderr)
            finished_at = _iso8601_now()
            result = RunResult(
                automation=chat_automation.name,
                run_id=run_id,
                session_id=pinned_session_id,
                started_at=started_at,
                finished_at=finished_at,
                steps=[],
                final_reply="",
                notified=False,
                failed=True,
                error=abort_message,
                session_resumed=False,
            )
            _persist_run(
                automation=chat_automation,
                result=result,
                runs_dir=runs_dir,
                stderr_chunks=[],
                intent=_aborted_run_intent(abort_message),
                final_reply_rule=FINAL_REPLY_RULE_ABORTED,
            )
            return result
    else:
        session_id = f"{chat_automation.slug}-{run_id}"
        is_new_session = True
        print(
            f"[{chat_automation.name}] no pinned chat session -- creating {session_id!r}",
            file=sys.stderr,
        )

    if is_new_session:
        # Same posture as run(): ABORT, never warn-and-proceed. A chat whose
        # pin never landed answers once and then forgets the conversation it
        # just had, every single time -- and the user, who can see the reply,
        # has no way to know.
        try:
            session_pins.upsert(
                chat_automation.slug,
                session_id=session_id,
                session_workspace=_derive_workspace_slug(cwd),
                created_by=session_pins.CREATED_BY_CHAT,
                runs_dir=runs_dir,
            )
        except session_pins.PinStoreError as exc:
            abort_message = (
                f"failed to record the pinned chat session {session_id!r} in "
                f"{session_pins.pins_path(runs_dir)}: {exc} -- ABORTING before "
                "sending any turn. Proceeding would answer this message and "
                "then lose the conversation it happened in."
            )
            print(f"[{chat_automation.name}] ABORTED: {abort_message}", file=sys.stderr)
            finished_at = _iso8601_now()
            result = RunResult(
                automation=chat_automation.name,
                run_id=run_id,
                session_id=session_id,
                started_at=started_at,
                finished_at=finished_at,
                steps=[],
                final_reply="",
                notified=False,
                failed=True,
                error=abort_message,
                session_resumed=False,
            )
            _persist_run(
                automation=chat_automation,
                result=result,
                runs_dir=runs_dir,
                stderr_chunks=[],
                intent=_aborted_run_intent(abort_message),
                final_reply_rule=FINAL_REPLY_RULE_ABORTED,
            )
            return result

        print(
            f"[{chat_automation.name}] pinned chat session {session_id!r} "
            f"recorded in {session_pins.pins_path(runs_dir).name}",
            file=sys.stderr,
        )

    # Fix 2's gate, every message: cheap, local, no LLM call.
    requirement_checks = check_requirements(
        chat_automation.requires, cwd=cwd, runs_dir=runs_dir
    )
    unsatisfied = [c for c in requirement_checks if not c.satisfied]
    if unsatisfied:
        abort_message = _unsatisfied_requirements_message(requirement_checks)
        print(f"[{chat_automation.name}] ABORTED: {abort_message}", file=sys.stderr)
        finished_at = _iso8601_now()
        result = RunResult(
            automation=chat_automation.name,
            run_id=run_id,
            session_id=session_id,
            started_at=started_at,
            finished_at=finished_at,
            steps=[],
            final_reply="",
            notified=False,
            failed=True,
            error=abort_message,
            session_resumed=not is_new_session,
        )
        _persist_run(
            automation=chat_automation,
            result=result,
            runs_dir=runs_dir,
            stderr_chunks=[],
            intent=_aborted_run_intent(abort_message),
            final_reply_rule=FINAL_REPLY_RULE_ABORTED,
        )
        return result

    turns: list[StepResult] = []
    stderr_chunks: list[tuple[int, str]] = []
    turn_index = itertools.count()
    failed = False

    if is_new_session:
        # Identity/scope-establishing turns, fired ONCE ever for this pinned
        # session -- exactly like an ordinary automation's numbered steps,
        # just never repeated on later messages.
        for i, step in enumerate(chat_automation.steps, start=1):
            print(
                f"[{chat_automation.name}] identity turn {i}/{len(chat_automation.steps)} "
                f"({step.id})",
                file=sys.stderr,
            )
            index = next(turn_index)
            result, stderr_text = _execute_turn(
                session_id=session_id,
                fresh=(i == 1),
                cwd=cwd,
                text=step.prompt,
                index=index,
                runs_dir=runs_dir,
                wait_seconds=resolved_wait_seconds,
                progress_callback=progress_callback,
                host_config_path=host_config_path,
                # Setup turns carry no injector preamble -- injectors ride the
                # message turn that answers the person (see the message turn
                # below). A brand-new session's identity turns run once, ever,
                # and are not seeded from the owner's message.
            )
            turns.append(result)
            stderr_chunks.append((index, stderr_text))
            if result.error:
                print(
                    f"[{chat_automation.name}] identity turn {i} FAILED: "
                    f"{result.error} -- aborting",
                    file=sys.stderr,
                )
                failed = True
                break

        if not failed:
            requirements_turn_text = format_requirements_turn(
                requirement_checks, mode=chat_automation.guidance_delivery
            )
            if requirements_turn_text is not None:
                print(f"[{chat_automation.name}] requirements turn", file=sys.stderr)
                index = next(turn_index)
                req_result, req_stderr = _execute_turn(
                    session_id=session_id,
                    fresh=False,
                    cwd=cwd,
                    text=requirements_turn_text,
                    index=index,
                    runs_dir=runs_dir,
                    wait_seconds=resolved_wait_seconds,
                    progress_callback=progress_callback,
                    host_config_path=host_config_path,
                    # No injector preamble on the requirements turn -- see the
                    # message turn below.
                )
                turns.append(req_result)
                stderr_chunks.append((index, req_stderr))
                if req_result.error:
                    print(
                        f"[{chat_automation.name}] requirements turn FAILED: "
                        f"{req_result.error} -- aborting",
                        file=sys.stderr,
                    )
                    failed = True

    final_reply = ""
    if not failed:
        index = next(turn_index)
        message_result, message_stderr = _execute_turn(
            session_id=session_id,
            fresh=False,
            cwd=cwd,
            text=text,
            index=index,
            runs_dir=runs_dir,
            wait_seconds=resolved_wait_seconds,
            progress_callback=progress_callback,
            host_config_path=host_config_path,
            # Injector preamble blocks ride ONLY the message turn -- the turn
            # that answers the person. The identity/requirements setup turns
            # (fired once, ever, on a brand-new session) carry different text and
            # are not seeded from the owner's message, so a block would be a
            # mismatch there.
            preamble_blocks=preamble_blocks,
        )
        turns.append(message_result)
        stderr_chunks.append((index, message_stderr))
        if message_result.error:
            print(
                f"[{chat_automation.name}] chat message turn FAILED: "
                f"{message_result.error}",
                file=sys.stderr,
            )
            failed = True
        else:
            final_reply = message_result.reply

    finished_at = _iso8601_now()
    # Loudness (silent-failure defect class): derive the top-level error from
    # the FIRST failing turn, not just ``turns[-1]``. The old ``turns[-1].error``
    # check only worked because every failure above either ``break``s its loop
    # or is followed solely by ``if not failed:``-gated code -- true today, but
    # a positional assumption, not a structural guarantee. This is exactly the
    # "weaker turns[-1].error pattern" ``_persist_run``'s own choke-point fix
    # named as the next latent silent-failure risk (see that function's
    # docstring) without actually closing it here. Mirrors ``_run_body``'s own
    # derivation (same file, same fallback text) so both entry points use one
    # refactor-proof rule instead of two different ones.
    error = (
        next(
            (t.error for t in turns if t.error),
            "run failed but no turn recorded an error -- see this run's "
            "stderr.log and step artifacts",
        )
        if failed
        else None
    )

    result = RunResult(
        automation=chat_automation.name,
        run_id=run_id,
        session_id=session_id,
        started_at=started_at,
        finished_at=finished_at,
        steps=turns,
        final_reply=final_reply,
        notified=False,  # chat replies return over HTTP; never pushed
        failed=failed,
        error=error,
        session_resumed=not is_new_session,
    )
    _persist_run(
        automation=chat_automation,
        result=result,
        runs_dir=runs_dir,
        stderr_chunks=stderr_chunks,
        intent=_chat_run_intent(final_reply),
        final_reply_rule=FINAL_REPLY_RULE_CHAT,
    )
    return result


def _record_ci_upload_outcome(
    *,
    runs_dir: Path,
    automation: Automation,
    result: RunResult,
    outcome: ci_upload.UploadOutcome,
) -> None:
    """Patch this run's already-persisted result.json with the CI upload outcome.

    Called AFTER ``_persist_run`` has already written result.json (see the
    end of ``run()``) -- the upload only starts once the run's own result
    is safely on disk, so a slow or unreachable CI server can never delay
    persisting the actual automation outcome. FAIL LOUD, NO SILENT
    FALLBACKS: an un-recorded upload failure is exactly the kind of silent
    failure this project has repeatedly been bitten by (see this module's
    docstring) -- every outcome, success or failure or skipped, lands in
    result.json, never just in a log line that scrolls away.
    """
    result.ci_upload_attempted = outcome.attempted
    result.ci_upload_exit_code = outcome.exit_code
    result.ci_upload_error = outcome.error

    run_dir = runs_dir / automation.slug / result.run_id
    result_path = run_dir / "result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(asdict(result), f, indent=2)
        f.write("\n")


def _log_run_failure(
    automation: Automation, result: RunResult, *, runs_dir: Path
) -> None:
    """Append one line to ``runs/failures.log`` for every failed run.

    FAIL LOUD: before this, a failed run's only trace was ``result.json``'s
    ``"failed": true`` field, one file among hundreds under
    ``runs/<automation>/<run_id>/`` -- invisible without deliberately
    scanning every run directory (verified: a real 15-minute-old channels-check
    failure and two others from the same afternoon were on disk, correctly
    recorded, and functionally undiscoverable without exactly that scan).
    This is the single, greppable, append-only place every failure lands,
    same pattern as ``_log_subscription_removal`` and
    ``_record_demotion`` above. A write failure here is printed to stderr;
    it must never mask (or re-raise over) the failure it's trying to record.
    """
    runs_dir = Path(runs_dir).expanduser()
    log_path = runs_dir / "failures.log"
    error_preview = (result.error or "").strip().replace("\n", " ")[:300]
    if not error_preview:
        # A per-step error (main step loop) rather than a top-level abort --
        # find the first step that actually failed rather than logging a
        # blank reason.
        step_error = next((s.error for s in result.steps if s.error), None)
        error_preview = (
            step_error or "unknown failure -- no error text recorded"
        ).strip()[:300]
    line = (
        f"{_iso8601_now()} automation={automation.name} run_id={result.run_id} "
        f"session_id={result.session_id} error={error_preview!r}\n"
    )
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        print(f"[failures] failed to write {log_path}: {exc}", file=sys.stderr)


def _notify_run_failure(
    automation: Automation, result: RunResult, *, runs_dir: Path
) -> None:
    """Emit the ``automation_error`` event for a failed run, unconditionally.

    FAIL LOUD, NOT A POLICY CHOICE: every other notification decision in
    this module (``notify: always|auto|never|urgent-only``) is the
    automation author's call, expressed in the automation's own
    frontmatter -- but "did a run fail" is not one of those judgment
    calls, it is a safety property, so it bypasses ``automation.notify``
    entirely (a ``notify: never`` automation still reports a failure).

    Post-seam, this emits rather than pushes: the delivery worker forwards
    ``automation_error`` events to the phone, so the loudness is unchanged
    and the transport is on the far side of the boundary. The event is
    fsync'd before its lock is released -- a failure report that existed
    only in the page cache would be the silence this exists to break.
    Best-effort only, same as before: an emit failure must never mask the
    failure it is reporting.
    """
    error_text = (result.error or "").strip()
    if not error_text:
        error_text = "see runs/failures.log and this run's stderr.log for detail"
    text = (
        f"AUTOMATION FAILED: {automation.name}\n"
        f"run_id: {result.run_id}\n"
        f"session_id: {result.session_id}\n"
        f"{error_text}"
    )
    try:
        engine_events.append_event(
            runs_dir,
            engine_events.EventType.AUTOMATION_ERROR,
            {
                "run_id": result.run_id,
                "automation": automation.name,
                "automation_slug": automation.slug,
                "session_id": result.session_id,
                "reason": error_text,
                "text": text,
            },
        )
    except Exception as exc:  # noqa: BLE001 - never let a failure NOTIFICATION mask the failure
        print(
            f"[failures] failed to emit automation_error event for "
            f"{automation.name} run {result.run_id}: {exc}",
            file=sys.stderr,
        )


def _persist_run(
    *,
    automation: Automation,
    result: RunResult,
    runs_dir: Path,
    stderr_chunks: list[tuple[int, str]],
    intent: DeliveryIntent,
    final_reply_rule: str,
) -> None:
    """Write a run's artifacts AND its delivery record, in that order of guarantee.

    ``intent`` is required with no default on purpose. This is the single
    choke point every run passes through, so making the intent a required
    parameter here is what makes "a run record without a delivery-intent
    event is an invalid run" (section 6) structurally true instead of a
    convention someone remembers: a new call site cannot compile without
    deciding, and recording, what happened to the output.

    ``final_reply_rule`` names WHICH reply became ``result.final_reply`` and
    by what rule -- the first of the three gates that killed Daily Rollup,
    which previously left no record at all.

    The events are emitted BEFORE result.json is written. A crash between
    the two leaves an intent with no run record, which the invalid-run sweep
    reports; the inverse ordering would leave a run whose output silently
    never reached anyone, which is the exact failure class this seam exists
    to close.
    """
    runs_dir = Path(runs_dir).expanduser()

    # Loudness invariant, enforced HERE at the choke point -- not trusted to
    # each caller. Every RunResult-constructing path is *supposed* to lift a
    # failing turn's error to the top level, but "supposed to" is exactly what
    # let the silent ``failed: true, error: null`` shape onto the owner's box:
    # the earlier fix taught only ``_run_body`` to do it, while the other
    # callers used weaker patterns (``run_chat_message``'s ``turns[-1].error``)
    # and result.json + the RUN_COMPLETED event below still wrote
    # ``result.error`` VERBATIM. ``_log_run_failure`` / ``_notify_run_failure``
    # already derived a step fallback, so failures.log and the automation_error
    # notification were loud -- but the run RECORD and the RUN_COMPLETED event
    # (what ``drumbeat doctor`` and the runs API read) stayed silent. Measured on
    # this box: recency-check 4/4 failed with ``"error": null`` while
    # ``steps[0].error`` held the real ``"amplifier-agent exited 1"``. Normalize
    # ONCE, before either surface is written, so all four agree and no present
    # or future caller can reintroduce the gap. Guarded by ``failed`` so a
    # successful run is never given a manufactured error.
    if result.failed and not (result.error or "").strip():
        result.error = next(
            (s.error for s in result.steps if s.error),
            "run failed but no turn recorded an error -- see this run's "
            "stderr.log and step artifacts",
        )

    engine_events.append_event(
        runs_dir,
        engine_events.EventType.RUN_COMPLETED,
        {
            "run_id": result.run_id,
            "automation": automation.name,
            "automation_slug": automation.slug,
            "session_id": result.session_id,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "failed": result.failed,
            "error": result.error,
            "final_reply_rule": final_reply_rule,
            "final_reply": result.final_reply,
            "steps": [
                {
                    "index": step.index,
                    "id": step.step_id,
                    "reply": step.reply,
                    "error": step.error,
                    "duration_ms": step.duration_ms,
                    "tokens_in": step.tokens_in,
                    "tokens_out": step.tokens_out,
                }
                for step in result.steps
            ],
        },
    )
    engine_events.append_event(
        runs_dir,
        engine_events.EventType.DELIVERY_INTENT,
        {
            "run_id": result.run_id,
            "automation": automation.name,
            "automation_slug": automation.slug,
            "session_id": result.session_id,
            "notify_policy": automation.notify,
            "verdict": intent.verdict.value,
            "gate": intent.gate.value,
            "reason": intent.reason,
            "text": intent.text,
        },
    )

    run_dir = runs_dir / automation.slug / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    result_path = run_dir / "result.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(asdict(result), f, indent=2)
        f.write("\n")

    for step in result.steps:
        step_path = run_dir / f"step-{step.index:02d}.txt"
        with open(step_path, "w", encoding="utf-8") as f:
            f.write(step.reply if step.reply else (step.error or ""))
            f.write("\n")

    stderr_path = run_dir / "stderr.log"
    with open(stderr_path, "w", encoding="utf-8") as f:
        for index, chunk in stderr_chunks:
            f.write(f"=== turn {index} ===\n")
            f.write(chunk)
            if not chunk.endswith("\n"):
                f.write("\n")

    # Fail loud: this is the single choke point every run (scheduled
    # automation or chat message) already passes through to persist its
    # result -- see the 6 call sites across run() and run_chat_message().
    # A failed run is logged AND pushed here so no future call site can
    # forget either half of "fail loud, don't bury it in result.json".
    if result.failed:
        _log_run_failure(automation, result, runs_dir=runs_dir)
        _notify_run_failure(automation, result, runs_dir=runs_dir)


def _synthesize_session_automation(session_id: str) -> Automation:
    """A minimal, file-less ``Automation`` describing a bare-session (reply)
    turn, purely so it can pass through ``_persist_run`` -- the single
    run-persistence choke point -- exactly like a real automation's run.

    No frontmatter backs a bare-session turn, so nothing is loaded from disk. The run
    record it produces carries its own authoritative display name via
    ``RunResult.automation`` (set to ``session_id``), so
    ``management_api._resolve_automation_name`` resolves the name straight from
    the record and never needs to find this synthetic automation on disk.

    ``slug`` is the filesystem-safe form of the session id (``_sanitize_run_id``,
    the same charset ``management_api._SAFE_SEGMENT_RE`` accepts for a
    ``GET /api/runs/<slug>/<run_id>`` path), so every turn of one bare-session
    conversation groups under ``runs/<session-slug>/``. ``notify: never``
    matches the delivery posture: the reply returns over the turn API and is
    never pushed from this path.
    """
    return Automation(
        name=session_id,
        enabled=False,
        trigger=Trigger(type="manual", expression=None),
        notify="never",
        requires=[],
        steps=[],
        path=Path(f"<session:{session_id}>"),
        slug=_sanitize_run_id(session_id),
    )


def _persist_session_turn(
    *,
    session_id: str,
    run_id: str,
    started_at: str,
    result: StepResult,
    stderr_text: str,
    runs_dir: Path,
) -> None:
    """Persist a bare-session (reply) turn as a first-class run record.

    Bare-session reply persistence (the defect below): ``/api/turns``
    (see ``turns.py``) routes a chat/automation turn through
    ``run_chat_message`` -> ``_persist_run`` -- a retrievable run record plus
    transcript artifacts -- but a bare-``session_id`` turn (the reply path)
    runs through ``resume_turn``, which
    historically executed the turn and returned the reply while writing NO run
    record at all. Its only trace was the caller's ``turns/<turn_id>.json``, a
    location the runs API (``management_api.list_runs`` / ``get_run_detail``)
    does not read -- so a typed reply left no retrievable transcript while chat
    automations were saved.

    This wires the session/reply path to the SAME persistence choke point every
    other run passes through, so a bare-session reply turn is retrievable
    exactly like a chat run.

    IMPORTANT SCOPE (do not let this drift back into a false claim): this
    persists only turns that actually run THROUGH drumbeat. A realtime
    interaction a consumer streams straight to the provider never reaches
    ``resume_turn`` at all, so the engine has nothing to persist for it and
    makes no claim to -- that record, if any, is the consumer's to keep.

    ONE carve-out, and it is not a silent skip: ``ERROR_KIND_SESSION_LOCKED``
    means the per-session lock was never acquired, so no subprocess ever ran
    and NO transcript exists -- there is genuinely nothing to persist, and the
    outcome is backpressure ("resend your text"), not a run failure. Every
    other outcome -- a real reply, a timeout, a non-zero exit, a spawn failure
    -- persists here, failure logging and notification included, identical to a
    chat run.
    """
    if result.error_kind == ERROR_KIND_SESSION_LOCKED:
        return

    finished_at = _iso8601_now()
    failed = result.error is not None
    run_result = RunResult(
        automation=session_id,
        run_id=run_id,
        session_id=session_id,
        started_at=started_at,
        finished_at=finished_at,
        steps=[result],
        final_reply=result.reply,
        # bare-session replies return over the turn API; never pushed
        notified=False,
        failed=failed,
        # A per-step error IS the run's error here (one turn == one run), so
        # surface it at the top level too -- same as run_chat_message -- so
        # failures.log and the automation_error notification carry the real
        # reason rather than a generic placeholder.
        error=result.error if failed else None,
        session_resumed=True,
    )
    _persist_run(
        automation=_synthesize_session_automation(session_id),
        result=run_result,
        runs_dir=runs_dir,
        stderr_chunks=[(result.index, stderr_text)],
        intent=_session_run_intent(run_result.final_reply),
        final_reply_rule=FINAL_REPLY_RULE_SESSION,
    )
