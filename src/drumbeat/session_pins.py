"""Session pins: which amplifier-agent conversation each automation resumes.

This is ENGINE STATE. It used to live in the automation file's own
frontmatter (``session:`` / ``session_workspace:``), which made a policy
file carry machine-local conversation ids -- the exact "state the server
owns, held elsewhere" shape (failure class 7). Versioning those files
would have committed conversation ids into an archive that restores them
onto machines where those conversations do not exist.

The store lives under the engine's data dir, keyed by automation **slug**,
because the pin answers "which conversation does this automation resume" --
a per-automation fact, not a per-session one.

Posture, all three halves deliberate:

- **Atomic writes.** Temp file plus rename (``fsutil.atomic_write``), under
  an exclusive flock held across the whole read-modify-write, so two
  processes upserting different automations cannot lose each other's pin.
- **Refuse on corrupt.** An unparseable, torn, or wrong-shaped store raises
  ``PinStoreError``. It is never read as empty: read-as-empty is a silent
  mass rotation -- every automation cold-starts, every accumulated
  conversation is abandoned, and nothing says so. That is class 7 wearing
  class 2's coat.
- **Abort on write failure.** Callers must treat a failed upsert as fatal to
  the run. The old frontmatter write-back logged a WARNING and proceeded;
  carried over here, a full disk would become a silent fresh-session fork on
  every single run, forever.

The one case that IS read as empty is an **absent** file. Absent is not
corrupt -- it is the legitimate state of a workspace whose engine has never
pinned anything (a fresh install, or a packet clone before its first run),
and there is no way to distinguish "never created" from "deleted" without
inventing a second state file to guard the first. The fail-loud for a
deleted store is downstream and immediate: every run prints "no pinned
session -- creating" and ``drumbeat sessions`` reports zero pins.
"""

from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from drumbeat.fsutil import atomic_write

PINS_FILENAME = "session_pins.json"
PINS_FORMAT = 1

# Recorded on every pin so "where did this pin come from" is answerable from
# the store alone. ``run`` = a scheduled/manual automation run created the
# session; ``chat`` = a chat message did.
CREATED_BY_RUN = "run"
CREATED_BY_CHAT = "chat"


class PinStoreError(Exception):
    """The pin store could not be read or written. Always fatal to the caller.

    Never raised for an absent store (see the module docstring) -- only for
    a store that exists and cannot be trusted, or a write that did not land.
    """


@dataclass(frozen=True)
class Pin:
    """One automation's pinned conversation.

    ``session_workspace`` is the amplifier-agent workspace slug the session
    was created under. It stays with the pin (rather than being re-derived
    at read time) because it is what makes a cwd move DETECTABLE -- the
    runner compares it against the current derivation and aborts on
    mismatch instead of silently cold-starting. ``None`` only for pins
    lifted from pre-``session_workspace`` frontmatter.
    """

    slug: str
    session_id: str
    session_workspace: str | None
    created_at: str
    created_by: str

    def as_json(self) -> dict[str, str | None]:
        return {
            "session_id": self.session_id,
            "session_workspace": self.session_workspace,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


def pins_path(runs_dir: Path) -> Path:
    """The store file for this data dir."""
    return Path(runs_dir).expanduser() / PINS_FILENAME


def _lock_path(runs_dir: Path) -> Path:
    return Path(runs_dir).expanduser() / (PINS_FILENAME + ".lock")


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(path: Path, raw: str) -> dict[str, Pin]:
    """Turn stored JSON into pins, refusing anything it cannot fully trust."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PinStoreError(
            f"{path}: session pin store is unparseable ({exc}). REFUSING to "
            "read it as empty -- that would silently cold-start every "
            "automation and abandon every accumulated conversation. Restore "
            "the file, or delete it deliberately if a mass rotation is what "
            "you actually want."
        ) from exc

    if not isinstance(data, dict):
        raise PinStoreError(
            f"{path}: session pin store must be a JSON object, got "
            f"{type(data).__name__}"
        )

    fmt = data.get("pins_format")
    if fmt != PINS_FORMAT:
        raise PinStoreError(
            f"{path}: session pin store declares pins_format={fmt!r}, this "
            f"engine understands {PINS_FORMAT}. Refusing to guess at a "
            "format it was not written in."
        )

    pins_raw = data.get("pins")
    if not isinstance(pins_raw, dict):
        raise PinStoreError(
            f"{path}: session pin store's 'pins' must be an object, got "
            f"{type(pins_raw).__name__}"
        )

    pins: dict[str, Pin] = {}
    for slug, entry in pins_raw.items():
        if not isinstance(slug, str) or not slug.strip():
            raise PinStoreError(f"{path}: pin key {slug!r} is not a usable slug")
        if not isinstance(entry, dict):
            raise PinStoreError(
                f"{path}: pin {slug!r} must be an object, got {type(entry).__name__}"
            )
        session_id = entry.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise PinStoreError(
                f"{path}: pin {slug!r} has no usable session_id "
                f"({session_id!r}) -- a pin that names no conversation is "
                "worse than no pin, because it reads as pinned"
            )
        workspace = entry.get("session_workspace")
        if workspace is not None and (
            not isinstance(workspace, str) or not workspace.strip()
        ):
            raise PinStoreError(
                f"{path}: pin {slug!r} has a malformed session_workspace "
                f"({workspace!r})"
            )
        created_at = entry.get("created_at")
        created_by = entry.get("created_by")
        pins[slug] = Pin(
            slug=slug,
            session_id=session_id.strip(),
            session_workspace=workspace.strip() if isinstance(workspace, str) else None,
            created_at=created_at if isinstance(created_at, str) else "",
            created_by=created_by if isinstance(created_by, str) else "",
        )
    return pins


def _serialize(pins: dict[str, Pin]) -> str:
    payload = {
        "pins": {slug: pins[slug].as_json() for slug in sorted(pins)},
        "pins_format": PINS_FORMAT,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _read_unlocked(path: Path) -> dict[str, Pin]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PinStoreError(
            f"{path}: session pin store exists but could not be read ({exc}). "
            "REFUSING to proceed as if there were no pins."
        ) from exc
    if not raw.strip():
        raise PinStoreError(
            f"{path}: session pin store is empty/torn (zero bytes of content). "
            "REFUSING to read it as 'no pins' -- an interrupted write must "
            "not look like a deliberate mass rotation."
        )
    return _parse(path, raw)


class _StoreLock:
    """Exclusive flock over the store, held across read-modify-write."""

    def __init__(self, runs_dir: Path) -> None:
        self._path = _lock_path(runs_dir)
        self._handle = None

    def __enter__(self) -> Self:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "w", encoding="utf-8")
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            raise PinStoreError(
                f"{self._path}: could not take the session pin store lock ({exc})"
            ) from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def read_all(runs_dir: Path) -> dict[str, Pin]:
    """Every pin, keyed by automation slug. Raises ``PinStoreError`` if corrupt."""
    path = pins_path(runs_dir)
    with _StoreLock(runs_dir):
        return _read_unlocked(path)


def get(slug: str, *, runs_dir: Path) -> Pin | None:
    """This automation's pin, or ``None`` if it has never been pinned."""
    return read_all(runs_dir).get(slug)


def upsert(
    slug: str,
    *,
    session_id: str,
    session_workspace: str | None,
    created_by: str,
    runs_dir: Path,
) -> Pin:
    """Record (or replace) this automation's pin. Raises on any failure.

    Callers MUST treat a raise as fatal to whatever they were doing. See the
    module docstring: warn-and-proceed here turns a full disk into a silent
    fresh-session fork on every run.
    """
    if not slug.strip():
        raise PinStoreError("refusing to pin a session under an empty slug")
    if not session_id.strip():
        raise PinStoreError(f"refusing to pin an empty session id for {slug!r}")

    path = pins_path(runs_dir)
    pin = Pin(
        slug=slug,
        session_id=session_id.strip(),
        session_workspace=session_workspace,
        created_at=_iso_now(),
        created_by=created_by,
    )
    with _StoreLock(runs_dir):
        pins = _read_unlocked(path)
        pins[slug] = pin
        try:
            atomic_write(path, _serialize(pins))
        except OSError as exc:
            raise PinStoreError(
                f"{path}: failed to write session pin for {slug!r} "
                f"({session_id!r}): {exc}"
            ) from exc
        # Read back inside the lock: a write that did not take effect must
        # not be reported as a pin that landed.
        written = _read_unlocked(path).get(slug)
        if written is None or written.session_id != pin.session_id:
            raise PinStoreError(
                f"{path}: session pin for {slug!r} did not take effect as "
                f"expected (wrote {pin.session_id!r}, read back "
                f"{written.session_id if written else None!r})"
            )
    return pin


def delete(slug: str, *, runs_dir: Path) -> Pin | None:
    """Remove this automation's pin. Returns the removed pin, or ``None``.

    Returning the removed pin is load-bearing: rotation must never destroy
    the only record of the just-abandoned session id without surfacing it
    first.
    """
    path = pins_path(runs_dir)
    with _StoreLock(runs_dir):
        pins = _read_unlocked(path)
        removed = pins.pop(slug, None)
        if removed is None:
            return None
        try:
            atomic_write(path, _serialize(pins))
        except OSError as exc:
            raise PinStoreError(
                f"{path}: failed to remove session pin for {slug!r}: {exc}"
            ) from exc
        if slug in _read_unlocked(path):
            raise PinStoreError(
                f"{path}: session pin removal for {slug!r} did not take effect"
            )
    return removed


def orphans(runs_dir: Path, *, known_slugs: set[str]) -> list[str]:
    """Pins whose slug matches no automation file, sorted.

    The named cost of keying by slug (section 5): renaming an automation
    file is a cold start plus a stranded pin. This is the fence -- a set
    difference over data already in hand, surfaced by ``drumbeat doctor``
    and ``drumbeat sessions``, rather than rename-detection machinery for a
    hand-performed event.
    """
    return sorted(slug for slug in read_all(runs_dir) if slug not in known_slugs)


__all__ = [
    "CREATED_BY_CHAT",
    "CREATED_BY_RUN",
    "PINS_FILENAME",
    "PINS_FORMAT",
    "Pin",
    "PinStoreError",
    "delete",
    "get",
    "orphans",
    "pins_path",
    "read_all",
    "upsert",
]
