"""Tests for ``drumbeat.fsutil``'s shared JSONL-append crash-safety helpers.

``append_line_single_write`` and ``heal_torn_tail`` are the one home for a
discipline every JSONL appender in this codebase needs (``engine_events``'s
outbox, ``error_log``'s automation/vocabulary error logs, ``rotation_log``'s
session-rotation log): a SIGKILL mid-append must not be able to tear a line
in half, and a torn tail left by a prior killed writer must be healed
before the next line is added, never fused onto it. See
``engine_events.append_event`` for the fuller narrative -- these tests
cover the shared mechanism directly, at its one implementation.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from drumbeat import fsutil


class TestAppendLineSingleWrite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sub" / "log.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_parent_directories(self) -> None:
        fsutil.append_line_single_write(self.path, "line one\n")
        self.assertTrue(self.path.is_file())

    def test_one_line_is_exactly_one_os_write_call(self) -> None:
        calls: list[bytes] = []
        real_write = fsutil.os.write

        def _counting_write(fd: int, data: bytes) -> int:
            calls.append(data)
            return real_write(fd, data)

        with unittest.mock.patch.object(fsutil.os, "write", _counting_write):
            fsutil.append_line_single_write(self.path, "a single line\n")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], b"a single line\n")

    def test_a_large_line_is_still_one_write_call(self) -> None:
        calls: list[bytes] = []
        real_write = fsutil.os.write

        def _counting_write(fd: int, data: bytes) -> int:
            calls.append(data)
            return real_write(fd, data)

        big_line = ("x" * 500_000) + "\n"
        with unittest.mock.patch.object(fsutil.os, "write", _counting_write):
            fsutil.append_line_single_write(self.path, big_line)

        self.assertEqual(len(calls), 1)

    def test_returns_the_file_size_after_the_append(self) -> None:
        size = fsutil.append_line_single_write(self.path, "abc\n")
        self.assertEqual(size, 4)
        size2 = fsutil.append_line_single_write(self.path, "de\n")
        self.assertEqual(size2, 7)

    def test_multiple_appends_are_byte_identical_to_a_plain_concatenation(self) -> None:
        fsutil.append_line_single_write(self.path, "one\n")
        fsutil.append_line_single_write(self.path, "two\n")
        fsutil.append_line_single_write(self.path, "three\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "one\ntwo\nthree\n")

    def test_torn_tail_is_healed_before_the_new_line_is_added(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"incomplete", no trailing newline', encoding="utf-8")

        fsutil.append_line_single_write(self.path, '{"complete": true}\n')

        raw = self.path.read_text(encoding="utf-8")
        lines = raw.split("\n")
        self.assertEqual(lines[0], '{"incomplete", no trailing newline')
        self.assertEqual(lines[1], '{"complete": true}')
        # Never fused onto one line.
        self.assertNotIn('newline{"complete"', raw)

    def test_heal_is_logged_to_stderr(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("torn", encoding="utf-8")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            fsutil.append_line_single_write(self.path, "next\n")

        self.assertIn("healed a torn tail", stderr.getvalue())

    def test_no_heal_and_no_log_when_file_already_ends_in_newline(self) -> None:
        fsutil.append_line_single_write(self.path, "first\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            fsutil.append_line_single_write(self.path, "second\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "first\nsecond\n")

    def test_fsync_true_calls_os_fsync(self) -> None:
        with unittest.mock.patch.object(fsutil.os, "fsync") as mock_fsync:
            fsutil.append_line_single_write(self.path, "durable\n", fsync=True)
        mock_fsync.assert_called_once()

    def test_fsync_false_never_calls_os_fsync(self) -> None:
        with unittest.mock.patch.object(fsutil.os, "fsync") as mock_fsync:
            fsutil.append_line_single_write(self.path, "not durable\n", fsync=False)
        mock_fsync.assert_not_called()


class TestHealTornTail(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "log.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_file_needs_no_heal(self) -> None:
        self.path.write_bytes(b"")
        fd = os.open(self.path, os.O_RDWR)
        try:
            self.assertFalse(fsutil.heal_torn_tail(fd))
        finally:
            os.close(fd)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_clean_trailing_newline_needs_no_heal(self) -> None:
        self.path.write_bytes(b"a line\n")
        fd = os.open(self.path, os.O_RDWR)
        try:
            self.assertFalse(fsutil.heal_torn_tail(fd))
        finally:
            os.close(fd)
        self.assertEqual(self.path.read_bytes(), b"a line\n")

    def test_missing_trailing_newline_is_healed(self) -> None:
        self.path.write_bytes(b"a torn line")
        fd = os.open(self.path, os.O_RDWR)
        try:
            self.assertTrue(fsutil.heal_torn_tail(fd))
        finally:
            os.close(fd)
        self.assertEqual(self.path.read_bytes(), b"a torn line\n")
