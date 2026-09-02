"""Which notify-capable automations' most recent run CRASHED rather than passed.

THE FAILURE THIS CLOSES. A notify-capable automation crashed mid-pass
(``ContextLengthError`` at step 5 of 8) and produced ``notified: false`` --
byte-for-byte what a healthy run reports when it decides nothing needs the
owner's attention. From outside, "nothing needs you" and "the thing that
decides whether anything needs you crashed" were the same observable. The
vision names this exact enemy: *the successful-looking run that did nothing*;
the consumer's own guidance puts it as *silence from a starved aggregate is
indistinguishable from a calm day*.

The evidence was never missing -- ``result.json`` carries ``failed: true``,
``failures.log`` carries a line, an ``automation_error`` event is emitted. What
was missing is a **standing, one-read answer** to "is this automation's most
recent run a crash?" Every existing surface is a stream or a per-run file: to
answer it you walk every run directory, or replay the outbox from a cursor.
This store is that one read.

WHAT IT HOLDS. At most one record per automation slug: *the most recent run of
this automation failed, and no successful run has happened since*. Not a
history -- ``failures.log`` and the outbox are the history. Consecutive
failures REPLACE the record rather than accumulating, which is what keeps the
successor's notice about the latest failure only.

LIFECYCLE, and it has exactly two edges:

- a run of a **notify-capable** automation (``notify:`` policy other than
  ``never``) FAILS -> ``record()``, replacing any earlier record for that slug;
- a run of that automation SUCCEEDS -> ``clear()``.

``notify: never`` automations are deliberately absent from this store. Their
failures are still logged and still emit ``automation_error`` (a safety
property that bypasses notify policy -- see ``runner._notify_run_failure``);
what they cannot do is produce the ambiguity this store resolves, because a
``notify: never`` run's silence was never going to be read as a judgment.

TWO CONSUMERS, ONE FACT:

1. an operator or consumer POLLS this file -- one small read, no run-directory
   walk, no cursor -- to see which automations are currently in a crashed
   state;
2. ``runner`` reads it at the start of the next run of the same automation and
   tells that run, in plain language, that its predecessor did not complete, so
   the gap is accounted for rather than read as a quiet period.

POSTURE. Deliberately different from ``session_pins``: that store REFUSES to be
read as empty because reading-as-empty there means a silent mass rotation.
Here, a raise would take down otherwise-healthy runs to protect a notice. So
this module NEVER raises -- but it is never silent either. Every unreadable
store, rejected shape, and failed write is named on stderr, and an unreadable
store is reported and then treated as holding no records (the next write
replaces it wholesale). The cost of that fallback is one missed notice; the
cost of the alternative is every run of every automation failing because a
marker file got truncated.
"""

from __future__ import annotations

import fcntl
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from drumbeat.fsutil import atomic_write

STORE_FILENAME = "failed_passes.json"

# Bumped only when the on-disk shape itself changes. A file declaring an
# unrecognized format is treated exactly like an unparseable one: reported and
# then replaced, never guessed at.
STORE_FORMAT = 1

# Failure text is a preview, not an archive -- the full error is in the run's
# own result.json and stderr.log, which this record names by run_id.
_ERROR_PREVIEW_CHARS = 300


@dataclass(frozen=True)
class FailedPass:
    """One automation whose most recent run failed."""

    slug: str
    automation: str
    run_id: str
    session_id: str
    failed_at: str
    error: str

    def as_json(self) -> dict[str, str]:
        return {
            "automation": self.automation,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "failed_at": self.failed_at,
            "error": self.error,
        }


def store_path(runs_dir: Path) -> Path:
    """The store file for this data dir."""
    return Path(runs_dir).expanduser() / STORE_FILENAME


def _lock_path(runs_dir: Path) -> Path:
    return Path(runs_dir).expanduser() / (STORE_FILENAME + ".lock")


def _log(message: str) -> None:
    print(f"[failed_passes] {message}", file=sys.stderr)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _StoreLock:
    """Exclusive flock over the store, held across read-modify-write.

    Two engine processes (the scheduler and notify-serve) share this data dir,
    so a read-modify-write that is not locked can lose one automation's record
    to another automation's write -- the same reason ``session_pins`` locks.
    ``taken`` is False when the lock could not be acquired at all, which the
    callers treat as "do nothing, loudly" rather than proceeding unlocked.
    """

    def __init__(self, runs_dir: Path) -> None:
        self._path = _lock_path(runs_dir)
        self._handle = None
        self.taken = False

    def __enter__(self) -> Self:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "w", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
            self.taken = True
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            _log(f"could not take the store lock at {self._path}: {exc}")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def _read_unlocked(path: Path) -> dict[str, FailedPass]:
    """Every record in the store. Never raises; reports and returns {} instead.

    An absent store is the legitimate state of a data dir where nothing has
    failed yet, and is NOT reported. Everything else -- unreadable, torn,
    unparseable, wrong shape, wrong format -- is reported by name first.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _log(f"{path}: could not be read ({exc}) -- proceeding as if empty")
        return {}

    if not raw.strip():
        _log(f"{path}: is empty/torn -- proceeding as if empty")
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log(f"{path}: is unparseable ({exc}) -- proceeding as if empty")
        return {}

    if not isinstance(data, dict):
        _log(f"{path}: must be a JSON object, got {type(data).__name__}")
        return {}

    fmt = data.get("failed_passes_format")
    if fmt != STORE_FORMAT:
        _log(
            f"{path}: declares failed_passes_format={fmt!r}, this engine "
            f"understands {STORE_FORMAT} -- proceeding as if empty"
        )
        return {}

    entries = data.get("failed_passes")
    if not isinstance(entries, dict):
        _log(
            f"{path}: 'failed_passes' must be an object, got "
            f"{type(entries).__name__} -- proceeding as if empty"
        )
        return {}

    records: dict[str, FailedPass] = {}
    for slug, entry in entries.items():
        if not isinstance(slug, str) or not slug.strip():
            _log(f"{path}: skipping record under unusable key {slug!r}")
            continue
        if not isinstance(entry, dict):
            _log(f"{path}: skipping {slug!r} -- not an object")
            continue
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            # A record naming no run is worse than no record: it reads as a
            # crash nobody can go look at.
            _log(f"{path}: skipping {slug!r} -- no usable run_id ({run_id!r})")
            continue
        records[slug] = FailedPass(
            slug=slug,
            automation=_as_str(entry.get("automation")) or slug,
            run_id=run_id.strip(),
            session_id=_as_str(entry.get("session_id")),
            failed_at=_as_str(entry.get("failed_at")),
            error=_as_str(entry.get("error")),
        )
    return records


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _serialize(records: dict[str, FailedPass]) -> str:
    payload = {
        "failed_passes": {slug: records[slug].as_json() for slug in sorted(records)},
        "failed_passes_format": STORE_FORMAT,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write_unlocked(path: Path, records: dict[str, FailedPass]) -> bool:
    try:
        atomic_write(path, _serialize(records))
    except OSError as exc:
        _log(f"{path}: write failed ({exc}) -- the store is now stale")
        return False
    return True


def read_all(runs_dir: Path) -> dict[str, FailedPass]:
    """Every automation currently in a crashed state, keyed by slug.

    This is the poll surface: one file read answers "which automations' most
    recent run failed", with no run-directory walk and no outbox cursor.
    """
    with _StoreLock(runs_dir) as lock:
        if not lock.taken:
            return {}
        return _read_unlocked(store_path(runs_dir))


def get(slug: str, *, runs_dir: Path) -> FailedPass | None:
    """This automation's crashed-run record, or ``None`` if its last run was fine."""
    return read_all(runs_dir).get(slug)


def record(
    *,
    slug: str,
    automation: str,
    run_id: str,
    session_id: str,
    error: str,
    runs_dir: Path,
    failed_at: str | None = None,
) -> FailedPass | None:
    """Mark this automation's most recent run as failed, replacing any earlier record.

    Replacement, not accumulation, is the point: two failures in a row leave
    ONE record, so the next run is told about the latest failure once rather
    than handed a growing pile of notices.

    Returns the stored record, or ``None`` if the store could not be written
    (already reported on stderr by then -- a marker that could not be written
    must never mask or escalate the run failure it was describing).
    """
    if not slug.strip() or not run_id.strip():
        _log(f"refusing to record a failed pass with slug={slug!r} run_id={run_id!r}")
        return None
    entry = FailedPass(
        slug=slug,
        automation=automation or slug,
        run_id=run_id.strip(),
        session_id=session_id,
        failed_at=failed_at or _iso_now(),
        error=(error or "").strip().replace("\n", " ")[:_ERROR_PREVIEW_CHARS],
    )
    path = store_path(runs_dir)
    with _StoreLock(runs_dir) as lock:
        if not lock.taken:
            return None
        records = _read_unlocked(path)
        records[slug] = entry
        if not _write_unlocked(path, records):
            return None
    return entry


def clear(slug: str, *, runs_dir: Path) -> FailedPass | None:
    """Drop this automation's crashed-run record; its latest run succeeded.

    Returns the record that was cleared, or ``None`` when there was nothing to
    clear (the ordinary case: a healthy automation clears nothing on every
    single run) or the write failed.
    """
    path = store_path(runs_dir)
    with _StoreLock(runs_dir) as lock:
        if not lock.taken:
            return None
        records = _read_unlocked(path)
        existing = records.pop(slug, None)
        if existing is None:
            return None
        if not _write_unlocked(path, records):
            return None
    return existing


__all__ = [
    "STORE_FILENAME",
    "STORE_FORMAT",
    "FailedPass",
    "clear",
    "get",
    "read_all",
    "record",
    "store_path",
]
