"""``drumbeat`` -- the engine's command line.

    drumbeat serve    --workspace <dir> [--port N]   the engine: scheduler + HTTP API
    drumbeat doctor   --workspace <dir>              is the running engine running this code?
    drumbeat drain    --workspace <dir> --reason R   stop starting runs; verify it is safe to kill
    drumbeat sweep    --workspace <dir> [--since T]  invalid-run sweep (section 6)
    drumbeat api-key  --workspace <dir> [--show]     this instance's key
    drumbeat sessions --workspace <dir>              which conversation each automation resumes
    drumbeat rotate-session <slug> --reason R        abandon one pinned conversation

``--workspace`` is required with no default on every command. The engine
serves one consumer's workspace and there is no "the" workspace to guess at
-- guessing would let a command aimed at one consumer's engine silently act
on another's, which is the exact failure the per-instance topology exists to
prevent.

``--data-dir`` (optional, default ``<workspace>/runs``) splits engine STATE
from consumer POLICY. Every ``--workspace`` command accepts it, deliberately:
these commands all read the same state directory, so a flag that existed only
on ``serve`` would let ``doctor``/``drain``/``sweep`` quietly inspect an empty
directory and report health for an engine they were never looking at. Pass it
consistently, or not at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from drumbeat import api_key as api_key_mod
from drumbeat import automation as automation_mod
from drumbeat import drain as drain_mod
from drumbeat import (
    error_log,
    invalid_runs,
    packs,
    paths,
    runner,
    session_health,
    session_pins,
    staleness,
    workspace_git,
    workspace_init,
)
from drumbeat import serve as serve_mod
from drumbeat import service as service_mod
from drumbeat.automation import AutomationError
from drumbeat.management_api import EngineContext
from drumbeat.rotation_log import log_session_rotation


def _data_dir(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "data_dir", None)
    return Path(raw) if raw else None


def _ctx(args: argparse.Namespace) -> EngineContext:
    ctx = serve_mod.resolve_workspace(Path(args.workspace), data_dir=_data_dir(args))
    # Sixth wall: every CLI verb knows its resolved data dir right here --
    # plumb it into the engine's log modules explicitly, so an
    # AutomationError raised while this verb loads automations lands in the
    # data dir, not under whatever cwd the operator's shell happened to have.
    error_log.set_log_data_dir(ctx.runs_dir)
    return ctx


def _runs_dir(args: argparse.Namespace) -> Path:
    return _ctx(args).runs_dir


def _known_slugs(automations_dir: Path) -> set[str]:
    """Slugs of every automation file on disk, parse failures included.

    Uses the tolerant loader plus a filename fallback, deliberately: an
    orphan-pin count computed from only the files that PARSE would report a
    pin as orphaned the moment its automation had a typo -- a scary number
    with an unrelated cause.
    """
    automations, failures = automation_mod.load_all_tolerant(automations_dir)
    slugs = {a.slug for a in automations}
    slugs |= {f.path.stem for f in failures}
    return slugs


def _cmd_serve(args: argparse.Namespace) -> int:
    serve_mod.serve(
        workspace=Path(args.workspace),
        port=args.port,
        host=args.host,
        data_dir=_data_dir(args),
    )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ctx = _ctx(args)
    runs_dir = ctx.runs_dir
    report = staleness.check_staleness(serve_mod.STALENESS_SERVICE, runs_dir)
    print(f"service:  {report.service}")
    print(f"status:   {report.status.upper()}")
    print(f"reason:   {report.reason}")
    if report.pid:
        print(f"pid:      {report.pid} (started {report.started_at})")
    if report.cmdline:
        print(f"cmdline:  {' '.join(report.cmdline)}")
    for changed in report.changed:
        print(f"  CHANGED: {changed.path}")
        print(f"           on disk since start (mtime {changed.current_mtime})")
    if report.status == "stale":
        print()
        print(
            "This process is running code that no longer matches disk. Restart it "
            "with the drain procedure -- `drumbeat drain --workspace <dir> "
            "--reason 'picking up engine edits' --wait`, then kill by explicit "
            "pid, then start again. Never pkill -f."
        )
    in_flight = staleness.count_agent_turns_in_flight()
    print()
    print(
        "agent turns in flight: "
        + (str(in_flight) if in_flight is not None else "UNKNOWN (check failed)")
    )

    # The binary every turn is spawned as. Reported here (not only refused at
    # `serve` startup) because a doctor that stays silent about the one
    # dependency the engine cannot run without would report a perfectly
    # healthy engine that fails every automation at spawn.
    print()
    agent_path = runner.check_agent_command(ctx.cwd)
    if agent_path is None:
        print("agent command: MISSING")
        print(runner.AGENT_INSTALL_HINT)
    else:
        print(f"agent command: {agent_path}")

    # Drumpack wiring (drumpack-card.v1 rule 5 / VISION §4). A missing or empty
    # drumpacks.txt is a VISIBLE condition here, never a silent zero-tools turn:
    # a turn that silently runs with no drumpack tools is the successful-looking
    # run that did nothing. Read from ctx.cwd -- the SAME path serve loads packs
    # from -- so this reports on the exact list the running engine would use.
    print()
    pack_list = packs.read_pack_list(ctx.cwd)
    pack_warning = packs.pack_list_visibility(pack_list)
    if pack_warning is None:
        print(f"drumpacks: {len(pack_list.paths)} declared in {pack_list.source}")
    else:
        print(f"drumpacks: NONE declared -- {pack_warning}")

    # Draining is a state the operator asked for and can forget they asked
    # for. A drained scheduler is enabled, healthy, and inert -- which reads
    # exactly like a broken one from every other signal on this page.
    print()
    drain_request = drain_mod.drain_state(runs_dir)
    if drain_request is None:
        print("draining: no (scheduler is starting runs normally)")
    else:
        print(f"draining: YES -- {drain_request.get('reason')}")
        print(
            f"          requested at {drain_request.get('requested_at')} "
            f"by pid {drain_request.get('requested_by_pid')}"
        )
        print(
            f"          NO NEW RUNS START until this is cleared: "
            f"`drumbeat drain --workspace {args.workspace} --clear`"
        )

    # Orphan pins: the named cost of keying the pin store by slug (section
    # 5). A rename is a cold start plus a stranded entry; this is the fence.
    print()
    try:
        orphans = session_pins.orphans(
            ctx.runs_dir, known_slugs=_known_slugs(ctx.automations_dir)
        )
        print(
            f"orphan pins: {len(orphans)}"
            + (f" ({', '.join(orphans)})" if orphans else "")
        )
    except (session_pins.PinStoreError, AutomationError, OSError) as exc:
        print(f"orphan pins: UNKNOWN ({exc})")

    # Ambient workspace override (failure class 15). Rotations and drills
    # run from a human's terminal, where a lingering
    # debug export silently re-buckets every session id
    # (paths.derive_workspace_slug honors it first) and records pins under a
    # slug no service unit will ever derive.
    ambient = os.environ.get("AMPLIFIER_AGENT_WORKSPACE", "").strip()
    if ambient:
        print(
            f"ambient workspace override SET: AMPLIFIER_AGENT_WORKSPACE={ambient!r} "
            "-- every session id resolved in THIS shell is bucketed under that "
            "slug, not the cwd-derived one the service uses. Unset it before "
            "running pin surgery."
        )
    else:
        print("ambient workspace override: not set")

    # Workspace git drift. Read-only, and reported
    # even when the workspace is not a checkout, because "you have no
    # archive" is a fact an operator should meet in a health check rather
    # than on the day the disk dies. This block is also PUSHED, not only
    # pulled: the reference deployment injects the same three numbers into a
    # `notify: always` automation, because a doctor nobody schedules is a
    # check-engine light nobody reads.
    print()
    for line in workspace_git.format_block(
        workspace_git.inspect(ctx.workspace, data_dir=ctx.runs_dir),
        workspace=ctx.workspace,
    ):
        print(line)

    return staleness.exit_code_for([report])


def _cmd_sessions(args: argparse.Namespace) -> int:
    """List every pin. Engine state that nothing lists comes back as class 2."""
    ctx = _ctx(args)
    try:
        pins = session_pins.read_all(ctx.runs_dir)
    except session_pins.PinStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"pin store: {session_pins.pins_path(ctx.runs_dir)}")
    print()
    if not pins:
        print("no pinned sessions -- every automation's next run starts fresh")
    else:
        rows = [("SLUG", "SESSION", "CREATED", "BY")]
        for slug in sorted(pins):
            pin = pins[slug]
            rows.append(
                (slug, pin.session_id, pin.created_at or "—", pin.created_by or "—")
            )
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print(
                "  ".join(
                    cell.ljust(width) for cell, width in zip(row, widths, strict=True)
                )
            )

    try:
        orphans = session_pins.orphans(
            ctx.runs_dir, known_slugs=_known_slugs(ctx.automations_dir)
        )
    except (AutomationError, OSError) as exc:
        print()
        print(f"orphan pins: UNKNOWN (could not enumerate automations: {exc})")
        return 1

    print()
    print(f"orphan pins: {len(orphans)}")
    for slug in orphans:
        print(
            f"  {slug} -> {pins[slug].session_id} (no automation file with this "
            "slug; renaming an automation is a fresh conversation by design -- "
            "`drumbeat rotate-session` to retire the entry)"
        )
    return 0


def _cmd_session_health(args: argparse.Namespace) -> int:
    """Per-automation session health: consecutive failures, ceiling/drift detail.

    Makes ``session_health.health_for`` reachable. Before this command
    existed, ``health_for`` (and the ``_scan_recent_runs`` consecutive-
    failure counter it wraps) had ZERO callers anywhere in src/ -- computed
    on every invocation and then thrown away. This is the first caller.
    """
    ctx = _ctx(args)
    automations, _failures = automation_mod.load_all_tolerant(ctx.automations_dir)
    try:
        reports = session_health.health_for(
            automations,
            runs_dir=ctx.runs_dir,
            agent_home=paths.amplifier_agent_home(),
            workspace=paths.derive_workspace_slug(ctx.cwd),
        )
    except session_pins.PinStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not reports:
        print("no automations found")
        return 0

    rows = [("AUTOMATION", "SESSION", "CONSEC FAILS", "DETAIL")]
    for report in reports:
        rows.append(
            (
                report.automation,
                report.session_id or "\u2014",
                str(report.consecutive_failures),
                report.detail,
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print(
            "  ".join(
                cell.ljust(width) for cell, width in zip(row, widths, strict=True)
            )
        )
    return 0


def _cmd_rotate_session(args: argparse.Namespace) -> int:
    """Deliberately abandon one automation's pinned conversation."""
    ctx = _ctx(args)
    slug = args.slug
    try:
        removed = session_pins.delete(slug, runs_dir=ctx.runs_dir)
    except session_pins.PinStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if removed is None:
        # Exit non-zero: "rotated nothing, exited 0" is the exact shape this
        # verb replaced in the consumer's CLI. A rotate that found no pin did not
        # do what the operator asked.
        print(
            f"{slug}: no pinned session to clear in "
            f"{session_pins.pins_path(ctx.runs_dir)} -- NOTHING WAS ROTATED. "
            "Check `drumbeat sessions` (and that --data-dir matches the "
            "running engine's).",
            file=sys.stderr,
        )
        return 1

    automation_path = ctx.automations_dir / f"{slug}.md"
    log_session_rotation(
        automation_name=slug,
        automation_slug=slug,
        automation_path=automation_path,
        old_session_id=removed.session_id,
        reason=args.reason,
        log_path=ctx.runs_dir / "session_rotations.jsonl",
    )
    print(
        f"{slug}: cleared pinned session {removed.session_id} "
        f"(recorded in {ctx.runs_dir / 'session_rotations.jsonl'})"
    )
    print("The next run of this automation starts a fresh conversation.")
    return 0


def _cmd_drain(args: argparse.Namespace) -> int:
    runs_dir = _runs_dir(args)

    if args.clear:
        cleared = drain_mod.clear_drain(runs_dir)
        print(
            "drain flag cleared -- scheduling resumes on the next tick"
            if cleared
            else "no drain flag was set; nothing to clear"
        )
        return 0

    if args.status:
        status = drain_mod.check_drained(runs_dir, scheduler_pid=args.pid)
        print(status.render())
        return 0 if status.drained else 1

    if not args.reason:
        print(
            "error: --reason is required to drain (no default). A drained "
            "scheduler that cannot say why is indistinguishable from a broken one.",
            file=sys.stderr,
        )
        return 2

    path = drain_mod.set_drain(runs_dir, reason=args.reason)
    print(f"drain flag set: {path}")
    print(f"reason: {args.reason}")
    print("The scheduler will start no new runs. Work in flight is left alone.")
    print()

    if not args.wait:
        print("Re-check with: drumbeat drain --workspace <dir> --status [--pid N]")
        return 0

    print(f"waiting for drain (timeout {args.timeout}s, polling every {args.poll}s)...")

    def _report(status: drain_mod.DrainStatus) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%SZ")
        if status.drained:
            print(f"  [{stamp}] DRAINED")
            return
        print(f"  [{stamp}] not yet: {'; '.join(status.blockers)}")

    final = drain_mod.wait_until_drained(
        runs_dir,
        scheduler_pid=args.pid,
        timeout_seconds=args.timeout,
        poll_seconds=args.poll,
        on_poll=_report,
    )
    print()
    print(final.render())
    return 0 if final.drained else 1


def _cmd_sweep(args: argparse.Namespace) -> int:
    runs_dir = _runs_dir(args)
    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            print(
                f"error: --since must be ISO-8601 UTC like 2026-08-09T16:00:00Z, "
                f"got {args.since!r}",
                file=sys.stderr,
            )
            return 2
    findings = invalid_runs.sweep(runs_dir, since=since)
    print(invalid_runs.render(findings, since=since))
    return 0 if not findings else 1


def _cmd_api_key(args: argparse.Namespace) -> int:
    runs_dir = _runs_dir(args)
    path = api_key_mod.api_key_path(runs_dir)
    key = api_key_mod.ensure_api_key(runs_dir)
    if args.show:
        print(key)
        return 0
    print(f"engine API key file: {path}")
    print("(re-run with --show to print the key itself)")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a fresh workspace: the four dirs plus generic default files.

    Deliberately NOT a ``--workspace`` verb: init MAKES a workspace, so it takes
    a plain positional directory (default cwd) and creates it if absent. Every
    other verb operates on a workspace that already exists.
    """
    target = Path(args.dir).expanduser()
    try:
        result = workspace_init.scaffold(target, force=args.force)
    except workspace_init.InitError as exc:
        # Refusal is fail-loud and total (nothing was written). Non-zero exit:
        # a second init that clobbered nothing did not do what the caller asked.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Initialized drumbeat workspace at {result.target}")
    print()
    print("directories:")
    for rel in workspace_init.SCAFFOLD_DIRS:
        state = "created" if rel in result.created_dirs else "exists"
        print(f"  {rel}/  ({state})")
    print()
    print("files:")
    for rel in workspace_init.SCAFFOLD_FILES:
        if rel in result.overwritten_files:
            state = "overwritten"
        elif rel in result.created_files:
            state = "created"
        else:
            state = "wrote"
        print(f"  {rel}  ({state})")
    print()
    print("Next:")
    print(f"  drumbeat doctor --workspace {result.target}")
    print(f"  drumbeat serve  --workspace {result.target} --port 9100")
    return 0


def _cmd_service_install(args: argparse.Namespace) -> int:
    return service_mod.install(
        workspace=args.workspace,
        port=args.port,
        host=args.host,
        data_dir=getattr(args, "data_dir", None),
        skip_turn_verify=getattr(args, "skip_turn_verify", False),
    )


def _cmd_service_status(_args: argparse.Namespace) -> int:
    return service_mod.status()


def _cmd_service_uninstall(_args: argparse.Namespace) -> int:
    return service_mod.uninstall()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drumbeat",
        description="The automation engine: scheduler, runner, and engine HTTP API.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _with_workspace(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument(
            "--workspace",
            required=True,
            help="the consumer workspace this engine serves (contains automations/, "
            "prompts/, guidance/, drumpacks.txt). Policy only. Required, no default.",
        )
        sub.add_argument(
            "--data-dir",
            default=None,
            help="where the engine keeps its STATE -- run artifacts, the delivery "
            "outbox, locks, the api key, the staleness fingerprint (default: "
            "<workspace>/runs). Point it outside the workspace when the "
            "workspace is a git-tracked policy tree, so no git operation there "
            "can destroy server state. Every command that takes --workspace "
            "takes this too: pass it consistently or they will read different "
            "directories.",
        )
        return sub

    # init MAKES a workspace, so -- unlike every other verb -- it takes a plain
    # positional directory (default cwd), not --workspace, and creates it.
    init_parser = subparsers.add_parser(
        "init",
        help="scaffold a fresh workspace (dirs + generic default files)",
    )
    init_parser.add_argument(
        "dir",
        nargs="?",
        default=".",
        help="directory to scaffold (default: current directory; created if absent)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite scaffold files that already exist (init refuses by "
        "default rather than clobber your work)",
    )
    init_parser.set_defaults(func=_cmd_init)

    serve_parser = _with_workspace(
        subparsers.add_parser("serve", help="run the engine (scheduler + HTTP API)")
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=serve_mod.DEFAULT_PORT,
        help=f"loopback port for the engine API (default: {serve_mod.DEFAULT_PORT}; "
        "one engine per workspace, so give each its own port and keep a note of "
        "which is which before first bind)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; loopback only, by design (default: 127.0.0.1)",
    )
    serve_parser.set_defaults(func=_cmd_serve)

    doctor_parser = _with_workspace(
        subparsers.add_parser(
            "doctor", help="is the running engine executing the code on disk?"
        )
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

    drain_parser = _with_workspace(
        subparsers.add_parser(
            "drain",
            help="stop starting runs, and verify it is safe to kill the scheduler",
        )
    )
    drain_parser.add_argument(
        "--reason", help="why (required to set a drain; there is no default)"
    )
    drain_parser.add_argument(
        "--clear", action="store_true", help="clear the drain flag and resume"
    )
    drain_parser.add_argument(
        "--status", action="store_true", help="report drain state without changing it"
    )
    drain_parser.add_argument(
        "--wait", action="store_true", help="poll until drained (or --timeout)"
    )
    drain_parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="the scheduler's pid, so in-flight turns can be attributed to it",
    )
    drain_parser.add_argument("--timeout", type=float, default=1800.0)
    drain_parser.add_argument("--poll", type=float, default=5.0)
    drain_parser.set_defaults(func=_cmd_drain)

    sweep_parser = _with_workspace(
        subparsers.add_parser(
            "sweep", help="invalid-run sweep: runs with no delivery intent (section 6)"
        )
    )
    sweep_parser.add_argument(
        "--since",
        help="only runs at/after this ISO-8601 UTC time (2026-08-09T16:00:00Z)",
    )
    sweep_parser.set_defaults(func=_cmd_sweep)

    # ---- the three session-pin store verbs ----
    #
    # All three take --data-dir, and the module docstring says so, because
    # the workspace/data-dir pairing is non-default once the workspace is a
    # policy repo: a defaulting rotate would open a DIFFERENT (probably
    # empty) store and print "no pinned session to clear" while the real pin
    # sat untouched.

    sessions_parser = _with_workspace(
        subparsers.add_parser(
            "sessions",
            help="list which amplifier-agent conversation each automation resumes",
        )
    )
    sessions_parser.set_defaults(func=_cmd_sessions)

    session_health_parser = _with_workspace(
        subparsers.add_parser(
            "session-health",
            help="per-automation session health: consecutive failures, "
            "ceiling/drift detail (see drumbeat.session_health)",
        )
    )
    session_health_parser.set_defaults(func=_cmd_session_health)

    rotate_parser = _with_workspace(
        subparsers.add_parser(
            "rotate-session",
            help="deliberately abandon one automation's pinned conversation",
        )
    )
    rotate_parser.add_argument("slug", help="the automation's slug")
    rotate_parser.add_argument(
        "--reason",
        required=True,
        help="why (required, no default). Rotation abandons accumulated "
        "session memory; a rotation that cannot say why is indistinguishable "
        "from an accident.",
    )
    rotate_parser.set_defaults(func=_cmd_rotate_session)

    key_parser = _with_workspace(
        subparsers.add_parser("api-key", help="this instance's engine API key")
    )
    key_parser.add_argument(
        "--show", action="store_true", help="print the key to stdout"
    )
    key_parser.set_defaults(func=_cmd_api_key)

    # ---- run the engine under the platform's own supervisor ----
    #
    # `serve` is the engine in the foreground; leaving it running is a separate
    # job that belongs to systemd --user (Linux) or launchd (macOS). These verbs
    # generate the correct unit/plist, install it, and VERIFY /api/health before
    # reporting success -- an install that walked away could enable a unit that
    # never bound its port and still look like it worked.
    service_parser = subparsers.add_parser(
        "service",
        help="install/status/uninstall the engine as a supervised service "
        "(systemd --user on Linux, launchd on macOS)",
    )
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)

    svc_install = service_sub.add_parser(
        "install",
        help="generate + install the unit/plist, start it, and verify health",
    )
    svc_install.add_argument(
        "--workspace",
        required=True,
        help="the consumer workspace this engine serves (contains automations/, "
        "prompts/, guidance/, drumpacks.txt). Required, no default -- the same "
        "contract as `serve`.",
    )
    svc_install.add_argument(
        "--data-dir",
        default=None,
        help="where the engine keeps its STATE (default: <workspace>/runs). "
        "Baked into serve, drain and clear alike so all three agree; pass it "
        "when the workspace is a git-tracked policy tree.",
    )
    svc_install.add_argument(
        "--port",
        type=int,
        default=serve_mod.DEFAULT_PORT,
        help=f"loopback port for the engine API (default: {serve_mod.DEFAULT_PORT})",
    )
    svc_install.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; loopback only, by design (default: 127.0.0.1)",
    )
    svc_install.add_argument(
        "--skip-turn-verify",
        action="store_true",
        help="install without proving the supervised unit can execute one real "
        "turn. NOT recommended: /api/health can pass on a unit whose every "
        "scheduled run fails (e.g. missing provider key, or a tool not on the "
        "unit's PATH). Off by default -- install verifies a real turn.",
    )
    svc_install.set_defaults(func=_cmd_service_install)

    svc_status = service_sub.add_parser(
        "status",
        help="report the supervised unit's state and probe /api/health",
    )
    svc_status.set_defaults(func=_cmd_service_status)

    svc_uninstall = service_sub.add_parser(
        "uninstall",
        help="stop, disable, remove the unit/plist, and verify it is gone",
    )
    svc_uninstall.set_defaults(func=_cmd_service_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
