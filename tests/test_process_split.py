"""Step 3: the process split -- auth posture, drain, lock handoff, sweep, API.

Each test here corresponds to a claim docs/ARCHITECTURE.md makes about the
engine running as its own process, and is written so that removing the
mechanism makes it fail (red-provable). The three that matter most:

- ``test_mutating_request_from_loopback_is_refused_without_key`` -- the
  council's amendment 6. Delete the method check in ``api_key`` and this
  goes red.
- ``test_second_scheduler_cannot_acquire_the_lock`` -- the flock guarantee,
  in the direction the runbook actually needs it: the INCUMBENT keeps the
  lock, the newcomer fails.
- ``test_completed_run_without_delivery_intent_is_invalid`` -- section 6's
  invariant, which is the whole reason failure class 1 is closed.
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
from drumbeat.scheduler import (
    SchedulerError,
    SchedulerState,
    acquire_scheduler_lock,
)

from drumbeat import api_key, drain, engine_api, engine_events, invalid_runs, serve

# ---------------------------------------------------------------- auth ----

KEY = "test-key-not-a-real-secret"


def test_read_from_loopback_needs_no_key():
    assert (
        api_key.check_request(
            client_address="127.0.0.1",
            method="GET",
            path="/api/automations",
            header_value=None,
            expected_key=KEY,
        )
        is None
    )


def test_mutating_request_from_loopback_is_refused_without_key():
    """The amendment-6 property: loopback is not a credential for writes."""
    error = api_key.check_request(
        client_address="127.0.0.1",
        method="POST",
        path="/api/automations",
        header_value=None,
        expected_key=KEY,
    )
    assert error is not None
    assert "X-API-Key" in error


def test_mutating_request_from_loopback_succeeds_with_key():
    assert (
        api_key.check_request(
            client_address="127.0.0.1",
            method="POST",
            path="/api/automations",
            header_value=KEY,
            expected_key=KEY,
        )
        is None
    )


def test_wrong_key_is_refused():
    error = api_key.check_request(
        client_address="127.0.0.1",
        method="PUT",
        path="/api/automations/x",
        header_value="wrong",
        expected_key=KEY,
    )
    assert error == "incorrect X-API-Key header"


def test_health_is_public_even_off_loopback():
    assert (
        api_key.check_request(
            client_address="10.0.0.5",
            method="GET",
            path="/api/health",
            header_value=None,
            expected_key=KEY,
        )
        is None
    )


def test_unknown_method_is_treated_as_a_write():
    """Default direction: unrecognised method => key required, never assumed safe."""
    assert api_key.is_mutating("PATCH")
    assert api_key.is_mutating("PROPFIND")
    assert not api_key.is_mutating("GET")


def test_empty_key_file_raises_rather_than_regenerating(tmp_path: Path):
    (tmp_path / api_key.API_KEY_FILENAME).write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="empty"):
        api_key.ensure_api_key(tmp_path)


def test_generated_key_file_is_owner_only(tmp_path: Path):
    api_key.ensure_api_key(tmp_path)
    mode = (tmp_path / api_key.API_KEY_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600


# --------------------------------------------------------------- drain ----


def test_drain_requires_a_reason(tmp_path: Path):
    with pytest.raises(ValueError, match="reason"):
        drain.set_drain(tmp_path, reason="")


def test_drain_flag_roundtrip(tmp_path: Path):
    assert drain.drain_state(tmp_path) is None
    drain.set_drain(tmp_path, reason="cutting over to drumbeat serve")
    state = drain.drain_state(tmp_path)
    assert state is not None
    assert state["reason"] == "cutting over to drumbeat serve"
    assert drain.is_draining(tmp_path)
    assert drain.clear_drain(tmp_path)
    assert drain.drain_state(tmp_path) is None
    assert not drain.clear_drain(tmp_path)


def test_corrupt_drain_flag_still_counts_as_draining(tmp_path: Path):
    """Presence is the signal. Refusing to honour a corrupt flag would resume
    scheduling at the exact moment somebody was trying to stop it."""
    drain.drain_flag_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert drain.is_draining(tmp_path)
    state = drain.drain_state(tmp_path)
    assert state is not None
    assert "unreadable" in str(state["reason"])


def test_not_drained_without_the_flag(tmp_path: Path):
    status = drain.check_drained(tmp_path)
    assert not status.drained
    assert any("drain flag is NOT set" in b for b in status.blockers)


def test_drained_once_flag_set_and_nothing_in_flight(tmp_path: Path):
    drain.set_drain(tmp_path, reason="test")
    status = drain.check_drained(tmp_path)
    assert status.drained, status.blockers


def test_held_session_lock_blocks_the_drain(tmp_path: Path):
    lock_dir = tmp_path / ".session-locks"
    lock_dir.mkdir()
    lock_file = lock_dir / "some-session.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert drain.held_session_locks(tmp_path) == ["some-session.lock"]
        drain.set_drain(tmp_path, reason="test")
        status = drain.check_drained(tmp_path)
        assert not status.drained
        assert any("session lock" in b for b in status.blockers)
    finally:
        os.close(fd)

    # Released: the same file is now provably free, though it still exists.
    assert lock_file.exists()
    assert drain.held_session_locks(tmp_path) == []


def test_unheld_lock_files_do_not_block(tmp_path: Path):
    """Presence of a .lock file means nothing -- only a held flock does."""
    lock_dir = tmp_path / ".session-locks"
    lock_dir.mkdir()
    (lock_dir / "ancient.lock").write_text("", encoding="utf-8")
    drain.set_drain(tmp_path, reason="test")
    assert drain.check_drained(tmp_path).drained


# ------------------------------------------------------- scheduler lock ----


def test_second_scheduler_cannot_acquire_the_lock(tmp_path: Path):
    """The guarantee, in the direction the runbook needs it.

    A lingering incumbent KEEPS the lock; the newcomer is the one that must
    fail loudly and refuse to schedule. Getting this backwards is how a
    handoff double-fires every automation.
    """
    incumbent = acquire_scheduler_lock(tmp_path)
    try:
        with pytest.raises(SchedulerError, match="already holds"):
            acquire_scheduler_lock(tmp_path)
    finally:
        incumbent.close()

    # Once the incumbent is gone the lock is takeable -- the kernel releases
    # a flock at process/fd death, which is exactly why the drain must
    # happen BEFORE the kill, not after.
    newcomer = acquire_scheduler_lock(tmp_path)
    newcomer.close()


def test_lock_file_records_the_holding_pid(tmp_path: Path):
    handle = acquire_scheduler_lock(tmp_path)
    try:
        assert (tmp_path / ".scheduler.lock").read_text(
            encoding="utf-8"
        ).strip() == str(os.getpid())
    finally:
        handle.close()


# -------------------------------------------------------- invalid runs ----


def _write_run(runs_dir: Path, slug: str, run_id: str, *, result: dict | None) -> Path:
    run_dir = runs_dir / slug / run_id
    run_dir.mkdir(parents=True)
    if result is not None:
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return run_dir


def test_completed_run_with_delivery_intent_is_clean(tmp_path: Path):
    _write_run(
        tmp_path,
        "teams-check",
        "20260809T120000Z",
        result={"run_id": "20260809T120000Z"},
    )
    engine_events.append_event(
        tmp_path,
        engine_events.EventType.DELIVERY_INTENT,
        {
            "run_id": "20260809T120000Z",
            "automation": "teams-check",
            "automation_slug": "teams-check",
            "session_id": "s1",
            "verdict": "withhold",
            "gate": "auto-sentinel",
            "reason": "agent reported nothing to report",
            "text": "",
        },
    )
    assert invalid_runs.sweep(tmp_path) == []


def test_completed_run_without_delivery_intent_is_invalid(tmp_path: Path):
    """Section 6's invariant -- the close on failure class 1."""
    _write_run(
        tmp_path,
        "teams-check",
        "20260809T130000Z",
        result={"run_id": "20260809T130000Z"},
    )
    findings = invalid_runs.sweep(tmp_path)
    assert len(findings) == 1
    assert findings[0].run_id == "20260809T130000Z"
    assert "no delivery_intent" in findings[0].problem


def test_non_terminal_run_is_reported(tmp_path: Path):
    """What a process killed mid-turn leaves behind."""
    run_dir = _write_run(tmp_path, "teams-check", "20260809T140000Z", result=None)
    (run_dir / "status.json").write_text(
        json.dumps({"status": "running", "started_at": "2026-08-09T14:00:00Z"}),
        encoding="utf-8",
    )
    findings = invalid_runs.sweep(tmp_path)
    assert len(findings) == 1
    assert "NON-TERMINAL" in findings[0].problem


def test_terminally_failed_run_is_not_a_finding(tmp_path: Path):
    run_dir = _write_run(tmp_path, "teams-check", "20260809T150000Z", result=None)
    (run_dir / "status.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )
    assert invalid_runs.sweep(tmp_path) == []


def test_sweep_since_window_excludes_older_runs(tmp_path: Path):
    from datetime import UTC, datetime

    _write_run(tmp_path, "teams-check", "20260801T120000Z", result={"run_id": "old"})
    since = datetime(2026, 8, 9, tzinfo=UTC)
    assert invalid_runs.sweep(tmp_path, since=since) == []
    assert len(invalid_runs.sweep(tmp_path)) == 1


# ------------------------------------------------------------ workspace ----


def test_workspace_without_automations_is_refused(tmp_path: Path):
    with pytest.raises(SystemExit, match="automations"):
        serve.resolve_workspace(tmp_path)


def test_workspace_resolves_the_four_dirs(tmp_path: Path):
    (tmp_path / "automations").mkdir()
    ctx = serve.resolve_workspace(tmp_path)
    assert ctx.automations_dir == tmp_path.resolve() / "automations"
    assert ctx.prompts_dir == tmp_path.resolve() / "prompts"
    assert ctx.runs_dir == tmp_path.resolve() / "runs"
    assert ctx.cwd == tmp_path.resolve()


def test_missing_worker_cursor_reads_as_unknown_not_zero(tmp_path: Path):
    """A fabricated 0 would render as either 'fully caught up' or 'lag = whole
    file' depending on how the reader squints. Both are lies."""
    result = engine_api.read_worker_cursor(tmp_path)
    assert result["cursor"] is None
    assert "does not exist" in result["source"]


def test_published_worker_cursor_is_read_back(tmp_path: Path):
    (tmp_path / engine_api.WORKER_CURSOR_FILENAME).write_text(
        json.dumps({"cursor": 4096, "updated_at": "2026-08-09T16:00:00Z"}),
        encoding="utf-8",
    )
    result = engine_api.read_worker_cursor(tmp_path)
    assert result["cursor"] == 4096


# ----------------------------------------------------------- HTTP face ----


@pytest.fixture
def engine_server(tmp_path: Path):
    """A real EngineServer on an ephemeral loopback port."""
    workspace = tmp_path / "ws"
    (workspace / "automations").mkdir(parents=True)
    (workspace / "prompts").mkdir()
    (workspace / "runs").mkdir()
    ctx = EngineContext(
        automations_dir=workspace / "automations",
        prompts_dir=workspace / "prompts",
        runs_dir=workspace / "runs",
        cwd=workspace,
    )
    state = SchedulerState(
        lock_held=True, lock_path=str(workspace / "runs/.scheduler.lock")
    )
    server = engine_api.EngineServer(
        ("127.0.0.1", 0),
        engine_api.EngineRequestHandler,
        ctx=ctx,
        workspace=workspace,
        api_key_value=KEY,
        scheduler_state=state,
        staleness_service="drumbeat-serve",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", workspace
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str, key: str | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url)
    if key:
        request.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post(url: str, body: dict, key: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_reports_lock_workspace_and_outbox(engine_server):
    base, workspace = engine_server
    status, payload = _get(f"{base}/api/health")
    assert status == 200
    assert payload["scheduler_lock"] == "held"
    assert payload["workspace"] == str(workspace)
    assert "outbox" in payload
    assert "worker_cursor_lag" in payload["outbox"]
    assert payload["doctor"]["status"] in {"FRESH", "STALE", "UNKNOWN"}


def test_health_shows_not_held_when_lock_is_not_held(tmp_path: Path):
    """'Up but lockless' must be visible, not silent."""
    workspace = tmp_path / "ws"
    (workspace / "automations").mkdir(parents=True)
    (workspace / "runs").mkdir()
    ctx = EngineContext(
        automations_dir=workspace / "automations",
        prompts_dir=workspace / "prompts",
        runs_dir=workspace / "runs",
        cwd=workspace,
    )
    server = engine_api.EngineServer(
        ("127.0.0.1", 0),
        engine_api.EngineRequestHandler,
        ctx=ctx,
        workspace=workspace,
        api_key_value=KEY,
        scheduler_state=SchedulerState(lock_held=False),
        staleness_service="drumbeat-serve",
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _, payload = _get(f"http://127.0.0.1:{server.server_address[1]}/api/health")
        assert payload["scheduler_lock"] == "not_held"
    finally:
        server.shutdown()
        server.server_close()


def test_get_automations_from_loopback_needs_no_key(engine_server):
    base, _ = engine_server
    status, payload = _get(f"{base}/api/automations")
    assert status == 200
    assert payload["count"] == 0


def test_post_from_loopback_without_key_is_401(engine_server):
    """The live property: mutating endpoints are closed even to localhost."""
    base, _ = engine_server
    status, payload = _post(f"{base}/api/automations", {"content": "x"})
    assert status == 401
    assert "X-API-Key" in payload["error"]


def test_post_with_key_reaches_the_handler(engine_server):
    base, workspace = engine_server
    content = (
        "---\n"
        "automation:\n"
        "  name: Drill\n"
        "  enabled: false\n"
        "  trigger:\n"
        "    type: manual\n"
        "  notify: never\n"
        "---\n\n"
        "1. do nothing\n"
    )
    status, payload = _post(f"{base}/api/automations", {"content": content}, key=KEY)
    assert status == 201, payload
    assert (workspace / "automations" / f"{payload['slug']}.md").is_file()


def test_unknown_route_is_404_not_a_guess(engine_server):
    base, _ = engine_server
    status, payload = _get(f"{base}/api/nope")
    assert status == 404
    assert "no such route" in payload["error"]
