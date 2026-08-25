"""Durable ``slug -> next_due`` cache for the scheduler.

The trigger loop (``scheduler.serve``) holds each schedule-triggered
automation's next due time in an in-memory ``dict[str, float]``. That dict
was *only* in memory: every process restart rebuilt it from scratch,
re-registering each automation as ``now + interval + stagger`` -- i.e. every
restart reset the clock. The longest-interval automation loses the most:
``channels-check`` runs ``every 90 minutes``, so a scheduler that bounces
more often than every 90 minutes defers it forever while shorter-interval
checks still slip through. That is the observed shape of f199 -- "Channel
check automation is not running automatically" -- while other checks kept
running.

This module is the fix: persist ``next_due`` to a small JSON file after every
mutation and reload it on startup, so a restart resumes the schedule where it
left off instead of pushing every automation a full interval into the future.
A due time that elapsed while the scheduler was down reloads as *already
past*, so the automation fires on the next tick (correct for an interval
freshness cadence) rather than waiting a fresh full interval.

Design posture (matches ``session_pins.py`` / ``schedule`` parsing):
FAIL LOUD, NO SILENT FALLBACKS. ``load`` never raises -- a scheduler must
keep ticking even when its own cache is unreadable -- but it never fails
silently either: every rejected/corrupt file is named on stderr first, and
the worst a bad file can do is cost this one restart the same clock-reset the
system already paid on *every* restart before this module existed. ``save``
deliberately DOES propagate its write error, same posture as
``fsutil.atomic_write``: a scheduler that believes it persisted a due time it
did not is worse than one that knows the write failed. ``serve`` catches that
at the call site and logs it, exactly as it contains a single automation's
own exception, rather than letting one bad write take down the loop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from drumbeat.fsutil import atomic_write

STATE_FILENAME = "schedule-state.json"

# Bumped only when the on-disk shape itself changes. A file declaring an
# unrecognized format is treated exactly like unparseable JSON (see ``load``)
# rather than guessed at -- the same refusal ``session_pins`` makes about
# reading a foreign shape as if it were this one.
STATE_FORMAT = 1


def state_path(runs_dir: Path) -> Path:
    """The persisted ``next_due`` file for this data dir."""
    return Path(runs_dir).expanduser() / STATE_FILENAME


def _log(message: str) -> None:
    print(f"[schedule_state] {message}", file=sys.stderr)


def load(runs_dir: Path) -> dict[str, float]:
    """Return every persisted ``slug -> next_due`` (Unix timestamp), best-effort.

    Returns ``{}`` for a missing file (nothing to restore) or an
    unreadable/corrupt/foreign one -- but NEVER silently: a message naming
    the file and the exact reason is written to stderr first, and each
    automation then simply re-registers fresh on the next tick, as it did
    before this module existed. Never raises.
    """
    path = state_path(runs_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _log(
            f"{path}: unreadable ({exc}) -- treating as empty; every schedule "
            "automation re-registers fresh on the next tick."
        )
        return {}

    if not raw.strip():
        _log(f"{path}: exists but is empty/torn -- treating as empty.")
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log(f"{path}: unparseable JSON ({exc}) -- treating as empty.")
        return {}

    if not isinstance(data, dict):
        _log(
            f"{path}: top level is {type(data).__name__}, expected object -- "
            "treating as empty."
        )
        return {}

    fmt = data.get("schedule_state_format")
    if fmt != STATE_FORMAT:
        _log(
            f"{path}: declares schedule_state_format={fmt!r}, this engine "
            f"understands {STATE_FORMAT} -- refusing to guess; treating as empty."
        )
        return {}

    raw_next_due = data.get("next_due")
    if not isinstance(raw_next_due, dict):
        _log(
            f"{path}: 'next_due' is {type(raw_next_due).__name__}, expected "
            "object -- treating as empty."
        )
        return {}

    restored: dict[str, float] = {}
    for slug, due in raw_next_due.items():
        if not isinstance(slug, str) or not slug.strip():
            _log(f"{path}: dropping non-string/blank slug key {slug!r}")
            continue
        # bool is an int subclass -- excluded explicitly so a stray
        # ``true``/``false`` is never read as 1.0/0.0.
        if isinstance(due, bool) or not isinstance(due, (int, float)):
            _log(f"{path}: dropping slug {slug!r} -- due time {due!r} is not a number")
            continue
        restored[slug] = float(due)
    return restored


def save(runs_dir: Path, next_due: dict[str, float]) -> None:
    """Persist ``next_due`` atomically (temp file + rename via ``fsutil.atomic_write``).

    Called after every mutation the scheduler makes to its in-memory
    ``next_due`` -- registration and post-run reschedule -- so the on-disk
    file is never more than one such mutation stale.

    Raises:
        OSError: the write itself failed. Deliberately NOT swallowed here
            (same posture as ``fsutil.atomic_write``): the scheduler's call
            site logs it loudly and keeps ticking, rather than the loop
            silently believing a due time was persisted when it was not.
    """
    path = state_path(runs_dir)
    payload: dict[str, Any] = {
        "schedule_state_format": STATE_FORMAT,
        "next_due": dict(sorted(next_due.items())),
    }
    atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


__all__ = ["STATE_FILENAME", "STATE_FORMAT", "load", "save", "state_path"]
