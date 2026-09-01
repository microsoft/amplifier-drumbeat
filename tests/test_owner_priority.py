"""Tests for drumbeat.owner_priority -- the owner-priority latch.

Covers the module in isolation: marking, live-window expiry, and the bounded
deferral ceiling. Cross-module wiring (turns.py intake, scheduler.py's
due-automation loop) is covered in test_turns.py and test_scheduler.py.
"""

from __future__ import annotations

import unittest

from drumbeat import owner_priority


class TestMarkAndIsWaiting(unittest.TestCase):
    def setUp(self) -> None:
        owner_priority._reset_for_tests()

    def tearDown(self) -> None:
        owner_priority._reset_for_tests()

    def test_unmarked_session_never_waiting(self) -> None:
        # The hard "unmarked callers are unaffected" invariant: a session
        # id nobody has ever called mark_waiting() for is never reported as
        # waiting, regardless of what it is.
        self.assertFalse(owner_priority.is_waiting("session-nobody-marked"))
        self.assertFalse(owner_priority.is_waiting(None))
        self.assertFalse(owner_priority.is_waiting(""))

    def test_mark_then_is_waiting_true(self) -> None:
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        self.assertTrue(owner_priority.is_waiting("s1"))
        # A different, unrelated session is untouched.
        self.assertFalse(owner_priority.is_waiting("s2"))

    def test_window_expires(self) -> None:
        # A vanishingly short window expires essentially immediately --
        # avoids a real sleep() in the test while still proving expiry.
        owner_priority.mark_waiting("s1", window_seconds=-1.0)
        self.assertFalse(owner_priority.is_waiting("s1"))

    def test_mark_is_noop_for_falsy_session_id(self) -> None:
        # Must never raise, and must never manufacture a waiting entry for
        # "no session" (a brand-new chat session, e.g., has no session_id
        # yet and cannot be contended by anything -- see turns.py).
        owner_priority.mark_waiting(None)
        owner_priority.mark_waiting("")
        self.assertFalse(owner_priority.is_waiting(None))
        self.assertFalse(owner_priority.is_waiting(""))

    def test_refresh_extends_window(self) -> None:
        owner_priority.mark_waiting("s1", window_seconds=-1.0)
        self.assertFalse(owner_priority.is_waiting("s1"))
        # A fresh mark after expiry is a brand new, live episode.
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        self.assertTrue(owner_priority.is_waiting("s1"))


class TestShouldDefer(unittest.TestCase):
    def setUp(self) -> None:
        owner_priority._reset_for_tests()

    def tearDown(self) -> None:
        owner_priority._reset_for_tests()

    def test_no_waiting_never_defers(self) -> None:
        self.assertFalse(owner_priority.should_defer("s1"))

    def test_defers_while_waiting_and_under_ceiling(self) -> None:
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        self.assertTrue(owner_priority.should_defer("s1", max_deferrals=3))
        self.assertTrue(owner_priority.should_defer("s1", max_deferrals=3))
        self.assertTrue(owner_priority.should_defer("s1", max_deferrals=3))

    def test_bounded_deferral_never_starves_automation(self) -> None:
        # The hard "bounded, never a permanent freeze" invariant: even
        # with the owner continuously re-marking the session (simulating a
        # gateway retry loop that never stops), the automation is allowed
        # through once the ceiling is reached.
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        results = []
        for _ in range(5):
            owner_priority.mark_waiting("s1", window_seconds=30.0)  # owner still asking
            results.append(owner_priority.should_defer("s1", max_deferrals=3))
        self.assertEqual(results, [True, True, True, False, False])

    def test_deferral_count_resets_after_episode_lapses(self) -> None:
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        for _ in range(3):
            self.assertTrue(owner_priority.should_defer("s1", max_deferrals=3))
        self.assertFalse(owner_priority.should_defer("s1", max_deferrals=3))
        # Episode lapses (owner stopped asking).
        owner_priority.mark_waiting("s1", window_seconds=-1.0)
        self.assertFalse(owner_priority.is_waiting("s1"))
        # A fresh episode gets its own full budget.
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        self.assertTrue(owner_priority.should_defer("s1", max_deferrals=3))

    def test_independent_sessions_have_independent_budgets(self) -> None:
        owner_priority.mark_waiting("s1", window_seconds=30.0)
        owner_priority.mark_waiting("s2", window_seconds=30.0)
        for _ in range(2):
            self.assertTrue(owner_priority.should_defer("s1", max_deferrals=2))
        self.assertFalse(owner_priority.should_defer("s1", max_deferrals=2))
        # s2's budget is untouched by s1 being exhausted.
        self.assertTrue(owner_priority.should_defer("s2", max_deferrals=2))

    def test_falsy_session_id_never_defers(self) -> None:
        self.assertFalse(owner_priority.should_defer(None))
        self.assertFalse(owner_priority.should_defer(""))


if __name__ == "__main__":
    unittest.main()
