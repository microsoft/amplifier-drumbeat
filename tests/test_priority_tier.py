"""Tests for the `priority:` dispatch tier (contracts/automation-file.v1.md, 2026-09-02).

THE EVIDENCE. The consumer team's 24h measurement plus an 8-day window: the
fleet demands ~412 runs/day and completes ~127 (31%); 30-minute automations
attain 52-72% of their declared cadence, the 20-minute one 46%. A schedule
expression had become *a bid in an auction nobody clears, and the auction had
no priority* -- so the single notify-capable path to the owner starved on
exactly equal terms with bulk background checks.

WHAT IS AND IS NOT UNDER TEST. `priority:` orders DUE automations at dispatch.
That is all it does, and these tests are written to hold it to exactly that: no
test here asserts that more runs complete, because the key does not make more
runs complete. It changes WHO WAITS. The capacity fix is a separate
architecture decision.

Harness note: ``scheduler.serve`` is deliberately an infinite loop, so it is
bounded here the same way ``test_scheduler.py`` bounds it -- monkeypatching the
real ``time.sleep`` to raise a sentinel after N ticks, and seeding ``next_due``
directly so each test controls exactly which tick an automation is due on. The
REAL serve loop runs; only ``run`` (the turn executor) is stubbed, so what these
tests observe is genuine dispatch order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drumbeat import automation as automation_mod
from drumbeat import owner_priority, schedule_state, scheduler, session_pins
from drumbeat.automation import (
    DEFAULT_PRIORITY,
    VALID_PRIORITY_VALUES,
    AutomationError,
    load,
    load_all_tolerant,
)

_AUTOMATION_MD = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: schedule
    expression: every 60 minutes
  notify: never
{priority_line}  steps:
    - id: say-something
      prompt: Say something.
---

A test automation.
"""


def _write(dir_: Path, slug: str, name: str, priority: str | None) -> Path:
    line = "" if priority is None else f"  priority: {priority}\n"
    path = dir_ / f"{slug}.md"
    path.write_text(
        _AUTOMATION_MD.format(name=name, priority_line=line), encoding="utf-8"
    )
    return path


class _StopLoop(Exception):
    """Breaks scheduler.serve()'s ``while True`` loop from inside a test."""


@pytest.fixture(autouse=True)
def _reset_owner_priority():
    owner_priority._reset_for_tests()
    yield
    owner_priority._reset_for_tests()


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    automations_dir = tmp_path / "automations"
    prompts_dir = tmp_path / "prompts"
    runs_dir = tmp_path / "runs"
    for d in (automations_dir, prompts_dir, runs_dir):
        d.mkdir()
    return {
        "cwd": tmp_path,
        "automations_dir": automations_dir,
        "prompts_dir": prompts_dir,
        "runs_dir": runs_dir,
    }


def _dispatch_order_over_one_tick(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Drive the REAL serve loop for one tick; return the slugs it dispatched, in order."""
    automations, _ = load_all_tolerant(dirs["automations_dir"])
    schedule_state.save(dirs["runs_dir"], {a.slug: 1.0 for a in automations})

    dispatched: list[str] = []

    def _run_stub(automation, **_kwargs):
        dispatched.append(automation.slug)

        class _Result:
            run_id = "r"
            failed = False
            notified = False

        return _Result()

    calls = {"sleep": 0}

    def _fake_sleep(_seconds: float) -> None:
        calls["sleep"] += 1
        raise _StopLoop()

    monkeypatch.setattr(scheduler.time, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler, "run", _run_stub)

    lock_handle = scheduler.acquire_scheduler_lock(dirs["runs_dir"])
    try:
        with pytest.raises(_StopLoop):
            scheduler.serve(
                dirs["automations_dir"],
                dirs["cwd"],
                dirs["runs_dir"],
                dirs["prompts_dir"],
                state=scheduler.SchedulerState(),
                lock_handle=lock_handle,
                write_fingerprint=False,
            )
    finally:
        lock_handle.close()
    return dispatched


# ---- (a) the contract vocabulary, at the parser -------------------------


class TestPriorityVocabulary:
    def test_absent_key_parses_as_normal(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "quiet-check", "Quiet Check", None)
        assert load(path).priority == DEFAULT_PRIORITY == "normal"

    @pytest.mark.parametrize("value", sorted(VALID_PRIORITY_VALUES))
    def test_every_declared_value_parses(self, tmp_path: Path, value: str) -> None:
        path = _write(tmp_path, "check", "Check", value)
        assert load(path).priority == value

    def test_the_vocabulary_is_exactly_two_values(self) -> None:
        """Pinned deliberately: widening this set is a contract amendment, not
        an implementation detail."""
        assert VALID_PRIORITY_VALUES == {"high", "normal"}
        assert set(automation_mod.PRIORITY_RANK) == VALID_PRIORITY_VALUES

    def test_unknown_value_refuses_loudly_naming_the_vocabulary(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, "check", "Check", "urgent")
        with pytest.raises(AutomationError) as exc:
            load(path)
        message = str(exc.value)
        assert "automation.priority must be one of" in message
        assert "'high'" in message and "'normal'" in message
        assert "'urgent'" in message  # names what it actually found

    def test_a_truthy_looking_non_value_is_still_refused(self, tmp_path: Path) -> None:
        """`priority: true` is the shape an author reaches for when guessing.
        Silently accepting it would make the key read as meaningful while doing
        nothing -- the exact fail-quiet shape this parser exists to prevent."""
        path = _write(tmp_path, "check", "Check", "true")
        with pytest.raises(AutomationError, match="automation.priority must be one of"):
            load(path)

    def test_priority_is_a_registered_top_level_key(self) -> None:
        assert "priority" in automation_mod.KNOWN_AUTOMATION_KEYS


# ---- (b) dispatch ordering ----------------------------------------------


class TestHighIsDispatchedFirst:
    def test_high_runs_before_normal_when_both_are_due(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Filenames chosen so that ANY name-ordered load puts the high-priority
        # automation LAST -- if the tier did nothing, "z-rollup" would run last.
        _write(dirs["automations_dir"], "a-bulk-check", "A Bulk Check", None)
        _write(dirs["automations_dir"], "m-bulk-check", "M Bulk Check", "normal")
        _write(dirs["automations_dir"], "z-rollup", "Z Rollup", "high")

        order = _dispatch_order_over_one_tick(dirs, monkeypatch)

        assert order[0] == "z-rollup", f"high tier did not go first: {order}"
        assert set(order) == {"a-bulk-check", "m-bulk-check", "z-rollup"}
        # ...and nothing was dropped: ordering is not rationing.
        assert len(order) == 3

    def test_within_a_tier_the_existing_order_is_untouched(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for slug in ("a-check", "b-check", "c-check"):
            _write(dirs["automations_dir"], slug, slug.upper(), "normal")
        _write(dirs["automations_dir"], "d-rollup", "D Rollup", "high")

        order = _dispatch_order_over_one_tick(dirs, monkeypatch)

        assert order[0] == "d-rollup"
        assert order[1:] == ["a-check", "b-check", "c-check"]

    def test_two_high_automations_keep_their_relative_order(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(dirs["automations_dir"], "a-rollup", "A Rollup", "high")
        _write(dirs["automations_dir"], "b-bulk", "B Bulk", None)
        _write(dirs["automations_dir"], "c-rollup", "C Rollup", "high")

        order = _dispatch_order_over_one_tick(dirs, monkeypatch)

        assert order == ["a-rollup", "c-rollup", "b-bulk"]


class TestAbsentKeyIsByteIdenticalPriorBehavior:
    """The regression pin. A fleet that never names `priority:` must dispatch
    in exactly the order it did before the key existed -- otherwise this
    "ordering only" change silently reordered everyone's automations."""

    def test_dispatch_order_helper_is_an_identity_when_nothing_declares_a_tier(
        self, dirs: dict[str, Path]
    ) -> None:
        for slug in ("z-last", "a-first", "m-middle"):
            _write(dirs["automations_dir"], slug, slug, None)
        automations, _ = load_all_tolerant(dirs["automations_dir"])

        ordered = scheduler._dispatch_order(list(automations))

        # Same objects, same order -- not merely an equal-looking list.
        assert [id(a) for a in ordered] == [id(a) for a in automations]

    def test_end_to_end_order_matches_the_unsorted_load_order(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for slug in ("z-last", "a-first", "m-middle"):
            _write(dirs["automations_dir"], slug, slug, None)
        automations, _ = load_all_tolerant(dirs["automations_dir"])
        expected = [a.slug for a in automations]

        assert _dispatch_order_over_one_tick(dirs, monkeypatch) == expected

    def test_an_all_normal_fleet_is_also_an_identity(
        self, dirs: dict[str, Path]
    ) -> None:
        """Explicit `priority: normal` must behave exactly like the absent key."""
        for slug in ("z-last", "a-first", "m-middle"):
            _write(dirs["automations_dir"], slug, slug, "normal")
        automations, _ = load_all_tolerant(dirs["automations_dir"])

        ordered = scheduler._dispatch_order(list(automations))
        assert [id(a) for a in ordered] == [id(a) for a in automations]


# ---- (c) the owner latch still outranks every tier ----------------------


class TestOwnerPrecedenceIsUnchanged:
    def test_a_high_automation_is_still_deferred_to_the_owner(
        self, dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`high` orders scheduled work BENEATH the owner, never alongside.
        A tier that could jump the owner latch would have quietly converted a
        scheduling hint into a priority inversion against the human."""
        _write(dirs["automations_dir"], "a-rollup", "A Rollup", "high")
        _write(dirs["automations_dir"], "b-bulk", "B Bulk", None)

        contended = "a-rollup-session"
        session_pins.upsert(
            "a-rollup",
            session_id=contended,
            session_workspace=None,
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=dirs["runs_dir"],
        )
        owner_priority.mark_waiting(contended, window_seconds=3600.0)

        order = _dispatch_order_over_one_tick(dirs, monkeypatch)

        # The high automation was considered FIRST and deferred anyway; the
        # normal one still ran. Deferral leaves it due, never dropped.
        assert order == ["b-bulk"]
        assert schedule_state.load(dirs["runs_dir"])["a-rollup"] == 1.0
