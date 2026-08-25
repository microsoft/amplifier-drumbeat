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


__all__ = ["atomic_write", "atomic_write_with_backup"]
