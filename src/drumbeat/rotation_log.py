"""Durable logging of every deliberate pinned-session rotation.

``drumbeat rotate-session <slug>`` (see ``cli._cmd_rotate_session``) deletes
an automation's entry from the engine's session pin store
(``<data-dir>/session_pins.json``) so the next run starts a fresh
amplifier-agent session. Before this module existed, that action's only
trace was a stdout print -- easy to miss and not greppable in one place.
Rotation is a deliberate abandonment of accumulated session memory; that
decision must be recorded somewhere durable and findable later, the same
discipline ``error_log.py`` already applies to ``AutomationError`` and
``runner._log_run_failure`` applies to failed runs.

FAIL LOUD: a write failure here must never be silently swallowed into
nothing -- it is printed to stderr as a fallback so the operator still sees
it, but it must never mask the rotation itself (the rotation has already
happened by the time this is called; this only records it).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from drumbeat import error_log

# WHERE THIS LOG LIVES: a
# data-dir resident, resolved through drumbeat.error_log's shared order --
# explicit set_log_data_dir() > DRUMBEAT_DATA_DIR env > <cwd>/runs at CALL
# time -- never Path.cwd() captured at module import. Every real call site
# passes log_path explicitly today, and B5's rotations were unaffected; the
# default is fixed anyway, because a default that is only safe while nobody
# uses it is class-2 furniture. See error_log.py for the full post-mortem.


def _default_log_path() -> Path:
    return error_log.resolve_log_data_dir() / "session_rotations.jsonl"


def log_session_rotation(
    *,
    automation_name: str,
    automation_slug: str,
    automation_path: Path,
    old_session_id: str,
    reason: str,
    log_path: Path | None = None,
) -> None:
    """Append one JSON line recording a deliberate session rotation.

    Args:
        automation_name: the automation's display name.
        automation_slug: the automation's filesystem-safe slug.
        automation_path: path to the automation's ``.md`` file.
        old_session_id: the session id being abandoned (never ``None`` --
            callers only invoke this when a real rotation happened).
        reason: why the rotation happened (e.g. "manual: drumbeat
            rotate-session", or a specific diagnosis like "prompt exceeded
            provider token ceiling").
        log_path: explicit destination; when omitted, resolves through
            ``error_log.resolve_log_data_dir()`` (explicit set >
            DRUMBEAT_DATA_DIR env > ``<cwd>/runs`` at call time).
    """
    record = {
        "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "automation": automation_name,
        "slug": automation_slug,
        "path": str(automation_path),
        "old_session_id": old_session_id,
        "reason": reason,
    }
    line = json.dumps(record)
    path = (log_path or _default_log_path()).expanduser()
    try:
        # Migration requirement (the engine and its consumer ran as two
        # processes over one workspace during the extraction): flocked
        # append -- engine code runs in two processes during the hybrid
        # window and this file is engine state shared between them.
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    except OSError as exc:
        sys.stderr.write(f"[rotation_log] failed to write {path}: {exc}\n")
        sys.stderr.write(f"[rotation_log] {line}\n")


__all__ = ["log_session_rotation"]
