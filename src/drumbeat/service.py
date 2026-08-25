"""``drumbeat service`` -- run the engine under the platform's own supervisor.

``drumbeat serve`` is the engine as a foreground process. Leaving it running is
a separate problem, and the honest answer to it is the OS service manager --
``systemd --user`` on Linux, ``launchd`` on macOS. This module generates the
correct unit/plist, installs it, and -- crucially -- **verifies the engine is
actually answering before it reports success**. An install that enabled a unit
and walked away would be free to report a healthy service that never bound its
port; the health probe is what makes that impossible.

The split here is deliberate: the file-*content* generators (``render_*``) and
the supervisor-*status* parsers are pure functions of their inputs, unit-tested
directly. The imperative ``install``/``status``/``uninstall`` drivers shell out
to ``systemctl``/``launchctl`` and are exercised live in a real environment.

Design notes carried over from the reference deployment (they are why the
generated systemd unit looks the way it does, not incidental):

- ``ExecStop`` is the drain, blocking, with a ``TimeoutStopSec`` longer than the
  drain's own timeout so the supervisor never ``SIGKILL``s a drain that is
  legitimately waiting on a real turn.
- ``KillMode=process`` -- signal only the main process. In-flight agent children
  must outlive the scheduler's stop, never be killed with it.
- ``ExecStartPre=`` clears any drain flag ``ExecStop`` set, with a ``-`` prefix so
  a clear that finds nothing is a no-op rather than a start failure. Putting the
  clear on the *start* path (not the stop path) means a crash-restart cannot
  strand the engine drained-but-healthy-looking.
- The provider key belongs in the unit's environment via ``EnvironmentFile=`` --
  a login shell's ``export`` is invisible to a service.
- ``AMPLIFIER_AGENT_WORKSPACE`` is never set: it silently re-buckets session ids
  under a slug no other launch path derives. ``WorkingDirectory`` is pinned to
  the workspace instead, so the service's cwd-derived slug matches a hand-run
  ``drumbeat serve`` from that directory.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape as xml_escape

from drumbeat import api_key as api_key_mod
from drumbeat.serve import DEFAULT_DATA_DIRNAME, DEFAULT_PORT, resolve_workspace

# The one supervised unit per host, by this fixed name. One engine per host is
# the common case; a second engine is a second workspace, a second data dir and
# a second port (docs/PLATFORMS.md section 1) -- run that one in the foreground
# or hand-adapt a copy of the generated unit under a distinct name.
SERVICE_LABEL = "drumbeat"

# How long to wait, after start, for the engine to answer /api/health before
# declaring the install failed. Startup does real work before it binds (lock,
# key, fingerprint, pack load, agent-binary check) so this is generous.
HEALTH_TIMEOUT_SECONDS = 30.0
HEALTH_POLL_SECONDS = 0.5

# Longer than the drain CLI's own default timeout (1800s), so the supervisor
# waits out a legitimate drain rather than killing it. See module docstring.
STOP_TIMEOUT_SECONDS = 2000

# The base PATH a supervised unit falls back to, matching systemd --user's own
# default. `uv` (the standard installer puts it in ~/.local/bin) and the
# co-installed amplifier-agent bin dir are prepended AHEAD of this at install
# time -- see ``resolve_service_path`` for why that is not optional.
_DEFAULT_SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# The turn-verify gate: after health passes, `service install` proves the
# supervised unit can execute ONE REAL turn (not merely answer /api/health)
# before reporting success. A real agent turn is seconds-to-minutes of work;
# this bound is generous so a slow first turn (cold caches, a real provider
# round-trip) is not mistaken for a broken unit.
TURN_VERIFY_TIMEOUT_SECONDS = 180.0
TURN_VERIFY_POLL_SECONDS = 1.0

# The name (=> slug ``drumbeat-install-check``) of the throwaway automation the
# turn-verify gate creates, runs once, and deletes. Deliberately not a name any
# consumer would choose, and ``enabled: false`` so the scheduler never touches
# it in the window it exists. Using a dedicated ephemeral automation -- rather
# than one of the consumer's -- is what keeps the check from resuming, rewriting,
# or wedging any REAL automation's pinned session (the compounding failure in
# this defect's own evidence: one PATH-caused failure left a real pin in an
# ambiguous state and wedged every subsequent run).
_VERIFY_AUTOMATION_NAME = "Drumbeat Install Check"
_VERIFY_TURN_PROMPT = (
    "Reply with the single word READY and nothing else. This turn exists only "
    "to confirm the supervised engine can execute one real agent turn end to end."
)
_VERIFY_AUTOMATION_CONTENT = f"""\
---
automation:
  name: {_VERIFY_AUTOMATION_NAME}
  # Created, run once, and deleted by `drumbeat service install`'s turn-verify
  # gate. enabled: false so the scheduler never fires it in the window it exists.
  enabled: false
  trigger:
    type: schedule
    expression: daily at 09:00
  # never: this check reads back the turn's own reply; nothing is ever delivered.
  notify: never
  requires: []
  steps:
    - id: health-check
      prompt: |-
        You are a one-shot install health check. Do exactly what the next turn
        asks and nothing more.
---
"""


# --------------------------------------------------------------------------- #
# The invocation to supervise
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServiceSpec:
    """Everything the file generators need, and nothing they do not.

    ``exec_argv`` is the absolute invocation of *this* ``drumbeat`` -- a single
    console-script path, or ``(python, -m, drumbeat.cli)`` when no script is on
    PATH. ``workspace``/``data_dir`` are absolute path strings (the supervisor
    has no cwd of yours to resolve a relative path against). ``data_dir`` of
    ``None`` means "let ``serve`` default it to ``<workspace>/runs``" and is
    then omitted from every generated command, so drain/clear/serve all agree.
    """

    exec_argv: tuple[str, ...]
    workspace: str
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"
    data_dir: str | None = None
    label: str = SERVICE_LABEL
    # The PATH to bake into the unit/plist, resolved at INSTALL time. A
    # supervised unit runs with the service manager's reduced PATH, which omits
    # ~/.local/bin -- where the standard installer puts ``uv``, which the engine
    # shells out to on every turn to activate its modules. Left unset, every
    # scheduled run fails at module activation while /api/health still answers
    # ok. ``None`` omits the directive (the pure generators stay behavior-
    # preserving for callers that do not supply it); every install path sets it
    # via ``resolve_service_path``. See that function.
    env_path: str | None = None
    # launchd only: where the supervised process's stdout/stderr are written.
    stdout_path: str | None = None
    stderr_path: str | None = None


def resolve_exec_argv() -> tuple[str, ...]:
    """The absolute invocation to bake into a unit/plist.

    Prefer the installed ``drumbeat`` console script (a service manager needs an
    absolute ``ExecStart``; a bare name is not resolvable in its reduced PATH).
    Fall back to running the module through the current interpreter, which is
    also absolute.
    """
    found = shutil.which("drumbeat")
    if found:
        return (str(Path(found).resolve()),)
    return (str(Path(sys.executable).resolve()), "-m", "drumbeat.cli")


def resolve_service_path(
    *,
    exec_argv: tuple[str, ...] | None = None,
    default_path: str = _DEFAULT_SERVICE_PATH,
) -> str:
    """The ``PATH`` to bake into a unit/plist, resolved AT INSTALL TIME.

    A supervised unit runs with the service manager's own reduced PATH -- which
    does NOT include ``~/.local/bin``, where the standard ``uv`` installer puts
    ``uv``. The engine shells out to ``uv`` on every turn to activate its
    modules, so a unit that inherits systemd's default PATH fails EVERY
    scheduled run at module activation (``uv is not installed``) while
    ``/api/health`` still answers ``ok`` -- the exact silent failure this
    prepends past.

    The fix is to capture the answer HERE, in the shell running the install
    (which does have ``uv`` on PATH), not to leave it to the unit's environment.
    Prepended, in order, ahead of a sane default:

    1. the directory of ``uv`` (``shutil.which("uv")`` resolved now); and
    2. the directory holding the co-installed ``amplifier-agent`` (a sibling of
       this interpreter -- the tool venv's ``bin/``); and
    3. the directory of the ``drumbeat`` console script being installed.

    Duplicates are collapsed preserving first-seen order. If ``uv`` cannot be
    found on the installer's PATH the unit is still given (2) and (3) plus the
    default -- an honest best effort rather than a silent omission.
    """
    leading: list[str] = []

    uv_found = shutil.which("uv")
    if uv_found:
        leading.append(str(Path(uv_found).resolve().parent))

    # The co-installed amplifier-agent lives beside this interpreter (the tool
    # venv bin); that same directory is what the engine resolves the agent from
    # by locus. Put it on PATH so any sibling binary a turn shells out to
    # resolves too.
    leading.append(str(Path(sys.executable).resolve().parent))

    if exec_argv:
        leading.append(str(Path(exec_argv[0]).resolve().parent))

    ordered: list[str] = []
    for entry in [*leading, *default_path.split(":")]:
        if entry and entry not in ordered:
            ordered.append(entry)
    return ":".join(ordered)


def _fmt_env_assignment(name: str, value: str) -> str:
    """One ``Environment=`` directive, quoted only when the value needs it.

    systemd parses ``Environment="NAME=value with space"`` -- so a PATH entry
    containing whitespace (a home dir with a space) survives as one assignment.
    """
    assignment = f"{name}={value}"
    if value == "" or any(ch in value for ch in ' \t"\\'):
        escaped = assignment.replace("\\", "\\\\").replace('"', '\\"')
        return f'Environment="{escaped}"'
    return f"Environment={assignment}"


def _serve_tokens(spec: ServiceSpec) -> list[str]:
    tokens = ["serve", "--workspace", spec.workspace]
    if spec.data_dir:
        tokens += ["--data-dir", spec.data_dir]
    tokens += ["--port", str(spec.port), "--host", spec.host]
    return tokens


def _drain_tokens(spec: ServiceSpec, *extra: str) -> list[str]:
    tokens = ["drain", "--workspace", spec.workspace]
    if spec.data_dir:
        tokens += ["--data-dir", spec.data_dir]
    tokens += list(extra)
    return tokens


def _fmt_exec_line(tokens: list[str]) -> str:
    """Join argv into one ExecXxx= line, double-quoting tokens that need it.

    systemd parses double quotes itself, so a workspace path with a space -- or
    the multi-word drain reason -- survives as one argument.
    """
    out: list[str] = []
    for token in tokens:
        if token == "" or any(ch in token for ch in ' \t"\\'):
            out.append('"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"')
        else:
            out.append(token)
    return " ".join(out)


# --------------------------------------------------------------------------- #
# systemd --user unit generation
# --------------------------------------------------------------------------- #


def render_systemd_unit(spec: ServiceSpec) -> str:
    """The ``.service`` file content for ``systemd --user``.

    Faithful to the reference-deployment notes in this module's docstring:
    drain-on-stop, clear-on-start, ``KillMode=process``, a stop timeout that
    outlasts the drain, and an optional env file for the provider key.
    """
    exec_prefix = list(spec.exec_argv)
    start_line = _fmt_exec_line(exec_prefix + _serve_tokens(spec))
    stop_line = _fmt_exec_line(
        exec_prefix
        + _drain_tokens(spec, "--reason", "systemd stop (drumbeat service)", "--wait")
    )
    pre_line = _fmt_exec_line(exec_prefix + _drain_tokens(spec, "--clear"))

    lines = [
        "# Generated by `drumbeat service install`. Do not hand-edit -- re-run",
        "# the command to regenerate it. `drumbeat service uninstall` removes it.",
        "",
        "[Unit]",
        f"Description=drumbeat automation engine (serve) for {spec.workspace}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={spec.workspace}",
        # The base PATH, baked in at install time. Without it the unit inherits
        # systemd's default PATH (no ~/.local/bin), the engine cannot find `uv`
        # to activate its modules, and EVERY scheduled run fails while
        # /api/health still answers ok. Set BEFORE EnvironmentFile so the
        # operator's env file can still override PATH if they must. See
        # resolve_service_path. Omitted only when no env_path was resolved.
        *([_fmt_env_assignment("PATH", spec.env_path)] if spec.env_path else []),
        # Optional: the provider key (and any pack config) lives here, not in a
        # login shell. `-` => a missing file is not a start failure.
        "EnvironmentFile=-%h/.config/drumbeat/drumbeat.env",
        # Clear a drain a previous ExecStop set, on the START path so a
        # crash-restart cannot strand the engine drained. `-` => no-op if absent.
        f"ExecStartPre=-{pre_line}",
        f"ExecStart={start_line}",
        # Graceful stop == drain, blocking until in-flight turns finish.
        f"ExecStop={stop_line}",
        "Restart=on-failure",
        "RestartSec=5",
        # Signal only the scheduler; never its in-flight agent children.
        "KillMode=process",
        f"TimeoutStopSec={STOP_TIMEOUT_SECONDS}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def extract_systemd_exec_start(unit_text: str) -> list[str] | None:
    """The ExecStart invocation, tokenized, from an installed unit -- or None.

    Lets ``status``/``uninstall`` recover the port/host a unit was installed
    with by reading the file itself, rather than making the operator re-supply
    flags that must match exactly.
    """
    for raw in unit_text.splitlines():
        line = raw.strip()
        if line.startswith("ExecStart=") and not line.startswith("ExecStartPre="):
            value = line[len("ExecStart=") :].strip()
            try:
                return shlex.split(value)
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# launchd plist generation
# --------------------------------------------------------------------------- #


def render_launchd_plist(spec: ServiceSpec) -> str:
    """The LaunchAgent ``.plist`` content for macOS ``launchd``.

    ``RunAtLoad`` starts it now and at login; ``KeepAlive`` restarts it only on
    a non-clean exit (the launchd analogue of ``Restart=on-failure``). launchd
    has no ExecStop hook, so a graceful drain-on-stop is systemd-only; ``stop``
    on macOS is a plain SIGTERM.
    """
    program_args = list(spec.exec_argv) + _serve_tokens(spec)
    args_xml = "\n".join(
        f"        <string>{xml_escape(arg)}</string>" for arg in program_args
    )
    log_keys = ""
    if spec.stdout_path:
        log_keys += (
            "    <key>StandardOutPath</key>\n"
            f"    <string>{xml_escape(spec.stdout_path)}</string>\n"
        )
    if spec.stderr_path:
        log_keys += (
            "    <key>StandardErrorPath</key>\n"
            f"    <string>{xml_escape(spec.stderr_path)}</string>\n"
        )
    # The same PATH gap systemd has: a LaunchAgent inherits launchd's minimal
    # PATH, not the login shell's, so ``uv`` (in ~/.local/bin) is invisible and
    # every turn fails at module activation. Bake it in via EnvironmentVariables.
    # See resolve_service_path.
    env_keys = ""
    if spec.env_path:
        env_keys = (
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            "        <key>PATH</key>\n"
            f"        <string>{xml_escape(spec.env_path)}</string>\n"
            "    </dict>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Generated by `drumbeat service install`. Do not hand-edit; re-run\n"
        "     the command to regenerate it. `drumbeat service uninstall` removes it. -->\n"
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{xml_escape(spec.label)}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>WorkingDirectory</key>\n"
        f"    <string>{xml_escape(spec.workspace)}</string>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <dict>\n"
        "        <key>SuccessfulExit</key>\n"
        "        <false/>\n"
        "    </dict>\n"
        f"{env_keys}"
        f"{log_keys}"
        "</dict>\n"
        "</plist>\n"
    )


def extract_launchd_program_arguments(plist_text: str) -> list[str] | None:
    """``ProgramArguments`` from an installed plist -- or None if unparseable."""
    try:
        data = plistlib.loads(plist_text.encode("utf-8"))
    except (plistlib.InvalidFileException, ExpatError, ValueError):
        return None
    args = data.get("ProgramArguments")
    if isinstance(args, list) and all(isinstance(a, str) for a in args):
        return list(args)
    return None


def find_flag_value(tokens: list[str], flag: str) -> str | None:
    """Value of ``--flag VALUE`` (or ``--flag=VALUE``) in an argv token list."""
    for i, token in enumerate(tokens):
        if token == flag:
            if i + 1 < len(tokens):
                return tokens[i + 1]
            return None
        if token.startswith(flag + "="):
            return token[len(flag) + 1 :]
    return None


# --------------------------------------------------------------------------- #
# Supervisor status parsing (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServiceStatus:
    """The distilled verdict from ``systemctl --user show``."""

    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    main_pid: int | None

    @property
    def loaded(self) -> bool:
        return self.load_state == "loaded"

    @property
    def active(self) -> bool:
        return self.active_state == "active"


def parse_systemctl_show(output: str) -> ServiceStatus:
    """Parse ``systemctl --user show <unit> --property=...`` key=value output.

    A not-found unit reports ``LoadState=not-found`` / ``ActiveState=inactive``
    -- so this same parser distinguishes running, stopped, and never-installed.
    """
    fields: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    pid_raw = fields.get("MainPID", "0").strip()
    try:
        pid = int(pid_raw)
    except ValueError:
        pid = 0
    return ServiceStatus(
        load_state=fields.get("LoadState", ""),
        active_state=fields.get("ActiveState", ""),
        sub_state=fields.get("SubState", ""),
        unit_file_state=fields.get("UnitFileState", ""),
        main_pid=pid or None,
    )


@dataclass(frozen=True)
class LaunchdStatus:
    """The distilled verdict from ``launchctl list <label>``."""

    loaded: bool
    pid: int | None
    last_exit_status: int | None

    @property
    def active(self) -> bool:
        return self.pid is not None


_LAUNCHCTL_FIELD = re.compile(r'"?(PID|LastExitStatus)"?\s*=\s*(-?\d+)')


def parse_launchctl_list(output: str) -> LaunchdStatus:
    """Parse ``launchctl list <label>`` output into a status.

    A loaded-but-idle agent has a ``LastExitStatus`` and no ``PID``; a running
    one has both. ``launchctl`` prints ``Could not find service`` (and exits
    non-zero) when the label is not loaded at all.
    """
    if not output.strip() or "could not find" in output.lower():
        return LaunchdStatus(loaded=False, pid=None, last_exit_status=None)
    pid: int | None = None
    last_exit: int | None = None
    for raw in output.splitlines():
        match = _LAUNCHCTL_FIELD.search(raw)
        if not match:
            continue
        if match.group(1) == "PID":
            pid = int(match.group(2))
        else:
            last_exit = int(match.group(2))
    return LaunchdStatus(loaded=True, pid=pid, last_exit_status=last_exit)


# --------------------------------------------------------------------------- #
# Health probe
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    detail: str


def interpret_health(status_code: int | None, payload: object) -> HealthResult:
    """Turn a raw /api/health response into a pass/fail verdict.

    Healthy means: HTTP 200, ``status == "ok"``, and the scheduler holds its
    lock. "Up but lockless" is the silent double-fire hazard the lock exists to
    prevent -- an install that accepted it would be verifying the wrong thing.
    """
    if status_code != 200:
        return HealthResult(False, f"HTTP {status_code} from /api/health")
    if not isinstance(payload, dict):
        return HealthResult(False, "health response was not a JSON object")
    if payload.get("status") != "ok":
        return HealthResult(False, f"status={payload.get('status')!r} (expected 'ok')")
    lock = payload.get("scheduler_lock")
    if lock != "held":
        return HealthResult(False, f"scheduler_lock={lock!r} (expected 'held')")
    scheduling = (payload.get("scheduler") or {}).get("scheduling")
    return HealthResult(True, f"ok (scheduler {scheduling})")


def probe_health_once(host: str, port: int, timeout: float = 2.0) -> HealthResult:
    """One GET of ``/api/health``. Loopback read -- no API key required."""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        body = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return HealthResult(False, f"cannot reach {url}: {exc}")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        payload = None
    return interpret_health(code, payload)


def wait_for_health(
    host: str,
    port: int,
    *,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
    poll: float = HEALTH_POLL_SECONDS,
) -> HealthResult:
    """Poll ``/api/health`` until it passes or the deadline expires."""
    deadline = time.monotonic() + timeout
    result = HealthResult(False, "health never probed")
    while time.monotonic() < deadline:
        result = probe_health_once(host, port)
        if result.ok:
            return result
        time.sleep(poll)
    return result


# --------------------------------------------------------------------------- #
# The turn-verify gate (does the supervised unit actually run a turn?)
# --------------------------------------------------------------------------- #
#
# /api/health proves the HTTP face is up and the scheduler holds its lock. It
# does NOT prove a scheduled run would succeed: this defect shipped a unit that
# answered health "ok" while EVERY run failed at module activation, because the
# unit's PATH omitted `uv`. So after health passes, install runs one REAL turn
# through the running unit and gates success on it. The turn executes inside the
# supervised process -- with its baked PATH and its EnvironmentFile provider key
# -- so it exercises exactly the environment a 03:40 scheduled run will have.
#
# The turn is run against a dedicated, ephemeral automation this gate creates
# and deletes, NOT one of the consumer's. That is deliberate: routing a check
# turn through a real automation would resume, rewrite, or (on failure) wedge
# its pinned session -- the precise compounding failure in this defect's own
# evidence. A throwaway slug, enabled: false, notify: never, touches no real
# state.


@dataclass(frozen=True)
class TurnVerifyResult:
    ok: bool
    detail: str


def runs_dir_for(spec: ServiceSpec) -> Path:
    """Where this spec's engine keeps its state -- to find the API key file.

    Mirrors ``serve``'s own default (``<workspace>/runs``) so the two always
    agree; ``spec.workspace`` is already the resolved absolute path.
    """
    if spec.data_dir:
        return Path(spec.data_dir)
    return Path(spec.workspace) / DEFAULT_DATA_DIRNAME


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: dict[str, object] | None = None,
    timeout: float = 5.0,
) -> tuple[int | None, object]:
    """One JSON request/response. Returns ``(status_code_or_None, payload)``.

    A transport failure (host unreachable, timeout) returns ``(None, message)``.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            code = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"cannot reach {url}: {exc}"
    try:
        return code, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return code, raw.decode("utf-8", "replace")


def _read_engine_api_key(runs_dir: Path) -> str:
    """This instance's API key, as ``serve`` already minted it. Never mints one.

    ``serve`` writes the key at startup, so by the time install reaches the
    turn-verify gate the file exists. Read it -- do not call ``ensure_api_key``,
    which would MINT one, masking a genuinely-missing-key fault as success.
    """
    key_path = api_key_mod.api_key_path(runs_dir)
    key = key_path.read_text(encoding="utf-8").strip()
    if not key:
        raise OSError(f"engine API key file is empty: {key_path}")
    return key


def _poll_turn_to_terminal(
    base_url: str,
    api_key: str,
    turn_id: str,
    *,
    timeout: float,
    poll: float,
) -> TurnVerifyResult:
    """Poll ``GET /api/turns/{turn_id}`` until done/failed or the deadline."""
    deadline = time.monotonic() + timeout
    last = "no status observed yet"
    while time.monotonic() < deadline:
        code, payload = _http_json(
            "GET", f"{base_url}/api/turns/{turn_id}", api_key=api_key
        )
        if code == 200 and isinstance(payload, dict):
            status = payload.get("status")
            if status == "done":
                reply = payload.get("reply")
                if isinstance(reply, str) and reply.strip():
                    snippet = reply.strip().splitlines()[0][:60]
                    return TurnVerifyResult(
                        True, f"one real turn completed (agent replied {snippet!r})"
                    )
                return TurnVerifyResult(
                    False, "the turn finished but produced an empty reply"
                )
            if status == "failed":
                return TurnVerifyResult(
                    False, f"the turn failed: {payload.get('error')}"
                )
            last = f"status={status!r}"
        elif code is None:
            last = str(payload)
        time.sleep(poll)
    return TurnVerifyResult(
        False, f"the turn did not finish within {timeout:.0f}s (last seen: {last})"
    )


def verify_one_real_turn(
    spec: ServiceSpec,
    *,
    timeout: float = TURN_VERIFY_TIMEOUT_SECONDS,
    poll: float = TURN_VERIFY_POLL_SECONDS,
) -> TurnVerifyResult:
    """Run ONE real turn through the running unit; True iff it succeeds.

    Creates a throwaway automation, submits one turn against it via the engine's
    own HTTP API (so the turn runs inside the SUPERVISED process, with its baked
    PATH and provider key), polls to a terminal state, and deletes the throwaway
    -- always, even on failure. Never raises: every failure becomes a
    ``TurnVerifyResult(False, ...)`` whose detail is safe to show an operator.
    """
    runs_dir = runs_dir_for(spec)
    try:
        api_key = _read_engine_api_key(runs_dir)
    except OSError as exc:
        return TurnVerifyResult(
            False,
            f"could not read the engine API key ({exc}) -- cannot submit a turn",
        )

    base_url = f"http://{spec.host}:{spec.port}"
    automations_dir = Path(spec.workspace) / "automations"

    # Create the throwaway automation. A leftover from a previous interrupted
    # verify (same fixed slug) would 409; clear it and retry once rather than
    # fail on our own residue.
    code, payload = _http_json(
        "POST",
        f"{base_url}/api/automations",
        api_key=api_key,
        body={"content": _VERIFY_AUTOMATION_CONTENT},
    )
    if code == 409 and isinstance(payload, dict):
        stale = payload.get("slug") or _slug_from_error(payload.get("error"))
        if stale:
            _http_json("DELETE", f"{base_url}/api/automations/{stale}", api_key=api_key)
        code, payload = _http_json(
            "POST",
            f"{base_url}/api/automations",
            api_key=api_key,
            body={"content": _VERIFY_AUTOMATION_CONTENT},
        )
    if code != 201 or not isinstance(payload, dict) or not payload.get("slug"):
        return TurnVerifyResult(
            False,
            "could not create the throwaway verification automation "
            f"(HTTP {code}): {payload}",
        )
    slug = str(payload["slug"])

    try:
        code, payload = _http_json(
            "POST",
            f"{base_url}/api/turns",
            api_key=api_key,
            body={
                "automation_slug": slug,
                "origin": "manual",
                "text": _VERIFY_TURN_PROMPT,
                "lock_wait_seconds": 30,
                "ceiling_seconds": timeout,
            },
        )
        if code != 202 or not isinstance(payload, dict) or not payload.get("turn_id"):
            return TurnVerifyResult(
                False,
                f"the engine refused the verification turn (HTTP {code}): {payload}",
            )
        return _poll_turn_to_terminal(
            base_url, api_key, str(payload["turn_id"]), timeout=timeout, poll=poll
        )
    finally:
        _http_json("DELETE", f"{base_url}/api/automations/{slug}", api_key=api_key)
        # delete_automation copies the file to <name>.bak before unlinking;
        # remove that residue too so the check leaves nothing behind.
        bak = automations_dir / f"{slug}.md.bak"
        try:
            bak.unlink(missing_ok=True)
        except OSError:
            pass


def _slug_from_error(message: object) -> str | None:
    """Best-effort slug from a 409 body like ``... slug 'x' already exists ...``."""
    if not isinstance(message, str):
        return None
    match = re.search(r"slug '([^']+)'", message)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# Filesystem locations
# --------------------------------------------------------------------------- #


def systemd_unit_path(label: str = SERVICE_LABEL) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user" / f"{label}.service"


def launchd_plist_path(label: str = SERVICE_LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


# --------------------------------------------------------------------------- #
# Imperative drivers
# --------------------------------------------------------------------------- #


def _print(message: str) -> None:
    print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _current_os() -> str:
    return platform.system()


def install(
    *,
    workspace: str,
    port: int,
    host: str,
    data_dir: str | None,
    skip_turn_verify: bool = False,
) -> int:
    """Generate, install, start and VERIFY the service. Never reports success
    it has not confirmed -- first with a health probe, then (unless
    ``skip_turn_verify``) by executing one real turn through the running unit."""
    system = _current_os()
    if system == "Linux":
        return _install_systemd(
            workspace=workspace,
            port=port,
            host=host,
            data_dir=data_dir,
            skip_turn_verify=skip_turn_verify,
        )
    if system == "Darwin":
        return _install_launchd(
            workspace=workspace,
            port=port,
            host=host,
            data_dir=data_dir,
            skip_turn_verify=skip_turn_verify,
        )
    _err(
        f"error: `drumbeat service` supports Linux (systemd --user) and macOS "
        f"(launchd); this host is {system!r}. Any process supervisor works -- "
        f"point it at: {' '.join(resolve_exec_argv())} serve --workspace "
        f"{workspace} --port {port}."
    )
    return 2


def _spec_for(
    *, workspace: str, port: int, host: str, data_dir: str | None
) -> ServiceSpec:
    # Validate + absolutize exactly as `serve` will, so a bad workspace fails
    # here (before a unit is written) with serve's own error text.
    ctx = resolve_workspace(
        Path(workspace), data_dir=Path(data_dir) if data_dir else None
    )
    exec_argv = resolve_exec_argv()
    return ServiceSpec(
        exec_argv=exec_argv,
        workspace=str(ctx.cwd),
        port=port,
        host=host,
        data_dir=str(ctx.runs_dir) if data_dir else None,
        env_path=resolve_service_path(exec_argv=exec_argv),
    )


def _install_systemd(
    *,
    workspace: str,
    port: int,
    host: str,
    data_dir: str | None,
    skip_turn_verify: bool = False,
) -> int:
    spec = _spec_for(workspace=workspace, port=port, host=host, data_dir=data_dir)
    unit_path = systemd_unit_path(spec.label)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(render_systemd_unit(spec), encoding="utf-8")
    _print(f"wrote unit: {unit_path}")

    for step in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", f"{spec.label}.service"],
        ["systemctl", "--user", "restart", f"{spec.label}.service"],
    ):
        result = _run(step)
        if result.returncode != 0:
            _err(f"error: `{' '.join(step)}` failed:\n{result.stderr.strip()}")
            return 1
        _print(f"ran: {' '.join(step)}")

    _print(f"verifying health on http://{spec.host}:{spec.port}/api/health ...")
    health = wait_for_health(spec.host, spec.port)
    if not health.ok:
        _err(
            "error: service started but did not become healthy: "
            f"{health.detail}\n"
            f"  inspect: systemctl --user status {spec.label}.service\n"
            f"  logs:    journalctl --user -u {spec.label}.service -n 80 --no-pager"
        )
        return 1
    _print(f"health verified: {health.detail}")

    log_hint = f"journalctl --user -u {spec.label}.service -n 80 --no-pager"
    if not _verify_turn_or_report(spec, skip=skip_turn_verify, log_hint=log_hint):
        return 1

    _print_linger_note()
    _print(
        f"installed and running: {spec.label}.service "
        f"(workspace {spec.workspace}, port {spec.port})"
    )
    return 0


def _verify_turn_or_report(spec: ServiceSpec, *, skip: bool, log_hint: str) -> bool:
    """Run the turn-verify gate and print its verdict. True => proceed.

    A skipped gate prints an explicit, NAMED notice (never a silent pass): the
    whole point of this defect is that a unit can look healthy and still fail
    every run, so \"I did not check\" must be visible.
    """
    if skip:
        _print(
            "turn verification SKIPPED (--skip-turn-verify): the unit serves "
            "/api/health but this did NOT confirm it can execute a real turn. "
            "A scheduled run may still fail (e.g. missing provider key, or a "
            "tool not on the unit's PATH). Trigger one run and check its result."
        )
        return True
    _print(
        "verifying one real turn (runs a real agent turn through the unit; "
        f"up to {TURN_VERIFY_TIMEOUT_SECONDS:.0f}s) ..."
    )
    result = verify_one_real_turn(spec)
    if result.ok:
        _print(f"turn verified: {result.detail}")
        return True
    _err(
        "error: the service is up but could not execute one real turn: "
        f"{result.detail}\n"
        "  This is the failure /api/health cannot see -- the unit serves its "
        "port, but a scheduled run would fail the same way. Common causes:\n"
        "    - the provider key is missing from the unit's EnvironmentFile "
        "(~/.config/drumbeat/drumbeat.env)\n"
        "    - a tool the engine shells out to (e.g. uv) is not on the unit's PATH\n"
        f"  logs: {log_hint}\n"
        "  Once fixed, re-run `drumbeat service install`. To install without "
        "this check (not recommended), pass --skip-turn-verify."
    )
    return False


def _print_linger_note() -> None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    lingering = _linger_enabled(user)
    if lingering is True:
        _print("linger: enabled -- the service survives logout and reboot.")
        return
    hint_user = user or "$USER"
    if lingering is False:
        _print(
            "linger: NOT enabled -- systemd --user services stop when your last "
            "session ends. To keep the engine running across logout/reboot:\n"
            f"  loginctl enable-linger {hint_user}"
        )
    else:
        _print(
            "linger: unknown -- if the engine should survive logout/reboot, "
            f"enable it:\n  loginctl enable-linger {hint_user}"
        )


def _linger_enabled(user: str) -> bool | None:
    if not user:
        return None
    result = _run(["loginctl", "show-user", user, "--property=Linger", "--value"])
    if result.returncode != 0:
        return None
    answer = result.stdout.strip().lower()
    if answer in {"yes", "true", "1"}:
        return True
    if answer in {"no", "false", "0"}:
        return False
    return None


def _install_launchd(
    *,
    workspace: str,
    port: int,
    host: str,
    data_dir: str | None,
    skip_turn_verify: bool = False,
) -> int:
    base = _spec_for(workspace=workspace, port=port, host=host, data_dir=data_dir)
    log_dir = Path.home() / "Library" / "Logs" / "drumbeat"
    log_dir.mkdir(parents=True, exist_ok=True)
    spec = ServiceSpec(
        exec_argv=base.exec_argv,
        workspace=base.workspace,
        port=base.port,
        host=base.host,
        data_dir=base.data_dir,
        label=base.label,
        env_path=base.env_path,
        stdout_path=str(log_dir / "serve.out.log"),
        stderr_path=str(log_dir / "serve.err.log"),
    )
    plist_path = launchd_plist_path(spec.label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_launchd_plist(spec), encoding="utf-8")
    _print(f"wrote plist: {plist_path}")

    # Reload cleanly: an unload of an absent agent is a harmless no-op.
    _run(["launchctl", "unload", str(plist_path)])
    load = _run(["launchctl", "load", "-w", str(plist_path)])
    if load.returncode != 0:
        _err(f"error: `launchctl load` failed:\n{load.stderr.strip()}")
        return 1
    _print(f"ran: launchctl load -w {plist_path}")

    _print(f"verifying health on http://{spec.host}:{spec.port}/api/health ...")
    health = wait_for_health(spec.host, spec.port)
    if not health.ok:
        _err(
            "error: service loaded but did not become healthy: "
            f"{health.detail}\n"
            f"  inspect: launchctl list {spec.label}\n"
            f"  logs:    {spec.stderr_path}"
        )
        return 1
    _print(f"health verified: {health.detail}")

    log_hint = str(spec.stderr_path)
    if not _verify_turn_or_report(spec, skip=skip_turn_verify, log_hint=log_hint):
        return 1

    _print(
        f"installed and running: {spec.label} "
        f"(workspace {spec.workspace}, port {spec.port})"
    )
    return 0


def status() -> int:
    system = _current_os()
    if system == "Linux":
        return _status_systemd()
    if system == "Darwin":
        return _status_launchd()
    _err(
        f"error: `drumbeat service` supports Linux and macOS; this host is {system!r}."
    )
    return 2


def _recover_port_host_systemd(unit_text: str) -> tuple[int, str]:
    tokens = extract_systemd_exec_start(unit_text) or []
    port_raw = find_flag_value(tokens, "--port")
    host = find_flag_value(tokens, "--host") or "127.0.0.1"
    try:
        port = int(port_raw) if port_raw else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return port, host


def _status_systemd() -> int:
    unit_path = systemd_unit_path()
    if not unit_path.is_file():
        _print(f"not installed: no unit at {unit_path}")
        return 1
    port, host = _recover_port_host_systemd(unit_path.read_text(encoding="utf-8"))

    show = _run(
        [
            "systemctl",
            "--user",
            "show",
            f"{SERVICE_LABEL}.service",
            "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID",
        ]
    )
    svc = parse_systemctl_show(show.stdout)
    _print(f"unit:        {unit_path}")
    _print(f"load state:  {svc.load_state}")
    _print(f"active:      {svc.active_state} ({svc.sub_state})")
    _print(f"enabled:     {svc.unit_file_state}")
    if svc.main_pid:
        _print(f"main pid:    {svc.main_pid}")

    health = probe_health_once(host, port)
    _print(f"health:      {'HEALTHY' if health.ok else 'UNHEALTHY'} -- {health.detail}")
    return 0 if (svc.active and health.ok) else 1


def _status_launchd() -> int:
    plist_path = launchd_plist_path()
    if not plist_path.is_file():
        _print(f"not installed: no plist at {plist_path}")
        return 1
    tokens = extract_launchd_program_arguments(plist_path.read_text(encoding="utf-8"))
    tokens = tokens or []
    port_raw = find_flag_value(tokens, "--port")
    host = find_flag_value(tokens, "--host") or "127.0.0.1"
    try:
        port = int(port_raw) if port_raw else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT

    listing = _run(["launchctl", "list", SERVICE_LABEL])
    combined = listing.stdout if listing.returncode == 0 else listing.stderr
    agent = parse_launchctl_list(combined)
    _print(f"plist:       {plist_path}")
    _print(f"loaded:      {'yes' if agent.loaded else 'no'}")
    if agent.pid:
        _print(f"pid:         {agent.pid}")
    if agent.last_exit_status is not None:
        _print(f"last exit:   {agent.last_exit_status}")

    health = probe_health_once(host, port)
    _print(f"health:      {'HEALTHY' if health.ok else 'UNHEALTHY'} -- {health.detail}")
    return 0 if (agent.active and health.ok) else 1


def uninstall() -> int:
    system = _current_os()
    if system == "Linux":
        return _uninstall_systemd()
    if system == "Darwin":
        return _uninstall_launchd()
    _err(
        f"error: `drumbeat service` supports Linux and macOS; this host is {system!r}."
    )
    return 2


def _uninstall_systemd() -> int:
    unit_path = systemd_unit_path()
    if not unit_path.is_file():
        _print(f"nothing to uninstall: no unit at {unit_path}")
        return 0

    # Stop (triggers the drain-on-stop ExecStop) then disable, both tolerant of
    # an already-stopped/already-disabled unit.
    for step in (
        ["systemctl", "--user", "stop", f"{SERVICE_LABEL}.service"],
        ["systemctl", "--user", "disable", f"{SERVICE_LABEL}.service"],
    ):
        result = _run(step)
        _print(f"ran: {' '.join(step)}")
        if result.returncode != 0 and result.stderr.strip():
            _print(f"  (note: {result.stderr.strip()})")

    unit_path.unlink()
    _print(f"removed unit: {unit_path}")
    _run(["systemctl", "--user", "daemon-reload"])
    _print("ran: systemctl --user daemon-reload")

    # Verify the removal actually took: file gone AND the unit no longer loaded.
    show = _run(
        [
            "systemctl",
            "--user",
            "show",
            f"{SERVICE_LABEL}.service",
            "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID",
        ]
    )
    svc = parse_systemctl_show(show.stdout)
    if unit_path.exists() or svc.active:
        _err(
            "error: uninstall did not fully take -- "
            f"file_exists={unit_path.exists()}, active_state={svc.active_state}"
        )
        return 1
    _print("uninstalled cleanly.")
    return 0


def _uninstall_launchd() -> int:
    plist_path = launchd_plist_path()
    if not plist_path.is_file():
        _print(f"nothing to uninstall: no plist at {plist_path}")
        return 0
    unload = _run(["launchctl", "unload", "-w", str(plist_path)])
    _print(f"ran: launchctl unload -w {plist_path}")
    if unload.returncode != 0 and unload.stderr.strip():
        _print(f"  (note: {unload.stderr.strip()})")
    plist_path.unlink()
    _print(f"removed plist: {plist_path}")

    listing = _run(["launchctl", "list", SERVICE_LABEL])
    combined = listing.stdout if listing.returncode == 0 else listing.stderr
    agent = parse_launchctl_list(combined)
    if plist_path.exists() or agent.loaded:
        _err(
            "error: uninstall did not fully take -- "
            f"file_exists={plist_path.exists()}, loaded={agent.loaded}"
        )
        return 1
    _print("uninstalled cleanly.")
    return 0


__all__ = [
    "DEFAULT_PORT",
    "HEALTH_TIMEOUT_SECONDS",
    "SERVICE_LABEL",
    "TURN_VERIFY_TIMEOUT_SECONDS",
    "HealthResult",
    "LaunchdStatus",
    "ServiceSpec",
    "ServiceStatus",
    "TurnVerifyResult",
    "extract_launchd_program_arguments",
    "extract_systemd_exec_start",
    "find_flag_value",
    "install",
    "interpret_health",
    "parse_launchctl_list",
    "parse_systemctl_show",
    "probe_health_once",
    "render_launchd_plist",
    "render_systemd_unit",
    "resolve_exec_argv",
    "resolve_service_path",
    "runs_dir_for",
    "status",
    "uninstall",
    "verify_one_real_turn",
    "wait_for_health",
]
