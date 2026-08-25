"""Step 5: the turn API -- the reply cutover's engine half.

Each test corresponds to a claim ``docs/ARCHITECTURE.md`` section 9 (reply
routing) makes about ``POST /api/turns``, and is written so that removing
the mechanism makes it fail (red-provable). The load-bearing ones:

- ``test_unknown_session_is_404`` and ``test_unknown_automation_slug_is_404``
  -- "never guesses, never falls back". Delete the probe and these go red.
- ``test_locked_session_without_wait_budget_is_423`` -- the honest busy
  signal, and its companion ``test_423_creates_no_turn_record``: the whole
  reason a 423 cannot lose the user's typed text is that the engine never
  took it.
- ``test_locked_session_with_wait_budget_is_accepted`` -- the other half of
  the same decision. A consumer's reply path passes a wait budget, so the
  behaviour a client sees on a reply landing mid-run is UNCHANGED by the
  split. If this goes red, the split shipped a behaviour delta.
- ``test_keyless_mutating_request_is_refused_from_loopback`` -- section 3,
  restated for the new endpoint specifically, because "a path allowlist is
  a thing you forget to update when you add an endpoint".
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from drumbeat.management_api import EngineContext
from drumbeat.scheduler import SchedulerState

from drumbeat import api_key, engine_api, runner, turns

KEY = "test-key-not-a-real-secret"

AUTOMATION = """---
name: Drill
slug: drill
enabled: false
trigger: manual
notify: never
---

1. Say something.
"""


@pytest.fixture
def ctx(tmp_path: Path) -> EngineContext:
    (tmp_path / "automations").mkdir()
    (tmp_path / "automations" / "drill.md").write_text(AUTOMATION, encoding="utf-8")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "runs").mkdir()
    return EngineContext(
        automations_dir=tmp_path / "automations",
        prompts_dir=tmp_path / "prompts",
        runs_dir=tmp_path / "runs",
        cwd=tmp_path,
    )


def _make_session(ctx: EngineContext, session_id: str) -> None:
    """Create the on-disk shape ``runner.probe_session`` calls EXISTS."""
    from drumbeat.paths import amplifier_agent_home, derive_workspace_slug

    session_dir = (
        amplifier_agent_home()
        / "state"
        / "workspaces"
        / derive_workspace_slug(ctx.cwd)
        / "sessions"
        / session_id
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "transcript.jsonl").write_text("", encoding="utf-8")


# ------------------------------------------------------- request shape ----


def test_text_is_required():
    with pytest.raises(turns.TurnError) as exc:
        turns.parse_request({"origin": "reply", "session_id": "s"})
    assert exc.value.status == 400
    assert "text" in exc.value.message


def test_origin_is_required_with_no_default():
    """Section 5's design rule: every required field is required-with-no-default."""
    with pytest.raises(turns.TurnError) as exc:
        turns.parse_request({"text": "hi", "session_id": "s"})
    assert exc.value.status == 400
    assert "origin" in exc.value.message


def test_naming_neither_target_is_refused():
    with pytest.raises(turns.TurnError) as exc:
        turns.parse_request({"text": "hi", "origin": "reply"})
    assert exc.value.status == 400
    assert "exactly one" in exc.value.message


def test_naming_both_targets_is_refused():
    """Ambiguity is refused rather than resolved by precedence."""
    with pytest.raises(turns.TurnError) as exc:
        turns.parse_request(
            {
                "text": "hi",
                "origin": "reply",
                "session_id": "s",
                "automation_slug": "drill",
            }
        )
    assert exc.value.status == 400
    assert "exactly one" in exc.value.message


def test_caller_states_its_own_wait_and_ceiling():
    request = turns.parse_request(
        {
            "text": "hi",
            "origin": "reply",
            "session_id": "s",
            "lock_wait_seconds": 900,
            "ceiling_seconds": 1200,
        }
    )
    assert request.lock_wait_seconds == 900.0
    assert request.ceiling_seconds == 1200.0


def test_omitted_wait_budget_means_do_not_wait():
    """The default is the honest 423, not a silent queue."""
    request = turns.parse_request({"text": "hi", "origin": "reply", "session_id": "s"})
    assert request.lock_wait_seconds == 0.0


# ------------------------------------------------------------- refusals ----


def test_unknown_session_is_404(ctx: EngineContext):
    with pytest.raises(turns.TurnError) as exc:
        turns.submit_turn(
            {"text": "hi", "origin": "reply", "session_id": "no-such-session"},
            ctx,
        )
    assert exc.value.status == 404
    assert "refusing to guess a session" in exc.value.message


def test_unknown_automation_slug_is_404(ctx: EngineContext):
    with pytest.raises(turns.TurnError) as exc:
        turns.submit_turn(
            {"text": "hi", "origin": "chat", "automation_slug": "no-such-automation"},
            ctx,
        )
    assert exc.value.status == 404
    assert "refusing to guess a session" in exc.value.message


def test_unknown_turn_id_is_404(ctx: EngineContext):
    with pytest.raises(turns.TurnError) as exc:
        turns.get_turn("t-nope", ctx)
    assert exc.value.status == 404
    assert "unknown turn_id" in exc.value.message


# ------------------------------------------------------------ the lock ----


def _hold_session_lock(ctx: EngineContext, session_id: str):
    """Hold the session flock the way a real in-flight turn holds it."""
    lock_dir = ctx.runs_dir / ".session-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / f"{session_id}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_session_lock_probe_reports_held_and_free(ctx: EngineContext):
    fd = _hold_session_lock(ctx, "busy-session")
    try:
        assert runner.session_lock_is_held("busy-session", runs_dir=ctx.runs_dir)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert not runner.session_lock_is_held("busy-session", runs_dir=ctx.runs_dir)


def test_probe_does_not_itself_hold_the_lock(ctx: EngineContext):
    """The probe must not become the contention it reports on."""
    (ctx.runs_dir / ".session-locks").mkdir(parents=True, exist_ok=True)
    (ctx.runs_dir / ".session-locks" / "free-session.lock").touch()
    assert not runner.session_lock_is_held("free-session", runs_dir=ctx.runs_dir)
    # Immediately checkable again -- if the first probe kept the lock, this
    # would report held.
    assert not runner.session_lock_is_held("free-session", runs_dir=ctx.runs_dir)


def test_locked_session_without_wait_budget_is_423(ctx: EngineContext):
    _make_session(ctx, "locked-session")
    fd = _hold_session_lock(ctx, "locked-session")
    try:
        with pytest.raises(turns.TurnError) as exc:
            turns.submit_turn(
                {
                    "text": "hi",
                    "origin": "reply",
                    "session_id": "locked-session",
                    "lock_wait_seconds": 0,
                },
                ctx,
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert exc.value.status == 423
    assert "locked by another in-flight turn" in exc.value.message
    assert exc.value.extra["retry_after_seconds"] == turns.RETRY_AFTER_SECONDS


def test_423_creates_no_turn_record(ctx: EngineContext):
    """Section 7.3's flag, server half: a 423 never took the text.

    Nothing half-accepted exists to poll or to clean up, which is exactly
    what makes it safe for a client to keep the draft and re-offer send.
    """
    _make_session(ctx, "locked-session")
    fd = _hold_session_lock(ctx, "locked-session")
    try:
        with pytest.raises(turns.TurnError):
            turns.submit_turn(
                {
                    "text": "the user's typed text",
                    "origin": "reply",
                    "session_id": "locked-session",
                    "lock_wait_seconds": 0,
                },
                ctx,
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert list(turns.turns_dir(ctx.runs_dir).glob("*.json")) == []


def test_locked_session_with_wait_budget_is_accepted(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """A caller that asked to wait is NOT 423'd -- today's reply behaviour.

    A consumer's reply path passes ``runner.background_lock_wait_seconds()``
    (15 minutes), exactly as an in-process caller passed it to
    ``resume_turn`` before the split. If this goes red, the migration
    shipped an unnamed behaviour delta on the most-used surface.
    """
    _make_session(ctx, "locked-session")
    started = threading.Event()

    def _fake_resume_turn(*args, **kwargs):
        started.set()
        return runner.StepResult(
            index=0,
            text="",
            reply="ok",
            error=None,
            duration_ms=1,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _fake_resume_turn)

    fd = _hold_session_lock(ctx, "locked-session")
    try:
        accepted = turns.submit_turn(
            {
                "text": "hi",
                "origin": "reply",
                "session_id": "locked-session",
                "lock_wait_seconds": 900,
            },
            ctx,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert accepted["status"] == turns.STATUS_RUNNING
    assert accepted["session_id"] == "locked-session"
    assert started.wait(timeout=5), "the accepted turn never started executing"


# ------------------------------------------------------ record lifecycle ----


def test_record_exists_before_submit_returns(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """A caller polling immediately never 404s on an id we just minted."""
    _make_session(ctx, "s1")
    release = threading.Event()

    def _slow_resume_turn(*args, **kwargs):
        release.wait(timeout=10)
        return runner.StepResult(
            index=0,
            text="",
            reply="ok",
            error=None,
            duration_ms=1,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _slow_resume_turn)
    accepted = turns.submit_turn(
        {"text": "hi", "origin": "reply", "session_id": "s1"}, ctx
    )
    record = turns.get_turn(accepted["turn_id"], ctx)
    assert record["status"] == turns.STATUS_RUNNING
    assert record["text"] == "hi"
    assert record["origin"] == "reply"
    release.set()


def test_successful_turn_reaches_done_with_the_reply(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    _make_session(ctx, "s1")

    def _resume(*args, **kwargs):
        return runner.StepResult(
            index=0,
            text="",
            reply="the agent's answer",
            error=None,
            duration_ms=1,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _resume)
    accepted = turns.submit_turn(
        {"text": "hi", "origin": "reply", "session_id": "s1"}, ctx
    )
    record = _await_terminal(ctx, accepted["turn_id"])
    assert record["status"] == turns.STATUS_DONE
    assert record["reply"] == "the agent's answer"
    assert record["error"] is None
    assert record["phase"] == turns.PHASE_FINISHED


def test_failed_turn_carries_the_real_error(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """Never a fabricated reply -- the turn's own error is what surfaces."""
    _make_session(ctx, "s1")

    def _resume(*args, **kwargs):
        return runner.StepResult(
            index=0,
            text="",
            reply="",
            error="session is locked (waited 900.0s)",
            duration_ms=1,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _resume)
    accepted = turns.submit_turn(
        {"text": "hi", "origin": "reply", "session_id": "s1"}, ctx
    )
    record = _await_terminal(ctx, accepted["turn_id"])
    assert record["status"] == turns.STATUS_FAILED
    assert "locked" in record["error"]


def test_lock_exhaustion_is_classified_as_session_locked(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """A wait that ran out is the SAME busy signal as a 423.

    The lock was never acquired, so no subprocess ran and the caller's text
    is provably safe to resend. ``failure_kind`` is what lets the consumer
    know that without substring-matching an error message -- see section
    7.3's flag. If this goes red, a reply that lost a race to a scheduled
    run silently loses its one-tap resend affordance.
    """
    _make_session(ctx, "s1")

    def _locked(*args, **kwargs):
        return runner.StepResult(
            index=0,
            text="",
            reply="",
            error="session 's1' is locked by another in-flight turn (waited 900.0s)",
            duration_ms=0,
            tokens_in=0,
            tokens_out=0,
            error_kind=runner.ERROR_KIND_SESSION_LOCKED,
        )

    monkeypatch.setattr(runner, "resume_turn", _locked)
    accepted = turns.submit_turn(
        {
            "text": "hi",
            "origin": "reply",
            "session_id": "s1",
            "lock_wait_seconds": 900,
        },
        ctx,
    )
    record = _await_terminal(ctx, accepted["turn_id"])
    assert record["status"] == turns.STATUS_FAILED
    assert record["failure_kind"] == turns.FAILURE_KIND_SESSION_LOCKED


def test_ordinary_failure_is_not_classified_as_resendable(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """Unknown never means resendable -- the conservative direction."""
    _make_session(ctx, "s1")

    def _failed(*args, **kwargs):
        return runner.StepResult(
            index=0,
            text="",
            reply="",
            error="step timed out after 900s",
            duration_ms=0,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _failed)
    accepted = turns.submit_turn(
        {"text": "hi", "origin": "reply", "session_id": "s1"}, ctx
    )
    record = _await_terminal(ctx, accepted["turn_id"])
    assert record["status"] == turns.STATUS_FAILED
    assert record["failure_kind"] is None


def test_real_lock_exhaustion_end_to_end(ctx: EngineContext):
    """Not a stub: a REAL held flock, a real short wait, a real classification.

    Proves the classification survives the actual runner path rather than
    only the monkeypatched one -- the layer that would otherwise be
    'green tests on the layer that wasn't broken'.
    """
    _make_session(ctx, "really-locked")
    fd = _hold_session_lock(ctx, "really-locked")
    try:
        accepted = turns.submit_turn(
            {
                "text": "hi",
                "origin": "reply",
                "session_id": "really-locked",
                "lock_wait_seconds": 1,
            },
            ctx,
        )
        record = _await_terminal(ctx, accepted["turn_id"], timeout=20)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert record["status"] == turns.STATUS_FAILED
    assert record["failure_kind"] == turns.FAILURE_KIND_SESSION_LOCKED
    assert "locked" in record["error"]
    # The text was never destroyed -- it is still on the record verbatim.
    assert record["text"] == "hi"


def test_raising_turn_is_recorded_failed_not_lost(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    _make_session(ctx, "s1")

    def _boom(*args, **kwargs):
        raise RuntimeError("the subprocess exploded")

    monkeypatch.setattr(runner, "resume_turn", _boom)
    accepted = turns.submit_turn(
        {"text": "hi", "origin": "reply", "session_id": "s1"}, ctx
    )
    record = _await_terminal(ctx, accepted["turn_id"])
    assert record["status"] == turns.STATUS_FAILED
    assert "the subprocess exploded" in record["error"]


def test_ceiling_tombstones_a_turn_that_never_finishes(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """The backstop: SOME terminal outcome is always recorded."""
    _make_session(ctx, "s1")
    release = threading.Event()

    def _hang(*args, **kwargs):
        release.wait(timeout=30)
        return runner.StepResult(
            index=0,
            text="",
            reply="late",
            error=None,
            duration_ms=1,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _hang)
    accepted = turns.submit_turn(
        {
            "text": "hi",
            "origin": "reply",
            "session_id": "s1",
            "ceiling_seconds": 0.5,
        },
        ctx,
    )
    record = _await_terminal(ctx, accepted["turn_id"], timeout=15)
    assert record["status"] == turns.STATUS_FAILED
    assert "maximum allowed processing time" in record["error"]
    release.set()


def test_real_outcome_still_lands_after_the_ceiling_fired(
    ctx: EngineContext, monkeypatch: pytest.MonkeyPatch
):
    """The ceiling's tombstone is a placeholder, never a final verdict.

    Deliberately asymmetric: the real answer, whenever it arrives, is the
    authoritative last word.
    """
    _make_session(ctx, "s1")
    release = threading.Event()

    def _slow(*args, **kwargs):
        release.wait(timeout=30)
        return runner.StepResult(
            index=0,
            text="",
            reply="the late but real answer",
            error=None,
            duration_ms=1,
            tokens_in=0,
            tokens_out=0,
        )

    monkeypatch.setattr(runner, "resume_turn", _slow)
    accepted = turns.submit_turn(
        {
            "text": "hi",
            "origin": "reply",
            "session_id": "s1",
            "ceiling_seconds": 0.5,
        },
        ctx,
    )
    tombstoned = _await_terminal(ctx, accepted["turn_id"], timeout=15)
    assert tombstoned["status"] == turns.STATUS_FAILED
    release.set()
    final = _await_status(ctx, accepted["turn_id"], turns.STATUS_DONE, timeout=15)
    assert final["reply"] == "the late but real answer"


# ---------------------------------------------------------- reconcile ----


def test_reconciliation_tombstones_an_orphaned_turn(ctx: EngineContext):
    """A turn the previous engine process was running is never re-run."""
    turns.turns_dir(ctx.runs_dir).mkdir(parents=True, exist_ok=True)
    record = {
        "turn_id": "t-orphan",
        "status": turns.STATUS_RUNNING,
        "phase": turns.PHASE_EXECUTING,
        "session_id": "s1",
        "text": "the user's words",
    }
    turns.turn_path(ctx.runs_dir, "t-orphan").write_text(
        json.dumps(record), encoding="utf-8"
    )
    result = turns.reconcile_turns_on_startup(ctx.runs_dir)
    assert result["tombstoned"] == ["t-orphan"]
    after = turns.get_turn("t-orphan", ctx)
    assert after["status"] == turns.STATUS_FAILED
    assert after["phase_at_interruption"] == turns.PHASE_EXECUTING
    assert "NOT automatically retried" in after["error"]
    # The text is never destroyed by reconciliation.
    assert after["text"] == "the user's words"


def test_reconciliation_leaves_terminal_turns_alone(ctx: EngineContext):
    turns.turns_dir(ctx.runs_dir).mkdir(parents=True, exist_ok=True)
    record = {
        "turn_id": "t-done",
        "status": turns.STATUS_DONE,
        "phase": turns.PHASE_FINISHED,
        "reply": "already answered",
    }
    turns.turn_path(ctx.runs_dir, "t-done").write_text(
        json.dumps(record), encoding="utf-8"
    )
    result = turns.reconcile_turns_on_startup(ctx.runs_dir)
    assert result["tombstoned"] == []
    assert turns.get_turn("t-done", ctx)["reply"] == "already answered"


# ------------------------------------------------------------- the API ----


def test_keyless_mutating_request_is_refused_from_loopback():
    """Section 3, restated for /api/turns specifically.

    The check is by METHOD, not by a path allowlist, which is what makes a
    newly added endpoint inherit it automatically. This test exists so that
    property is asserted for the endpoint the cutover added, not merely
    assumed.
    """
    assert (
        api_key.check_request(
            client_address="127.0.0.1",
            method="POST",
            path="/api/turns",
            header_value=None,
            expected_key=KEY,
        )
        is not None
    )


def test_keyed_mutating_request_is_allowed():
    assert (
        api_key.check_request(
            client_address="127.0.0.1",
            method="POST",
            path="/api/turns",
            header_value=KEY,
            expected_key=KEY,
        )
        is None
    )


@pytest.fixture
def live_engine(ctx: EngineContext):
    server = engine_api.EngineServer(
        ("127.0.0.1", 0),
        engine_api.EngineRequestHandler,
        ctx=ctx,
        workspace=ctx.cwd,
        api_key_value=KEY,
        scheduler_state=SchedulerState(),
        staleness_service="drumbeat-serve-test",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", ctx
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: dict, *, key: str | None = KEY):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    if key is not None:
        request.add_header("X-API-Key", key)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_keyless_post_turns_over_http_is_401(live_engine):
    base, _ = live_engine
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/api/turns", {"text": "hi", "origin": "reply"}, key=None)
    assert exc.value.code == 401


def test_unknown_session_over_http_is_404(live_engine):
    base, _ = live_engine
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(
            f"{base}/api/turns",
            {"text": "hi", "origin": "reply", "session_id": "nope"},
        )
    assert exc.value.code == 404
    body = json.loads(exc.value.read().decode("utf-8"))
    assert "refusing to guess a session" in body["error"]


def test_locked_session_over_http_is_423_with_retry_after(live_engine):
    base, engine_ctx = live_engine
    _make_session(engine_ctx, "http-locked")
    fd = _hold_session_lock(engine_ctx, "http-locked")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(
                f"{base}/api/turns",
                {
                    "text": "hi",
                    "origin": "reply",
                    "session_id": "http-locked",
                    "lock_wait_seconds": 0,
                },
            )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert exc.value.code == 423
    assert exc.value.headers["Retry-After"] == str(turns.RETRY_AFTER_SECONDS)
    body = json.loads(exc.value.read().decode("utf-8"))
    assert body["retry_after_seconds"] == turns.RETRY_AFTER_SECONDS


def test_unknown_turn_over_http_is_404(live_engine):
    base, _ = live_engine
    request = urllib.request.Request(f"{base}/api/turns/t-nope", method="GET")
    request.add_header("X-API-Key", KEY)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 404


# --------------------------------------------------------------- helpers ----


def _await_terminal(ctx: EngineContext, turn_id: str, *, timeout: float = 10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = turns.get_turn(turn_id, ctx)
        if record["status"] in turns.TERMINAL_STATUSES:
            return record
        time.sleep(0.05)
    raise AssertionError(f"turn {turn_id} never reached a terminal status")


def _await_status(ctx: EngineContext, turn_id: str, status: str, *, timeout: float):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = turns.get_turn(turn_id, ctx)
        if record["status"] == status:
            return record
        time.sleep(0.05)
    raise AssertionError(f"turn {turn_id} never reached status {status!r}")


# ------------------- the 404 that stopped implying deletion (B4 prereq 1) ----
#
# The turn API cannot pass a `recorded_workspace` (an item carries none), so
# `_probe_session`'s moved-vs-deleted detector is structurally bypassed here
# and every "not found under this workspace" arrives as MISSING. These lock
# the copy that keeps MISSING from reading as "deleted": delete the
# three-cause sentence and they go red.


def test_missing_session_404_names_all_three_causes(ctx: EngineContext):
    """A real-looking id under a workspace that EXISTS but does not hold it
    must not be reported as a deletion. It has three possible causes and this
    code path cannot tell them apart -- so it must name all three.

    The workspace has to exist for this to be the MISSING branch: a
    nonexistent workspace dir already trips the probe's own moved-detector
    (WORKSPACE_MISMATCH) and is honest without help. MISSING is the branch
    that lied, and it is the branch a post-cutover workspace lands in from
    its very first run onward -- which is the whole window pre-cutover items
    live in."""
    _make_session(ctx, "some-other-live-session")
    with pytest.raises(turns.TurnError) as exc:
        turns.submit_turn(
            {"text": "hi", "origin": "reply", "session_id": "a-real-looking-id"},
            ctx,
        )
    message = exc.value.message
    assert exc.value.status == 404
    assert "three possible causes" in message
    assert "never existed under this workspace" in message
    assert "was deleted" in message
    assert "WORKSPACE ITSELF MOVED" in message
    # And it must say so explicitly, not leave it to inference.
    assert "'missing' here never means 'deleted' on its own evidence" in message


def test_missing_session_404_still_refuses(ctx: EngineContext):
    """Honesty is not permissiveness. The engine still refuses -- it genuinely
    cannot resume a session it cannot find."""
    _make_session(ctx, "some-other-live-session")
    with pytest.raises(turns.TurnError) as exc:
        turns.submit_turn(
            {"text": "hi", "origin": "reply", "session_id": "a-real-looking-id"},
            ctx,
        )
    assert exc.value.status == 404
    assert "refusing to guess a session" in exc.value.message


def test_non_missing_probe_verdicts_pass_through_unchanged(ctx: EngineContext):
    """WORKSPACE_MISMATCH and UNKNOWN already carry a self-describing detail
    from the probe. They must NOT be padded with the three-cause paragraph --
    that paragraph exists precisely because MISSING has no such detail."""
    detail = "the project directory was very likely renamed or moved"
    message = turns._unresolvable_session_detail(
        "s-1", runner.SessionProbe.WORKSPACE_MISMATCH, detail
    )
    assert "three possible causes" not in message
    assert detail in message
    assert message.endswith("refusing to guess a session")


# --------------- the turn env: the session id var ----------------------------


def test_turn_env_emits_the_session_id():
    """The turn env stamps this turn's own session id so a consumer CLI the
    agent invokes can attribute an item to exactly the session that created
    it. One name only -- the legacy dual-emission under a consumer's brand is
    gone (clean cut, no compat alias)."""
    env = runner._turn_env(Path("/tmp"), runs_dir=Path("/tmp/runs"), session_id="s-abc")
    assert env["DRUMBEAT_TURN_SESSION_ID"] == "s-abc"


def test_turn_env_sets_no_session_id_without_a_session():
    """No session, no stamp. An empty-string session id would be worse than
    an absent one -- it reads as 'known' to a consumer testing truthiness."""
    env = runner._turn_env(Path("/tmp"), runs_dir=Path("/tmp/runs"))
    assert "DRUMBEAT_TURN_SESSION_ID" not in env


def test_turn_env_always_carries_the_data_dir_address(tmp_path):
    """Every turn env carries the data-dir address (a data-dir resident).

    Every turn env carries DRUMBEAT_DATA_DIR = the engine's resolved data
    dir, unconditionally -- session or no session. This is the ledger's
    address: consumer CLIs spawned inside a turn (a consumer's ledger tool, both as an
    inject: tool and as the agent's own bash call) resolve their store
    through it instead of the turn's cwd. Without it, re-pointing
    --workspace at a policy checkout while --data-dir stays behind makes
    ./runs resolve to an EMPTY store -- a silent false-idle over every open
    item, plus a shadow ledger minted inside the policy tree on first write.
    Resolved absolute, so a relative --data-dir can't smuggle cwd-dependence
    back in through the env itself."""
    runs = tmp_path / "state" / "runs"
    env = runner._turn_env(tmp_path, runs_dir=runs)
    assert env["DRUMBEAT_DATA_DIR"] == str(runs.resolve())
    env_with_session = runner._turn_env(tmp_path, runs_dir=runs, session_id="s-1")
    assert env_with_session["DRUMBEAT_DATA_DIR"] == str(runs.resolve())


def test_interactive_lock_wait_reads_override(monkeypatch):
    monkeypatch.setenv("DRUMBEAT_SESSION_LOCK_WAIT_SECONDS", "11")
    assert runner._interactive_lock_wait_seconds() == 11.0


def test_interactive_lock_wait_default_when_override_absent(monkeypatch):
    monkeypatch.delenv("DRUMBEAT_SESSION_LOCK_WAIT_SECONDS", raising=False)
    assert (
        runner._interactive_lock_wait_seconds()
        == runner._DEFAULT_INTERACTIVE_LOCK_WAIT_SECONDS
    )


def test_background_lock_wait_reads_override(monkeypatch):
    monkeypatch.delenv("DRUMBEAT_BACKGROUND_LOCK_WAIT_SECONDS", raising=False)
    assert (
        runner.background_lock_wait_seconds()
        == runner._DEFAULT_BACKGROUND_LOCK_WAIT_SECONDS
    )
    monkeypatch.setenv("DRUMBEAT_BACKGROUND_LOCK_WAIT_SECONDS", "44")
    assert runner.background_lock_wait_seconds() == 44.0


# ------------------------------- the namespace and the prefix ----------


def test_now_context_line_is_engine_branded_and_domain_free():
    """The per-turn now-context line states what time it is and nothing else.
    Connector-specific timestamp advice belongs on that connector's pack card,
    which is re-read verbatim every run."""
    line = runner._now_context_line()
    assert line.startswith("[drumbeat] Current date/time: ")
    assert ("[att" + "end]") not in line
    assert "Graph" not in line
    assert "Microsoft" not in line


def test_no_consumer_namespaced_ci_events_remain_in_engine_source():
    """Namespace follows the emitter. These are engine telemetry about engine
    mechanics; the first consumer's own prefix names a consumer that does not
    emit them."""
    import drumbeat

    # Built from fragments so this guard never itself plants the literal brand
    # token it hunts for into the repo.
    consumer_prefix = '"' + "att" + "end:"
    src = Path(drumbeat.__file__).parent
    offenders = [
        f"{path.name}:{n}"
        for path in sorted(src.rglob("*.py"))
        for n, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if consumer_prefix in text
    ]
    assert offenders == []
