"""Stale-process guard: detect when a long-running service is executing code
that no longer matches what is on disk.

The bug this exists to close (in the consumer that extracted this engine):
long-running services are started with ``setsid``, survive terminal death,
and **never reload code**. Editing a ``.py`` file changes nothing until the
specific process holding it in memory is restarted. This reached a real
phone twice before this guard existed.

DETECTION CHOICE: content hash of the actual imported module closure,
captured once at process startup, not a bare mtime comparison — a content
hash never fires on a git-touch-with-no-real-change. The closure is computed
by a fresh SUBPROCESS that imports *only* the named entry module, so the
fingerprint reflects only what that one entry point actually pulls in —
never modules present in the parent's ``sys.modules`` as a CLI-dispatch side
effect (that contamination is failure mode 2: attributing another service's
edits to this one).

ENGINE/CONSUMER SPLIT (decomposition step 2; see docs/ARCHITECTURE.md): this
module is the domain-free
MECHANISM. It does not know which services exist or which packages they run
— the caller passes ``entry_module`` and ``packages`` explicitly, required,
no defaults. The consumer's own staleness module owns the service
registry, the in-flight (safe-to-restart) check that reads its reply store,
and report rendering.

FAIL LOUD: every function here that cannot determine an answer returns or
raises an explicit "I don't know", never a silent "assume fresh" —
``status="unknown"`` is a distinct state from both ``"fresh"`` and
``"stale"``.

This module never restarts, kills, or otherwise acts on a service. It only
detects and reports.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from pathlib import Path as pathlib_Path

from drumbeat import drain, fsutil

_CLOSURE_SUBPROCESS_TIMEOUT_SECONDS = 30
_PS_TIMEOUT_SECONDS = 10
# Real, exact substring of the cmdline every agent turn's worker carries: every
# turn runs as ``python -m drumbeat.agent_worker`` (see runner._submit_turn).
# Single source of truth in ``drain`` so the two live-turn readers cannot drift.
# There is nothing here for a substring match to accidentally hit: this
# process's own argv is a service or operator command ("drumbeat serve",
# "drumbeat doctor", a consumer's "notify-serve"), and the staleness closure
# subprocess runs "-m drumbeat.staleness" -- none carry "drumbeat.agent_worker".
_AGENT_TURN_MARKER = drain.AGENT_TURN_MARKER


def _fingerprint_path(runs_dir: Path, service: str) -> Path:
    return Path(runs_dir).expanduser() / f".{service}.fingerprint.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _imported_package_files(packages: tuple[str, ...]) -> dict[str, str]:
    """Every module from the named ``packages`` already present in *this
    interpreter's* ``sys.modules``, mapped to its content hash. Intended to
    be called immediately after importing exactly one entry module in an
    otherwise-fresh interpreter (see ``_closure_main``) -- see this module's
    docstring for why that precondition matters.

    ``packages`` is required with no default: which packages constitute "the
    service's own code" is the consumer's declaration, not this mechanism's
    guess (a consumer that layers its own package on top passes both its
    package name and "drumbeat"; a standalone engine passes ("drumbeat",)).
    """
    import importlib

    roots: dict[str, pathlib_Path] = {}
    for pkg_name in packages:
        pkg = importlib.import_module(pkg_name)
        pkg_file = getattr(pkg, "__file__", None)
        if not pkg_file:
            raise ValueError(
                f"package {pkg_name!r} has no __file__; cannot fingerprint it"
            )
        roots[pkg_name] = pathlib_Path(pkg_file).resolve().parent

    result: dict[str, str] = {}
    for name, module in list(sys.modules.items()):
        top = name.split(".", 1)[0]
        if top not in roots:
            continue
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = pathlib_Path(file).resolve()
        try:
            path.relative_to(roots[top])
        except ValueError:
            continue  # not actually under this package (shouldn't happen, but never guess)
        result[str(path)] = _sha256_file(path)
    return result


def _closure_main(argv: list[str]) -> int:
    """Entry point for ``python -m drumbeat.staleness <entry.module> <pkg> [<pkg>...]``.

    Not a public API and not registered as a console script -- it exists
    solely to be invoked as a subprocess by ``write_startup_fingerprint``,
    in an interpreter that has imported nothing of the named packages yet.
    Imports exactly the one module named, then prints the resulting
    ``{file: sha256}`` closure (restricted to the named packages) as JSON
    on stdout.
    """
    import importlib

    if len(argv) < 2:
        print(
            "usage: python -m drumbeat.staleness <dotted.module.name> <package> [<package>...]",
            file=sys.stderr,
        )
        return 2
    entry_module, packages = argv[0], tuple(argv[1:])
    try:
        importlib.import_module(entry_module)
    except ImportError as exc:
        print(f"failed to import {entry_module!r}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(_imported_package_files(packages)))
    return 0


def _subprocess_env(packages: tuple[str, ...]) -> dict[str, str]:
    """Environment for the closure subprocess: the parent's own environment,
    with each named package's own source root prepended to ``PYTHONPATH``.

    Needed because not every caller runs an installed console script: bin/
    shims invoke ``python3 -m ...`` with ``PYTHONPATH`` set by hand.
    Whatever let *this* process import the packages must also be threaded
    through to the child, or the child's import fails for a reason that has
    nothing to do with staleness.
    """
    import importlib

    src_dirs: list[str] = []
    for pkg_name in packages:
        pkg = importlib.import_module(pkg_name)
        pkg_file = getattr(pkg, "__file__", None)
        if pkg_file:
            src_dirs.append(str(Path(pkg_file).resolve().parent.parent))
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = src_dirs + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _process_start_epoch(pid: int) -> float | None:
    """Absolute Unix epoch seconds process ``pid`` started, or ``None`` if
    it cannot be determined (process gone, ``/proc`` unavailable, parse
    failure).

    Used only to detect PID reuse (a dead service's pid coincidentally
    picked up by an unrelated later process) -- never to assume liveness;
    liveness is ``os.kill(pid, 0)``, this is an extra honesty check on
    top of it. Linux-only (reads ``/proc``); this project runs on a single
    Linux host (see SCRATCH.md), so no portability shim is warranted here.
    """
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # comm (arg 2) is parenthesized and may itself contain spaces/parens;
        # splitting on the LAST ')' is the documented-safe way to skip past it.
        after_comm = stat_text.rsplit(")", 1)[1].split()
        starttime_ticks = float(
            after_comm[19]
        )  # field 22 (1-indexed), i.e. index 19 here
        btime: float | None = None
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                btime = float(line.split()[1])
                break
        if btime is None:
            return None
        return btime + starttime_ticks / clk_tck
    except (OSError, IndexError, ValueError):
        return None


def _read_cmdline(pid: int) -> tuple[str, ...]:
    """The exact argv a running process was launched with, or ``()`` if it
    cannot be read. Used to print the exact relaunch command in
    ``render_report`` -- reconstructed from the real invocation rather than
    a hardcoded guess, so it stays correct even when someone passes
    non-default flags (e.g. a different ``--port``).
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p)


def write_startup_fingerprint(
    service: str,
    runs_dir: Path,
    *,
    entry_module: str,
    packages: tuple[str, ...],
) -> Path | None:
    """Called once by a long-running service, right after it knows it is
    really starting (e.g. after the scheduler acquires its single-instance
    lock), before entering its main loop.

    ``entry_module`` and ``packages`` are REQUIRED with no defaults: the
    consumer declares which module's import closure defines "this service's
    code" and which packages that closure is allowed to span (a consumer's
    notify-serve may span both its own package and "drumbeat"; the engine's own
    scheduler spans only "drumbeat").

    Spawns a fresh subprocess that imports *only* ``entry_module`` and
    reports every file from ``packages`` that pulls in, then writes
    ``{service, pid, started_at, start_epoch, modules}`` to
    ``runs/.{service}.fingerprint.json``.

    Never raises: a failure here (subprocess error, unreadable output) is
    printed to stderr and this returns ``None`` -- the staleness guard
    must never be able to prevent the actual service from starting or
    serving. A missing fingerprint is exactly the "cannot determine"
    signal ``check_staleness`` already reports loudly; better that than
    a crashed scheduler.
    """
    if not entry_module or not packages:
        print(
            f"[staleness] refusing to fingerprint {service!r}: entry_module and "
            "packages are required with no defaults",
            file=sys.stderr,
        )
        return None

    runs_dir = Path(runs_dir).expanduser()
    pid = os.getpid()

    try:
        result = subprocess.run(
            [sys.executable, "-m", "drumbeat.staleness", entry_module, *packages],
            capture_output=True,
            text=True,
            timeout=_CLOSURE_SUBPROCESS_TIMEOUT_SECONDS,
            env=_subprocess_env(packages),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"[staleness] could not compute import closure for {service}: {exc} -- "
            "staleness will be reported as unknown until this succeeds",
            file=sys.stderr,
        )
        return None
    if result.returncode != 0:
        print(
            f"[staleness] closure subprocess for {service} exited "
            f"{result.returncode}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        modules = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[staleness] closure subprocess for {service} produced unparsable "
            f"output: {exc}",
            file=sys.stderr,
        )
        return None
    if not isinstance(modules, dict) or not modules:
        print(
            f"[staleness] closure subprocess for {service} reported no modules "
            "-- refusing to write an empty fingerprint",
            file=sys.stderr,
        )
        return None

    payload = {
        "service": service,
        "pid": pid,
        "start_epoch": _process_start_epoch(pid),
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modules": modules,
    }
    path = _fingerprint_path(runs_dir, service)
    fsutil.atomic_write(path, json.dumps(payload, indent=2) + "\n")
    return path


@dataclass(frozen=True)
class ChangedModule:
    """One imported file whose content no longer matches the fingerprint."""

    path: str
    fingerprint_sha256: str
    current_sha256: str | None  # None if the file no longer exists at all
    current_mtime: str | None  # ISO8601 UTC; None if the file no longer exists


@dataclass(frozen=True)
class StalenessReport:
    """Result of checking one service. ``status`` is one of ``"fresh"``,
    ``"stale"``, or ``"unknown"`` -- see this module's docstring for why
    ``"unknown"`` is never collapsed into ``"fresh"``.
    """

    service: str
    status: str
    reason: str
    pid: int | None = None
    started_at: str | None = None
    changed: tuple[ChangedModule, ...] = field(default_factory=tuple)
    cmdline: tuple[str, ...] = field(default_factory=tuple)


def check_staleness(service: str, runs_dir: Path) -> StalenessReport:
    """Is ``service`` (``"scheduler"`` or ``"notify-serve"``) currently
    running code that matches what's on disk right now?

    Reads the fingerprint written by ``write_startup_fingerprint``, and
    honestly reports ``"unknown"`` (never guesses ``"fresh"``) whenever:
    the fingerprint doesn't exist or can't be parsed, the recorded pid
    isn't running, or the recorded pid's process-start time no longer
    matches (pid reuse -- a different process has since taken that pid).
    """
    runs_dir = Path(runs_dir).expanduser()
    path = _fingerprint_path(runs_dir, service)
    if not path.is_file():
        return StalenessReport(
            service=service,
            status="unknown",
            reason=(
                f"no startup fingerprint at {path} -- {service} either isn't "
                "running, predates this guard, or its fingerprint was removed. "
                "Starting it writes a fresh fingerprint automatically."
            ),
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return StalenessReport(
            service=service,
            status="unknown",
            reason=f"fingerprint at {path} is unreadable/corrupt: {exc}",
        )

    pid = payload.get("pid")
    modules = payload.get("modules")
    started_at = payload.get("started_at")
    recorded_start_epoch = payload.get("start_epoch")

    if not isinstance(pid, int) or not isinstance(modules, dict) or not modules:
        return StalenessReport(
            service=service,
            status="unknown",
            reason=f"fingerprint at {path} is missing pid/modules -- cannot verify.",
        )

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return StalenessReport(
            service=service,
            status="unknown",
            pid=pid,
            started_at=started_at,
            reason=(
                f"fingerprint says pid {pid} but no such process is running -- "
                f"{service} is not up (or was restarted through a path that "
                "didn't refresh the fingerprint). Starting it writes a fresh "
                "fingerprint automatically."
            ),
        )
    except PermissionError:
        pass  # exists, just not owned by us -- still alive for our purposes

    current_start_epoch = _process_start_epoch(pid)
    if (
        isinstance(recorded_start_epoch, (int, float))
        and current_start_epoch is not None
        and abs(current_start_epoch - recorded_start_epoch) > 2.0
    ):
        return StalenessReport(
            service=service,
            status="unknown",
            pid=pid,
            started_at=started_at,
            reason=(
                f"pid {pid} is running, but its process-start time no longer "
                "matches the fingerprint -- that pid has been reused by a "
                "different, unrelated process since. Cannot trust this "
                "fingerprint."
            ),
        )

    changed: list[ChangedModule] = []
    for file_str, old_hash in modules.items():
        file_path = Path(file_str)
        if not file_path.is_file():
            changed.append(ChangedModule(file_str, old_hash, None, None))
            continue
        new_hash = _sha256_file(file_path)
        if new_hash != old_hash:
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime, UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            changed.append(ChangedModule(file_str, old_hash, new_hash, mtime))

    cmdline = _read_cmdline(pid)
    if changed:
        return StalenessReport(
            service=service,
            status="stale",
            pid=pid,
            started_at=started_at,
            changed=tuple(changed),
            cmdline=cmdline,
            reason=(
                f"{len(changed)} imported module(s) changed on disk since "
                f"{service} started."
            ),
        )
    return StalenessReport(
        service=service,
        status="fresh",
        pid=pid,
        started_at=started_at,
        cmdline=cmdline,
        reason="",
    )


def count_agent_turns_in_flight() -> int | None:
    """How many ``python -m drumbeat.agent_worker`` turns (any automation,
    any session) are currently executing on this host.

    Reads ``/proc`` in Python via ``drain.find_agent_turns`` -- the same
    reader the drain check already trusts to answer "is it safe to kill the
    scheduler."

    It used to shell out to ``ps -eo cmd`` and return None on any failure.
    Caught by the clean-container walk of the quickstart: `ps` is part of
    `procps`, which minimal images (python:3.13-slim, most distroless bases,
    plenty of hardened hosts) do not ship. So the very first `drumbeat
    doctor` a new user ran printed ``agent turns in flight: UNKNOWN (check
    failed)`` for the number that decides whether a restart is safe --
    on a machine where the /proc reader beside it worked fine and returned
    the real answer.

    Two implementations of one question, and doctor had the fragile one.
    None is still returned when /proc itself cannot be read, because "I
    could not tell" must never render as a confident zero.
    """
    try:
        return len(drain.find_agent_turns())
    except OSError:
        return None


def exit_code_for(reports: list[StalenessReport]) -> int:
    """0 if every report is fresh, 1 if any is stale (most actionable,
    takes priority), else 2 if any is unknown. Never 0 unless every
    service was actually confirmed fresh -- fail loud, no silent pass.
    """
    statuses = {r.status for r in reports}
    if "stale" in statuses:
        return 1
    if "unknown" in statuses:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_closure_main(sys.argv[1:]))


__all__ = [
    "ChangedModule",
    "StalenessReport",
    "check_staleness",
    "count_agent_turns_in_flight",
    "exit_code_for",
    "write_startup_fingerprint",
]
