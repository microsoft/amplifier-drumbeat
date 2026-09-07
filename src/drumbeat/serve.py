"""``drumbeat serve`` -- the engine as its own process.

One instance per consuming workspace: own port, own data dir, own flock
(section 3). This module is the composition root that step 3 moved out of
the consumer's service, and its startup ORDER is the interesting part:

    1. resolve the workspace
    2. **acquire the scheduler flock** -- and fail here, loudly, if another
       scheduler holds it
    3. mint/load this instance's API key
    4. fingerprint the running code (staleness guard)
    5. reap unheld session locks
    6. bind the HTTP API on loopback
    7. run the scheduler loop

Step 2 comes before step 6 deliberately. The flock guarantee runs in exactly
one direction: a **lingering old scheduler keeps the lock, and the new
process is the one that must refuse to run**. If binding came first, a
refused engine would still have a live HTTP face on the registered port --
an engine that answers "I am here" while scheduling nothing, which is the
"up but lockless" state ``/api/health``'s ``scheduler_lock`` field exists to
make impossible to miss. Failing before the bind means the failure is total
and obvious instead of partial and plausible.

The scheduler runs on the MAIN thread and the HTTP API on background
threads. That way a run in flight (minutes long, blocking) never stops the
API from answering, and Ctrl+C/SIGTERM lands on the loop rather than in a
worker thread where it would be swallowed.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from drumbeat import api_key as api_key_mod
from drumbeat import drain, engine_api, error_log, scheduler, staleness, turns
from drumbeat import packs as packs_mod
from drumbeat.management_api import EngineContext
from drumbeat.runner import (
    AGENT_INSTALL_HINT,
    check_agent_command,
    reap_stale_session_locks,
)

# The service name this process fingerprints itself under. Distinct from the
# consumer's old "scheduler" service: after step 3 they are different
# processes running different code closures, and reusing the name would let
# a stale fingerprint from the retired process answer for this one.
STALENESS_SERVICE = "drumbeat-serve"
STALENESS_ENTRY_MODULE = "drumbeat.serve"
STALENESS_PACKAGES = ("drumbeat",)

DEFAULT_PORT = 9100

# Bind addresses this is willing to use. The engine has no public surface by
# design (section 3): the consumer stays the one public face and proxies
# what the phone needs. A --host flag that accepted 0.0.0.0 would make that
# a convention instead of a property.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _log(message: str) -> None:
    print(f"[drumbeat-serve] {message}", file=sys.stderr)


DEFAULT_DATA_DIRNAME = "runs"


def resolve_workspace(
    workspace: Path, *, data_dir: Path | None = None
) -> EngineContext:
    """The section-7.2 workspace handoff: one directory in, four dirs out.

    FAIL LOUD: a workspace with no ``automations/`` is almost certainly the
    wrong directory (a typo, or the repo root instead of the consumer dir),
    and starting anyway would produce an engine that schedules nothing and
    reports itself perfectly healthy while doing it.

    **The policy root and the state root are two different things.** The
    workspace is *policy* -- automations, prompts, guidance, packs -- files a
    human owns, edits, and (increasingly) keeps under git. ``data_dir`` is
    *state* -- run artifacts, the delivery outbox, locks, the api key, the
    staleness fingerprint -- files only the engine writes and no human should
    ever hand-edit or version.

    Until now those were the same tree by construction, which made "the
    server owns its state" a rule enforced by discipline. It is a structural
    property once they can be separated: a ``git clean -fdx`` in a policy repo
    must be *incapable* of destroying server state, not merely unlikely to.

    ``data_dir`` defaults to ``<workspace>/runs`` -- today's layout, byte for
    byte -- because behavior preservation is the point of introducing the seam
    before anyone moves through it. Passing it explicitly is what a versioned
    policy tree does.
    """
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"error: workspace is not a directory: {workspace}")
    automations_dir = workspace / "automations"
    if not automations_dir.is_dir():
        raise SystemExit(
            f"error: no automations/ directory under {workspace} -- refusing to "
            "start an engine that would schedule nothing while looking healthy. "
            "Point --workspace at the consumer's project directory."
        )
    if data_dir is None:
        resolved_data_dir = workspace / DEFAULT_DATA_DIRNAME
    else:
        # No .resolve() ambiguity and no silent creation here: resolve the path
        # so every downstream consumer sees one spelling of it, and let the
        # caller that actually intends to write (serve) be the one that mkdirs.
        resolved_data_dir = Path(data_dir).expanduser().resolve()
        if resolved_data_dir.exists() and not resolved_data_dir.is_dir():
            raise SystemExit(
                f"error: --data-dir exists and is not a directory: {resolved_data_dir}"
            )
    return EngineContext(
        automations_dir=automations_dir,
        prompts_dir=workspace / "prompts",
        runs_dir=resolved_data_dir,
        cwd=workspace,
    )


def serve(
    *,
    workspace: Path,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    data_dir: Path | None = None,
) -> None:
    """Run the engine (scheduler + HTTP API) until interrupted."""
    if host not in _ALLOWED_HOSTS:
        raise SystemExit(
            f"error: refusing to bind {host!r}. The engine binds loopback only "
            "(docs/ARCHITECTURE.md section 2) -- it has no public surface, and the "
            "consumer proxies what needs to be reachable."
        )

    ctx = resolve_workspace(workspace, data_dir=data_dir)
    ctx.runs_dir.mkdir(parents=True, exist_ok=True)
    # Sixth wall: the engine's own log modules resolve from the data dir,
    # never from cwd captured at import. This process knows its resolved data
    # dir right here -- plumb it once, explicitly (error_log's option 1).
    error_log.set_log_data_dir(ctx.runs_dir)
    if data_dir is not None:
        _log(
            f"data dir: {ctx.runs_dir} (explicit --data-dir; policy tree "
            f"{ctx.cwd} holds no engine state)"
        )

    # (2) The lock, before the port. See this module's docstring.
    try:
        lock_handle = scheduler.acquire_scheduler_lock(ctx.runs_dir)
    except scheduler.SchedulerError as exc:
        _log(f"REFUSING TO START: {exc}")
        raise SystemExit(2) from exc
    # Say exactly what was acquired. The lock file lives in the DATA DIR, so
    # what it guarantees is "one scheduler per data dir" -- and the old string
    # said "for this workspace", which is a claim the lock does not make and
    # cannot enforce. Verified: two `serve` processes on ONE workspace with
    # two --data-dirs each acquired their own lock, each printed "the only
    # scheduler for this workspace", and each registered the same automations
    # on its own cadence. That is the double-fire incident this lock was added
    # for, announced twice as safety.
    _log(
        f"scheduler lock acquired: {scheduler.scheduler_lock_path(ctx.runs_dir)} "
        f"(this process is now the only scheduler for data dir {ctx.runs_dir}). "
        "NOTE: the lock is per data dir, not per workspace -- a second serve on "
        "this workspace with a different --data-dir would take its own lock and "
        "double-fire every automation."
    )

    state = scheduler.SchedulerState()

    # (3) Per-instance key, beside the workspace it protects.
    key_path = api_key_mod.api_key_path(ctx.runs_dir)
    existed = key_path.is_file()
    api_key_value = api_key_mod.ensure_api_key(ctx.runs_dir)
    _log(
        f"engine API key {'loaded from' if existed else 'GENERATED at'} {key_path} "
        "(never logged here). Required on every mutating request, including "
        "from loopback."
    )

    # (4) Fingerprint the whole process -- scheduler AND API are one code
    # closure now, so one fingerprint answers "is this process running the
    # code on disk?" for both.
    fingerprint_path = staleness.write_startup_fingerprint(
        STALENESS_SERVICE,
        ctx.runs_dir,
        entry_module=STALENESS_ENTRY_MODULE,
        packages=STALENESS_PACKAGES,
    )
    _log(f"staleness fingerprint written: {fingerprint_path}")

    # (4b) Pin the base PATH and load every declared pack -- BEFORE the bind,
    # for the same reason the scheduler lock comes before the bind: a refused
    # engine must not have a live HTTP face claiming it is here.
    #
    # Pinning first is what makes "the base PATH is a property of this
    # process, not of whoever launched it" true rather than aspirational --
    # every later turn reads the pin, never os.environ. Loading packs here
    # (rather than only lazily, per turn) means a card that lies, a tool
    # missing its exec bit, or two packs claiming one tool name fails NOW,
    # named, at startup -- instead of at 03:40 in the middle of a scheduled
    # run against a real inbox.
    pinned_base = packs_mod.pin_base_path()
    _log(f"base PATH pinned ({len(pinned_base.split(':'))} entries): {pinned_base}")
    try:
        loaded_packs = packs_mod.load_workspace_packs(ctx.cwd)
    except packs_mod.PackError as exc:
        _log(f"REFUSING TO START: pack load failed: {exc}")
        raise SystemExit(2) from exc
    pack_list = packs_mod.read_pack_list(ctx.cwd)
    if not pack_list.declared:
        _log(
            f"no {packs_mod.PACK_LIST_FILENAME} in {ctx.cwd} -- zero packs declared. "
            "Automations requiring a pack tool will abort at the requirements gate."
        )
    for pack in loaded_packs:
        _log(
            f"pack loaded: {pack.name} (format {pack.pack_format}) "
            f"from {pack.directory} -- tools: {', '.join(pack.tools)}"
        )
    _log(f"turn PATH: {packs_mod.turn_path(ctx.cwd, loaded_packs)}")

    # (4c) The agent binary, on that exact turn PATH -- before the bind, for
    # the same reason as the two checks above. An engine that cannot spawn a
    # single turn but answers /api/health "ok" is the "up but useless" state
    # this startup order exists to make impossible; without this it would
    # schedule every automation, fire them on time, and fail each one at
    # spawn. Checked here rather than trusted from the launcher's PATH
    # because the pin above has already replaced it.
    agent_path = check_agent_command(ctx.cwd)
    if agent_path is None:
        _log(f"REFUSING TO START: {AGENT_INSTALL_HINT}")
        raise SystemExit(2)
    _log(f"agent command: {agent_path}")

    # (5) Locks nobody holds, from processes that are gone.
    reaped, skipped_active = reap_stale_session_locks(ctx.runs_dir)
    _log(
        f"session-lock reap: removed {len(reaped)} unheld, "
        f"left {len(skipped_active)} active untouched"
    )

    # (5b) Turns a PREVIOUS engine process left in flight (step 5). Runs
    # before the bind so no live request can race a reconciliation
    # decision, and before the scheduler loop so a stale `running` record
    # can never be mistaken for real contention. Never retried -- see
    # turns.reconcile_turns_on_startup.
    reconciled = turns.reconcile_turns_on_startup(ctx.runs_dir)
    _log(
        f"turn reconciliation: tombstoned {len(reconciled['tombstoned'])} "
        f"orphaned turn(s) {reconciled['tombstoned']!r}"
    )

    # (6) Bind loopback.
    server = engine_api.EngineServer(
        (host, port),
        engine_api.EngineRequestHandler,
        ctx=ctx,
        workspace=ctx.cwd,
        api_key_value=api_key_value,
        scheduler_state=state,
        staleness_service=STALENESS_SERVICE,
    )
    threading.Thread(
        target=server.serve_forever, name="drumbeat-api", daemon=True
    ).start()
    _log(
        f"engine API listening on http://{host}:{port} "
        f"(workspace={ctx.cwd}, runs={ctx.runs_dir})"
    )

    # (6b) A drain flag this process inherited. LAST thing printed before the
    # loop, deliberately: it is the line an operator is most likely to still
    # have on screen, and the state it describes is the one that looks most
    # like health. The drain flag lives in the data dir and OUTLIVES the
    # process that set it -- which is exactly the normal restart sequence
    # (drain, stop, apply, start), so a forgotten `--clear` produces an engine
    # that starts clean, logs every automation as "scheduled", answers
    # /api/health, and never fires anything. Not a refusal: coming up drained
    # is the correct behavior mid-cutover. Just impossible to miss.
    drain_request = drain.drain_state(ctx.runs_dir)
    if drain_request is not None:
        _log("=" * 72)
        _log(
            f"DRAINING -- NO SCHEDULED RUN WILL START. reason: {drain_request.get('reason')}"
        )
        _log(
            f"  requested at {drain_request.get('requested_at')} by pid "
            f"{drain_request.get('requested_by_pid')}"
        )
        _log(f"  flag file: {drain.drain_flag_path(ctx.runs_dir)}")
        _log(
            f"  resume with: drumbeat drain --workspace {ctx.cwd} "
            f"--data-dir {ctx.runs_dir} --clear"
        )
        _log("=" * 72)

    # (7) The loop, on the main thread, with the lock already in hand.
    try:
        scheduler.serve(
            ctx.automations_dir,
            ctx.cwd,
            ctx.runs_dir,
            ctx.prompts_dir,
            state=state,
            lock_handle=lock_handle,
            write_fingerprint=False,
        )
    except KeyboardInterrupt:
        _log("interrupted -- shutting down")
    finally:
        server.shutdown()
        server.server_close()


__all__ = [
    "DEFAULT_DATA_DIRNAME",
    "DEFAULT_PORT",
    "STALENESS_ENTRY_MODULE",
    "STALENESS_PACKAGES",
    "STALENESS_SERVICE",
    "resolve_workspace",
    "serve",
]
