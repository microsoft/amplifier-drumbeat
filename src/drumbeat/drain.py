"""Drain: stop the scheduler from *starting* work, without killing work in flight.

This exists because of a specific, verified hazard (recorded during the
engine extraction,
section 12, council blocker B2 -- it replaced a bare kill 6-0):

    The runner spawns each turn's worker in **its own process group, with
    no SIGTERM handling and close_fds=True**. Kill the scheduler mid-turn
    and that worker keeps running and keeps writing the session transcript --
    while the kernel releases the parent's per-session flock the instant the
    parent dies. The next scheduler tick then resumes that same session
    underneath the still-writing orphan: the exact transcript corruption the
    flocks exist to prevent, executed deliberately by runbook.

So a scheduler is never killed. It is **drained, verified empty, and only
then killed by explicit pid**:

    1. set the drain flag        -> no new runs start
    2. verify drained            -> no agent turns, no held session locks
    3. kill by explicit pid      -> read from /proc/<pid>/cmdline first
    4. start the replacement     -> it must ACQUIRE the scheduler flock
    5. invalid-run sweep         -> no run left non-terminal by the transition

This module owns steps 1 and 2, as an ops mechanism rather than a migration
one-off -- the same two steps become the future systemd ``ExecStop``
(section 11 supervision). Steps 3-5 are the operator's, deliberately: this
module never kills anything.

FAIL LOUD: every "is it drained?" answer here is either a definite yes with
evidence, or a definite no naming what is still in flight. There is no
"probably fine" -- a check that cannot run returns the failure, never an
empty list that reads like "all clear."
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DRAIN_FILENAME = ".scheduler.drain"

# The exact, real substring every agent turn's process carries in its cmdline.
# Every turn runs in an isolated worker launched as
# ``python -m drumbeat.agent_worker`` (see runner._submit_turn), so the dotted
# module name is present in argv by construction -- this equals
# ``drumbeat.agent_worker.WORKER_MODULE`` (kept as a literal here so this
# low-level module imports nothing of the runner). Matched in Python against
# /proc entries we read ourselves -- never through a shell `pkill -f`/`grep`,
# which has matched its own invoking shell and killed a live service in this
# project four times now. Nothing else on this host runs that module: a
# ``drumbeat serve``/``doctor`` process, or the staleness closure subprocess
# (``-m drumbeat.staleness``), never carries ``drumbeat.agent_worker`` in argv.
AGENT_TURN_MARKER = "drumbeat.agent_worker"


def drain_flag_path(runs_dir: Path) -> Path:
    return Path(runs_dir).expanduser() / DRAIN_FILENAME


def set_drain(runs_dir: Path, *, reason: str) -> Path:
    """Ask the scheduler to stop starting new runs.

    ``reason`` is required with no default -- the same rule the rest of this
    design applies to every state-changing act. A drained scheduler that
    cannot say why it is drained is indistinguishable from a broken one, and
    "enabled but silently inert" is a failure class this project has already
    paid for.
    """
    if not reason or not reason.strip():
        raise ValueError("drain requires a reason; there is no default")
    path = drain_flag_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason.strip(),
        "requested_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_by_pid": os.getpid(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def clear_drain(runs_dir: Path) -> bool:
    """Resume scheduling. Returns True if a flag was actually cleared."""
    path = drain_flag_path(runs_dir)
    if not path.is_file():
        return False
    path.unlink()
    return True


def drain_state(runs_dir: Path) -> dict[str, object] | None:
    """The active drain request, or ``None`` when scheduling is live.

    A flag file that exists but cannot be parsed still counts as draining --
    the *presence* of the file is the signal, and refusing to honour a
    corrupt flag would resume scheduling at the exact moment somebody was
    trying to stop it.
    """
    path = drain_flag_path(runs_dir)
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "reason": f"<drain flag present but unreadable: {exc}>",
            "requested_at": None,
            "requested_by_pid": None,
        }
    if not isinstance(parsed, dict):
        return {"reason": "<drain flag present but not an object>"}
    return parsed


def is_draining(runs_dir: Path) -> bool:
    return drain_flag_path(runs_dir).is_file()


# ---- process inspection (platform-neutral) ----
#
# Linux keeps the /proc fast-path: it needs no external binary, which is the
# whole reason ``staleness.count_agent_turns_in_flight`` was moved onto this
# reader in the first place (a minimal container ships no ``ps``).
#
# Every OTHER platform gets a ``ps`` path. macOS has no ``/proc`` AT ALL, so
# the /proc-only reader did not degrade there -- it raised FileNotFoundError
# out of ``Path("/proc").iterdir()`` and tracebacked ``drumbeat drain
# --status/--wait``, i.e. the documented safe-shutdown procedure could not be
# completed on the platform (field report #504). ``ps -eo pid,ppid,args`` is
# POSIX-portable and needs no new dependency.
#
# Still never a shell ``pkill -f``/``grep``: the argv is built as a list, run
# without a shell, and matched in Python. A shell match has caught its own
# invoking shell and killed a live service in this project four times.


def _uses_proc() -> bool:
    """True where ``/proc`` is the authoritative process table (Linux only)."""
    return sys.platform.startswith("linux")


@dataclass(frozen=True)
class _ProcessRow:
    """One live process, as read from whichever inspection path is in use."""

    pid: int
    ppid: int | None
    cmdline: str


def _ps_rows() -> list[_ProcessRow]:
    """Every live process via portable ``ps -eo pid,ppid,args``.

    Raises ``OSError`` -- naming the real cause -- when the sweep cannot be
    performed at all (no ``ps`` on PATH, non-zero exit). It never returns an
    empty list to mean "could not look": an empty list here reads as "nothing
    is in flight", which is precisely the false all-clear this module exists
    to refuse.
    """
    argv = ["ps", "-eo", "pid,ppid,args"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except (OSError, ValueError) as exc:  # ps absent, not executable, ...
        raise OSError(f"cannot inspect processes: `{' '.join(argv)}` failed: {exc}")
    if result.returncode != 0:
        detail = result.stderr.strip() or "<no stderr>"
        raise OSError(
            f"cannot inspect processes: `{' '.join(argv)}` exited "
            f"{result.returncode}: {detail}"
        )
    rows: list[_ProcessRow] = []
    for raw in result.stdout.splitlines():
        parts = raw.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            # The header row ("PID PPID COMMAND"), or any line ps wrapped.
            continue
        rows.append(
            _ProcessRow(pid=pid, ppid=ppid, cmdline=parts[2].strip() if len(parts) > 2 else "")
        )
    return rows


def _process_table() -> dict[int, _ProcessRow] | None:
    """One whole-host process snapshot, or ``None`` on Linux (/proc is used).

    Taken ONCE per sweep: the ``ps`` path must not re-run ``ps`` per pid (that
    would be one subprocess per process on the host) and must not read a
    different snapshot halfway through an ancestry walk.
    """
    if _uses_proc():
        return None
    return {row.pid: row for row in _ps_rows()}


def _read_cmdline(pid: int, table: dict[int, _ProcessRow] | None = None) -> str | None:
    """This pid's full argv as one string, or ``None`` if it is not live.

    ``table``: a snapshot from ``_process_table()``. Omitted, one is taken --
    correct for a single lookup, wasteful inside a loop, which is why the
    sweeps below thread their own snapshot through.
    """
    if _uses_proc():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    if table is None:
        table = _process_table()
    row = (table or {}).get(pid)
    return row.cmdline if row is not None else None


def _read_ppid(pid: int, table: dict[int, _ProcessRow] | None = None) -> int | None:
    """Parent pid from /proc/<pid>/stat, parsed from the RIGHT of comm.

    The process name field is parenthesised and may itself contain spaces
    and parentheses, so the fields are located relative to the LAST ')' --
    splitting the line on whitespace is the classic way to misread this file
    for any process whose name has a space in it.

    Off Linux the answer comes from the ``ps`` snapshot instead; see
    ``_read_cmdline`` for the ``table`` argument.
    """
    if _uses_proc():
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return None
        close = stat_text.rfind(")")
        if close == -1:
            return None
        fields = stat_text[close + 2 :].split()
        if len(fields) < 2:
            return None
        try:
            return int(fields[1])
        except ValueError:
            return None
    if table is None:
        table = _process_table()
    row = (table or {}).get(pid)
    return row.ppid if row is not None else None


def _live_pids(table: dict[int, _ProcessRow] | None = None) -> list[int]:
    if not _uses_proc():
        if table is None:
            table = _process_table()
        return sorted(table or {})
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            pids.append(int(entry.name))
    return sorted(pids)


@dataclass(frozen=True)
class AgentTurn:
    """One live ``amplifier-agent`` turn found on this host."""

    pid: int
    ppid: int | None
    cmdline: str
    is_descendant: bool  # of the pid we were asked about, when we were asked


def find_agent_turns(*, ancestor_pid: int | None = None) -> list[AgentTurn]:
    """Every live agent turn, read from ``/proc`` -- never via a shell match.

    When ``ancestor_pid`` is given, each result is additionally marked with
    whether it descends from that pid, so the caller can distinguish "the
    scheduler still has work in flight" from "notify-serve is executing a
    reply right now" -- during the migration's hybrid window both are true
    at once, and only the first blocks a kill.

    Raises ``OSError`` when this host's process table cannot be read at all
    (no ``/proc`` on Linux, no ``ps`` elsewhere). That is the established
    contract -- ``staleness.count_agent_turns_in_flight`` turns it into "I
    could not tell", never a confident zero -- and ``check_drained`` turns it
    into a named blocker rather than a traceback.
    """
    table = _process_table()
    turns: list[AgentTurn] = []
    for pid in _live_pids(table):
        cmdline = _read_cmdline(pid, table)
        if not cmdline or AGENT_TURN_MARKER not in cmdline:
            continue
        ppid = _read_ppid(pid, table)
        descendant = False
        if ancestor_pid is not None:
            walker: int | None = pid
            seen: set[int] = set()
            while walker is not None and walker > 1 and walker not in seen:
                seen.add(walker)
                if walker == ancestor_pid:
                    descendant = True
                    break
                walker = _read_ppid(walker, table)
        turns.append(
            AgentTurn(pid=pid, ppid=ppid, cmdline=cmdline, is_descendant=descendant)
        )
    return turns


def held_session_locks(runs_dir: Path) -> list[str]:
    """Session-lock files some process currently HOLDS, by trying the lock.

    Presence of a file means nothing -- ``.session-locks/`` accumulates one
    file per session id ever locked, and flock is held by an open file
    description, not by anything on disk. The only honest test is to attempt
    the lock: fail => somebody holds it => a turn is in flight.
    """
    lock_dir = Path(runs_dir).expanduser() / ".session-locks"
    if not lock_dir.is_dir():
        return []
    held: list[str] = []
    for entry in sorted(lock_dir.glob("*.lock")):
        try:
            fd = os.open(entry, os.O_RDWR)
        except OSError:
            # Cannot test it => cannot claim it is free.
            held.append(f"{entry.name} (unopenable -- cannot prove it is free)")
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(entry.name)
                continue
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held


@dataclass(frozen=True)
class DrainStatus:
    """Whether it is safe to kill the scheduler right now, and why not."""

    drained: bool
    draining: bool
    scheduler_pid: int | None
    scheduler_cmdline: str | None
    agent_turns: list[AgentTurn] = field(default_factory=list)
    held_locks: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"drain flag:      {'SET' if self.draining else 'not set'}",
            f"scheduler pid:   {self.scheduler_pid if self.scheduler_pid else '<not given>'}",
        ]
        if self.scheduler_cmdline:
            lines.append(f"  cmdline:       {self.scheduler_cmdline}")
        lines.append(
            f"agent turns:     {len(self.agent_turns)} live"
            + (
                f" ({sum(1 for t in self.agent_turns if t.is_descendant)} under this scheduler)"
                if self.scheduler_pid
                else ""
            )
        )
        for turn in self.agent_turns:
            mark = "DESCENDANT" if turn.is_descendant else "other process"
            lines.append(f"  pid {turn.pid} ({mark}): {turn.cmdline[:120]}")
        lines.append(f"held session locks: {len(self.held_locks)}")
        for name in self.held_locks:
            lines.append(f"  {name}")
        lines.append("")
        if self.drained:
            lines.append("DRAINED -- safe to kill the scheduler by explicit pid.")
        else:
            lines.append("NOT DRAINED -- do not kill. Blockers:")
            for blocker in self.blockers:
                lines.append(f"  - {blocker}")
        return "\n".join(lines)


def check_drained(runs_dir: Path, *, scheduler_pid: int | None = None) -> DrainStatus:
    """Is it safe to kill the scheduler? Definite yes-with-evidence or no-with-blockers.

    Drained requires ALL of:

    - the drain flag is set (otherwise the scheduler may start a run one
      millisecond after this check returns -- a check without the flag is a
      race, not a verification);
    - no live agent turn descends from ``scheduler_pid`` (when given);
    - no session lock is currently held.

    Note the asymmetry on session locks: a lock held by *notify-serve*
    executing a reply also blocks. That is deliberate for this migration --
    the point of the sweep is that nothing is mid-transcript-write when the
    lock ownership changes hands, and the check has no way to attribute a
    held lock to a process, so it refuses rather than guesses.
    """
    runs_dir = Path(runs_dir).expanduser()
    draining = is_draining(runs_dir)

    # Process inspection can be impossible (no /proc, no `ps`). That is never
    # a traceback and never an empty turn list that reads "all clear": it is a
    # NAMED blocker, so `drumbeat drain --status/--wait` still completes and
    # still says NOT DRAINED. Losing this distinction is how a safe-shutdown
    # procedure certifies a host it could not actually look at.
    inspection_error: str | None = None
    try:
        turns = find_agent_turns(ancestor_pid=scheduler_pid)
    except OSError as exc:
        turns = []
        inspection_error = str(exc)

    locks = held_session_locks(runs_dir)

    blockers: list[str] = []
    if inspection_error:
        blockers.append(
            f"cannot inspect this host's processes ({inspection_error}) -- so "
            "'no agent turn is in flight' cannot be proven"
        )
    if not draining:
        blockers.append(
            "drain flag is NOT set -- the scheduler can start a new run at any "
            "moment, so 'no work in flight' cannot be relied on"
        )
    descendants = [t for t in turns if t.is_descendant]
    if scheduler_pid is not None and descendants:
        blockers.append(
            f"{len(descendants)} agent turn(s) still running under scheduler pid "
            f"{scheduler_pid}: " + ", ".join(str(t.pid) for t in descendants)
        )
    if locks:
        blockers.append(
            f"{len(locks)} session lock(s) currently held: " + ", ".join(locks)
        )

    cmdline: str | None = None
    if scheduler_pid is not None and inspection_error is None:
        try:
            cmdline = _read_cmdline(scheduler_pid)
        except OSError as exc:
            blockers.append(
                f"cannot read pid {scheduler_pid}'s command line ({exc}) -- "
                "verify you are killing the process you think you are"
            )
        else:
            if cmdline is None:
                blockers.append(
                    f"pid {scheduler_pid} is not in this host's process table "
                    "-- it is already gone; verify you are killing the process "
                    "you think you are"
                )

    return DrainStatus(
        drained=not blockers,
        draining=draining,
        scheduler_pid=scheduler_pid,
        scheduler_cmdline=cmdline,
        agent_turns=turns,
        held_locks=locks,
        blockers=blockers,
    )


def wait_until_drained(
    runs_dir: Path,
    *,
    scheduler_pid: int | None = None,
    timeout_seconds: float = 1800.0,
    poll_seconds: float = 5.0,
    on_poll=None,
) -> DrainStatus:
    """Poll until drained or the deadline passes. Returns the final status.

    Never kills, never forces. A timeout returns a NOT-DRAINED status with
    its blockers intact -- the caller decides, and on a live system the
    correct decision is usually to wait longer rather than to cut.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = check_drained(runs_dir, scheduler_pid=scheduler_pid)
        if on_poll is not None:
            on_poll(status)
        if status.drained or time.monotonic() >= deadline:
            return status
        time.sleep(poll_seconds)


__all__ = [
    "AGENT_TURN_MARKER",
    "DRAIN_FILENAME",
    "AgentTurn",
    "DrainStatus",
    "check_drained",
    "clear_drain",
    "drain_flag_path",
    "drain_state",
    "find_agent_turns",
    "held_session_locks",
    "is_draining",
    "set_drain",
    "wait_until_drained",
]
