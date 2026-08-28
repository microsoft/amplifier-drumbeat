"""Durable logging of every ``AutomationError`` the codebase raises.

``automation.py`` already raises a structured ``AutomationError`` on every
malformed-automation case, but until now nothing recorded those occurrences
anywhere durable -- a broken automation file failed loudly to whichever
caller happened to be looking (an HTTP response, a CLI stderr line) and then
the fact that it failed was gone. This module is the single choke point that
fixes that: ``automation.AutomationError.__init__`` calls
``log_automation_error`` on every construction, so every raise site --
existing and future -- is covered without any caller needing to remember to
log it.

Logging failures must never turn a real parse error into a different, less
informative crash (e.g. a full disk raising here would be a strictly worse
failure than the AutomationError itself), so a write failure is swallowed
and reported to stderr as a fallback instead of propagating.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from drumbeat import fsutil

# WHERE THESE LOGS LIVE.
#
# These files are DATA-DIR residents: they must survive a reboot and be
# greppable long after the process that hit the error has exited, next to
# every other operational log the engine keeps. The design invariant
# is: *a data-dir resident's address is a property
# of the engine's data dir, never of the process's or turn's cwd.*
#
# The previous version of this module violated that in its own comment: it
# anchored the defaults to ``Path.cwd()`` **captured at module import**,
# reasoning that "both services launch from the workspace root ... same
# directory in every real deployment." True exactly until the workspace and
# the data dir stopped sharing a root (B5's packet cutover: --workspace
# points at a policy checkout, --data-dir stays behind) -- at which point a
# benign lookup-miss wrote ``automation_errors.jsonl`` INSIDE the policy
# tree, gitignored and invisible. Gate NEW-2 caught it in live operation.
#
# Resolution order, same seam as the ledger tools (fifth wall):
#
#   1. explicit plumbing -- ``set_log_data_dir()``, called once at process
#      startup by anything that knows its resolved data dir (drumbeat serve,
#      every drumbeat CLI verb, the consumer's notify-serve);
#   2. ``DRUMBEAT_DATA_DIR`` -- the env var the engine exports into every
#      turn, for consumer CLIs across the process boundary (DO-NOT-SWEEP:
#      it is the address of the ledger AND these logs; grazing it re-creates
#      walls 5-6);
#   3. ``<cwd>/runs`` resolved AT CALL TIME -- the historic behavior for a
#      bare operator shell, never captured at import.
DATA_DIR_ENV_VAR = "DRUMBEAT_DATA_DIR"

_data_dir_override: Path | None = None


def set_log_data_dir(data_dir: Path) -> None:
    """Point this process's engine logs at ``data_dir``. Call once, at startup.

    Not thread-guarded and not intended to be toggled at runtime: a process
    logging into two different data dirs over its lifetime is a bug, not a
    feature (the same posture as the consumer's ``set_pack_workspace_root``).
    """
    global _data_dir_override
    _data_dir_override = Path(data_dir).expanduser()


def resolve_log_data_dir() -> Path:
    """The data dir these logs resolve under, per the order documented above."""
    if _data_dir_override is not None:
        return _data_dir_override
    env_value = os.environ.get(DATA_DIR_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return Path.cwd() / "runs"


def automation_errors_path() -> Path:
    """Where ``AutomationError`` occurrences are appended, resolved now."""
    return resolve_log_data_dir() / "automation_errors.jsonl"


# Same convention, separate file: a malformed guidance vocabulary file (see
# the consumer's ``vocabulary.VocabularyError``) is a distinct failure class from a
# malformed automation file -- keeping them in separate greppable logs
# means "which guidance edit broke item validation" never requires
# filtering out unrelated automation-parse noise, and vice versa.
def vocabulary_errors_path() -> Path:
    """Where ``VocabularyError`` occurrences are appended, resolved now."""
    return resolve_log_data_dir() / "vocabulary_errors.jsonl"


def _append_jsonl(log_path: Path, record: dict) -> None:
    # Migration requirement (two processes, one workspace): during the
    # hybrid window engine code runs in TWO processes, and per-session flocks
    # do not arbitrate this file. The append happens inside an exclusive
    # flock on a sidecar .lock (the same convention the outbox uses) so two
    # processes can never interleave partial lines.
    #
    # The line itself is written via `fsutil.append_line_single_write`:
    # one `os.write` for the whole record (never a buffered multi-write
    # flush a SIGKILL could tear in half), and it heals a torn tail left by
    # a prior killed writer before adding this line -- the same crash-
    # safety discipline `engine_events.append_event` uses for the outbox.
    line = json.dumps(record)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(log_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fsutil.append_line_single_write(log_path, line + "\n")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except OSError as exc:
        sys.stderr.write(f"[error_log] failed to write {log_path}: {exc}\n")
        sys.stderr.write(f"[error_log] {line}\n")


def log_automation_error(path: Path, problem: str) -> None:
    """Append one JSON line recording an ``AutomationError`` occurrence.

    Fields are deliberately minimal and match what ``AutomationError``
    itself carries: the file path involved (the closest thing this
    exception has to "which automation") and the problem message (which,
    for every raise site in ``automation.py``, already names the specific
    field that failed validation -- e.g. ``"automation.trigger must be a
    mapping"``).
    """
    _append_jsonl(
        automation_errors_path(),
        {
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "path": str(path),
            "problem": problem,
        },
    )


def log_vocabulary_error(path: Path, problem: str) -> None:
    """Append one JSON line recording a ``vocabulary.VocabularyError`` occurrence.

    Same discipline as ``log_automation_error``, applied to the guidance
    vocabulary file (``guidance/ATTENTION.md``'s ``vocabulary:`` frontmatter)
    instead of an automation file -- see ``vocabulary.VocabularyError`` for
    every case this covers (missing file, missing/malformed frontmatter,
    empty or malformed lists).
    """
    _append_jsonl(
        vocabulary_errors_path(),
        {
            "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "path": str(path),
            "problem": problem,
        },
    )


__all__ = [
    "DATA_DIR_ENV_VAR",
    "automation_errors_path",
    "log_automation_error",
    "log_vocabulary_error",
    "resolve_log_data_dir",
    "set_log_data_dir",
    "vocabulary_errors_path",
]
