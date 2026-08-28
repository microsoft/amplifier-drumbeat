"""The engine half of the management API: automations, prompts, and run
history -- the CRUD surface the engine owns and the
engine (the HTTP layer itself arrives in step 3; today these are library
functions the consumer's service routes to).

The consumer keeps its own management module for everything domain-side
(guidance files, notification stats, service status) and delegates the
automations/runs/prompts half here.

FAIL LOUD, NO FALLBACKS: a write that would leave a file the loader can't
parse never reaches disk (validate-then-write for automations); a read of
something that doesn't exist is a 404, never an empty/default value stood
in for the real answer.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from drumbeat import fsutil, prompts, runner, session_health, session_pins
from drumbeat.automation import Automation, AutomationError
from drumbeat.automation import load as load_automation
from drumbeat.automation import load_all as load_automations
from drumbeat.paths import workspace_for_automations_dir
from drumbeat.scheduler import parse_schedule, seconds_until_next_fire

_MAX_RUN_REPLY_CHARS = 20_000
_MAX_STDERR_LINES = 200
_DEFAULT_RUNS_LIMIT = 50

# The dead-brain outage class: a run can exit 0 (``failed: False``) while its
# reply is itself a statement of failure ("Error: No providers available"),
# and a consumer that only reads ``failed``/``notified`` off the run-record
# contract has no way to tell. ``final_reply_preview`` closes that: a bounded
# excerpt of the reply every run-record row carries (list AND detail, both
# on-disk shapes), so a doctor-style consumer can classify a suspicious reply
# without fetching every step body. Bounded so a megabyte reply from a
# misbehaving turn cannot blow up the run listing it appears in.
_FINAL_REPLY_PREVIEW_MAX_CHARS = 200
# Never an invented string, and never indistinguishable from a genuinely
# empty-but-real reply: a run that aborted before any turn ran (see
# ``runner.FINAL_REPLY_RULE_ABORTED``) has ``final_reply == ""``, and an
# in-flight run (``status.json``) has no ``final_reply`` key at all. Both
# get this explicit marker rather than an empty string a consumer could
# misread as "the model replied with nothing".
_NO_REPLY_MARKER = "(no reply)"


def _final_reply_preview(final_reply: Any) -> str:
    """Bound ``final_reply`` (raw, possibly absent) to a single-line excerpt.

    The ONE place this truncation happens -- every caller that needs a
    reply preview (the run-record contract, the per-automation ``last_run``
    summary) goes through here, so the bound and the no-reply marker can
    never drift apart between surfaces the way the run-record contract
    itself once did (see ``_RUN_RECORD_REQUIRED_FIELDS`` above).
    """
    if isinstance(final_reply, str) and final_reply.strip():
        return final_reply.strip().replace("\n", " ")[:_FINAL_REPLY_PREVIEW_MAX_CHARS]
    return _NO_REPLY_MARKER


# Filename for an in-flight run's status record. Distinct from
# ``result.json`` (written exactly once, at the very end, by
# ``runner._persist_run``) -- this file exists solely to make a run
# observable between "202 Accepted" and "result.json exists", so a client
# polling immediately after "run now" gets a real 200, never a 404 for a
# run id the server itself just minted. See ``run_automation_async`` and
# ``get_run_detail``.
_RUN_STATUS_FILENAME = "status.json"

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_FRONTMATTER_BLOCK_RE = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*\n?)(.*)\Z", re.DOTALL)
_ENABLED_LINE_RE = re.compile(r"(?m)^([ \t]*)enabled:\s*(?:true|false)\s*$")
_NAME_LINE_RE = re.compile(r"(?m)^([ \t]*)name:.*$")


class ManagementError(Exception):
    """Raised by any management_api function; carries the HTTP status to send."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class EngineContext:
    """The resolved directories the engine operates on for one consumer.

    Every field required, no defaults -- this is the section-7.2 workspace
    handoff in miniature: automations, prompts, a data dir, and the cwd the
    agent runs under.
    """

    automations_dir: Path
    prompts_dir: Path
    runs_dir: Path
    cwd: Path

    @property
    def workspace(self) -> Path:
        """The policy root the directories above were derived from.

        A property, not a fifth field, deliberately: ``resolve_workspace``
        derives ``automations_dir`` from the workspace and refuses to start
        without it, so the parent link always holds and can never disagree
        with the fields beside it. A stored copy could -- and "which
        workspace was that, exactly" having two answers is the shape of
        every path-resolution wall this migration hit.

        Delegated to ``paths.workspace_for_automations_dir`` rather than
        inlining ``.parent`` here: ``capabilities`` needs the identical
        derivation from the identical input, and when it had its own copy
        the copy grew a ``.resolve()`` and started reporting a different
        workspace than the one turns actually run in. One implementation is
        the only structural guard against that recurring.
        """
        return workspace_for_automations_dir(self.automations_dir)


# ---- shared helpers ----


def _atomic_write_with_backup(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically, keeping a ``.bak`` of any prior version.

    These are hand-authored files (automations, prompts, guidance policy) --
    a bad write must never lose real work, so any existing version is
    copied to ``<name>.bak`` before the new content lands via
    temp-file-plus-rename (the same atomicity pattern the consumer's stores
    use).
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        backup_path = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup_path)

    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _validate_automation_content(content: str, *, label: str) -> Automation:
    """Parse+validate automation markdown without writing it anywhere real.

    Writes to a throwaway temp file (so ``automation.load``'s real parsing
    logic runs unmodified) purely to get a validated ``Automation`` back;
    the temp file is always removed. Raises ``ManagementError(400, ...)``
    on any parse/validation failure, with the message re-labeled to refer
    to ``label`` instead of the temp path (which the caller never sees).
    """
    fd, tmp_path_str = tempfile.mkstemp(suffix=".md")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            return load_automation(tmp_path)
        except AutomationError as exc:
            raise ManagementError(400, f"{label}: {exc.problem}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ==== Automations ====


def _automation_summary(a: Automation) -> dict[str, Any]:
    return {
        "name": a.name,
        "slug": a.slug,
        "enabled": a.enabled,
        "trigger": {"type": a.trigger.type, "expression": a.trigger.expression},
        "notify": a.notify,
        "step_count": len(a.steps),
        "requires": a.requires,
        "path": str(a.path),
    }


def _load_automations_or_500(automations_dir: Path) -> list[Automation]:
    try:
        return load_automations(automations_dir)
    except AutomationError as exc:
        raise ManagementError(500, f"failed to load automations: {exc}") from exc


def _get_automation_or_404(slug: str, automations_dir: Path) -> Automation:
    for a in _load_automations_or_500(automations_dir):
        if a.slug == slug:
            return a
    raise ManagementError(404, f"no automation with slug {slug!r}")


def _last_run_summary(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Shape one ``_iter_run_records`` entry into the ``last_run`` field, or None.

    ``entry`` must be the single most recent ATTEMPT (see ``_latest_attempt``
    below) -- never the most recent *success*. Carrying ``error`` alongside
    ``failed`` is the whole point: a failed last run must be able to say why,
    not just that it happened.

    ``final_reply_preview`` carries the same dead-brain signal here as on the
    runs API row: this is the field a per-automation health scan (``list
    automations`` -> each one's ``last_run``) sees WITHOUT a second request
    per automation, and ``failed`` alone cannot show a reply that is itself
    a statement of failure on an otherwise-exit-0 run.
    """
    if entry is None:
        return None
    return {
        "run_id": entry["run_id"],
        "started_at": entry["started_at"],
        "finished_at": entry["finished_at"],
        "failed": entry["failed"],
        "error": entry.get("error"),
        "final_reply_preview": _final_reply_preview(entry.get("final_reply")),
    }


def _latest_attempt(slug: str, runs_dir: Path) -> dict[str, Any] | None:
    """The single most recent run ATTEMPT for ``slug`` -- success or failure.

    Mechanism 2 (see the task spec this implements): reuses
    ``_iter_run_records`` (already sorted newest-attempt-first by
    ``started_at``, reference:
    ``~/dev/amplifier-attention-manager/drumbeat/src/drumbeat/management_api.py:644``)
    and takes element 0. It must NEVER filter on ``failed`` -- the entire
    bug this fixes is a served "last run" that silently means "last
    success", which lets a failing automation display a stale success
    timestamp and read as healthy.
    """
    entries = _iter_run_records(runs_dir, automation_filter=slug)
    return entries[0] if entries else None


def _read_pins_or_report(
    runs_dir: Path,
) -> tuple[dict[str, session_pins.Pin] | None, str | None]:
    """Read the session pin store ONCE for a whole listing.

    ``list_automations`` is a list endpoint -- reading the store once here
    and passing it to every automation's status computation (rather than
    once per automation) is the difference between one file read and N.

    FAIL LOUD, NO SILENT DEGRADATION (this project's first law) cuts both
    ways here: a corrupt store must never be read as "nothing pinned" (that
    would report the whole fleet as healthy-and-unpinned, session_health's
    own words for the single most misleading answer it could give) -- but it
    also must never turn the entire automations listing into a 500 over one
    broken file that a `drumbeat rotate-session`/pin-store fix can repair
    independently of every other automation. The compromise: print the
    failure to stderr naming the store path, and let the caller render each
    automation's ``session_status`` as the honest ``"unknown"`` instead of
    ``"healthy"``.
    """
    try:
        return session_pins.read_all(runs_dir), None
    except session_pins.PinStoreError as exc:
        path = session_pins.pins_path(runs_dir)
        message = f"cannot read session pin store {path}: {exc}"
        sys.stderr.write(f"[management-api] {message}\n")
        return None, message


def _status_fields(
    a: Automation,
    *,
    runs_dir: Path,
    pins: dict[str, session_pins.Pin] | None,
    pins_error: str | None,
) -> dict[str, Any]:
    """``last_run`` / ``consecutive_failures`` / ``session_status`` for one automation.

    Mechanisms 2 and 4 (see the task spec this implements) -- computed
    today, served nowhere before this. ``pins`` is the whole store, read
    ONCE by the caller (see ``_read_pins_or_report``), never re-read here.
    """
    last_run = _last_run_summary(_latest_attempt(a.slug, runs_dir))

    if pins_error is not None:
        return {
            "last_run": last_run,
            "consecutive_failures": 0,
            "session_status": "unknown",
        }

    pin = (pins or {}).get(a.slug)
    if pin is None:
        # Nothing is wrong with an automation that has simply never been
        # pinned -- its next run creates a fresh session. That is healthy,
        # not unknown and not degraded.
        return {
            "last_run": last_run,
            "consecutive_failures": 0,
            "session_status": "healthy",
        }

    health = session_health.run_health(
        a.slug, session_id=pin.session_id, runs_dir=runs_dir
    )
    return {
        "last_run": last_run,
        "consecutive_failures": health.consecutive_failures,
        "session_status": health.state,
    }


def list_automations(ctx: EngineContext) -> list[dict[str, Any]]:
    automations = _load_automations_or_500(ctx.automations_dir)
    pins, pins_error = _read_pins_or_report(ctx.runs_dir)
    return [
        {
            **_automation_summary(a),
            **_status_fields(
                a, runs_dir=ctx.runs_dir, pins=pins, pins_error=pins_error
            ),
        }
        for a in automations
    ]


def get_automation_detail(slug: str, ctx: EngineContext) -> dict[str, Any]:
    a = _get_automation_or_404(slug, ctx.automations_dir)
    content = a.path.read_text(encoding="utf-8")
    pins, pins_error = _read_pins_or_report(ctx.runs_dir)
    return {
        **_automation_summary(a),
        "content": content,
        "steps": [{"id": s.id, "prompt": s.prompt, "label": s.label} for s in a.steps],
        **_status_fields(a, runs_dir=ctx.runs_dir, pins=pins, pins_error=pins_error),
    }


def create_automation(content: str, ctx: EngineContext) -> dict[str, Any]:
    parsed = _validate_automation_content(content, label="new automation")
    automations_dir = Path(ctx.automations_dir).expanduser()
    automations_dir.mkdir(parents=True, exist_ok=True)
    target_path = automations_dir / f"{parsed.slug}.md"
    if target_path.exists():
        raise ManagementError(
            409, f"automation with slug {parsed.slug!r} already exists at {target_path}"
        )
    _atomic_write_with_backup(target_path, content)
    final = load_automation(target_path)
    return _automation_summary(final)


def update_automation(slug: str, content: str, ctx: EngineContext) -> dict[str, Any]:
    a = _get_automation_or_404(slug, ctx.automations_dir)
    parsed = _validate_automation_content(content, label=f"{slug}.md")
    if parsed.slug != slug:
        raise ManagementError(
            400,
            f"updated content's name produces slug {parsed.slug!r}, which does not "
            f"match the existing slug {slug!r} -- renaming via PUT is not supported "
            "(create a new automation with POST and DELETE the old one instead)",
        )
    _atomic_write_with_backup(a.path, content)
    final = load_automation(a.path)
    return _automation_summary(final)


def _toggle_enabled_in_frontmatter(text: str, enabled: bool, *, label: str) -> str:
    match = _FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        raise ManagementError(
            500, f"{label}: frontmatter block not found while toggling enabled"
        )
    open_delim, frontmatter, close_delim, body = match.groups()

    new_value = "true" if enabled else "false"
    enabled_match = _ENABLED_LINE_RE.search(frontmatter)
    if enabled_match:
        indent = enabled_match.group(1)
        new_frontmatter = _ENABLED_LINE_RE.sub(
            f"{indent}enabled: {new_value}", frontmatter, count=1
        )
    else:
        # No explicit `enabled:` line (defaults to true per automation.py) --
        # insert one right after `name:`, matching its indentation.
        name_match = _NAME_LINE_RE.search(frontmatter)
        if not name_match:
            raise ManagementError(
                500,
                f"{label}: could not locate 'name:' line to anchor a new 'enabled:' line",
            )
        indent = name_match.group(1)
        insert_at = name_match.end()
        new_frontmatter = (
            frontmatter[:insert_at]
            + f"\n{indent}enabled: {new_value}"
            + frontmatter[insert_at:]
        )

    return open_delim + new_frontmatter + close_delim + body


def set_automation_enabled(
    slug: str, enabled: bool, ctx: EngineContext
) -> dict[str, Any]:
    a = _get_automation_or_404(slug, ctx.automations_dir)
    text = a.path.read_text(encoding="utf-8")
    new_text = _toggle_enabled_in_frontmatter(text, enabled, label=a.path.name)

    parsed = _validate_automation_content(new_text, label=f"{slug}.md (enable toggle)")
    if parsed.slug != slug:
        raise ManagementError(
            500, f"{a.path}: enable toggle unexpectedly changed the automation's slug"
        )
    if parsed.enabled != enabled:
        raise ManagementError(
            500, f"{a.path}: enable toggle did not take effect as expected"
        )

    _atomic_write_with_backup(a.path, new_text)
    final = load_automation(a.path)
    return _automation_summary(final)


def delete_automation(slug: str, ctx: EngineContext) -> None:
    a = _get_automation_or_404(slug, ctx.automations_dir)
    if a.path.is_file():
        backup_path = a.path.with_name(a.path.name + ".bak")
        shutil.copy2(a.path, backup_path)
    a.path.unlink()


# ==== Validate (structured editor pre-save + raw-mode parse-on-exit) ====

_YAML_ERROR_LINE_RE = re.compile(r"\bline (\d+)\b")
_QUOTED_LINE_RE = re.compile(r": '(.*)'$")


def _guess_error_line(content: str, problem: str) -> int | None:
    """Best-effort line number for a parser error, for the raw-mode escape
    hatch to highlight the offending line. Only the cases below are
    locatable with real confidence; everything else honestly returns
    ``None`` rather than guessing wrong -- a wrong highlighted line would be
    worse than none:

    - PyYAML's own error messages embed a 0-indexed line number *within the
      frontmatter block* (e.g. ``'...in "<unicode string>", line 3, ...'``)
      -- offset by 2 (the opening ``---`` delimiter line, plus 1-indexing)
      to land on the real file line.
    - Step-parsing errors that quote the offending raw line verbatim (via
      Python's ``!r`` formatting) can be located by searching for that
      exact line's text in the candidate content.
    """
    if "invalid YAML frontmatter" in problem:
        match = _YAML_ERROR_LINE_RE.search(problem)
        return int(match.group(1)) + 2 if match else None

    quoted = _QUOTED_LINE_RE.search(problem)
    if quoted:
        needle = quoted.group(1)
        for i, line in enumerate(content.splitlines(), start=1):
            if line == needle or line.strip() == needle:
                return i
    return None


def _expression_line(content: str) -> int | None:
    """Line number of the frontmatter's ``expression:`` key, or None.

    Same honesty rule as ``_guess_error_line``: located only when there is
    exactly one candidate. Two ``expression:`` lines means the file is doing
    something this cannot resolve, and no highlight beats the wrong one.
    """
    hits = [
        i
        for i, line in enumerate(content.splitlines(), start=1)
        if line.strip().startswith("expression:")
    ]
    return hits[0] if len(hits) == 1 else None


def validate_automation_content(content: str, ctx: EngineContext) -> dict[str, Any]:
    """Run the REAL ``automation.py`` parser over candidate text.

    The Kotlin client must never reimplement this parsing logic -- drift
    between a client-side reimplementation and this parser would be the
    worst possible bug (the editor says fine, the runner says no). Used by
    the structured editor before every save is composed into a whole-file
    write, and by the raw-text escape hatch on exit.

    Returns ``{"valid": True, ...summary, "steps": [...]}`` on success.
    Raises ``ManagementError(400, ...)`` on failure, with the parser's exact
    message plus a best-effort ``" (line N)"`` suffix when locatable (see
    ``_guess_error_line``).

    ``ctx`` is currently unused but kept in the signature so a future
    validation rule needing server state (e.g. cross-checking ``requires:``
    against ``get_capabilities``) doesn't force a signature change on every
    caller.
    """
    del ctx  # not needed yet -- see docstring
    try:
        parsed = _validate_automation_content(content, label="candidate")
    except ManagementError as exc:
        line = _guess_error_line(content, exc.message)
        message = exc.message if line is None else f"{exc.message} (line {line})"
        raise ManagementError(exc.status, message) from exc
    # The schedule expression, through the SAME parser the scheduler runs.
    # The automation parser only checks that `expression` is a non-empty
    # string, so `every other tuesday-ish` validated clean and then failed
    # every poll tick forever with a log line nobody was watching -- an
    # automation that saves green, shows Active, and never fires. Validating
    # here (rather than teaching the automation parser about schedules) keeps
    # the grammar owned by the one module that implements it, and the error
    # already enumerates every supported form.
    if parsed.trigger.type == "schedule":
        try:
            parse_schedule(parsed.trigger.expression or "")
        except ValueError as exc:
            line = _expression_line(content)
            message = str(exc) if line is None else f"{exc} (line {line})"
            raise ManagementError(400, message) from exc
    return {
        "valid": True,
        "name": parsed.name,
        "slug": parsed.slug,
        "enabled": parsed.enabled,
        "trigger": {
            "type": parsed.trigger.type,
            "expression": parsed.trigger.expression,
        },
        "notify": parsed.notify,
        "requires": parsed.requires,
        "steps": parsed.steps,
    }


# ==== Export / import (share, QR, curated library) ====

_EXPORT_SESSION_LINE_RE = re.compile(r"(?m)^[ \t]*session:.*$")
_EXPORT_SESSION_WORKSPACE_LINE_RE = re.compile(r"(?m)^[ \t]*session_workspace:.*$")


def export_automation(slug: str, ctx: EngineContext) -> dict[str, Any]:
    """Produce the share/export payload for an automation.

    Same file format, minus machine-local fields that are meaningless (or a
    privacy leak) off this device: strips ``session:``/``session_workspace:``
    -- ``session_workspace`` encodes a literal home-directory path -- and
    forces ``enabled: false``. Both scrubs are unconditional; there is no
    flag to skip either one, and the result is re-validated (and the scrub
    re-checked) before being returned, so a scrub that silently failed to
    take effect is a loud 500, never a quiet leak.
    """
    a = _get_automation_or_404(slug, ctx.automations_dir)
    text = a.path.read_text(encoding="utf-8")

    match = _FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        raise ManagementError(
            500, f"{a.path}: frontmatter block not found while exporting"
        )
    open_delim, frontmatter, close_delim, body = match.groups()

    session_removed = bool(_EXPORT_SESSION_LINE_RE.search(frontmatter))
    workspace_removed = bool(_EXPORT_SESSION_WORKSPACE_LINE_RE.search(frontmatter))
    scrubbed_frontmatter = re.sub(r"(?m)^[ \t]*session:.*\n?", "", frontmatter)
    scrubbed_frontmatter = re.sub(
        r"(?m)^[ \t]*session_workspace:.*\n?", "", scrubbed_frontmatter
    )
    scrubbed_text = open_delim + scrubbed_frontmatter + close_delim + body

    forced_off_text = _toggle_enabled_in_frontmatter(
        scrubbed_text, False, label=f"{slug}.md (export scrub)"
    )

    parsed = _validate_automation_content(
        forced_off_text, label=f"{slug}.md (export scrub)"
    )
    if _EXPORT_SESSION_LINE_RE.search(forced_off_text) or (
        _EXPORT_SESSION_WORKSPACE_LINE_RE.search(forced_off_text)
    ):
        raise ManagementError(
            500, f"{a.path}: export scrub did not remove session fields as expected"
        )
    if parsed.enabled is not False:
        raise ManagementError(
            500, f"{a.path}: export scrub did not force enabled: false as expected"
        )

    return {
        "slug": slug,
        "filename": f"{slug}.md",
        "content": forced_off_text,
        "scrubbed": {
            "session": session_removed,
            "session_workspace": workspace_removed,
        },
    }


def import_automation(content: str, ctx: EngineContext) -> dict[str, Any]:
    """Create an automation from imported/shared content -- arrives disabled
    ALWAYS, regardless of what the incoming content says.

    An imported automation is untrusted prose about to be handed to an
    agent holding live credentials. Forcing ``enabled: false`` here, at the
    server, means the guarantee holds even if the mobile app has a bug or
    is bypassed entirely (a raw HTTP client hitting this endpoint directly).
    The client's Review screen is a UX nicety on top of this; this is the
    real gate.
    """
    _validate_automation_content(content, label="import")
    forced_off = _toggle_enabled_in_frontmatter(content, False, label="import")
    check = _validate_automation_content(forced_off, label="import")
    if check.enabled is not False:
        raise ManagementError(500, "import: failed to force enabled: false as expected")
    return create_automation(forced_off, ctx)


def _run_dir_path(slug: str, run_id: str, ctx: EngineContext) -> Path:
    return Path(ctx.runs_dir).expanduser() / slug / run_id


def _write_run_status(run_dir: Path, status: dict[str, Any]) -> None:
    """Atomically write the in-progress status record for a run.

    Machine-generated and overwritten several times over a run's life
    (starting -> running -> failed, or superseded by ``result.json`` on
    success) -- uses ``fsutil.atomic_write`` (no ``.bak``), not
    ``atomic_write_with_backup``, since a growing pile of backups of a
    file that changes every few seconds would be pure noise.
    """
    status_path = run_dir / _RUN_STATUS_FILENAME
    fsutil.atomic_write(status_path, json.dumps(status, indent=2) + "\n")


def run_automation_async(slug: str, ctx: EngineContext) -> dict[str, Any]:
    """Kick off a run in a background thread; return immediately.

    ``run_id`` is minted here (not inside ``runner.run``) so it can be
    returned to the caller before the run finishes -- it is threaded into
    ``runner.run(..., run_id=run_id)`` so the run directory this mints is
    guaranteed to be the one the background thread eventually writes.

    ``session_id`` returned here is the best-known guess, not a guaranteed
    final value: if the automation already has a pinned session (in the
    engine's pin store), that pinned id is what ``runner.run`` will actually
    resume in the common case, so it is returned directly; otherwise the
    same ``f"{slug}-{run_id}"`` fallback ``runner.run`` itself uses for a
    never-pinned automation is minted here. Only the rare corrupt-pin
    paths inside ``runner.run`` (confirmed-missing -> recreate, or
    indeterminate -> abort) can diverge from this guess -- the
    authoritative value always lands in ``result.json`` (and, while the
    run is in flight, in the status file below) once the real resolution
    happens inside ``runner.run``.

    The run directory and a synchronous status file are created BEFORE
    this function returns, so ``GET /api/runs/{slug}/{run_id}`` returns a
    real 200 immediately -- never a 404 for a run id this function itself
    minted. See ``get_run_detail``.
    """
    a = _get_automation_or_404(slug, ctx.automations_dir)
    run_id = runner.new_run_id()
    pin = session_pins.get(a.slug, runs_dir=ctx.runs_dir)
    session_id = pin.session_id if pin else f"{a.slug}-{run_id}"
    run_dir = _run_dir_path(slug, run_id, ctx)
    # Minted ONCE, then reused by every subsequent status write for this run.
    # Each write used to stamp a fresh `_iso_now()` under `started_at`, so a
    # run's recorded start time crept forward to the time of its most recent
    # status write -- making the elapsed time a client computes from it wrong
    # (and, for the failure record below, always ~zero).
    started_at = _iso_now()

    _write_run_status(
        run_dir,
        {
            "status": "starting",
            "automation": a.name,
            "run_id": run_id,
            "session_id": session_id,
            "started_at": started_at,
        },
    )

    def _background() -> None:
        _write_run_status(
            run_dir,
            {
                "status": "running",
                "automation": a.name,
                "run_id": run_id,
                "session_id": session_id,
                "started_at": started_at,
            },
        )
        try:
            runner.run(
                a,
                cwd=ctx.cwd,
                runs_dir=ctx.runs_dir,
                prompts_dir=ctx.prompts_dir,
                run_id=run_id,
            )
            # runner.run() persists result.json itself (both the ordinary
            # and the pinned-session-abort paths) -- get_run_detail() prefers
            # that file the moment it exists, so no further status write is
            # needed on any path that returns normally.
        except Exception as exc:  # noqa: BLE001 - background thread must never crash the process
            error_traceback = traceback.format_exc()
            sys.stderr.write(
                f"[management-api] manual run {run_id} of {slug!r} raised: {exc}\n"
            )
            _write_run_status(
                run_dir,
                {
                    "status": "failed",
                    "automation": a.name,
                    "run_id": run_id,
                    "session_id": session_id,
                    "started_at": started_at,
                    # This run is OVER. Without a finish time the record is
                    # indistinguishable from one still in flight -- a client
                    # decides landed-vs-running from `finished_at`, so a run
                    # that died before `runner.run` could write its own
                    # `result.json` displayed as "running..." until the
                    # client's own poll ceiling gave up minutes later, and
                    # the real error here was never shown.
                    "finished_at": _iso_now(),
                    "error": str(exc),
                    "traceback": error_traceback,
                },
            )

    thread = threading.Thread(
        target=_background, name=f"run-{slug}-{run_id}", daemon=True
    )
    thread.start()
    return {"run_id": run_id, "session_id": session_id}


# ==== Prompts ====


def _known_prompt_or_404(name: str) -> None:
    if name not in prompts.KNOWN_PROMPTS:
        raise ManagementError(
            404,
            f"unknown prompt {name!r}; known prompts: {sorted(prompts.KNOWN_PROMPTS)}",
        )


def list_prompt_files(ctx: EngineContext) -> list[dict[str, Any]]:
    prompts_dir = Path(ctx.prompts_dir).expanduser()
    result: list[dict[str, Any]] = []
    for name in prompts.KNOWN_PROMPTS:
        path = prompts_dir / f"{name}.md"
        present = path.is_file()
        empty = False
        if present:
            try:
                body = prompts.load_prompt(name, prompts_dir)
                empty = body is None
            except prompts.PromptError:
                # Malformed, not empty -- the single-prompt GET surfaces the
                # real parse error; the list view just reports it's present.
                empty = False
        result.append(
            {"name": name, "present": present, "empty": empty, "path": str(path)}
        )
    return result


def read_prompt_file(name: str, ctx: EngineContext) -> dict[str, Any]:
    _known_prompt_or_404(name)
    path = Path(ctx.prompts_dir).expanduser() / f"{name}.md"
    if not path.is_file():
        raise ManagementError(404, f"prompt file does not exist: {path}")
    content = path.read_text(encoding="utf-8")
    return {"name": name, "content": content}


def write_prompt_file(name: str, content: str, ctx: EngineContext) -> dict[str, Any]:
    _known_prompt_or_404(name)
    path = Path(ctx.prompts_dir).expanduser() / f"{name}.md"
    _atomic_write_with_backup(path, content)
    return {"name": name, "content": content}


# ==== Run history ====


# ==== The run-record contract (ONE home, every endpoint that serves a run) ====
#
# A client that polls one run after pressing "run now" and a client that
# lists run history parse the SAME record shape with the SAME decoder.
# (Confirmed against a real API client: a single `RunSummary` initializer
# decodes both `GET /v1/automations/<slug>/runs` -> {"runs":[...]} and
# `GET /v1/automations/<slug>/runs/<run_id>`, the latter proxied verbatim
# to this service's `GET /api/runs/<slug>/<run_id>`.) Five of its fields are
# hard-required -- absent means the decode THROWS, and the whole run reads to
# the user as "tracking failed", regardless of how the run itself went.
#
# The bug this exists to kill: those five fields were assembled inline in
# `_iter_run_records` (the list) and NOWHERE AT ALL in `get_run_detail` (the
# poll), which served the on-disk `status.json`/`result.json` documents raw.
# Neither of those documents carries `automation_name`; `status.json` carries
# neither `failed` nor `notified` either. So EVERY manual run failed to track
# -- reported from the field on 2026-08-17 as
# "run <id>: tracking failed -- required field 'automation_name' is absent".
#
# Two homes for one contract is how they drifted apart. There is now one.
_RUN_RECORD_REQUIRED_FIELDS = (
    "run_id",
    "automation",
    "automation_name",
    "failed",
    "notified",
)


def _resolve_automation_name(
    slug: str, persisted: Any, automations_dir: Path, *, where: str
) -> str:
    """The real display name of the automation a run belongs to. Never a placeholder.

    Order of authority:

    1. The name the run record itself persisted (``runner`` writes it into
       ``result.json`` as ``automation``; ``run_automation_async`` writes the
       same into ``status.json``). This is the name as of the moment the run
       happened -- the truest answer, and correct even if the automation has
       since been renamed.
    2. The automation on disk, looked up by slug. Reached only for a record
       written before that field existed, or by something other than this
       module. This is a lookup of the REAL value from the authoritative
       source, not a stand-in.

    If neither answers, that is a corrupt record and it says so (see law 1,
    fail loud): a run whose automation cannot be named is never quietly
    served under the slug, an invented string, or ``null``.
    """
    if isinstance(persisted, str) and persisted.strip():
        return persisted
    for a in _load_automations_or_500(automations_dir):
        if a.slug == slug:
            return a.name
    raise ManagementError(
        500,
        f"{where}: cannot determine automation_name for a run of {slug!r} -- the "
        f"run record carries no usable 'automation' field and no automation with "
        f"that slug exists in {automations_dir}",
    )


def _run_contract_fields(
    slug: str,
    data: dict[str, Any],
    *,
    run_id_fallback: str,
    automations_dir: Path,
    where: str,
) -> dict[str, Any]:
    """The five always-required run-record fields, for any run document.

    Works on both on-disk shapes:

    * ``result.json`` (a finished run) -- carries ``failed``/``notified``
      already; supplies only ``automation_name`` and normalises ``automation``.
    * ``status.json`` (a run in flight, written by ``run_automation_async``)
      -- carries NEITHER ``failed`` nor ``notified``, so both are derived
      here. An in-flight run has not failed and has not notified, and
      ``status: "failed"`` is the one in-flight state that has.

    ``failed: False`` on a run still in flight can never be misread as
    success: a client decides landed-vs-running from ``finished_at``, which
    an in-flight record does not have.

    ``automation`` is the SLUG, matching what the list endpoint has always
    served for that key. The display name lives in ``automation_name``. The
    detail endpoint used to serve the display name under ``automation``
    (whatever the run document happened to hold) -- the same field carrying
    two different things depending on which endpoint answered.

    ``final_reply_preview`` is additive (not one of the five hard-required
    fields above -- an older client that decodes only those five is
    unaffected): a bounded excerpt of ``final_reply`` on both shapes, or
    ``_NO_REPLY_MARKER`` when there is none yet (in-flight) or none at all
    (a run that aborted before any turn ran). It exists so a dead-brain
    reply ("Error: No providers available" on an otherwise-successful,
    ``failed: False`` run) is visible from the run-record contract alone,
    without a consumer needing to fetch full step bodies.
    """
    if "failed" in data:
        failed = bool(data.get("failed"))
    else:
        failed = str(data.get("status", "")) in {"failed", "error"}
    final_reply_preview = _final_reply_preview(data.get("final_reply"))
    return {
        "run_id": data.get("run_id") or run_id_fallback,
        "automation": slug,
        "automation_name": _resolve_automation_name(
            slug, data.get("automation"), automations_dir, where=where
        ),
        "failed": failed,
        "notified": bool(data.get("notified", False)),
        "final_reply_preview": final_reply_preview,
    }


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        start = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        end = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (end - start).total_seconds()


def _iter_run_records(
    runs_dir: Path, *, automation_filter: str | None
) -> list[dict[str, Any]]:
    """Every persisted ``result.json`` under ``runs_dir``, newest attempt first.

    INTERNAL shape. The client-facing run-record contract (see
    ``_run_contract_fields``) is applied on top by ``list_runs`` -- not here,
    because ``_latest_attempt``/``compute_next_runs`` also read this and need
    only timestamps and outcome. Keeping the contract out of here is what
    stops one unnameable run record from being able to 500 ``list_automations``.
    """
    runs_dir = Path(runs_dir).expanduser()
    if not runs_dir.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    for slug_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        slug = slug_dir.name
        if automation_filter and slug != automation_filter:
            continue
        for run_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            result_path = run_dir / "result.json"
            if not result_path.is_file():
                continue
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                sys.stderr.write(
                    f"[management-api] skipping unreadable run record {result_path}: {exc}\n"
                )
                continue
            entries.append(
                {
                    "run_id": data.get("run_id", run_dir.name),
                    "automation": slug,
                    # The name as persisted, un-resolved. `list_runs` replaces
                    # this with the authoritative value via
                    # `_run_contract_fields`; it is kept here (rather than
                    # dropped) only so the raw record stays self-describing
                    # for the internal readers above.
                    "automation_name": data.get("automation"),
                    "session_id": data.get("session_id"),
                    "started_at": data.get("started_at"),
                    "finished_at": data.get("finished_at"),
                    "failed": bool(data.get("failed", False)),
                    "error": data.get("error"),
                    "notified": bool(data.get("notified", False)),
                    # Raw, un-truncated -- carried only so `list_runs` can
                    # derive `final_reply_preview` via `_run_contract_fields`
                    # (the one place bounding happens). Never served as-is.
                    "final_reply": data.get("final_reply"),
                    "step_count": len(data.get("steps") or []),
                    "duration_seconds": _duration_seconds(
                        data.get("started_at"), data.get("finished_at")
                    ),
                }
            )

    entries.sort(key=lambda e: e["started_at"] or "", reverse=True)
    return entries


def list_runs(
    *, limit: int, automation_filter: str | None, ctx: EngineContext
) -> list[dict[str, Any]]:
    """Run history, every record carrying the full run-record contract.

    A record whose automation cannot be named (see
    ``_resolve_automation_name``) is SKIPPED with a loud stderr line, not
    served with a ``null`` name -- the same treatment the unreadable-JSON
    case above it already gets, and for the same reason: one corrupt record
    must not take out the whole listing, and must not be quietly served in a
    shape the client cannot parse either. The single-record
    ``get_run_detail`` has no such option and raises instead.
    """
    if limit <= 0:
        raise ManagementError(400, f"'limit' must be a positive integer, got {limit}")
    entries = _iter_run_records(ctx.runs_dir, automation_filter=automation_filter)
    served: list[dict[str, Any]] = []
    for entry in entries:
        slug = entry["automation"]
        try:
            contract = _run_contract_fields(
                slug,
                # `_iter_run_records` already flattened the record; the
                # persisted name lives under `automation_name` there.
                {**entry, "automation": entry.get("automation_name")},
                run_id_fallback=entry["run_id"],
                automations_dir=ctx.automations_dir,
                where="list_runs",
            )
        except ManagementError as exc:
            sys.stderr.write(
                f"[management-api] skipping run {slug}/{entry['run_id']} in listing: "
                f"{exc.message}\n"
            )
            continue
        # `entry["final_reply"]` is the raw, un-truncated reply -- carried
        # only so `_run_contract_fields` above could derive the BOUNDED
        # `final_reply_preview` now sitting in `contract`. Drop the raw key
        # here so the listing never serves an unbounded reply body.
        trimmed_entry = {k: v for k, v in entry.items() if k != "final_reply"}
        served.append({**trimmed_entry, **contract})
        if len(served) == limit:
            break
    return served


def _run_dir_or_404(slug: str, run_id: str, ctx: EngineContext) -> Path:
    if not _SAFE_SEGMENT_RE.match(slug) or not _SAFE_SEGMENT_RE.match(run_id):
        raise ManagementError(400, "invalid automation slug or run_id in path")
    run_dir = Path(ctx.runs_dir).expanduser() / slug / run_id
    if not run_dir.is_dir():
        raise ManagementError(404, f"no such run: {slug}/{run_id}")
    return run_dir


def _read_run_status_or_404(slug: str, run_id: str, run_dir: Path) -> dict[str, Any]:
    """Serve the in-progress status record for a run that has no result.json yet.

    Only reached once ``run_dir`` is already known to exist (via
    ``_run_dir_or_404``) -- for any run this service itself minted via
    ``run_automation_async``, ``status.json`` was written synchronously
    before the 202 response went out, so this should always find it. A
    404 here means either a scheduler/CLI-triggered run this service was
    never told about, or a genuinely corrupt state -- never a run id this
    service itself handed out.
    """
    status_path = run_dir / _RUN_STATUS_FILENAME
    if not status_path.is_file():
        raise ManagementError(
            404, f"no result or status found for run {run_id!r} of {slug!r}"
        )
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManagementError(
            500, f"corrupt status.json for run {run_id!r}: {exc}"
        ) from exc


def get_run_detail(slug: str, run_id: str, ctx: EngineContext) -> dict[str, Any]:
    """One run, in whatever state it is in -- ALWAYS in the run-record contract.

    This is the endpoint a client polls after "run now" (reached as
    ``GET /api/runs/<slug>/<run_id>``). Both branches below serve an on-disk
    document that was written for this service's own bookkeeping, NOT for a
    client:

    * ``status.json`` (run in flight) carries no ``automation_name``, no
      ``failed`` and no ``notified``.
    * ``result.json`` (run finished) carries no ``automation_name``, and puts
      the automation's DISPLAY NAME under ``automation`` where the list
      endpoint puts the SLUG.

    Serving either raw is what made every manual run read as
    "tracking failed -- required field 'automation_name' is absent". Both are
    now normalised through ``_run_contract_fields`` before they leave here,
    so a client parses one shape whatever state the run is in and whichever
    endpoint it came from. Everything else in the document (steps, replies,
    session diagnostics) is passed through untouched.
    """
    run_dir = _run_dir_or_404(slug, run_id, ctx)
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        status = _read_run_status_or_404(slug, run_id, run_dir)
        return {
            **status,
            **_run_contract_fields(
                slug,
                status,
                run_id_fallback=run_id,
                automations_dir=ctx.automations_dir,
                where="get_run_detail(status.json)",
            ),
        }
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManagementError(
            500, f"corrupt result.json for run {run_id!r}: {exc}"
        ) from exc

    contract = _run_contract_fields(
        slug,
        data,
        run_id_fallback=run_id,
        automations_dir=ctx.automations_dir,
        where="get_run_detail(result.json)",
    )

    truncated_any = False
    for step in data.get("steps") or []:
        reply = step.get("reply") or ""
        if len(reply) > _MAX_RUN_REPLY_CHARS:
            step["reply"] = reply[:_MAX_RUN_REPLY_CHARS]
            step["truncated"] = True
            truncated_any = True
        else:
            step["truncated"] = False

    data["truncated"] = truncated_any
    return {**data, **contract}


def get_run_stderr(slug: str, run_id: str, ctx: EngineContext) -> dict[str, Any]:
    run_dir = _run_dir_or_404(slug, run_id, ctx)
    stderr_path = run_dir / "stderr.log"
    if not stderr_path.is_file():
        return {"run_id": run_id, "automation": slug, "lines": [], "truncated": False}
    text = stderr_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    truncated = len(lines) > _MAX_STDERR_LINES
    tail = lines[-_MAX_STDERR_LINES:]
    return {"run_id": run_id, "automation": slug, "lines": tail, "truncated": truncated}


# ==== Status ====


def compute_next_runs(
    automations: list[Automation], ctx: EngineContext
) -> list[dict[str, Any]]:
    """Best-effort next-run estimate: last actual run's finish time + schedule interval.

    The scheduler is a separate OS process with its own in-memory due-time
    tracking that this service has no channel to read -- so this is an
    honest extrapolation from persisted run history, not a read of the
    scheduler's real internal state. An automation with no prior run gets
    ``next_run_at: None`` rather than a guess.
    """
    next_runs: list[dict[str, Any]] = []
    for a in automations:
        if not a.enabled or a.trigger.type != "schedule":
            continue
        entry: dict[str, Any] = {"slug": a.slug, "next_run_at": None}
        try:
            schedule = parse_schedule(a.trigger.expression or "")
        except ValueError:
            next_runs.append(entry)
            continue
        last_runs = _iter_run_records(ctx.runs_dir, automation_filter=a.slug)
        if last_runs and last_runs[0].get("finished_at"):
            try:
                finished = datetime.strptime(
                    last_runs[0]["finished_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=UTC)
                delay = seconds_until_next_fire(schedule, finished.timestamp())
                next_at = finished + timedelta(seconds=delay)
                entry["next_run_at"] = next_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
        next_runs.append(entry)
    return next_runs


__all__ = [
    "EngineContext",
    "ManagementError",
    "compute_next_runs",
    "create_automation",
    "delete_automation",
    "export_automation",
    "get_automation_detail",
    "get_run_detail",
    "get_run_stderr",
    "import_automation",
    "list_automations",
    "list_prompt_files",
    "list_runs",
    "read_prompt_file",
    "run_automation_async",
    "set_automation_enabled",
    "update_automation",
    "validate_automation_content",
    "write_prompt_file",
]
