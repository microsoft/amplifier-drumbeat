"""The delivery seam: a durable, flock-guarded event outbox.

This is the file the engine writes and the delivery worker reads. It is the
class-1 boundary described in ``docs/ARCHITECTURE.md`` section 7 made
mechanical: **the engine evaluates delivery policy; it never performs
delivery.** Every run emits exactly one ``delivery_intent`` event carrying a
verdict, the gate that decided it, and a required reason -- so "ran and
delivered nothing" stops being a silence and becomes a queryable record.

Semantics, decided in section 6 and implemented here (not re-derived at the
call sites):

- **The file** is append-only ``runs/engine-events.jsonl``. Appends happen
  inside an exclusive ``fcntl.flock`` on a sidecar ``.lock`` file (the
  sidecar-lock convention used across this codebase), which is what makes the line-atomicity
  the reader depends on actually true: an ``O_APPEND`` write of a multi-KB
  line is *not* atomic against a concurrent writer on its own. The lock also
  covers offset allocation, which is the section-12 pre-step-3 requirement
  for the hybrid window where engine code runs in two processes.
- **fsync before releasing the lock** for any event a consumer must deliver
  (``delivery_intent``, ``automation_error``). An intent that only ever
  existed in the page cache is class 1 with extra steps. Other event types
  are allowed to ride the page cache.
- **The cursor is a byte offset, not a sequence number.** Byte offsets
  survive the writer restarting; a per-process counter does not. Nothing
  here mints or exposes a monotonic ``seq``.
- **Torn tail**: a reader never parses past the last ``\\n`` in the file. A
  partial final line is "not yet written," never an error. Paired with the
  flocked line-atomic appends above, a torn line is a read-side artifact
  only.
- **No rotation.** Declared, not forgotten: this file embeds the full output
  text of everything the system ever said *or withheld* -- a permanent
  shadow copy, and a sensitivity named plainly rather than assumed harmless.
  Its size and age are surfaced by both doctor faces so the watcher exists
  before the growth does.

FAIL LOUD, NO FALLBACKS. Required fields (``verdict``, ``gate``, ``reason``)
have **no defaults** -- an absent or blank one raises ``OutboxWriteError`` at
write time and ``OutboxParseError`` at read time. A reader that meets a line
it cannot parse, an unknown event type, or an unknown enum value stops and
says so; it never skips forward, and it never advances its cursor past
something it did not understand. "Lesser state" is not a state this module
has.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

OUTBOX_FILENAME = "engine-events.jsonl"


class OutboxError(Exception):
    """Base for every failure this module raises."""


class OutboxWriteError(OutboxError):
    """An event was rejected before it could be written.

    Always a programming error at the emit site (a missing required field,
    an unknown enum value) -- never an I/O condition, which propagates as
    ``OSError`` so the caller can tell "I built a bad event" apart from
    "the disk is full."
    """


class OutboxParseError(OutboxError):
    """A complete line in the outbox could not be understood.

    Carries the byte ``offset`` of the offending line so a human can seek
    straight to it. A consumer that raises this must halt at that offset,
    not skip it.
    """

    def __init__(self, message: str, *, offset: int) -> None:
        super().__init__(f"{message} (at byte offset {offset})")
        self.offset = offset


class EventType(str, Enum):
    """The closed set of event types the outbox carries.

    Closed on purpose: an unknown type means engine/worker version skew,
    and a worker that shrugged at one would be silently dropping whatever
    the newer engine was trying to say.
    """

    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    DELIVERY_INTENT = "delivery_intent"
    AUTOMATION_ERROR = "automation_error"
    SESSION_ROTATED = "session_rotated"
    # Section 7.1 (`inject:`): the reasoned, actor-attributed record that an
    # inject tool reported INJECT_IDLE and no turn was injected -- the skip
    # is a record, never an inference.
    INJECT_SKIPPED = "inject_skipped"
    # Section 6/7.1: an injected turn (from an `inject:` tool's stdout)
    # completed inside a run.
    TURN_COMPLETED = "turn_completed"


class Verdict(str, Enum):
    """What the engine decided to do with a run's output."""

    DELIVER = "deliver"
    WITHHOLD = "withhold"
    DEMOTE = "demote"


class Gate(str, Enum):
    """Every gate in ``runner`` that can decide a run's delivery verdict.

    **Enumerated from the code at HEAD, not from memory** (section 6). The
    three commonly-summarised gates are not the whole set; the two that a
    summary loses are exactly the two that make this enum worth pinning:

    - ``REFUSAL_DETECTED`` -- ``runner._looks_like_refusal`` turns a reply
      that is *itself* an inability statement into a run failure rather
      than a notification. It appears in no summary of "the three gates"
      and would have vanished silently in extraction.
    - ``DUPLICATE_SUPPRESSED`` -- the two-tier mechanical dedup against
      recently DELIVERED notifications. Since decomposition step 2 (see
      docs/ARCHITECTURE.md section 7) this gate fires in the
      consumer's delivery worker at notification-mint time, not in the
      engine: "what was delivered" belongs to the deliverer. The engine
      never emits this value any more; it stays in the enum so historical
      outbox lines keep parsing, and so the worker records its suppression
      under the same closed vocabulary.

    ``CHAT_HTTP_REPLY`` is the gate for ``runner.run_chat_message``: a chat
    or reply turn's answer travels back over HTTP and is never pushed from
    the run path (``notified=False,  # chat replies return over HTTP``). It
    is a real branch in the code that zeroes delivery, so it is a real gate
    with a real recorded reason -- not an unexplained absence of an event.
    """

    POLICY_NEVER = "policy-never"
    POLICY_ALWAYS = "policy-always"
    AUTO_SENTINEL = "auto-sentinel"
    URGENT_MARKER_PRESENT = "urgent-marker-present"
    URGENT_MARKER_MISSING = "urgent-marker-missing"
    FINAL_REPLY_EMPTY = "final-reply-empty"
    REFUSAL_DETECTED = "refusal-detected"
    DUPLICATE_SUPPRESSED = "duplicate-suppressed"
    RUN_FAILED = "run-failed"
    CHAT_HTTP_REPLY = "chat-http-reply"


# The event types a consumer must actually deliver somewhere. These are the
# ones fsync'd before the flock is released -- see the module docstring.
_DURABLE_TYPES: frozenset[EventType] = frozenset(
    {EventType.DELIVERY_INTENT, EventType.AUTOMATION_ERROR}
)

# Required, no-default fields per event type. "Required" here means present
# AND non-blank for strings: an empty reason is the same silence a missing
# one is (failure class 13 -- an optional field that decays into a
# constant, or in this case into nothing).
_REQUIRED_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.RUN_STARTED: ("run_id", "automation", "session_id", "trigger"),
    EventType.RUN_COMPLETED: (
        "run_id",
        "automation",
        "session_id",
        "final_reply_rule",
    ),
    EventType.DELIVERY_INTENT: (
        "run_id",
        "automation",
        "session_id",
        "verdict",
        "gate",
        "reason",
    ),
    EventType.AUTOMATION_ERROR: (
        "run_id",
        "automation",
        "session_id",
        "reason",
        "text",
    ),
    EventType.SESSION_ROTATED: (
        "automation",
        "old_session_id",
        "reason",
    ),
    # Section 7.1: tool + label + reason, required with no defaults -- an
    # inject skip with no named tool or reason would be the unexplained
    # silence the hybrid-sentinel contract exists to forbid.
    EventType.INJECT_SKIPPED: (
        "run_id",
        "automation",
        "session_id",
        "tool",
        "label",
        "reason",
    ),
    EventType.TURN_COMPLETED: (
        "run_id",
        "automation",
        "session_id",
        "origin",
        "label",
    ),
}


def outbox_path(runs_dir: Path) -> Path:
    """The outbox file, given the same ``runs_dir`` every other store uses."""
    return Path(runs_dir).expanduser() / OUTBOX_FILENAME


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _iso8601_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(event_type: EventType, payload: dict[str, Any]) -> None:
    """Reject an event that is missing a required field, before it is written.

    This is the class-13 guard living below the interface: no call site can
    pass a default in, because there is no default to pass.
    """
    missing: list[str] = []
    for field in _REQUIRED_FIELDS[event_type]:
        if field not in payload:
            missing.append(f"{field} (absent)")
            continue
        value = payload[field]
        if value is None:
            missing.append(f"{field} (None)")
        elif isinstance(value, str) and not value.strip():
            missing.append(f"{field} (blank)")
    if missing:
        raise OutboxWriteError(
            f"{event_type.value} event is missing required field(s) with no "
            f"default: {', '.join(missing)}. These fields exist so a zeroed "
            f"delivery can never be an unexplained silence; supply a real "
            f"value or do not emit the event."
        )

    if event_type is EventType.DELIVERY_INTENT:
        verdict = payload["verdict"]
        gate = payload["gate"]
        if verdict not in {v.value for v in Verdict}:
            raise OutboxWriteError(
                f"unknown verdict {verdict!r}; the closed set is "
                f"{sorted(v.value for v in Verdict)}"
            )
        if gate not in {g.value for g in Gate}:
            raise OutboxWriteError(
                f"unknown gate {gate!r}; the closed set is "
                f"{sorted(g.value for g in Gate)}. A gate found in the code "
                f"and absent from this enum is a blocking finding, not a "
                f"follow-up."
            )
        if "text" not in payload:
            raise OutboxWriteError(
                "delivery_intent must carry `text` (the full final output), "
                "even when the verdict is withhold -- the withheld text is "
                "the evidence that the gate fired on something real"
            )


def append_event(runs_dir: Path, event_type: EventType, payload: dict[str, Any]) -> int:
    """Append one event and return the file's size (in bytes) afterwards.

    The returned size is the byte offset immediately past this event's
    trailing newline -- i.e. exactly what a consumer's cursor becomes once
    it has processed this event. Callers that do not need it may ignore it.

    Raises:
        OutboxWriteError: a required field is missing/blank, or an enum
            value is unknown. Always a bug at the emit site.
        OSError: the write itself failed. Deliberately NOT swallowed here
            -- see the individual ``emit_*`` helpers for which call sites
            are allowed to treat a write failure as non-fatal, and why.
    """
    _validate(event_type, payload)

    record: dict[str, Any] = {
        "type": event_type.value,
        "time": _iso8601_now(),
        **payload,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path = outbox_path(runs_dir)

    with _locked(path):
        existed = path.is_file()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            if event_type in _DURABLE_TYPES:
                f.flush()
                os.fsync(f.fileno())
        if not existed:
            # A brand-new file needs its directory entry on disk too, or the
            # fsync above guarantees the contents of a file that a crash
            # could still lose entirely.
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        return path.stat().st_size


@dataclass(frozen=True)
class OutboxEvent:
    """One complete, parsed line from the outbox.

    ``offset`` is where this line starts; ``end_offset`` is where the next
    line starts (i.e. one past its ``\\n``). A consumer advances its cursor
    to ``end_offset`` only after it has finished acting on the event.
    """

    offset: int
    end_offset: int
    event_type: EventType
    data: dict[str, Any]

    @property
    def run_id(self) -> str:
        return str(self.data.get("run_id", ""))


def _parse_line(line: str, *, offset: int) -> OutboxEvent:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise OutboxParseError(f"line is not valid JSON: {exc}", offset=offset) from exc
    if not isinstance(data, dict):
        raise OutboxParseError(
            f"line is valid JSON but not an object (got {type(data).__name__})",
            offset=offset,
        )
    raw_type = data.get("type")
    try:
        event_type = EventType(raw_type)
    except ValueError as exc:
        raise OutboxParseError(
            f"unknown event type {raw_type!r}. This means the writer is a "
            f"newer version than this reader; halting rather than skipping "
            f"an event that may have needed delivering. Known types: "
            f"{sorted(t.value for t in EventType)}",
            offset=offset,
        ) from exc

    for field in _REQUIRED_FIELDS[event_type]:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise OutboxParseError(
                f"{event_type.value} event is missing required field {field!r}",
                offset=offset,
            )

    if event_type is EventType.DELIVERY_INTENT:
        try:
            Verdict(data["verdict"])
        except ValueError as exc:
            raise OutboxParseError(
                f"unknown verdict {data['verdict']!r}", offset=offset
            ) from exc
        try:
            Gate(data["gate"])
        except ValueError as exc:
            raise OutboxParseError(
                f"unknown gate {data['gate']!r}", offset=offset
            ) from exc
        if "text" not in data:
            raise OutboxParseError(
                "delivery_intent event has no `text` field", offset=offset
            )

    end_offset = offset + len(line.encode("utf-8"))
    return OutboxEvent(
        offset=offset, end_offset=end_offset, event_type=event_type, data=data
    )


def read_since(runs_dir: Path, cursor: int) -> tuple[list[OutboxEvent], int]:
    """Read every COMPLETE event at or after byte ``cursor``.

    Returns ``(events, complete_end)`` where ``complete_end`` is the byte
    offset one past the last complete line -- everything after it is a
    partial final line the writer has not finished, and is deliberately
    invisible to this reader (the torn-tail rule).

    An empty/absent file yields ``([], cursor)``. A cursor past EOF -- which
    can only mean the file was truncated or replaced underneath us -- raises
    rather than silently rewinding to 0 and replaying the world.

    Raises:
        OutboxParseError: a complete line could not be understood. The
            caller must halt at ``exc.offset``; skipping it would be the
            silent drop this whole seam exists to make impossible.
    """
    path = outbox_path(runs_dir)
    if not path.is_file():
        return [], cursor

    size = path.stat().st_size
    if cursor > size:
        raise OutboxParseError(
            f"cursor {cursor} is past the end of {path} (size {size}). The "
            f"outbox was truncated or replaced; refusing to guess whether to "
            f"replay from 0 or skip forward",
            offset=cursor,
        )
    if cursor == size:
        return [], cursor

    with open(path, "rb") as f:
        f.seek(cursor)
        chunk = f.read()

    newline = chunk.rfind(b"\n")
    if newline == -1:
        # Nothing but a partial line since the cursor: "not yet written."
        return [], cursor
    complete = chunk[: newline + 1]
    complete_end = cursor + len(complete)

    events: list[OutboxEvent] = []
    offset = cursor
    for raw in complete.splitlines(keepends=True):
        text = raw.decode("utf-8", errors="strict")
        if text.strip():
            events.append(_parse_line(text, offset=offset))
        offset += len(raw)
    return events, complete_end


def outbox_status(runs_dir: Path, *, cursor: int | None) -> dict[str, Any]:
    """Size, age and cursor lag -- the delivery worker's only liveness surface.

    The worker is a thread inside notify-serve, so it has no process of its
    own to doctor. Section 6 answers that by doubling this readout across
    both doctor faces: if either side goes quiet, the lag is visible from
    the other. ``cursor`` is the worker's persisted byte offset, or ``None``
    when it has never recorded one.
    """
    path = outbox_path(runs_dir)
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "size_bytes": 0,
            "last_write": None,
            "age_seconds": None,
            "cursor": cursor,
            "lag_bytes": None,
        }
    stat = path.stat()
    age = max(0.0, datetime.now(UTC).timestamp() - stat.st_mtime)
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "last_write": datetime.fromtimestamp(stat.st_mtime, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "age_seconds": round(age, 1),
        "cursor": cursor,
        "lag_bytes": (None if cursor is None else max(0, stat.st_size - cursor)),
    }


__all__ = [
    "OUTBOX_FILENAME",
    "EventType",
    "Gate",
    "OutboxError",
    "OutboxEvent",
    "OutboxParseError",
    "OutboxWriteError",
    "Verdict",
    "append_event",
    "outbox_path",
    "outbox_status",
    "read_since",
]
