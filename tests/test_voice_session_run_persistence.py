"""Bare-session persistence (drumbeat-owned part): a bare-``session_id`` turn must leave a
real run record AND a transcript artifact, retrievable through the SAME
management API a chat automation's run is read from.

SCOPE -- READ THIS BEFORE TRUSTING THE WORD "voice" BELOW. The bare-``session_id``
turn this pins is the notification / Conversation *reply* path (a typed reply
routed into an existing session), NOT a realtime voice CALL. A realtime voice
call never reaches drumbeat at all: the upstream voice gateway mints the
realtime session and the device streams straight to the provider, so
``runner.resume_turn`` is never invoked for it (measured on this box:
drumbeat's live ``runs/`` holds zero voice-session records). A voice call's
turn-by-turn record is persisted by that upstream gateway, not by drumbeat.
The fixtures below
name a ``voice-...`` session and use spoken-style reply text ONLY to exercise
the bare-session path -- they are not evidence that voice calls route here.

THE DEFECT THIS PINS. ``POST /api/turns`` (``turns.py``) branches on session
type:

* a chat/automation turn (``automation_slug`` given) runs through
  ``runner.run_chat_message`` -> ``runner._persist_run``, which writes
  ``runs/<slug>/<run_id>/result.json`` + ``step-NN.txt`` transcripts + a
  ``RUN_COMPLETED`` event -- all readable via ``management_api.list_runs`` /
  ``get_run_detail`` / ``get_run_stderr``.
* a bare-``session_id`` reply turn runs through ``runner.resume_turn``, which
  executed the turn and returned the reply but historically NEVER called
  ``_persist_run``. Its only trace was a ``turns/<turn_id>.json`` record the
  runs API does not read.

So a typed reply left no retrievable run/transcript, while chat automations
were saved. This is the genuinely drumbeat-owned slice; the
realtime-voice slice is the gateway's (above).

Both turns below are driven through the SAME faked agent seam
(``runner._execute_turn``), so the ONLY difference under test is the entry
point -- chat vs bare-session reply -- and therefore the persistence wiring.
Deleting the persistence wiring added to ``runner.resume_turn`` makes
``test_voice_session_turn_is_retrievable_like_a_chat_run`` go red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drumbeat import automation, management_api, runner
from drumbeat.management_api import EngineContext

CHAT_AUTOMATION = """---
automation:
  name: Chat
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: reply
      prompt: Reply to the message.
---
"""

VOICE_SESSION_ID = "voice-sess-7f3a2b9c"
VOICE_REPLY = "Assistant here -- I heard you on the call and this is my spoken reply."
CHAT_REPLY = "Assistant here -- this is my chat reply."


@pytest.fixture
def ctx(tmp_path: Path) -> EngineContext:
    (tmp_path / "automations").mkdir()
    (tmp_path / "automations" / "chat.md").write_text(CHAT_AUTOMATION, encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "runs").mkdir()
    return EngineContext(
        automations_dir=tmp_path / "automations",
        prompts_dir=tmp_path / "prompts",
        runs_dir=tmp_path / "runs",
        cwd=tmp_path,
    )


def _fake_execute_turn(reply: str):
    """Stand in for the amplifier-agent subprocess seam.

    ``runner.resume_turn`` and ``runner.run_chat_message`` both funnel every
    turn through ``runner._execute_turn`` -- faking it here exercises the real
    persistence code of both entry points without spawning an agent. The
    return contract ``(StepResult, stderr_text)`` is read from
    ``runner._execute_turn`` itself, not guessed.
    """

    def _stub(*, index: int, text: str, **_kwargs) -> tuple[runner.StepResult, str]:
        return (
            runner.StepResult(
                index=index,
                text=text,
                reply=reply,
                error=None,
                duration_ms=1234,
                tokens_in=10,
                tokens_out=20,
            ),
            f"[agent] turn {index} ok\n",
        )

    return _stub


def _run_ids_for_session(ctx: EngineContext, session_id: str) -> list[dict]:
    runs = management_api.list_runs(limit=50, automation_filter=None, ctx=ctx)
    return [r for r in runs if r.get("session_id") == session_id]


def test_chat_turn_is_retrievable(ctx: EngineContext, monkeypatch: pytest.MonkeyPatch):
    """Baseline: a chat automation run IS retrievable via the runs API.

    This is the behaviour the voice path must match. If this ever breaks, the
    parity claim below is meaningless -- so it is asserted independently.
    """
    monkeypatch.setattr(runner, "_execute_turn", _fake_execute_turn(CHAT_REPLY))
    chat = automation.load_by_slug("chat", ctx.automations_dir)

    result = runner.run_chat_message(
        chat, "what needs my attention?", cwd=ctx.cwd, runs_dir=ctx.runs_dir
    )
    assert not result.failed

    listed = _run_ids_for_session(ctx, result.session_id)
    assert len(listed) == 1, "the chat run must appear in list_runs exactly once"
    detail = management_api.get_run_detail(
        listed[0]["automation"], listed[0]["run_id"], ctx
    )
    assert any(CHAT_REPLY in (s.get("reply") or "") for s in detail["steps"])


def test_voice_session_turn_is_retrievable_like_a_chat_run(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """A bare-session (reply) turn must leave a run + transcript readable
    through the SAME API a chat run is -- ``list_runs`` then ``get_run_detail``.

    Pre-fix this is red: ``resume_turn`` executed the turn and returned the
    reply but persisted no run record, so ``list_runs`` never surfaces the
    reply session and ``get_run_detail`` has nothing to read. (This is the
    reply path, not a realtime voice call -- see the module docstring.)
    """
    monkeypatch.setattr(runner, "_execute_turn", _fake_execute_turn(VOICE_REPLY))

    step = runner.resume_turn(
        VOICE_SESSION_ID,
        "hey there, what did we decide?",
        cwd=ctx.cwd,
        runs_dir=ctx.runs_dir,
    )
    assert step.error is None
    assert step.reply == VOICE_REPLY

    # 1) Discoverable through the same listing endpoint a chat run is.
    listed = _run_ids_for_session(ctx, VOICE_SESSION_ID)
    assert len(listed) == 1, (
        "the bare-session reply turn left no run record retrievable via "
        "list_runs -- the drumbeat-owned slice (a typed reply "
        "left no transcript while chat automations were saved)"
    )

    entry = listed[0]
    # Same client-facing run-record contract every chat run carries.
    for field in ("run_id", "automation", "automation_name", "failed", "notified"):
        assert field in entry, f"voice run is missing contract field {field!r}"
    assert entry["failed"] is False

    # 2) The transcript is retrievable through the same detail endpoint.
    detail = management_api.get_run_detail(entry["automation"], entry["run_id"], ctx)
    assert detail["session_id"] == VOICE_SESSION_ID
    assert any(VOICE_REPLY in (s.get("reply") or "") for s in detail["steps"]), (
        "the voice turn's spoken reply was not persisted as a retrievable "
        "transcript artifact"
    )

    # 3) The stderr artifact is served by the same endpoint too.
    stderr = management_api.get_run_stderr(entry["automation"], entry["run_id"], ctx)
    assert stderr["run_id"] == entry["run_id"]


def test_voice_and_chat_runs_share_one_listing(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """The proof-bar parity, stated directly: after both a chat run and a
    voice-session turn, a single ``list_runs`` call returns BOTH, in the same
    shape -- the voice run is a first-class citizen of the runs history, not a
    second-class turn record living somewhere the API cannot see.
    """
    monkeypatch.setattr(runner, "_execute_turn", _fake_execute_turn(CHAT_REPLY))
    chat = automation.load_by_slug("chat", ctx.automations_dir)
    chat_result = runner.run_chat_message(
        chat, "morning sweep", cwd=ctx.cwd, runs_dir=ctx.runs_dir
    )

    monkeypatch.setattr(runner, "_execute_turn", _fake_execute_turn(VOICE_REPLY))
    runner.resume_turn(
        VOICE_SESSION_ID, "and the voice recap?", cwd=ctx.cwd, runs_dir=ctx.runs_dir
    )

    runs = management_api.list_runs(limit=50, automation_filter=None, ctx=ctx)
    session_ids = {r.get("session_id") for r in runs}
    assert chat_result.session_id in session_ids
    assert VOICE_SESSION_ID in session_ids

    keys_per_run = {
        frozenset(("run_id", "automation", "automation_name", "failed", "notified"))
        <= set(r)
        for r in runs
    }
    assert keys_per_run == {True}, "every run in the listing carries the same contract"
