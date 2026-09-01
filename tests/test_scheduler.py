"""Tests for the scheduler's owner-priority deferral.

The incident: POST /api/turns and the scheduler arbitrate a session's
flock with no ordering at all -- whoever calls flock() first wins. When a
session is shared between the owner's own conversation and a
schedule-triggered automation, back-to-back automation runs can keep
winning that race purely on timing. These tests prove the scheduler's half
of the fix: a due automation whose pinned session has a live
owner-priority signal (see drumbeat.owner_priority) is DEFERRED -- not run,
not dropped, retried the very next tick -- and only up to a bounded number
of consecutive ticks before it proceeds regardless.

``scheduler.serve`` is an intentionally infinite loop (see its own
docstring: "the whole point of a scheduler is that it keeps running"), so
these tests bound it the same way test_turns.py bounds other real
mechanisms: by monkeypatching a real dependency (``time.sleep``) to raise
a private sentinel after N ticks, rather than inventing new scheduler
infrastructure. ``next_due`` is seeded directly via ``schedule_state`` so
each test controls exactly which tick an automation becomes "due" on,
without needing to also control wall-clock time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drumbeat import owner_priority, schedule_state, scheduler, session_pins

AUTOMATION_MD = """---
automation:
  name: Daily Roundup
  enabled: true
  trigger:
    type: schedule
    expression: every 60 minutes
  notify: never
  steps:
    - id: say-something
      prompt: Say something.
---

A test automation.
"""

SLUG = "daily-roundup"


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
    automations_dir.mkdir()
    prompts_dir.mkdir()
    runs_dir.mkdir()
    (automations_dir / f"{SLUG}.md").write_text(AUTOMATION_MD, encoding="utf-8")
    return {
        "cwd": tmp_path,
        "automations_dir": automations_dir,
        "prompts_dir": prompts_dir,
        "runs_dir": runs_dir,
    }


def _seed_due_now(runs_dir: Path) -> None:
    """Make SLUG immediately due on tick 1 -- a timestamp far in the past."""
    schedule_state.save(runs_dir, {SLUG: 1.0})


def _pin_session(runs_dir: Path, session_id: str) -> None:
    session_pins.upsert(
        SLUG,
        session_id=session_id,
        session_workspace=None,
        created_by=session_pins.CREATED_BY_RUN,
        runs_dir=runs_dir,
    )


def _serve_n_ticks(dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch, *, ticks: int, run_stub):
    """Run scheduler.serve() for exactly ``ticks`` passes through the loop body."""
    calls = {"sleep": 0}

    def _fake_sleep(_seconds: float) -> None:
        calls["sleep"] += 1
        if calls["sleep"] >= ticks:
            raise _StopLoop()

    monkeypatch.setattr(scheduler.time, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler, "run", run_stub)

    deferred_events: list[dict] = []
    real_emit = scheduler.ci_events.emit

    def _capture_emit(name, payload, *, cwd):
        if name == "drumbeat:owner_priority_deferred":
            deferred_events.append(payload)
        return real_emit(name, payload, cwd=cwd)

    monkeypatch.setattr(scheduler.ci_events, "emit", _capture_emit)

    lock_handle = scheduler.acquire_scheduler_lock(dirs["runs_dir"])
    state = scheduler.SchedulerState()
    try:
        with pytest.raises(_StopLoop):
            scheduler.serve(
                dirs["automations_dir"],
                dirs["cwd"],
                dirs["runs_dir"],
                dirs["prompts_dir"],
                state=state,
                lock_handle=lock_handle,
                write_fingerprint=False,
            )
    finally:
        lock_handle.close()
    return state, deferred_events


def test_due_automation_runs_when_no_owner_priority_marked(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """Baseline: with nothing marked, a due automation runs exactly as before."""
    _seed_due_now(dirs["runs_dir"])
    _pin_session(dirs["runs_dir"], "shared-session")
    run_calls = []

    def _run_stub(automation, **kwargs):
        run_calls.append(automation.slug)
        return _fake_run_result(automation)

    _serve_n_ticks(dirs, monkeypatch, ticks=1, run_stub=_run_stub)
    assert run_calls == [SLUG]


def test_due_automation_deferred_when_owner_priority_active(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """The core fix: a live owner-priority signal on the automation's OWN
    pinned session defers the start -- run() is never called, and next_due
    is left untouched (still due) rather than pushed a full interval out.
    """
    _seed_due_now(dirs["runs_dir"])
    _pin_session(dirs["runs_dir"], "shared-session")
    owner_priority.mark_waiting("shared-session")
    run_calls = []

    def _run_stub(automation, **kwargs):
        run_calls.append(automation.slug)
        return _fake_run_result(automation)

    state, deferred_events = _serve_n_ticks(dirs, monkeypatch, ticks=1, run_stub=_run_stub)

    assert run_calls == [], "run() must not be called while the owner is waiting"
    assert deferred_events == [{"automation": "Daily Roundup", "session_id": "shared-session"}]
    # next_due was left untouched -- it is still exactly the seeded past
    # timestamp (1970-01-01T00:00:01Z), never rescheduled to a future
    # interval the way a real run's completion would.
    assert state.registered[SLUG] == "1970-01-01T00:00:01Z"


def test_deferral_is_bounded_then_proceeds(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """The hard 'no starvation' invariant: even with the owner-priority
    window kept alive throughout (nothing here ever lets it lapse), the
    automation is allowed to run once should_defer's ceiling is reached --
    never held back forever.
    """
    _seed_due_now(dirs["runs_dir"])
    _pin_session(dirs["runs_dir"], "shared-session")
    owner_priority.mark_waiting("shared-session", window_seconds=3600.0)
    run_calls = []

    def _run_stub(automation, **kwargs):
        run_calls.append(automation.slug)
        return _fake_run_result(automation)

    ticks = owner_priority.DEFAULT_MAX_DEFERRALS + 2
    state, deferred_events = _serve_n_ticks(dirs, monkeypatch, ticks=ticks, run_stub=_run_stub)

    # Deferred exactly DEFAULT_MAX_DEFERRALS times, then ran exactly once --
    # never zero (starved) and never more than once (double-run).
    assert len(deferred_events) == owner_priority.DEFAULT_MAX_DEFERRALS
    assert run_calls == [SLUG]


def test_automation_with_no_pin_is_never_deferred(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """A never-run automation has no pinned session to be contended over --
    mirrors turns.submit_turn's own 'a brand-new session cannot be
    contended by anything'. Marking some OTHER (unrelated) session as
    owner-waiting must not defer this automation.
    """
    _seed_due_now(dirs["runs_dir"])
    # Deliberately no _pin_session call -- SLUG has never been run.
    owner_priority.mark_waiting("some-unrelated-session")
    run_calls = []

    def _run_stub(automation, **kwargs):
        run_calls.append(automation.slug)
        return _fake_run_result(automation)

    _serve_n_ticks(dirs, monkeypatch, ticks=1, run_stub=_run_stub)
    assert run_calls == [SLUG]


def test_owner_priority_on_a_different_session_does_not_defer(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """Only a match on THIS automation's own pinned session defers it --
    an owner turn contending a different conversation is irrelevant.
    """
    _seed_due_now(dirs["runs_dir"])
    _pin_session(dirs["runs_dir"], "shared-session")
    owner_priority.mark_waiting("some-other-conversation-entirely")
    run_calls = []

    def _run_stub(automation, **kwargs):
        run_calls.append(automation.slug)
        return _fake_run_result(automation)

    _serve_n_ticks(dirs, monkeypatch, ticks=1, run_stub=_run_stub)
    assert run_calls == [SLUG]


def _fake_run_result(automation):
    from drumbeat.runner import RunResult

    return RunResult(
        automation=automation.name,
        run_id="run-1",
        session_id="shared-session",
        started_at="2026-08-31T00:00:00Z",
        finished_at="2026-08-31T00:00:01Z",
        steps=[],
        final_reply="ok",
        notified=False,
        failed=False,
        error=None,
        session_resumed=True,
    )
