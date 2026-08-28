"""Shared filesystem write helpers.

Extracted because more than one module needs the identical atomic-write
discipline: ``management_api`` (automation/prompt/guidance file edits from
the phone, plus in-flight run status files) and ``automation`` (session-id
write-back into an automation's own frontmatter, and rotation). One
implementation, not copies that can silently drift apart.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + rename), no backup.

    Use for machine-generated, frequently-overwritten files (e.g. an
    in-flight run's status file) where a growing pile of ``.bak`` copies
    would be pure noise. For hand-authored files a human or agent edits,
    use ``atomic_write_with_backup`` instead.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_with_backup(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically, keeping a ``.bak`` of any prior version.

    These are hand-authored files (automations, prompts, guidance policy) --
    a bad write must never lose real work, so any existing version is
    copied to ``<name>.bak`` before the new content lands via
    temp-file-plus-rename (``atomic_write``).
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        backup_path = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup_path)

    atomic_write(path, content)


def heal_torn_tail(fd: int) -> bool:
    """If the open file's last byte is not ``\\n``, write one and return True.

    A durable, append-only JSONL log (``engine-events.jsonl``,
    ``automation_errors.jsonl``, ``vocabulary_errors.jsonl``,
    ``session_rotations.jsonl`` -- every JSONL append in this codebase)
    shares one crash-safety hazard: a writer killed mid-append (SIGKILL,
    which cannot be caught) can leave a torn final line -- a valid-JSON
    prefix with no trailing newline. Left alone, the NEXT append's bytes
    land directly after that fragment with no separating newline, fusing
    two events into one unparseable line and taking the new event down
    with the old corruption.

    Call this under whatever lock guards ``path`` (this function does not
    lock), immediately before appending new content, on a file descriptor
    opened for read+write. Sealing the torn line with its own newline
    keeps the NEW append independent of it -- at the cost of one line the
    reader will report as unparseable (the torn content itself, now
    newline-terminated) rather than two.

    Returns whether a heal happened, so the caller can log it: silence
    here would hide exactly the class of event this function exists to
    make visible.
    """
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0:
        return False
    os.lseek(fd, size - 1, os.SEEK_SET)
    last_byte = os.read(fd, 1)
    os.lseek(fd, 0, os.SEEK_END)
    if last_byte == b"\n":
        return False
    os.write(fd, b"\n")
    return True


def append_line_single_write(path: Path, line: str, *, fsync: bool = False) -> int:
    """Append ``line`` (newline included) to ``path`` in exactly one ``os.write``.

    Heals a torn tail left by a prior killed writer first (see
    ``heal_torn_tail``), then writes the WHOLE line as one ``os.write``
    call on an ``O_APPEND`` descriptor -- never through a buffered
    ``TextIOWrapper``, whose ``close()``/``flush()`` can issue several
    smaller ``write(2)`` calls for one logical line (exactly the gap that
    let a SIGKILL tear a line in half). One call closes the window that
    existed between multiple buffered ``write(2)`` syscalls for one
    logical line. It is NOT an unconditional crash-atomicity guarantee:
    for very large lines the kernel's regular-file write path can return
    a short write when a fatal signal lands mid-copy — ``heal_torn_tail``
    is what bounds that residual case to one isolated unparseable line
    instead of a fused, cascading corruption.

    Does NOT lock -- callers already hold whatever mutual-exclusion
    mechanism guards concurrent writers to ``path`` (every call site in
    this codebase uses a sidecar ``<path>.lock`` flock). ``fsync`` is the
    caller's choice: durable event types warrant it, routine log lines
    can ride the page cache like their siblings in the same file.

    Returns the file's size (bytes) after the append.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = line.encode("utf-8")
    existed = path.is_file()
    # O_RDWR (not O_WRONLY): heal_torn_tail reads the last byte first.
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        healed = existed and heal_torn_tail(fd)
        os.write(fd, data)
        if fsync:
            os.fsync(fd)
        size = os.fstat(fd).st_size
    finally:
        os.close(fd)
    if healed:
        sys.stderr.write(
            f"[fsutil] healed a torn tail in {path} -- the previous writer "
            "was killed mid-append; sealed its incomplete line with a "
            "newline before appending this one. The healed line will read "
            "as unparseable at its own offset; this append is not lost.\n"
        )
    return size


__all__ = [
    "append_line_single_write",
    "atomic_write",
    "atomic_write_with_backup",
    "heal_torn_tail",
]
