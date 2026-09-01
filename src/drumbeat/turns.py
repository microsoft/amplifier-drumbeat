"""``POST /api/turns`` -- the engine's turn surface (decomposition step 5;
see docs/ARCHITECTURE.md).

This is the last piece of engine code to leave the consumer's process. Until
now the consumer's notify-serve executed reply/message turns **in-process**,
importing the engine as a library: two OS processes running engine code
against one workspace, safe only because the pre-step-3 flock work made
every shared state file arbitrated (section 4, council amendment 5). That
hybrid window closes here.

What lives on which side is section 7.3's split, at the identifier:

- **The consumer owns** ``notification_id -> session_id``. Its map, its 404
  ("refusing to guess a session"). It also owns the reply record, dedup,
  and the completion push.
- **the engine owns** "append a turn to session X". Its lock, its 404.
  The SAME per-session flock that arbitrates scheduled runs arbitrates
  replies -- one lock namespace, so the "two processes resuming one
  session" corruption (constraint 6) cannot re-enter through the new door.

Neither side can hold the other's state, which is the point: a module-level
"current session" anywhere in this chain is the Scout correlation bug this
system exists to design past.

**Why 202-then-poll and not a synchronous answer.** A turn is 3-14 minutes
of real work. The HTTP response returns as soon as the turn is accepted and
its record exists; the caller polls ``GET /api/turns/{turn_id}``. A record
is written BEFORE ``submit_turn`` returns, so a caller polling immediately
never sees a 404 for an id this module itself minted.

**The three honest refusals**, each with a body that names the refusal:

- ``400`` -- a malformed request (no text, no origin, neither or both of
  ``session_id``/``automation_slug``).
- ``404`` -- an unknown session, or an unknown automation slug. Never a
  guess, never a fallback to "some session".
- ``423`` -- the session's flock is held by another in-flight turn AND the
  caller did not ask to wait (``lock_wait_seconds <= 0``). Honest, not
  queued: no turn record is created, so there is nothing half-accepted to
  poll. The response carries ``retry_after_seconds``.

**A 423 must never lose the user's typed text** (section 7.3's council
flag). The server half of that guarantee is this: the engine refuses
*before* it takes ownership of anything, so the text is still wholly in the
caller's hands -- the consumer has already persisted it in its own reply record
by the time it POSTs here. The client half (keep the draft, re-offer send)
is the parked UX redesign.

**Waiting is the caller's decision, not this module's.** ``lock_wait_seconds``
comes in on the request. The consumer's reply path passes its background budget
(``runner.background_lock_wait_seconds()``, 15 minutes), which is exactly
what it passed to ``runner.resume_turn`` in-process before this cutover --
so the behaviour a phone sees on a reply that lands mid-run is unchanged.
A caller that passes 0 (or omits it) gets the immediate 423 instead. Same
for ``ceiling_seconds``: the consumer owns its own outer bound and states
it per request, rather than this module growing an opinion about how long
somebody else's user should wait.

**The owner-priority marker.** ``priority`` is an OPTIONAL
field, one valid value today (``"owner"``), absent by default -- a caller
that never sends it is unaffected byte-for-byte. It exists because the
423 branch below is a bare lock probe with no ordering: whoever calls
``flock`` first wins, so a scheduled automation sharing a session with the
owner's own conversation can keep winning that race purely on timing while
a consumer retries the honest 423 (see ``docs/ISSUE_CASE_STUDIES.md``-style
incident: an owner-typed message rejected 16 times over ~4m42s). When a
423 is about to be raised for a ``priority="owner"`` request, this module
records "the owner is contending for this session" in
``drumbeat.owner_priority`` -- a short-lived, in-process latch the
scheduler consults before STARTING a new automation turn on that same
session, deferring it (never dropping it) for a bounded number of ticks.
This module never waits, never queues, and never learns whether the
scheduler actually deferred anything -- it only leaves the signal.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from drumbeat import (
    agent_config,
    fsutil,
    injectors,
    owner_priority,
    runner,
    session_pins,
)
from drumbeat.automation import AutomationError, load_by_slug
from drumbeat.management_api import EngineContext

TURNS_DIRNAME = "turns"

# Terminal + non-terminal statuses. Section 5 names exactly three
# (``running | done | failed``) and this reports exactly three -- "waiting
# for the lock" is a PHASE of running, not a fourth status, because from
# the caller's side the turn is accepted and in flight either way.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED})

# The phase distinction the status enum deliberately hides from the caller
# but reconciliation genuinely needs: has a subprocess been launched yet?
PHASE_WAITING_FOR_LOCK = "waiting_for_lock"
PHASE_EXECUTING = "executing"
PHASE_FINISHED = "finished"

# Defaults used only when the caller states no preference. Both are
# deliberately caller-supplied per request (see the module docstring): the
# engine has no business deciding how long another project's user waits.
DEFAULT_CEILING_SECONDS = 1200.0  # 20 minutes
DEFAULT_LOCK_WAIT_SECONDS = 0.0  # no wait => an immediate, honest 423

_HEARTBEAT_INTERVAL_SECONDS = 15.0

# Minimum spacing between two progress writes for the SAME (tool, activity)
# pair. amplifier-agent's NDJSON stream emits many events per second; a
# genuine activity CHANGE always writes immediately regardless.
_PROGRESS_WRITE_MIN_INTERVAL_SECONDS = 1.0

# Advertised on a 423 so a client has a number rather than a guess. One
# poll interval more than the delivery worker's, i.e. "ask again shortly".
RETRY_AFTER_SECONDS = 5

_VALID_ORIGINS = frozenset({"reply", "item_reply", "message", "chat", "manual"})

# The owner-priority marker. One value today; open to grow
# (e.g. a future non-owner priority tier) without touching every caller,
# same discipline as `_VALID_ORIGINS`. Absent (`None`) is the default and
# preserves today's behavior byte-for-byte -- see `owner_priority`.
PRIORITY_OWNER = "owner"
_VALID_PRIORITIES = frozenset({PRIORITY_OWNER})

# Re-exported so a consumer branching on a turn's ``failure_kind`` compares
# against a named constant rather than a copied string literal.
FAILURE_KIND_SESSION_LOCKED = runner.ERROR_KIND_SESSION_LOCKED

# One lock per turn record, because three threads legitimately write the
# same file (the executor, its heartbeat, its ceiling timer). In-process is
# sufficient and correct: turn records have exactly one writer PROCESS --
# this engine -- which is section 4's single-writer-per-file rule, held.
_REGISTRY_LOCK = threading.Lock()
_RECORD_LOCKS: dict[str, threading.Lock] = {}


class TurnError(Exception):
    """A refusal with the HTTP status to send and a body that names it."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        self.status = status
        self.message = message
        self.extra = extra
        super().__init__(message)


@dataclass(frozen=True)
class TurnRequest:
    """One validated turn request. Built only by ``parse_request``."""

    session_id: str | None
    automation_slug: str | None
    text: str
    origin: str
    lock_wait_seconds: float
    ceiling_seconds: float
    new_session: bool
    profile: str | None
    priority: str | None


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def turns_dir(runs_dir: Path) -> Path:
    return Path(runs_dir).expanduser() / TURNS_DIRNAME


def turn_path(runs_dir: Path, turn_id: str) -> Path:
    return turns_dir(runs_dir) / f"{turn_id}.json"


def new_turn_id() -> str:
    return f"t-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def _record_lock(turn_id: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _RECORD_LOCKS.get(turn_id)
        if lock is None:
            lock = threading.Lock()
            _RECORD_LOCKS[turn_id] = lock
        return lock


def _read_record(runs_dir: Path, turn_id: str) -> dict[str, Any] | None:
    path = turn_path(runs_dir, turn_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TurnError(500, f"turn record {turn_id!r} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise TurnError(500, f"turn record {turn_id!r} is not a JSON object")
    return payload


def _write_record(runs_dir: Path, record: dict[str, Any]) -> None:
    fsutil.atomic_write(
        turn_path(runs_dir, record["turn_id"]), json.dumps(record, indent=2) + "\n"
    )


def _update_record(
    runs_dir: Path, turn_id: str, **fields: Any
) -> dict[str, Any] | None:
    """Read-modify-write one turn record under its in-process lock.

    Returns the updated record, or ``None`` if it has vanished (which can
    only mean somebody deleted it out from under us -- never normal).
    """
    with _record_lock(turn_id):
        record = _read_record(runs_dir, turn_id)
        if record is None:
            return None
        record.update(fields)
        record["updated_at"] = _iso_now()
        _write_record(runs_dir, record)
        return record


def _finalize_if_not_terminal(
    runs_dir: Path, turn_id: str, **fields: Any
) -> dict[str, Any] | None:
    """Write a terminal outcome ONLY if one hasn't landed already.

    Race-safe no-op when the real outcome beat us here -- used by the
    ceiling watchdog, whose tombstone is a conservative placeholder for "we
    don't know what happened", never a verdict that suppresses a later
    truth.
    """
    with _record_lock(turn_id):
        record = _read_record(runs_dir, turn_id)
        if record is None:
            return None
        if record.get("status") in TERMINAL_STATUSES:
            return None
        record.update(fields)
        record["updated_at"] = _iso_now()
        _write_record(runs_dir, record)
        return record


# ---- request validation ----


def _coerce_positive_float(value: Any, *, field: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TurnError(400, f"{field!r} must be a number of seconds")
    result = float(value)
    if result < 0:
        raise TurnError(400, f"{field!r} must not be negative")
    return result


def parse_request(body: dict[str, Any]) -> TurnRequest:
    """Validate one turn request body. FAIL LOUD on every shape problem.

    ``origin`` is required with no default -- the design rule carried
    across this boundary (section 5): every "required" field is
    required-with-no-default, so a turn can never arrive unattributed.
    """
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TurnError(400, "'text' is required and must be a non-empty string")

    origin = body.get("origin")
    if not isinstance(origin, str) or not origin.strip():
        raise TurnError(
            400,
            "'origin' is required and must be a non-empty string (one of: "
            f"{', '.join(sorted(_VALID_ORIGINS))}) -- a turn is never accepted "
            "unattributed",
        )
    if origin not in _VALID_ORIGINS:
        raise TurnError(
            400,
            f"unknown origin {origin!r} -- must be one of: "
            f"{', '.join(sorted(_VALID_ORIGINS))}",
        )

    session_id = body.get("session_id")
    automation_slug = body.get("automation_slug")
    if session_id is not None and not isinstance(session_id, str):
        raise TurnError(400, "'session_id' must be a string")
    if automation_slug is not None and not isinstance(automation_slug, str):
        raise TurnError(400, "'automation_slug' must be a string")
    session_id = session_id.strip() if isinstance(session_id, str) else None
    automation_slug = (
        automation_slug.strip() if isinstance(automation_slug, str) else None
    )
    if bool(session_id) == bool(automation_slug):
        raise TurnError(
            400,
            "exactly one of 'session_id' or 'automation_slug' is required -- "
            "naming both is ambiguous about which session this turn targets, "
            "and naming neither would make this endpoint guess",
        )

    new_session = body.get("new_session", False)
    if not isinstance(new_session, bool):
        raise TurnError(400, "'new_session' must be a boolean")
    if new_session and session_id:
        raise TurnError(
            400,
            "'new_session' is only meaningful with 'automation_slug' -- naming "
            "an explicit session_id AND asking for a new session contradicts "
            "itself",
        )

    # ``profile`` is OPTIONAL: a turn that names one selects that profile's
    # agent-config overlay (its provider/model/etc.) from the workspace
    # ``agent-config.yaml`` ``profiles:`` block -- layer 3 of the shared config
    # merge. Absent means "no profile" -- the turn resolves against the base +
    # ``default:`` layers exactly as a profile-less turn, so every existing
    # caller is unaffected until it opts in. The NAME vocabulary is OPEN (the
    # owner picks it), so its existence cannot be checked against a constant
    # here; ``submit_turn`` validates it against the workspace's declared
    # profiles and fails loud listing the available ones. This step only refuses
    # a structurally malformed value (present-but-not-a-non-empty-string).
    profile = body.get("profile")
    if profile is not None:
        if not isinstance(profile, str) or not profile.strip():
            raise TurnError(400, "'profile' must be a non-empty string when present")
        profile = profile.strip()

    # ``priority`` is OPTIONAL and, unlike ``profile``, CLOSED:
    # it names a mechanism internal to this engine (the owner-priority
    # latch, see ``owner_priority``), not an owner-defined vocabulary, so an
    # unknown value is refused here rather than silently accepted and
    # silently doing nothing. Absent is the default and changes nothing.
    priority = body.get("priority")
    if priority is not None:
        if not isinstance(priority, str) or not priority.strip():
            raise TurnError(400, "'priority' must be a non-empty string when present")
        priority = priority.strip()
        if priority not in _VALID_PRIORITIES:
            raise TurnError(
                400,
                f"unknown priority {priority!r} -- must be one of: "
                f"{', '.join(sorted(_VALID_PRIORITIES))}",
            )

    return TurnRequest(
        session_id=session_id or None,
        automation_slug=automation_slug or None,
        text=text,
        origin=origin,
        lock_wait_seconds=_coerce_positive_float(
            body.get("lock_wait_seconds"),
            field="lock_wait_seconds",
            default=DEFAULT_LOCK_WAIT_SECONDS,
        ),
        ceiling_seconds=_coerce_positive_float(
            body.get("ceiling_seconds"),
            field="ceiling_seconds",
            default=DEFAULT_CEILING_SECONDS,
        ),
        new_session=new_session,
        profile=profile,
        priority=priority,
    )


# ---- progress / heartbeat / ceiling ----


def _make_progress_sink(runs_dir: Path, turn_id: str):
    """Mirror live NDJSON progress into the turn record, throttled.

    Best-effort and never fatal: a slow or failing progress write must
    never interrupt the real agent turn. Only the turn's actual terminal
    outcome is fail-loud.
    """
    last_write_monotonic = 0.0
    last_written_key: tuple[str | None, str] | None = None

    def _sink(event: runner.ProgressEvent) -> None:
        nonlocal last_write_monotonic, last_written_key
        now = time.monotonic()
        key = (event.tool, event.activity)
        changed = key != last_written_key
        if (
            not changed
            and (now - last_write_monotonic) < _PROGRESS_WRITE_MIN_INTERVAL_SECONDS
        ):
            return
        last_write_monotonic = now
        last_written_key = key
        try:
            _update_record(
                runs_dir,
                turn_id,
                progress={
                    "step": event.step,
                    "activity": event.activity,
                    "tool": event.tool,
                    "updated_at": _iso_now(),
                },
            )
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            sys.stderr.write(
                f"[drumbeat-turns] progress update failed for {turn_id!r}: {exc}\n"
            )

    return _sink


class _Supervision:
    """Heartbeat + wall-clock ceiling for one in-flight turn.

    Heartbeat: proof the executor thread is alive, independent of whatever
    the agent is doing -- a long silent tool call is otherwise
    indistinguishable from a dead worker.

    Ceiling: the backstop that guarantees SOME terminal outcome is always
    recorded rather than a turn hanging forever. Deliberately asymmetric:
    if the real work finishes after the ceiling already tombstoned the
    turn, the real outcome still lands and is the authoritative last word.
    """

    def __init__(self, runs_dir: Path, turn_id: str, ceiling_seconds: float) -> None:
        self._runs_dir = runs_dir
        self._turn_id = turn_id
        self._ceiling_seconds = ceiling_seconds
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(
            target=self._beat, name=f"turn-heartbeat-{turn_id}", daemon=True
        )
        self._timer = threading.Timer(ceiling_seconds, self._on_ceiling)
        self._timer.daemon = True

    def __enter__(self) -> Self:
        self._heartbeat.start()
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()
        self._stop.set()
        self._heartbeat.join(timeout=2)

    def _beat(self) -> None:
        while not self._stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                _update_record(self._runs_dir, self._turn_id, heartbeat_at=_iso_now())
            except Exception as exc:  # noqa: BLE001 - heartbeat is best-effort
                sys.stderr.write(
                    f"[drumbeat-turns] heartbeat failed for {self._turn_id!r}: {exc}\n"
                )

    def _on_ceiling(self) -> None:
        error = (
            f"turn exceeded the maximum allowed processing time "
            f"({self._ceiling_seconds:.0f}s) -- recorded as failed rather than "
            "hanging indefinitely"
        )
        try:
            _finalize_if_not_terminal(
                self._runs_dir,
                self._turn_id,
                status=STATUS_FAILED,
                phase=PHASE_FINISHED,
                error=error,
                completed_at=_iso_now(),
            )
        except Exception as exc:  # noqa: BLE001 - watchdog must never crash the process
            sys.stderr.write(
                f"[drumbeat-turns] ceiling watchdog failed for "
                f"{self._turn_id!r}: {exc}\n"
            )


# ---- execution ----


def _execute(ctx: EngineContext, record: dict[str, Any], request: TurnRequest) -> None:
    """Run one accepted turn to a terminal state. Never raises."""
    turn_id = record["turn_id"]
    runs_dir = ctx.runs_dir
    try:
        with _Supervision(runs_dir, turn_id, request.ceiling_seconds):
            progress = _make_progress_sink(runs_dir, turn_id)
            # Per-turn agent config: resolve WHICH provider/model/etc this turn
            # runs on from the layered agent-config merge -- the
            # ``$AMPLIFIER_AGENT_CONFIG`` base, the workspace ``agent-config.yaml``
            # ``default:`` layer, and (when the turn names one) the requested
            # ``profile`` from that file's ``profiles:`` block, layer 3 of the
            # merge. A profile-less turn uses the default; a turn in a workspace
            # with neither an env config nor a ``default:`` block nor a profile
            # resolves to nothing -- ``.path`` is ``None`` and the turn runs on
            # the engine's own defaults. An unknown profile name is
            # already refused synchronously by ``submit_turn``; resolving here
            # (re-read each turn) picks up any owner edit and, on a now-missing
            # profile, raises loudly so the turn is recorded failed. Keyed by
            # turn id so an interactive config can never collide with a scheduled
            # run's materialized file. The path (when any) is handed to the
            # runner, which threads it to the turn's worker as its host config.
            host_config_path = agent_config.resolve_turn(
                runs_dir=runs_dir,
                workspace=ctx.cwd,
                key=turn_id,
                profile=request.profile,
            ).path
            # Turn-context injectors: run every owner-declared injector (in the
            # workspace's ``injectors.yaml``) whose ``apply_to`` includes this
            # turn's profile, and collect their labeled preamble blocks -- in
            # file order -- for the turn to carry. An empty tuple -- no profile,
            # no policy file, or no injector for this profile -- carries nothing,
            # unchanged and with zero subprocess I/O. A configured injector that
            # fails (missing binary, timeout, non-zero exit, empty stdout) raises
            # loudly and the turn is recorded failed with the remedy -- never a
            # silently smaller context. Injectors run with the turn environment,
            # so an injector tool resolves exactly like a pack tool.
            preamble_blocks = injectors.collect_preamble(
                ctx.cwd,
                request.profile,
                env=runner._turn_env(ctx.cwd, runs_dir=runs_dir),
            )
            if request.automation_slug is not None:
                _execute_automation_turn(
                    ctx, turn_id, request, progress, host_config_path, preamble_blocks
                )
            else:
                _execute_session_turn(
                    ctx, turn_id, request, progress, host_config_path, preamble_blocks
                )
    except Exception as exc:  # noqa: BLE001 - executor thread must never crash the engine
        sys.stderr.write(
            f"[drumbeat-turns] turn {turn_id!r} raised: {exc}\n{traceback.format_exc()}"
        )
        _finalize_if_not_terminal(
            runs_dir,
            turn_id,
            status=STATUS_FAILED,
            phase=PHASE_FINISHED,
            error=str(exc),
            completed_at=_iso_now(),
        )


def _execute_session_turn(
    ctx: EngineContext,
    turn_id: str,
    request: TurnRequest,
    progress,
    host_config_path: Path | None = None,
    preamble_blocks: tuple[str, ...] = (),
) -> None:
    """Resume an explicit session with one turn -- the reply path."""
    assert request.session_id is not None
    _update_record(ctx.runs_dir, turn_id, phase=PHASE_EXECUTING, started_at=_iso_now())
    result = runner.resume_turn(
        request.session_id,
        request.text,
        cwd=ctx.cwd,
        runs_dir=ctx.runs_dir,
        wait_seconds=request.lock_wait_seconds or None,
        progress_callback=progress,
        host_config_path=host_config_path,
        preamble_blocks=preamble_blocks,
    )
    if result.error:
        # ``failure_kind`` carries the runner's machine-readable
        # classification straight through to the consumer. Only one value
        # is load-bearing today -- ``session_locked`` -- and it means the
        # lock was never acquired, so nothing ran and the caller's text is
        # provably safe to resend. That is the same busy signal a
        # synchronous 423 reports; the only difference is that this caller
        # asked to wait first. See section 7.3's flag.
        _update_record(
            ctx.runs_dir,
            turn_id,
            status=STATUS_FAILED,
            phase=PHASE_FINISHED,
            error=result.error,
            failure_kind=result.error_kind,
            completed_at=_iso_now(),
        )
        return
    _update_record(
        ctx.runs_dir,
        turn_id,
        status=STATUS_DONE,
        phase=PHASE_FINISHED,
        reply=result.reply,
        error=None,
        completed_at=_iso_now(),
    )


def _execute_automation_turn(
    ctx: EngineContext,
    turn_id: str,
    request: TurnRequest,
    progress,
    host_config_path: Path | None = None,
    preamble_blocks: tuple[str, ...] = (),
) -> None:
    """Route a turn through an automation's own machinery -- the chat path.

    Uses ``runner.run_chat_message`` unchanged, which is what the consumer
    called in-process before this cutover: pinned session, guidance,
    fail-loud requirements gate. The session id is not known until that
    call returns (a first-ever message mints one), so it is written back
    onto the record the moment it is.
    """
    assert request.automation_slug is not None
    automation = load_by_slug(request.automation_slug, ctx.automations_dir)
    _update_record(ctx.runs_dir, turn_id, phase=PHASE_EXECUTING, started_at=_iso_now())
    result = runner.run_chat_message(
        automation,
        request.text,
        cwd=ctx.cwd,
        runs_dir=ctx.runs_dir,
        lock_wait_seconds=request.lock_wait_seconds or None,
        progress_callback=progress,
        force_new=request.new_session,
        preamble_blocks=preamble_blocks,
    )
    if result.failed:
        _update_record(
            ctx.runs_dir,
            turn_id,
            status=STATUS_FAILED,
            phase=PHASE_FINISHED,
            session_id=result.session_id,
            error=result.error or "chat turn failed",
            completed_at=_iso_now(),
        )
        return
    _update_record(
        ctx.runs_dir,
        turn_id,
        status=STATUS_DONE,
        phase=PHASE_FINISHED,
        session_id=result.session_id,
        reply=result.final_reply,
        error=None,
        completed_at=_iso_now(),
    )


# ---- the 404 that stopped implying deletion ----
#
# THE DEFECT THIS FIXES. ``runner._probe_session`` has a genuinely good
# moved-vs-deleted detector: given the workspace slug a session was pinned
# under, it can say "the project directory was renamed or moved" rather than
# "the session is gone." But it can only say that when a ``recorded_workspace``
# is passed in, and the turn API structurally CANNOT pass one: a caller here
# hands over a bare ``session_id`` (the consumer resolves it from an item or a
# notification), and an item carries no automation frontmatter to read a
# recorded workspace out of. ``runner.probe_session`` therefore passes
# ``recorded_workspace=None``, the first check is skipped, and the answer
# arrives as ``MISSING`` -- whose detail reads "no directory at <path>".
#
# That is a lie of emphasis. ``MISSING`` here means precisely "not found under
# THE WORKSPACE THIS ENGINE IS CURRENTLY SERVING", which has three possible
# causes and the message named only one of them -- the one that sounds
# permanent and blameless. Anyone reading it concludes their conversation was
# deleted and stops looking. The real cause, at a workspace cutover, is that
# the workspace moved and the session is sitting safely under the old slug.
#
# The fix is copy, not mechanism: name all three possibilities and say which
# workspace was actually searched. The engine still refuses -- it must, it
# genuinely cannot resume a session it cannot find -- but it refuses with an
# accurate account of what it does and does not know. A refusal that
# misdiagnoses itself is worse than one that admits ambiguity, because it
# terminates the investigation at the wrong place.


def _unresolvable_session_detail(
    session_id: str, probe: runner.SessionProbe, detail: str
) -> str:
    """The 404 body for a session id this engine cannot resolve.

    ``MISSING`` gets the three-cause account (see the note above): the
    workspace-move cause is invisible to this code path by construction, so
    it must be named in words rather than detected. Every other verdict
    (``WORKSPACE_MISMATCH``, ``UNKNOWN``) already carries a self-describing
    detail from the probe and is passed through unchanged.
    """
    base = f"unknown session_id: {session_id!r} ({probe.value}: {detail})"
    if probe is runner.SessionProbe.MISSING:
        return (
            f"{base} -- refusing to guess a session. This means the session was "
            "not found under the workspace this engine is currently serving, "
            "which has three possible causes and this check cannot tell them "
            "apart: (1) the session never existed under this workspace; "
            "(2) it existed and was deleted; (3) THE WORKSPACE ITSELF MOVED "
            "or was renamed, in which case the session still exists, intact, "
            "under the previous workspace slug -- it is simply not reachable "
            "from here. Cause 3 is invisible to this code path: a bare "
            "session_id carries no recorded workspace to cross-check against, "
            "so 'missing' here never means 'deleted' on its own evidence. If "
            "the workspace was re-pointed recently, that is the first thing "
            "to check."
        )
    return f"{base} -- refusing to guess a session"


# ---- the public surface ----


def submit_turn(body: dict[str, Any], ctx: EngineContext) -> dict[str, Any]:
    """``POST /api/turns`` -- accept one turn; return the 202 payload.

    Raises ``TurnError`` with 400 / 404 / 423 and a body that names the
    refusal. See the module docstring for why each refusal exists.
    """
    request = parse_request(body)

    # A named profile must exist in the workspace's ``agent-config.yaml``
    # ``profiles:`` block. Validated HERE, synchronously, so a typo is an
    # immediate, honest refusal that lists the available profiles -- never a
    # silent fallback to the default (which would run on the wrong
    # provider/model), and never a turn record for a request that could never
    # succeed. ``select_profile`` also validates the profile's config shape, so a
    # malformed profile surfaces here too. The executor re-resolves per turn
    # (picking up any owner edit), so this is a fast-fail, not the only guard.
    if request.profile is not None:
        try:
            agent_config.select_profile(ctx.cwd, request.profile)
        except agent_config.AgentConfigError as exc:
            raise TurnError(400, str(exc)) from exc

    if request.automation_slug is not None:
        try:
            automation = load_by_slug(request.automation_slug, ctx.automations_dir)
        except AutomationError as exc:
            raise TurnError(
                404,
                f"unknown automation_slug: {request.automation_slug!r} -- "
                f"refusing to guess a session ({exc})",
            ) from exc
        # Best-known target. A never-pinned automation (or new_session) has
        # no session until run_chat_message mints one; the record is
        # updated with the real id the moment it exists.
        pin = (
            None
            if request.new_session
            else session_pins.get(automation.slug, runs_dir=ctx.runs_dir)
        )
        target_session = pin.session_id if pin else None
    else:
        # parse_request guarantees exactly one of the two is set.
        assert request.session_id is not None
        target_session = request.session_id
        probe, detail = runner.probe_session(target_session, cwd=ctx.cwd)
        if probe is not runner.SessionProbe.EXISTS:
            raise TurnError(
                404, _unresolvable_session_detail(target_session, probe, detail)
            )

    # The lock probe. Only meaningful when we already know the target: a
    # brand-new chat session cannot be contended by anything.
    if (
        target_session
        and request.lock_wait_seconds <= 0
        and runner.session_lock_is_held(target_session, runs_dir=ctx.runs_dir)
    ):
        # A priority="owner" caller refused here is exactly the
        # signal the scheduler needs -- "the owner is contending for this
        # session right now" -- so a due automation about to START on the
        # SAME session can defer instead of winning the next unordered race.
        # This never blocks or slows this response; it is a single dict
        # write (see owner_priority.mark_waiting) before the honest 423.
        if request.priority == PRIORITY_OWNER:
            owner_priority.mark_waiting(target_session)
        raise TurnError(
            423,
            f"session {target_session!r} is locked by another in-flight turn "
            "-- refusing to queue behind it. No turn was created; your text "
            "was not accepted and is still yours to resend.",
            session_id=target_session,
            retry_after_seconds=RETRY_AFTER_SECONDS,
        )

    turn_id = new_turn_id()
    record = {
        "turn_id": turn_id,
        "status": STATUS_RUNNING,
        "phase": PHASE_WAITING_FOR_LOCK,
        "session_id": target_session,
        "automation_slug": request.automation_slug,
        "origin": request.origin,
        "profile": request.profile,
        "priority": request.priority,
        "text": request.text,
        "reply": None,
        "error": None,
        "failure_kind": None,
        "progress": None,
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
        "started_at": None,
        "completed_at": None,
        "heartbeat_at": None,
        "lock_wait_seconds": request.lock_wait_seconds,
        "ceiling_seconds": request.ceiling_seconds,
        "new_session": request.new_session,
    }
    # Written BEFORE returning, so a caller polling immediately never sees
    # a 404 for an id this function itself minted.
    _write_record(ctx.runs_dir, record)

    threading.Thread(
        target=_execute,
        args=(ctx, record, request),
        name=f"turn-{turn_id}",
        daemon=True,
    ).start()

    return {
        "turn_id": turn_id,
        "session_id": target_session,
        "status": STATUS_RUNNING,
        "phase": PHASE_WAITING_FOR_LOCK,
    }


def get_turn(turn_id: str, ctx: EngineContext) -> dict[str, Any]:
    """``GET /api/turns/{turn_id}`` -- running | done {reply} | failed {error}."""
    record = _read_record(ctx.runs_dir, turn_id)
    if record is None:
        raise TurnError(404, f"unknown turn_id: {turn_id!r}")
    return record


def reconcile_turns_on_startup(runs_dir: Path) -> dict[str, list[str]]:
    """Resolve every turn left non-terminal by a PREVIOUS engine process.

    Call once at ``drumbeat serve`` startup, before the API binds. Every
    non-terminal record found here is unambiguously orphaned: this process
    has run no executor of its own yet, and it holds the scheduler lock, so
    nothing else could legitimately be in flight.

    **Never retried.** A turn's agent subprocess may have partially executed
    a real mutating action (send a chat message, mark mail read); blindly
    re-running it could do that action twice. Tombstoned ``failed`` with an
    honest reason that names the phase it died in -- ``waiting_for_lock``
    means no subprocess was ever launched, which is exactly the distinction
    a consumer needs to decide whether resubmitting is safe. Making that
    call is the consumer's, not the engine's.
    """
    directory = turns_dir(runs_dir)
    tombstoned: list[str] = []
    if not directory.is_dir():
        return {"tombstoned": tombstoned}
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"[drumbeat-turns] reconciliation: skipping unreadable {path}: {exc}\n"
            )
            continue
        if not isinstance(record, dict) or record.get("status") in TERMINAL_STATUSES:
            continue
        turn_id = record.get("turn_id")
        if not isinstance(turn_id, str):
            sys.stderr.write(
                f"[drumbeat-turns] reconciliation: skipping malformed {path}\n"
            )
            continue
        phase = record.get("phase") or "unknown"
        record["status"] = STATUS_FAILED
        record["phase"] = PHASE_FINISHED
        record["phase_at_interruption"] = phase
        record["error"] = (
            "the engine restarted while this turn was in flight (phase at "
            f"interruption: {phase}) -- it was NOT automatically retried, "
            "because a turn's agent subprocess may have partially completed a "
            "real action. phase 'waiting_for_lock' means no subprocess was "
            "ever launched for it."
        )
        record["completed_at"] = _iso_now()
        record["updated_at"] = _iso_now()
        try:
            _write_record(runs_dir, record)
        except OSError as exc:
            sys.stderr.write(
                f"[drumbeat-turns] reconciliation: cannot tombstone {turn_id}: {exc}\n"
            )
            continue
        tombstoned.append(turn_id)
    return {"tombstoned": tombstoned}


__all__ = [
    "DEFAULT_CEILING_SECONDS",
    "DEFAULT_LOCK_WAIT_SECONDS",
    "FAILURE_KIND_SESSION_LOCKED",
    "PHASE_EXECUTING",
    "PHASE_FINISHED",
    "PHASE_WAITING_FOR_LOCK",
    "PRIORITY_OWNER",
    "RETRY_AFTER_SECONDS",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "TERMINAL_STATUSES",
    "TURNS_DIRNAME",
    "TurnError",
    "TurnRequest",
    "get_turn",
    "new_turn_id",
    "parse_request",
    "reconcile_turns_on_startup",
    "submit_turn",
    "turn_path",
    "turns_dir",
]
