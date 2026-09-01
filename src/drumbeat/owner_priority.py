"""Owner-priority latch -- the human's turn runs next.

**The incident this fixes.** ``POST /api/turns`` and the scheduler share one
arbitration primitive per session: an ``fcntl.flock`` (see
``runner._session_lock``). That primitive has no queue and no ordering --
whoever's ``flock(LOCK_EX | LOCK_NB)`` call happens to land first wins,
full stop. When a session is shared between the owner's own conversation and
one or more schedule-triggered automations (an automation that injects into
the SAME ongoing thread resumes the SAME pinned session -- see
``session_pins``), a run of scheduled automations firing back-to-back can
keep winning that race purely on timing, for as long as they stay due. A
consumer retrying a synchronous, honest 423 (``turns.submit_turn``'s
immediate-refusal branch) has no way to say "let me go first next" -- it can
only ask again and take its chances in the same unordered race. That is the
measured incident: an owner-typed message rejected 16 times over ~4m42s
while automation turns kept winning the same race.

**What this module is, and is not.** It is NOT a queue, a scheduler, or a
lock manager -- the underlying ``flock`` arbitration is untouched, and nothing
here changes who *holds* a lock or how long a turn may run. It is a single,
narrow, additive signal: "the owner is currently contending for session X's
lock", with a short live window and a bounded budget, that the ONE other
caller who can choose not to race (the scheduler, deciding whether to START a
new automation turn) can consult before it tries. Everything else about lock
acquisition is exactly as before.

**Hard invariants, by construction:**

- **Never preempts a running turn.** This module has no handle on any
  in-flight turn and cannot cancel one -- it is consulted only before a NEW
  automation turn is about to start (see ``scheduler.serve``).
- **Automation work is deferred, never lost.** A deferred occurrence's
  ``next_due`` entry is left untouched by the caller, so it stays "due" and
  is retried the very next scheduler tick (10s later) -- never silently
  dropped, never pushed a full schedule interval into the future the way a
  real run's completion reschedules it.
- **Bounded, never a permanent freeze.** ``should_defer`` grants at most
  ``max_deferrals`` consecutive yields per live waiting-window; the
  ``max_deferrals + 1``th check returns ``False`` so the automation proceeds
  regardless of whether the owner is still waiting. See its docstring.
- **Unmarked callers are unaffected.** Nothing calls ``mark_waiting`` unless
  a turn explicitly carries the owner-priority marker (``turns.py``'s
  ``priority`` field). A workspace that never sets it sees this module do
  precisely nothing, ever -- ``is_waiting``/``should_defer`` are permanently
  ``False`` for every session id.

**Why in-process, in-memory is enough.** ``serve.py``'s composition root runs
the scheduler loop and the HTTP API in ONE process (scheduler on the main
thread, API on background threads -- see its module docstring). Both the
"mark" side (turn intake, in an API thread) and the "check" side (the
scheduler's due-automation loop, on the main thread) live in that same
process, so a plain thread-safe module-level dict is sufficient -- no file,
no cross-process IPC, and nothing to reap on restart (a fresh process starts
with an empty, and therefore harmless, latch).
"""

from __future__ import annotations

import threading
import time

# How long a single "the owner is waiting" signal remains live before it must
# be refreshed by another contended attempt. Deliberately short: a handful of
# multiples of the scheduler's own poll interval (10s, see scheduler.py) and
# the session-lock poll interval (0.5s, see runner._session_lock), so a caller
# that stops asking -- because it gave up, succeeded, or the process holding
# it crashed -- cannot freeze automation starts by accident. Each fresh
# refusal or wait-tick refreshes the window; it is not a standing reservation.
DEFAULT_WINDOW_SECONDS = 30.0

# Bounded deferral ceiling: how many CONSECUTIVE due-checks a single
# contention episode may defer before the automation is allowed to proceed
# regardless. At the scheduler's 10s poll interval this is roughly one
# minute -- long enough to let a brief, genuine race resolve in the owner's
# favor, short enough that an owner who keeps the window alive indefinitely
# (e.g. a long-lived gateway retry loop) cannot starve automation work
# forever. See ``should_defer``.
DEFAULT_MAX_DEFERRALS = 6

_lock = threading.Lock()
# session_id -> monotonic deadline the entry is live until.
_waiting: dict[str, float] = {}
# session_id -> how many consecutive should_defer() calls have already
# deferred within the CURRENT live window (reset whenever the window lapses
# or is refreshed after having lapsed).
_defer_counts: dict[str, int] = {}


def mark_waiting(session_id: str | None, *, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> None:
    """Record that the owner is (still) contending for ``session_id``'s lock.

    Call this every time an owner-priority turn is refused (423) for this
    session, or discovers the session's lock held while polling for it.
    Idempotent and cheap: a dict write under a short-held lock. A falsy
    ``session_id`` (``None`` or empty) is a no-op -- there is nothing to
    defer automation starts against.

    Refreshing an already-live window does NOT reset its deferral count --
    see ``should_defer``: the budget is per contention *episode*, and an
    episode is exactly the span between the first mark and the first gap
    longer than ``window_seconds`` with no mark.
    """
    if not session_id:
        return
    with _lock:
        _waiting[session_id] = time.monotonic() + window_seconds


def is_waiting(session_id: str | None) -> bool:
    """Is the owner currently, within the live window, contending for this session?

    An expired entry reads as absent and is dropped lazily here -- no sweep
    thread needed. The map can only ever hold as many entries as there are
    distinct session ids that have EVER had an owner-priority turn refused,
    and expired entries are reclaimed the next time anyone asks about them.
    """
    if not session_id:
        return False
    now = time.monotonic()
    with _lock:
        deadline = _waiting.get(session_id)
        if deadline is None:
            return False
        if deadline <= now:
            del _waiting[session_id]
            _defer_counts.pop(session_id, None)
            return False
        return True


def should_defer(session_id: str | None, *, max_deferrals: int = DEFAULT_MAX_DEFERRALS) -> bool:
    """Should a not-yet-started automation turn for ``session_id`` yield to the owner?

    ``True`` at most ``max_deferrals`` consecutive times per live
    owner-waiting episode -- the bounded-deferral rule: automation work is
    deferred, never lost, and never starved past this ceiling. Once the
    ceiling is reached this returns ``False`` (proceed) even though
    ``is_waiting`` may still report ``True`` -- the caller is expected to
    actually start the automation turn in that case, which is what lets the
    lock-acquisition race resolve one way or the other instead of spinning
    forever.

    A falsy ``session_id``, or one with no live waiting window, is always
    ``False`` (nothing to defer against) and resets its deferral count so a
    later, unrelated contention episode gets its own full budget.
    """
    if not is_waiting(session_id):
        with _lock:
            _defer_counts.pop(session_id, None)
        return False
    assert session_id is not None  # is_waiting(None) is always False
    with _lock:
        count = _defer_counts.get(session_id, 0)
        if count >= max_deferrals:
            return False
        _defer_counts[session_id] = count + 1
        return True


def _reset_for_tests() -> None:
    """Clear all state. Test-only -- module-level state must not leak between tests."""
    with _lock:
        _waiting.clear()
        _defer_counts.clear()


__all__ = [
    "DEFAULT_MAX_DEFERRALS",
    "DEFAULT_WINDOW_SECONDS",
    "is_waiting",
    "mark_waiting",
    "should_defer",
]
