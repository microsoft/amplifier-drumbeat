"""Bare-session reply persistence (drumbeat-owned slice): the documented carve-out on the
bare-`session_id` **reply** persistence path.

Context / boundary: a realtime voice CALL is
never persisted by drumbeat -- the gateway owns that. What drumbeat legitimately
owns is persisting a bare-`session_id` *reply* turn (`POST /api/turns` with a
`session_id` -> `runner.resume_turn` -> `runner._persist_session_turn`) so a
typed reply is retrievable via the same runs API as a chat automation. That
retrievability is pinned in `test_voice_session_run_persistence.py`.

This file pins the ONE branch that path deliberately does NOT persist, and which
had no test: a `session_locked` outcome. When the per-session lock is never
acquired, no subprocess ran and there is genuinely no transcript to save -- the
correct outcome is backpressure ("resend your text"), NOT a run record and NOT a
failed run. Persisting an empty run there would manufacture a transcript for a
turn that never executed, which is exactly the kind of false record this path
is about. If someone deletes the `ERROR_KIND_SESSION_LOCKED` early-return in
`_persist_session_turn`, this test goes red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drumbeat import management_api, runner
from drumbeat.management_api import EngineContext

REPLY_SESSION_ID = "voice-sess-7f3a2b9c"  # a bare session id; NOT a live call


@pytest.fixture
def ctx(tmp_path: Path) -> EngineContext:
    (tmp_path / "automations").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "runs").mkdir()
    return EngineContext(
        automations_dir=tmp_path / "automations",
        prompts_dir=tmp_path / "prompts",
        runs_dir=tmp_path / "runs",
        cwd=tmp_path,
    )


def _locked_execute_turn(*, index: int, text: str, **_kwargs):
    """Stand in for the amplifier-agent subprocess seam, returning the exact
    shape `runner._execute_turn` returns for a lock that was never acquired:
    a `StepResult` whose `error_kind` is `ERROR_KIND_SESSION_LOCKED` and whose
    reply is empty because nothing ran. Return contract `(StepResult, stderr)`
    is read from `runner._execute_turn`, not guessed.
    """
    return (
        runner.StepResult(
            index=index,
            text=text,
            reply="",
            error="session is locked by another in-flight turn -- not resumed",
            duration_ms=0,
            tokens_in=0,
            tokens_out=0,
            error_kind=runner.ERROR_KIND_SESSION_LOCKED,
        ),
        "",
    )


def test_session_locked_reply_persists_no_run(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """A `session_locked` bare-session turn leaves NO run record.

    The lock was never acquired, so no subprocess ran and there is nothing to
    persist. `resume_turn` still returns the real busy signal, but `list_runs`
    must surface no run for the session and no session-slug run dir may be
    created -- backpressure, not a fabricated transcript.
    """
    monkeypatch.setattr(runner, "_execute_turn", _locked_execute_turn)

    step = runner.resume_turn(
        REPLY_SESSION_ID,
        "hey there, are you there?",
        cwd=ctx.cwd,
        runs_dir=ctx.runs_dir,
    )

    # 1) The real busy signal is returned, not fabricated success.
    assert step.error is not None
    assert step.error_kind == runner.ERROR_KIND_SESSION_LOCKED

    # 2) Nothing was persisted for this session.
    runs = management_api.list_runs(limit=50, automation_filter=None, ctx=ctx)
    assert [r for r in runs if r.get("session_id") == REPLY_SESSION_ID] == [], (
        "a session-locked turn must not leave a run record -- no subprocess ran, "
        "so there is no transcript to persist (documented carve-out in "
        "_persist_session_turn)"
    )

    # 3) No session-slug run directory was materialized under runs/.
    session_run_dirs = [
        p
        for p in ctx.runs_dir.iterdir()
        if p.is_dir() and REPLY_SESSION_ID.replace(":", "-") in p.name
    ]
    assert session_run_dirs == [], (
        f"a session-locked turn created run dir(s) {session_run_dirs} -- it must "
        "persist nothing at all"
    )
