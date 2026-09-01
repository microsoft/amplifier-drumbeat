"""``drumbeat service`` -- generated unit/plist content and status parsing.

Live install/start/uninstall runs against a real ``systemd --user`` in the DTU
validation lane. What is provable here without a supervisor is everything that
matters most for getting the file right: the *content* the generators emit, and
the *parsers* that read a supervisor's status back. Each test is written so that
breaking the mechanism it names turns it red.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from drumbeat import service
from drumbeat.serve import DEFAULT_PORT

_EXEC = ("/opt/bin/drumbeat",)


def _spec(**over: object) -> service.ServiceSpec:
    base: dict[str, object] = {
        "exec_argv": _EXEC,
        "workspace": "/home/u/myspace",
        "port": 9100,
        "host": "127.0.0.1",
        "data_dir": None,
    }
    base.update(over)
    return service.ServiceSpec(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# systemd unit content
# --------------------------------------------------------------------------- #


def test_systemd_unit_has_serve_execstart_with_workspace_and_port() -> None:
    unit = service.render_systemd_unit(_spec(port=9137))
    assert (
        "ExecStart=/opt/bin/drumbeat serve --workspace /home/u/myspace "
        "--port 9137 --host 127.0.0.1" in unit
    )


def test_systemd_unit_restart_on_failure() -> None:
    # The one directive named explicitly in the spec.
    assert "Restart=on-failure" in service.render_systemd_unit(_spec())


def test_systemd_unit_stop_is_a_blocking_drain() -> None:
    unit = service.render_systemd_unit(_spec())
    # ExecStop drains and waits; the reason is required and survives quoting.
    assert "ExecStop=/opt/bin/drumbeat drain --workspace /home/u/myspace" in unit
    assert '--reason "systemd stop (drumbeat service)" --wait' in unit


def test_systemd_unit_clears_drain_on_start_path() -> None:
    unit = service.render_systemd_unit(_spec())
    # `-` prefix => a clear that finds no drain is a no-op, not a start failure.
    assert (
        "ExecStartPre=-/opt/bin/drumbeat drain --workspace /home/u/myspace --clear"
        in unit
    )


def test_systemd_unit_killmode_process_and_generous_stop_timeout() -> None:
    unit = service.render_systemd_unit(_spec())
    # In-flight agent children must outlive the scheduler's stop.
    assert "KillMode=process" in unit
    # Longer than the drain CLI's own 1800s default.
    assert f"TimeoutStopSec={service.STOP_TIMEOUT_SECONDS}" in unit
    assert service.STOP_TIMEOUT_SECONDS > 1800


def test_systemd_unit_enables_via_default_target() -> None:
    assert "WantedBy=default.target" in service.render_systemd_unit(_spec())


def test_systemd_unit_pins_workingdirectory_not_agent_workspace_env() -> None:
    unit = service.render_systemd_unit(_spec())
    assert "WorkingDirectory=/home/u/myspace" in unit
    # AMPLIFIER_AGENT_WORKSPACE silently re-buckets session ids; never set it.
    assert "AMPLIFIER_AGENT_WORKSPACE" not in unit


def test_systemd_unit_optional_env_file_for_provider_key() -> None:
    # `-` prefix => a missing env file is not a start failure.
    assert "EnvironmentFile=-" in service.render_systemd_unit(_spec())


def test_systemd_unit_omits_data_dir_when_defaulted() -> None:
    unit = service.render_systemd_unit(_spec(data_dir=None))
    assert "--data-dir" not in unit


def test_systemd_unit_threads_data_dir_through_every_command() -> None:
    unit = service.render_systemd_unit(_spec(data_dir="/var/state"))
    # serve, drain-on-stop and clear-on-start must all agree on the data dir,
    # or they read different directories.
    for line_prefix in ("ExecStart=", "ExecStop=", "ExecStartPre="):
        line = next(x for x in unit.splitlines() if x.startswith(line_prefix))
        assert "--data-dir /var/state" in line


def test_systemd_unit_quotes_workspace_with_spaces() -> None:
    unit = service.render_systemd_unit(_spec(workspace="/home/u/my space"))
    assert '"/home/u/my space"' in unit
    # And the quoting round-trips back to the original token.
    tokens = service.extract_systemd_exec_start(unit)
    assert tokens is not None
    assert "/home/u/my space" in tokens


# --------------------------------------------------------------------------- #
# launchd plist content
# --------------------------------------------------------------------------- #


def test_launchd_plist_is_well_formed_and_round_trips() -> None:
    plist = service.render_launchd_plist(_spec(port=9111))
    data = plistlib.loads(plist.encode("utf-8"))
    assert data["Label"] == "drumbeat"
    assert data["ProgramArguments"] == [
        "/opt/bin/drumbeat",
        "serve",
        "--workspace",
        "/home/u/myspace",
        "--port",
        "9111",
        "--host",
        "127.0.0.1",
    ]
    assert data["RunAtLoad"] is True
    # KeepAlive-on-non-clean-exit is launchd's Restart=on-failure.
    assert data["KeepAlive"] == {"SuccessfulExit": False}


def test_launchd_plist_round_trips_even_when_path_contains_double_dash() -> None:
    # A '--' in a path is legal on disk and must not corrupt the XML (comments
    # cannot contain '--'); the generated comment is static and path-free.
    plist = service.render_launchd_plist(_spec(workspace="/home/u/a--b"))
    data = plistlib.loads(plist.encode("utf-8"))
    assert "/home/u/a--b" in data["ProgramArguments"]


def test_launchd_plist_includes_log_paths_when_set() -> None:
    plist = service.render_launchd_plist(
        _spec(stdout_path="/tmp/o.log", stderr_path="/tmp/e.log")
    )
    data = plistlib.loads(plist.encode("utf-8"))
    assert data["StandardOutPath"] == "/tmp/o.log"
    assert data["StandardErrorPath"] == "/tmp/e.log"


def test_launchd_plist_omits_data_dir_when_defaulted() -> None:
    data = plistlib.loads(
        service.render_launchd_plist(_spec(data_dir=None)).encode("utf-8")
    )
    assert "--data-dir" not in data["ProgramArguments"]


def test_launchd_plist_threads_data_dir() -> None:
    data = plistlib.loads(
        service.render_launchd_plist(_spec(data_dir="/var/state")).encode("utf-8")
    )
    args = data["ProgramArguments"]
    assert "--data-dir" in args
    assert args[args.index("--data-dir") + 1] == "/var/state"


# --------------------------------------------------------------------------- #
# invocation recovery (status/uninstall read the file, not re-supplied flags)
# --------------------------------------------------------------------------- #


def test_extract_systemd_exec_start_ignores_execstartpre() -> None:
    unit = service.render_systemd_unit(_spec(port=9150))
    tokens = service.extract_systemd_exec_start(unit)
    assert tokens is not None
    assert tokens[0] == "/opt/bin/drumbeat"
    assert tokens[1] == "serve"
    assert service.find_flag_value(tokens, "--port") == "9150"


def test_extract_launchd_program_arguments() -> None:
    plist = service.render_launchd_plist(_spec(port=9160))
    tokens = service.extract_launchd_program_arguments(plist)
    assert tokens is not None
    assert service.find_flag_value(tokens, "--port") == "9160"


def test_extract_launchd_program_arguments_on_garbage_is_none() -> None:
    assert service.extract_launchd_program_arguments("not a plist") is None


def test_find_flag_value_handles_equals_form_and_absence() -> None:
    assert service.find_flag_value(["--port=9100"], "--port") == "9100"
    assert service.find_flag_value(["serve"], "--port") is None
    assert service.find_flag_value(["--port"], "--port") is None


# --------------------------------------------------------------------------- #
# systemctl status parsing
# --------------------------------------------------------------------------- #


def test_parse_systemctl_show_running() -> None:
    out = (
        "LoadState=loaded\nActiveState=active\nSubState=running\n"
        "UnitFileState=enabled\nMainPID=4242\n"
    )
    status = service.parse_systemctl_show(out)
    assert status.loaded is True
    assert status.active is True
    assert status.sub_state == "running"
    assert status.main_pid == 4242


def test_parse_systemctl_show_not_installed() -> None:
    out = (
        "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        "UnitFileState=\nMainPID=0\n"
    )
    status = service.parse_systemctl_show(out)
    assert status.loaded is False
    assert status.active is False
    assert status.main_pid is None


def test_parse_systemctl_show_tolerates_noise_and_bad_pid() -> None:
    out = "ActiveState=failed\nMainPID=notanumber\n\ngarbage-without-equals\n"
    status = service.parse_systemctl_show(out)
    assert status.active is False
    assert status.active_state == "failed"
    assert status.main_pid is None


# --------------------------------------------------------------------------- #
# launchctl status parsing
# --------------------------------------------------------------------------- #


def test_parse_launchctl_list_running() -> None:
    out = '{\n\t"PID" = 987;\n\t"LastExitStatus" = 0;\n\t"Label" = "drumbeat";\n};\n'
    status = service.parse_launchctl_list(out)
    assert status.loaded is True
    assert status.active is True
    assert status.pid == 987
    assert status.last_exit_status == 0


def test_parse_launchctl_list_loaded_but_idle() -> None:
    out = '{\n\t"LastExitStatus" = 1;\n\t"Label" = "drumbeat";\n};\n'
    status = service.parse_launchctl_list(out)
    assert status.loaded is True
    assert status.active is False  # no PID => not currently running
    assert status.last_exit_status == 1


def test_parse_launchctl_list_not_loaded() -> None:
    out = 'Could not find service "drumbeat" in domain for user\n'
    status = service.parse_launchctl_list(out)
    assert status.loaded is False
    assert status.active is False


# --------------------------------------------------------------------------- #
# health interpretation (the verify-before-success gate)
# --------------------------------------------------------------------------- #


def test_interpret_health_ok() -> None:
    result = service.interpret_health(
        200,
        {
            "status": "ok",
            "scheduler_lock": "held",
            "scheduler": {"scheduling": "active"},
        },
    )
    assert result.ok is True


def test_interpret_health_rejects_up_but_lockless() -> None:
    # "up but holds no lock" is the silent double-fire hazard; verifying it as
    # healthy would defeat the whole point of the probe.
    result = service.interpret_health(
        200, {"status": "ok", "scheduler_lock": "not_held"}
    )
    assert result.ok is False
    assert "scheduler_lock" in result.detail


def test_interpret_health_rejects_non_200() -> None:
    assert service.interpret_health(503, None).ok is False


def test_interpret_health_rejects_non_object_payload() -> None:
    assert service.interpret_health(200, "ok").ok is False


def test_interpret_health_rejects_wrong_status() -> None:
    assert service.interpret_health(200, {"status": "degraded"}).ok is False


# --------------------------------------------------------------------------- #
# exec resolution
# --------------------------------------------------------------------------- #


def test_resolve_exec_argv_is_absolute() -> None:
    argv = service.resolve_exec_argv()
    assert argv
    assert argv[0].startswith("/")  # a supervisor needs an absolute ExecStart


def test_default_port_is_shared_with_serve() -> None:
    # The install CLI defaults to serve's port; keep them one constant.
    assert service.DEFAULT_PORT == DEFAULT_PORT


# --------------------------------------------------------------------------- #
# baked PATH -- the fix for "generated unit omits uv => every scheduled run fails"
# --------------------------------------------------------------------------- #


def test_systemd_unit_bakes_path_when_env_path_set() -> None:
    unit = service.render_systemd_unit(_spec(env_path="/home/u/.local/bin:/usr/bin"))
    assert "Environment=PATH=/home/u/.local/bin:/usr/bin" in unit


def test_systemd_unit_path_precedes_environment_file() -> None:
    # PATH is baked BEFORE EnvironmentFile so the operator's env file can still
    # override it if they must -- and, structurally, so the directive exists.
    unit = service.render_systemd_unit(_spec(env_path="/home/u/.local/bin:/usr/bin"))
    lines = unit.splitlines()
    path_idx = lines.index("Environment=PATH=/home/u/.local/bin:/usr/bin")
    env_file_idx = next(
        i for i, line in enumerate(lines) if line.startswith("EnvironmentFile=")
    )
    assert path_idx < env_file_idx


def test_systemd_unit_omits_path_when_env_path_unset() -> None:
    # Backward-compatible: a spec with no resolved PATH emits no directive.
    assert "Environment=PATH=" not in service.render_systemd_unit(_spec())


def test_systemd_unit_quotes_path_with_space() -> None:
    unit = service.render_systemd_unit(_spec(env_path="/home/u/my bin:/usr/bin"))
    assert 'Environment="PATH=/home/u/my bin:/usr/bin"' in unit


def test_launchd_plist_bakes_path_when_env_path_set() -> None:
    plist = service.render_launchd_plist(_spec(env_path="/home/u/.local/bin:/usr/bin"))
    data = plistlib.loads(plist.encode("utf-8"))
    assert data["EnvironmentVariables"] == {"PATH": "/home/u/.local/bin:/usr/bin"}


def test_launchd_plist_omits_env_when_env_path_unset() -> None:
    data = plistlib.loads(service.render_launchd_plist(_spec()).encode("utf-8"))
    assert "EnvironmentVariables" not in data


# --------------------------------------------------------------------------- #
# resolve_service_path -- computed at install time from the installer's shell
# --------------------------------------------------------------------------- #


def test_resolve_service_path_prepends_uv_dir_then_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        service.shutil,
        "which",
        lambda name: "/opt/uv/bin/uv" if name == "uv" else None,
    )
    monkeypatch.setattr(service.sys, "executable", "/opt/toolvenv/bin/python")
    path = service.resolve_service_path(exec_argv=("/home/u/.local/bin/drumbeat",))
    entries = path.split(":")
    # uv's own directory is FIRST -- it is the binary the engine shells out to.
    assert entries[0] == "/opt/uv/bin"
    # the co-installed agent bin dir and the drumbeat script dir are present.
    assert "/opt/toolvenv/bin" in entries
    assert "/home/u/.local/bin" in entries
    # ahead of a sane default -- uv resolves before /usr/bin.
    assert entries.index("/opt/uv/bin") < entries.index("/usr/bin")
    assert path.endswith(service._DEFAULT_SERVICE_PATH)


def test_resolve_service_path_best_effort_when_uv_missing(monkeypatch) -> None:
    # uv not on the installer's PATH: still emit agent dir + default, never a
    # silent empty leading entry.
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    monkeypatch.setattr(service.sys, "executable", "/opt/toolvenv/bin/python")
    path = service.resolve_service_path()
    entries = path.split(":")
    assert "/opt/toolvenv/bin" in entries
    assert path.endswith(service._DEFAULT_SERVICE_PATH)
    assert "" not in entries  # no empty component from a missing uv dir


def test_resolve_service_path_dedups_preserving_order(monkeypatch) -> None:
    # uv landing in the same dir as the default must not duplicate the entry.
    monkeypatch.setattr(
        service.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(service.sys, "executable", "/usr/bin/python")
    path = service.resolve_service_path()
    entries = path.split(":")
    assert entries.count("/usr/bin") == 1


# --------------------------------------------------------------------------- #
# the turn-verify gate -- prove the unit runs ONE real turn, not just /health
# --------------------------------------------------------------------------- #


def test_runs_dir_for_defaults_to_workspace_runs() -> None:
    assert service.runs_dir_for(_spec(workspace="/w", data_dir=None)) == Path("/w/runs")
    assert service.runs_dir_for(_spec(data_dir="/state")) == Path("/state")


def test_slug_from_error_extracts_slug() -> None:
    assert (
        service._slug_from_error("automation with slug 'foo' already exists") == "foo"
    )
    assert service._slug_from_error(None) is None


def test_verify_automation_content_is_a_valid_automation(tmp_path) -> None:
    # The throwaway automation the gate creates must always parse -- otherwise
    # the gate could never run and would fail install on its own residue.
    from drumbeat import automation

    path = tmp_path / "check.md"
    path.write_text(service._VERIFY_AUTOMATION_CONTENT, encoding="utf-8")
    auto = automation.load(path)
    assert auto.slug == "drumbeat-install-check"
    assert auto.enabled is False
    assert auto.notify == "never"


def test_poll_turn_done_with_reply_is_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_http_json",
        lambda *a, **k: (200, {"status": "done", "reply": "READY"}),
    )
    result = service._poll_turn_to_terminal(
        "http://x", "k", "t-1", timeout=5, poll=0.01
    )
    assert result.ok
    assert "READY" in result.detail


def test_poll_turn_failed_surfaces_the_error(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "_http_json",
        lambda *a, **k: (
            200,
            {"status": "failed", "error": "amplifier-agent exited 1"},
        ),
    )
    result = service._poll_turn_to_terminal(
        "http://x", "k", "t-1", timeout=5, poll=0.01
    )
    assert not result.ok
    assert "amplifier-agent exited 1" in result.detail


def test_poll_turn_empty_reply_is_a_failure(monkeypatch) -> None:
    # "done" with an empty reply is not a real turn -- reject it.
    monkeypatch.setattr(
        service, "_http_json", lambda *a, **k: (200, {"status": "done", "reply": "  "})
    )
    result = service._poll_turn_to_terminal(
        "http://x", "k", "t-1", timeout=5, poll=0.01
    )
    assert not result.ok


def test_poll_turn_times_out_without_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        service, "_http_json", lambda *a, **k: (200, {"status": "running"})
    )
    result = service._poll_turn_to_terminal(
        "http://x", "k", "t-1", timeout=0.05, poll=0.01
    )
    assert not result.ok
    assert "did not finish" in result.detail


def test_verify_one_real_turn_happy_path_cleans_up(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "_read_engine_api_key", lambda runs: "k")
    seen: list[tuple[str, str]] = []

    def fake(method, url, *, api_key=None, body=None, timeout=5.0):
        seen.append((method, url))
        if method == "POST" and url.endswith("/api/automations"):
            return 201, {"slug": "drumbeat-install-check"}
        if method == "POST" and url.endswith("/api/turns"):
            return 202, {"turn_id": "t-1"}
        if method == "GET" and url.endswith("/api/turns/t-1"):
            return 200, {"status": "done", "reply": "READY"}
        if method == "DELETE":
            return 200, {"deleted": "drumbeat-install-check"}
        return 404, {}

    monkeypatch.setattr(service, "_http_json", fake)
    result = service.verify_one_real_turn(
        _spec(workspace=str(tmp_path)), timeout=2, poll=0.01
    )
    assert result.ok
    # The throwaway automation is always deleted -- even on the happy path.
    assert any(method == "DELETE" for method, _ in seen)


def test_verify_one_real_turn_reports_turn_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(service, "_read_engine_api_key", lambda runs: "k")

    def fake(method, url, *, api_key=None, body=None, timeout=5.0):
        if method == "POST" and url.endswith("/api/automations"):
            return 201, {"slug": "drumbeat-install-check"}
        if method == "POST" and url.endswith("/api/turns"):
            return 202, {"turn_id": "t-1"}
        if method == "GET":
            return 200, {"status": "failed", "error": "uv is not installed"}
        return 200, {}

    monkeypatch.setattr(service, "_http_json", fake)
    result = service.verify_one_real_turn(
        _spec(workspace=str(tmp_path)), timeout=2, poll=0.01
    )
    assert not result.ok
    assert "uv is not installed" in result.detail


def test_verify_one_real_turn_missing_api_key_is_a_failure(
    monkeypatch, tmp_path
) -> None:
    def boom(_runs):
        raise OSError("no such file")

    monkeypatch.setattr(service, "_read_engine_api_key", boom)
    result = service.verify_one_real_turn(_spec(workspace=str(tmp_path)))
    assert not result.ok
    assert "API key" in result.detail


# --------------------------------------------------------------------------- #
# field report #504 -- the four compounding failures, each pinned
#
# NO macOS HOST EXISTS IN THIS LANE. Every Darwin claim below is proven by
# unit-level SIMULATION: the platform is monkeypatched, `ps` output is faked,
# and the plist/wrapper are asserted as rendered text. That proves the branch
# is taken and the content is right; it does not prove launchd loads it. A
# macOS operator must re-verify end to end.
# --------------------------------------------------------------------------- #


# ---- (a) the turn-verify gate asserts the READY sentinel ----
#
# The verdict matrix. The gate used to pass on ANY non-empty reply, so it
# certified `Error: No providers available` as "turn verified" -- three green
# signals over an engine with no brain.


def test_verify_verdict_ready_passes() -> None:
    result = service.judge_verify_reply("READY")
    assert result.ok
    assert "READY" in result.detail


def test_verify_verdict_no_providers_fails_naming_expected_vs_got() -> None:
    """THE defect. Non-empty is not a verdict; the sentinel is."""
    result = service.judge_verify_reply("Error: No providers available")
    assert not result.ok
    # expected-vs-got, both named, so the operator does not have to guess.
    assert "READY" in result.detail
    assert "No providers available" in result.detail


def test_verify_verdict_empty_fails() -> None:
    result = service.judge_verify_reply("   \n  ")
    assert not result.ok
    assert "EMPTY" in result.detail
    assert "READY" in result.detail


def test_verify_verdict_decorated_ready_passes() -> None:
    """The PINNED tolerance: markdown decoration around the sentinel passes.

    Chosen deliberately, following runner._URGENT_MARKER_RE (fix 2026-08-07),
    where an exact-literal match silently demoted a real `**URGENT: ...**`.
    A model told to answer READY may answer `**READY**`; failing that install
    would be a false FAIL with no defect behind it.
    """
    for decorated in ("**READY**", "`READY`", "- READY", "> READY", "READY.", "ready"):
        assert service.judge_verify_reply(decorated).ok, decorated


def test_verify_verdict_sentinel_buried_in_prose_fails() -> None:
    """The other half of the pin: tolerance is NOT a substring search.

    A substring search would pass every string below -- including the
    no-providers reply the gate exists to catch.
    """
    for prose in (
        "Error: No providers available. READY?",
        "Sure, READY",
        "I am READY to help. What would you like?",
        "The system is not READY",
    ):
        assert not service.judge_verify_reply(prose).ok, prose


def test_poll_turn_rejects_a_non_sentinel_reply(monkeypatch) -> None:
    """The matrix reaches the real poller, not just the pure judge."""
    monkeypatch.setattr(
        service,
        "_http_json",
        lambda *a, **k: (200, {"status": "done", "reply": "Error: No providers available"}),
    )
    result = service._poll_turn_to_terminal("http://x", "k", "t-1", timeout=5, poll=0.01)
    assert not result.ok
    assert "READY" in result.detail


def test_verify_turn_prompt_asks_for_the_sentinel_it_asserts() -> None:
    """The prompt and the gate must never drift apart."""
    assert service._VERIFY_SENTINEL in service._VERIFY_TURN_PROMPT


# ---- (b) launchd gets the provider key BY REFERENCE, via an exec wrapper ----


def _launchd_spec(**over: object) -> service.ServiceSpec:
    base: dict[str, object] = {
        "wrapper_path": "/home/u/.config/drumbeat/drumbeat-launchd-exec.sh",
        "env_file": "/home/u/.config/drumbeat/drumbeat.env",
        "env_path": "/opt/uv/bin:/usr/bin",
    }
    base.update(over)
    return _spec(**base)


def test_launchd_plist_invokes_the_wrapper_first_then_the_real_argv() -> None:
    args = service.extract_launchd_program_arguments(
        service.render_launchd_plist(_launchd_spec())
    )
    assert args is not None
    assert args[0] == "/home/u/.config/drumbeat/drumbeat-launchd-exec.sh"
    assert args[1] == _EXEC[0]
    assert "serve" in args


def test_launchd_plist_still_carries_port_and_host_for_status_recovery() -> None:
    """`service status` recovers --port/--host by reading the plist back.

    Hiding the argv inside the wrapper would have broken that silently, so
    the real invocation stays in ProgramArguments after the wrapper.
    """
    args = service.extract_launchd_program_arguments(
        service.render_launchd_plist(_launchd_spec(port=9137))
    )
    assert service.find_flag_value(args or [], "--port") == "9137"
    assert service.find_flag_value(args or [], "--host") == "127.0.0.1"


def test_launchd_plist_contains_no_secret_value() -> None:
    """The whole reason this is by-reference: the plist is world-readable and
    is REWRITTEN on every install. Only the env file's PATH may appear."""
    plist = service.render_launchd_plist(
        _launchd_spec(env_file="/home/u/.config/drumbeat/drumbeat.env")
    )
    assert "drumbeat.env" not in plist  # not even the env file is named here
    for secret_ish in ("ANTHROPIC", "API_KEY", "sk-ant"):
        assert secret_ish not in plist


def test_launchd_wrapper_sources_the_env_file_then_execs_the_real_argv() -> None:
    wrapper = service.render_launchd_exec_wrapper(_launchd_spec())
    assert wrapper.startswith("#!/bin/sh")
    # set -a / source / set +a: the systemd EnvironmentFile= semantics, by hand.
    assert "set -a" in wrapper
    assert "/home/u/.config/drumbeat/drumbeat.env" in wrapper
    assert "set +a" in wrapper
    # A missing env file is a no-op, exactly like systemd's leading `-`.
    assert "[ -f /home/u/.config/drumbeat/drumbeat.env ]" in wrapper
    # and then it gets out of the way -- launchd supervises ONE process.
    assert 'exec "$@"' in wrapper


def test_launchd_wrapper_fails_loud_when_handed_no_argv() -> None:
    """A hand-edited plist that drops the argv must not exec nothing quietly."""
    wrapper = service.render_launchd_exec_wrapper(_launchd_spec())
    assert 'if [ "$#" -eq 0 ]' in wrapper
    assert "exit 64" in wrapper


def test_launchd_wrapper_is_a_runnable_shell_script_that_exports(tmp_path) -> None:
    """Executed for real (sh is portable; this assertion is not macOS-only).

    Proves the three claims that matter: the env file's assignment reaches the
    exec'd process's ENVIRONMENT (not just the wrapper's shell), the real argv
    runs, and a missing env file is survivable.
    """
    import os
    import subprocess

    env_file = tmp_path / "drumbeat.env"
    env_file.write_text("DRUMBEAT_TEST_TOKEN=from-the-env-file\n", encoding="utf-8")
    wrapper = tmp_path / "exec.sh"
    wrapper.write_text(
        service.render_launchd_exec_wrapper(_launchd_spec(env_file=str(env_file))),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    proc = subprocess.run(
        [str(wrapper), "/bin/sh", "-c", 'printf %s "$DRUMBEAT_TEST_TOKEN"'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "from-the-env-file"

    # Missing env file: still runs. `-` semantics, honoured.
    env_file.unlink()
    proc = subprocess.run(
        [str(wrapper), "/bin/sh", "-c", "printf ok"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "ok"


def test_launchd_wrapper_regeneration_is_byte_identical() -> None:
    """The reporter's hand-written wrapper was fragile because `install`
    regenerated the plist and dropped it. Install now regenerates BOTH, so
    a second install must reproduce the same wrapper exactly."""
    spec = _launchd_spec()
    assert service.render_launchd_exec_wrapper(spec) == (
        service.render_launchd_exec_wrapper(spec)
    )
    assert service.render_launchd_plist(spec) == service.render_launchd_plist(spec)


def test_launchd_plist_without_a_wrapper_is_unchanged() -> None:
    """The pure generator stays behaviour-preserving for callers that supply
    no wrapper (the systemd-only and test paths)."""
    args = service.extract_launchd_program_arguments(
        service.render_launchd_plist(_spec())
    )
    assert args is not None
    assert args[0] == _EXEC[0]


def test_both_platforms_name_the_same_env_file() -> None:
    """One file, one semantics. Drift here is how macOS lost the key."""
    unit = service.render_systemd_unit(_spec(env_path="/x"))
    assert f"EnvironmentFile=-%h/{service.ENV_FILE_RELATIVE}" in unit
    assert str(service.env_file_path()).endswith(service.ENV_FILE_RELATIVE)
    wrapper = service.render_launchd_exec_wrapper(
        _launchd_spec(env_file=str(service.env_file_path()))
    )
    assert str(service.env_file_path()) in wrapper


def test_launchd_wrapper_path_sits_beside_the_env_file_not_in_launchagents() -> None:
    path = service.launchd_wrapper_path("drumbeat")
    assert path.parent == service.env_file_path().parent
    assert "LaunchAgents" not in str(path)


# ---- (c) the unit's PATH is honest about Homebrew on macOS ----


def test_resolve_service_path_on_darwin_includes_homebrew(monkeypatch) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    monkeypatch.setattr(service.sys, "executable", "/usr/bin/python")
    entries = service.resolve_service_path(system="Darwin").split(":")
    assert "/opt/homebrew/bin" in entries
    assert "/opt/homebrew/sbin" in entries


def test_resolve_service_path_on_linux_is_unchanged(monkeypatch) -> None:
    """The Darwin fix must not leak a nonexistent directory onto Linux units."""
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    monkeypatch.setattr(service.sys, "executable", "/usr/bin/python")
    entries = service.resolve_service_path(system="Linux").split(":")
    assert "/opt/homebrew/bin" not in entries
    assert "/opt/homebrew/sbin" not in entries
    # Exactly the pre-fix answer: the interpreter's own bin dir, then the
    # default, deduped -- no new entry, no reordering.
    expected = ["/usr/bin"] + [
        e for e in service._DEFAULT_SERVICE_PATH.split(":") if e != "/usr/bin"
    ]
    assert entries == expected


def test_resolve_service_path_defaults_to_this_platform(monkeypatch) -> None:
    """The branch is wired to the real platform, not only to the argument."""
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    monkeypatch.setattr(service.sys, "executable", "/usr/bin/python")
    monkeypatch.setattr(service.platform, "system", lambda: "Darwin")
    assert "/opt/homebrew/bin" in service.resolve_service_path().split(":")
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    assert "/opt/homebrew/bin" not in service.resolve_service_path().split(":")


def test_darwin_homebrew_entries_rank_below_the_resolved_tool_dirs(
    monkeypatch,
) -> None:
    """uv and the agent bin dir must still win -- Homebrew is a fallback, not
    an override of the interpreter this install actually runs from."""
    monkeypatch.setattr(
        service.shutil, "which", lambda name: "/home/u/.local/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(service.sys, "executable", "/home/u/.local/share/uv/tools/bin/python")
    entries = service.resolve_service_path(system="Darwin").split(":")
    assert entries.index("/home/u/.local/bin") < entries.index("/opt/homebrew/bin")
    # ... and above the systemd default, which names only the Intel prefix.
    assert entries.index("/opt/homebrew/bin") < entries.index("/usr/local/bin")
