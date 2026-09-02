"""Trigger loop — the free-running scheduler daemon.

Loads automations, tracks when each schedule-triggered automation is next
due, and runs it when the time comes. A failing automation is logged loudly
but never brings the loop down; the whole point of a scheduler is that it
keeps running.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from drumbeat import (
    ci_events,
    drain,
    owner_priority,
    schedule_state,
    session_pins,
    staleness,
)
from drumbeat.automation import (
    PRIORITY_RANK,
    Automation,
    AutomationError,
    load_all_tolerant,
)
from drumbeat.capabilities import server_timezone
from drumbeat.prompts import DEFAULT_PROMPTS_DIR
from drumbeat.runner import reap_stale_session_locks, run

_POLL_INTERVAL_SECONDS = 10

# Two automations sharing an identical schedule expression (e.g. both
# "every 2 hours") that get registered on the same poll tick would otherwise
# fire in lockstep forever: every reschedule is `now + interval`, and if
# both `now`s stay in sync, they never drift apart. Observed live:
# reconcile.md and meetings-check.md, both "every 2 hours", firing
# simultaneously against the same shared ledger. A deterministic (not
# random -- the same automation must stagger the same way on every restart,
# so operators can reason about "when does X actually run") per-slug offset
# added at registration breaks the tie without any scheduling grammar --
# this is jitter, not a cron parser; a real expression/condition-trigger
# language is a separate, later piece of work.
_STAGGER_CAP_SECONDS = 300


def _stagger_offset_seconds(slug: str) -> int:
    """A small, deterministic 0-299s offset derived from the automation's slug.

    Deterministic so the same automation always staggers the same way
    (reasoning about "roughly when does X run" stays possible); capped low
    enough that it can only ever change *which second within the minute*
    an automation is due, never meaningfully shift *when* it's due.
    """
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _STAGGER_CAP_SECONDS


_SCHEDULE_RE = re.compile(
    r"^every\s+(?:(?P<count>\d+)\s+)?(?P<unit>minute|minutes|hour|hours)$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
}

# Daily-at-a-time form, added because automations/reconcile.md step 8's
# "have I already produced a daily rollup today (UTC)?" prose is the exact
# unmet need that justifies building
# an absolute-time schedule: "the first honest need is a guaranteed morning
# rollup... built then, on that evidence, not now on speculation." That
# evidence already exists (the prose workaround IS the unmet need), so this
# is built now rather than starved further. 24-hour clock only, deliberately
# -- am/pm doubles the parse surface for no real gain here.
_DAILY_RE = re.compile(
    r"^(?:daily|every\s+day)\s+at\s+(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IntervalSchedule:
    """Fires every ``seconds`` seconds, relative to whatever ``now`` the
    caller supplies (registration time, or the previous run's actual
    completion time -- see ``serve()``). This is the pre-existing, still
    default rhythm: a freshness cadence with no wall-clock anchor.
    """

    seconds: int


@dataclass(frozen=True)
class DailySchedule:
    """Fires once per local calendar day at a fixed wall-clock time.

    Anchored to ``hour:minute`` in the server's local timezone
    (``drumbeat.capabilities.server_timezone()``), never to an elapsed
    interval -- that anchoring is what lets it survive process restarts
    and DST transitions without drifting. See ``seconds_until_next_fire``
    for the anchoring math.
    """

    hour: int
    minute: int


Schedule = IntervalSchedule | DailySchedule


def parse_schedule(expression: str) -> Schedule:
    """Parse a free-text schedule expression into a ``Schedule``.

    Supports (case-insensitive):
      - ``every N minutes``, ``every N hours``, ``every minute``, ``every
        hour`` -> ``IntervalSchedule`` (a freshness cadence, no wall-clock
        anchor -- "how stale may this source get").
      - ``daily at HH:MM``, ``every day at HH:MM`` (24-hour clock, server
        local time) -> ``DailySchedule`` (a guaranteed once-a-day moment).

    Anything unparseable raises — silently defaulting to some interval or
    some time would run the wrong schedule without telling anyone.

    Examples:
        >>> parse_schedule("every 30 minutes")
        IntervalSchedule(seconds=1800)
        >>> parse_schedule("every hour")
        IntervalSchedule(seconds=3600)
        >>> parse_schedule("daily at 03:00")
        DailySchedule(hour=3, minute=0)

    Raises:
        ValueError: if the expression doesn't match a supported form.
    """
    stripped = expression.strip()

    daily_match = _DAILY_RE.match(stripped)
    if daily_match:
        return DailySchedule(
            hour=int(daily_match.group("hour")),
            minute=int(daily_match.group("minute")),
        )

    match = _SCHEDULE_RE.match(stripped)
    if not match:
        raise ValueError(
            f"unparseable schedule expression: {expression!r} "
            "(supported forms: 'every N minutes', 'every N hours', "
            "'every minute', 'every hour', 'daily at HH:MM', "
            "'every day at HH:MM')"
        )
    count = int(match.group("count")) if match.group("count") else 1
    if count <= 0:
        raise ValueError(f"schedule interval must be positive, got: {expression!r}")
    unit_seconds = _UNIT_SECONDS[match.group("unit").lower()]
    return IntervalSchedule(seconds=count * unit_seconds)


def seconds_until_next_fire(schedule: Schedule, now: float) -> float:
    """Seconds from the Unix timestamp ``now`` until ``schedule`` next fires.

    ``IntervalSchedule`` is unchanged from the original design: just the
    interval, relative to whatever ``now`` the caller passes.

    ``DailySchedule`` is anchored to a wall-clock hour:minute in the
    server's local timezone, recomputed fresh every call -- never
    persisted, never derived from a prior fire. That is deliberate: it is
    what makes a restart at any hour re-derive the correct next fire
    instead of pinning to the restart moment. Always returns the delay to
    the occurrence STRICTLY after ``now`` -- if the scheduler was down
    through today's anchor time, that occurrence is skipped (next fire is
    tomorrow), never run late and never queued. A missed daily fire costs
    at most one skipped day, never a burst of catch-up runs after an
    outage.

    DST is handled by construction: ``ZoneInfo`` is a "smart" tzinfo, so
    arithmetic on an aware datetime built from it reflects the correct
    wall-clock time across a spring-forward/fall-back boundary rather than
    a naive +86400 seconds.

    Raises:
        ValueError: if the server's timezone cannot be resolved to a real
            IANA zone -- treated the same as an unparseable schedule
            expression (the caller logs and skips this automation's tick
            rather than crashing the whole scheduler loop over one bad
            timezone lookup).
    """
    if isinstance(schedule, IntervalSchedule):
        return float(schedule.seconds)

    try:
        tz = ZoneInfo(server_timezone())
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"cannot resolve server timezone for a daily schedule: {exc}"
        ) from exc

    now_local = datetime.fromtimestamp(now, tz=tz)
    candidate = now_local.replace(
        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return (candidate - now_local).total_seconds()


def _dispatch_order(scheduled: list[Automation]) -> list[Automation]:
    """Order this tick's scheduled automations by priority tier, high first.

    WHAT THIS BUYS, AND WHAT IT DOES NOT. Runs are dispatched sequentially from
    this list, so on a tick where several automations are due, this decides
    WHO WAITS. It cannot decide how many get to run: it adds no concurrency,
    preempts no running turn, and never drops or defers a normal automation
    beyond what the backlog already imposes. Measured on the reference
    deployment, the fleet demands ~412 runs/day and completes ~127 -- this key
    does not change that number, and pretending otherwise would make it the
    kind of reassuring-but-inert knob this project refuses. The capacity fix
    (concurrency, or fewer automations) is a separate architecture decision.

    ``sorted`` is STABLE, and that is load-bearing rather than incidental:
    within a tier the prior order survives untouched, so a fleet where nothing
    declares ``priority:`` dispatches in byte-identical order to before this
    key existed.

    Owner-turn precedence is upstream of this and unaffected: the
    owner-priority latch is consulted per automation at dispatch time (below)
    and still defers ANY automation, of any tier, whose session the owner is
    using. The tier orders scheduled work strictly beneath that.
    """
    return sorted(scheduled, key=lambda a: PRIORITY_RANK.get(a.priority, 1))


def _describe_schedule(schedule: Schedule) -> str:
    """Human-readable one-liner for log lines -- used at registration only."""
    if isinstance(schedule, IntervalSchedule):
        return f"every {schedule.seconds}s"
    return f"daily at {schedule.hour:02d}:{schedule.minute:02d} local"


def _log(message: str) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}", file=sys.stderr)


class SchedulerError(RuntimeError):
    """Raised when the scheduler cannot safely start."""


@dataclass
class SchedulerState:
    """Live scheduler state, readable by another thread (the HTTP API).

    Mutable and deliberately lock-free: every field is written by the
    scheduler thread only and read by request threads, and each is a single
    reference assignment (atomic under the GIL). A reader can observe a
    momentarily inconsistent *combination* of fields; none of these are used
    to make a decision, only to report one, so that is acceptable and worth
    saying out loud rather than reaching for a mutex nobody needs.

    ``lock_held`` is the one that matters most: an engine that is up and
    answering health checks while holding no scheduler flock is the
    "up but lockless" state section 5 exists to make visible instead of
    silent.
    """

    lock_held: bool = False
    lock_path: str | None = None
    started_at: str | None = None
    last_tick_at: str | None = None
    ticks: int = 0
    draining: bool = False
    drain_reason: str | None = None
    current_run: dict[str, str] | None = None
    registered: dict[str, str] = field(default_factory=dict)
    load_failures: list[str] = field(default_factory=list)
    runs_started: int = 0
    last_run: dict[str, str] | None = None


# A second scheduler on the same host silently double-fires every automation.
# We hit exactly this: a restart whose kill did not take left two schedulers
# overlapping, producing two teams-check runs 6.5 minutes apart on a 30-minute
# schedule and two near-identical notifications to the phone. Scout has this
# same failure (per-device timers, last-run timestamp never synced) and we
# documented it as "we get single-writer free by running one server" -- then
# ran three on one host. Discipline did not hold, so make it structural.
def scheduler_lock_path(runs_dir: Path) -> Path:
    return (Path(runs_dir) / ".scheduler.lock").expanduser()


def acquire_scheduler_lock(runs_dir: Path) -> IO[str]:
    """Take the single-instance lock, or raise loudly.

    The guarantee runs in exactly one direction, and it is worth stating
    because the first draft of the migration runbook had it backwards: a
    **lingering old scheduler keeps the lock**, and it is the **new** process
    that fails to acquire and must refuse to schedule. A new scheduler that
    "helpfully" proceeded lockless would double-fire every automation --
    which is precisely the incident this lock was added for.
    """
    lock_path = scheduler_lock_path(runs_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # OSError, not BlockingIOError. Contention raises BlockingIOError, but
        # flock can fail for other reasons that are equally not "you hold the
        # lock" -- ENOLCK on an exhausted lock table, and EWOULDBLOCK/ENOTSUP
        # on a filesystem with no working advisory locking (NFS without lockd,
        # and drvfs under WSL, where a runs dir on /mnt/c looks like it locks
        # and does not). BlockingIOError IS an OSError subclass, so the
        # contention message below is unchanged for the case it always caught;
        # widening only converts "crash with a raw traceback and no scheduler"
        # into "refuse to start, and say why" for the rest. Never the third
        # option: proceeding lockless is what double-fires every automation.
        handle.close()
        raise SchedulerError(
            f"another scheduler already holds {lock_path} -- refusing to start a "
            "second one. Two schedulers double-fire every automation. If you are "
            "mid-cutover: the OLD process still holds it; drain and kill it by "
            "explicit pid first (see `drumbeat drain`), never by pkill -f."
        ) from None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


# Retained under the old private name for any in-tree caller/test that still
# reaches for it; the public name above is what new code uses.
_acquire_single_instance_lock = acquire_scheduler_lock


def serve(
    automations_dir: Path,
    cwd: Path,
    runs_dir: Path,
    prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    *,
    state: SchedulerState | None = None,
    lock_handle: IO[str] | None = None,
    write_fingerprint: bool = True,
) -> None:
    """Run the trigger loop until interrupted (Ctrl+C / SIGTERM).

    Reloads automations from disk on every poll so edits take effect without
    a restart. Only ``trigger.type == "schedule"`` and ``enabled: true``
    automations are tracked. A failure in one automation (parse error, run
    error) is logged and the loop continues — one broken automation must not
    take down every other automation's schedule.

    ``lock_handle``: when the caller has **already** acquired the
    single-instance lock, it passes the handle here and this does not try to
    take it again. That ordering exists for a reason -- ``drumbeat serve``
    must fail on a contended lock *before* it binds a port, so a refused
    engine never half-starts with a live HTTP face. When ``None``, this
    acquires the lock itself (the standalone/library path).

    ``state``: optional live-state object the HTTP API reads for
    ``/api/health``. Written by this thread only; see ``SchedulerState``.

    ``write_fingerprint``: the composition root writes its own staleness
    fingerprint for the whole process (scheduler + API), so it disables this
    one rather than have two writers race over the same file.
    """
    if lock_handle is None:
        lock_handle = acquire_scheduler_lock(runs_dir)
    _lock_handle = lock_handle
    if state is not None:
        state.lock_held = True
        state.lock_path = str(scheduler_lock_path(runs_dir))
        state.started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    automations_dir = Path(automations_dir).expanduser()
    cwd = Path(cwd).expanduser()
    runs_dir = Path(runs_dir).expanduser()
    prompts_dir = Path(prompts_dir).expanduser()

    # Stale-process guard (see drumbeat.staleness's module docstring): now
    # that this process holds the single-instance lock -- i.e. it IS the
    # scheduler -- record exactly which drumbeat.* files it loaded and their
    # content hashes, so `drumbeat doctor` can later tell whether any of them
    # changed on disk since this process started. Never fatal: a failure
    # here is reported as "unknown" by the doctor, not a crashed scheduler.
    if write_fingerprint:
        fingerprint_path = staleness.write_startup_fingerprint(
            "scheduler",
            runs_dir,
            # The scheduler's working code post-extraction is the engine
            # package alone.
            entry_module="drumbeat.scheduler",
            packages=("drumbeat",),
        )
        _log(f"staleness fingerprint written: {fingerprint_path}")

    # Orphaned-reply fix (Fix 2): reap session-lock files nobody currently
    # holds -- see runner.reap_stale_session_locks's docstring for the full
    # safety argument (this is provably safe, not a heuristic). The
    # scheduler is the other process that acquires ``.session-locks/``
    # entries (alongside notify-serve), so it reaps here too.
    reaped, skipped_active = reap_stale_session_locks(runs_dir)
    _log(
        f"session-lock reap: removed {len(reaped)} unheld, "
        f"left {len(skipped_active)} active untouched"
    )

    # Restart-safe schedule: seed next_due from disk instead of an empty dict
    # so a restart RESUMES the schedule where it left off rather than pushing
    # every automation a full interval into the future. Without this, every
    # restart re-registered each automation as `now + interval + stagger` --
    # the clock reset on every bounce, and the longest-interval automation
    # (channels-check, `every 90 minutes`) was starved worst: a scheduler that
    # bounced more often than every 90 min deferred it forever while shorter
    # checks still ran. That is the observed shape of f199. A due time that
    # elapsed while we were down reloads as already-past and fires on the next
    # tick (correct for an interval freshness cadence), never a burst.
    next_due: dict[str, float] = schedule_state.load(runs_dir)
    if next_due:
        _log(
            f"restored {len(next_due)} persisted due time(s) from "
            f"{schedule_state.state_path(runs_dir)}"
        )

    _log(
        f"scheduler starting: automations_dir={automations_dir} cwd={cwd} "
        f"runs_dir={runs_dir} prompts_dir={prompts_dir}"
    )

    drain_announced = False

    while True:
        if state is not None:
            state.ticks += 1
            state.last_tick_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Drain gate (see drumbeat.drain): stop STARTING work without
        # touching work in flight. Checked at the top of the tick and before
        # anything is due, so the answer to "can this process start a run in
        # the next instant?" is a flat no the moment the flag lands -- that
        # flatness is what makes the operator's "verify drained" check a
        # verification rather than a race.
        drain_request = drain.drain_state(runs_dir)
        if drain_request is not None:
            reason = str(drain_request.get("reason", "<no reason recorded>"))
            if state is not None:
                state.draining = True
                state.drain_reason = reason
            if not drain_announced:
                _log(
                    f"DRAINING: no new runs will start. Reason: {reason}. "
                    "In-flight work is left alone; clear with `drumbeat drain --clear`."
                )
                drain_announced = True
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue
        if drain_announced:
            _log("drain flag cleared -- resuming scheduling")
            drain_announced = False
        if state is not None:
            state.draining = False
            state.drain_reason = None

        try:
            automations, load_failures = load_all_tolerant(automations_dir)
        except AutomationError as exc:
            # The directory itself is missing/unreadable -- there is
            # nothing to isolate a per-file failure from, so this (unlike
            # a single bad file) really does skip the whole tick.
            _log(f"failed to load automations directory, skipping this tick: {exc}")
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue

        # A broken file's blast radius is itself, never the fleet -- see
        # load_all_tolerant's docstring. Named loudly, every tick it stays
        # broken, so it's never silently just... not running: this line is
        # the only place a user finds out this specific automation is
        # currently excluded from scheduling and why.
        for failure in load_failures:
            _log(
                f"automation file is broken and NOT scheduled (other "
                f"automations unaffected): {failure.path}: {failure.problem}"
            )

        now = time.time()
        scheduled = _dispatch_order(
            [a for a in automations if a.enabled and a.trigger.type == "schedule"]
        )

        # Set whenever next_due changes for a reason worth persisting
        # (registration or post-run reschedule). Stale-slug cleanup below is
        # deliberately NOT a reason to save on its own -- see that block.
        state_dirty = False

        for automation in scheduled:
            if automation.slug not in next_due:
                try:
                    schedule = parse_schedule(automation.trigger.expression or "")
                    delay = seconds_until_next_fire(schedule, now)
                except ValueError as exc:
                    _log(f"{automation.name}: bad schedule expression, skipping: {exc}")
                    ci_events.emit(
                        "drumbeat:schedule_skipped",
                        {
                            "automation": automation.name,
                            "reason": str(exc),
                            "phase": "registration",
                        },
                        cwd=cwd,
                    )
                    continue
                offset = _stagger_offset_seconds(automation.slug)
                next_due[automation.slug] = now + delay + offset
                state_dirty = True
                next_run_iso = datetime.fromtimestamp(
                    next_due[automation.slug], UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                _log(
                    f"{automation.name}: scheduled ({_describe_schedule(schedule)}), "
                    f"stagger_offset={offset}s, first run at {next_run_iso} "
                    f"(+{delay + offset:.0f}s)"
                )
                continue

            if now >= next_due[automation.slug]:
                # Owner-priority deferral. Read-only, side-effect-free
                # lookup of whatever session this automation would resume -- the
                # SAME lookup `run()` itself performs a moment later. A never-run
                # automation has no pin yet and therefore no session to be
                # contended over (mirrors turns.submit_turn's own "a brand-new
                # session cannot be contended by anything"), so it is never
                # deferred. `next_due` is left UNTOUCHED on a defer: the
                # occurrence stays due and is retried next tick (10s), never
                # dropped and never pushed a full schedule interval out --
                # only a REAL run attempt (below) reschedules that far.
                pin = session_pins.get(automation.slug, runs_dir=runs_dir)
                target_session = pin.session_id if pin else None
                if target_session and owner_priority.should_defer(target_session):
                    _log(
                        f"{automation.name}: due, but deferring to the owner "
                        f"(session {target_session!r} is contended) -- retrying "
                        "next tick, not dropped"
                    )
                    ci_events.emit(
                        "drumbeat:owner_priority_deferred",
                        {
                            "automation": automation.name,
                            "session_id": target_session,
                        },
                        cwd=cwd,
                    )
                    continue

                _log(f"{automation.name}: due, running now")
                if state is not None:
                    state.runs_started += 1
                    state.current_run = {
                        "automation": automation.slug,
                        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                try:
                    result = run(
                        automation,
                        cwd=cwd,
                        runs_dir=runs_dir,
                        prompts_dir=prompts_dir,
                        trigger="schedule",
                    )
                    status = "FAILED" if result.failed else "ok"
                    _log(
                        f"{automation.name}: run {result.run_id} finished ({status}, notified={result.notified})"
                    )
                    if state is not None:
                        state.last_run = {
                            "automation": automation.slug,
                            "run_id": result.run_id,
                            "status": status,
                            "finished_at": datetime.now(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                        }
                except Exception as exc:  # noqa: BLE001 - a broken automation must not kill the loop
                    _log(f"{automation.name}: run raised an unhandled exception: {exc}")
                    if state is not None:
                        state.last_run = {
                            "automation": automation.slug,
                            "run_id": "<none — raised>",
                            "status": f"EXCEPTION: {exc}",
                            "finished_at": datetime.now(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                        }
                finally:
                    if state is not None:
                        state.current_run = None

                try:
                    schedule = parse_schedule(automation.trigger.expression or "")
                    completion = time.time()
                    delay = seconds_until_next_fire(schedule, completion)
                except ValueError as exc:
                    _log(
                        f"{automation.name}: bad schedule expression after run, skipping: {exc}"
                    )
                    continue
                next_due[automation.slug] = completion + delay
                state_dirty = True

        stale_slugs = set(next_due) - {a.slug for a in scheduled}
        for slug in stale_slugs:
            del next_due[slug]

        # Persist next_due after registration/reschedule so a restart resumes
        # the schedule instead of resetting the clock (see the seed at the top
        # of serve()). Only on an actual register/reschedule -- a transient
        # stale-slug drop is not a reason to rewrite on its own, and because
        # save() writes the WHOLE dict, the next real save drops any pruned
        # slug anyway (self-healing). A write failure is logged and the loop
        # keeps ticking, exactly as a single automation's failure is contained
        # -- the worst case degrades to the pre-persistence clock-reset, never
        # a dead scheduler.
        if state_dirty:
            try:
                schedule_state.save(runs_dir, next_due)
            except OSError as exc:
                _log(
                    f"could not persist schedule state to "
                    f"{schedule_state.state_path(runs_dir)} ({exc}) -- continuing; "
                    "a restart before the next successful save will reset this "
                    "automation's clock, the pre-persistence behavior."
                )

        if state is not None:
            state.registered = {
                slug: datetime.fromtimestamp(due, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                for slug, due in sorted(next_due.items())
            }
            state.load_failures = [
                f"{failure.path}: {failure.problem}" for failure in load_failures
            ]

        time.sleep(_POLL_INTERVAL_SECONDS)


__all__ = [
    "DailySchedule",
    "IntervalSchedule",
    "Schedule",
    "SchedulerError",
    "SchedulerState",
    "acquire_scheduler_lock",
    "parse_schedule",
    "scheduler_lock_path",
    "seconds_until_next_fire",
    "serve",
]
