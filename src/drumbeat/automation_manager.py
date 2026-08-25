"""Surface-agnostic library for managing automations.

This is the LIBRARY layer the owner asked for (feedback f198): "implemented at
the drumbeat repo library level with light tool wrappers, then packaged as a
consumer pack." Every client surface -- CLI, the HTTP management API, and any
consumer wrapper -- calls these functions rather than reimplementing automation
CRUD. The logic lives in ONE home so two different clients can never drift apart.

Deliberately NOT coupled to HTTP: it operates on an automations directory
(``Path``), returns plain frozen dataclasses (each with ``to_dict()`` for a
thin wrapper to serialise), and raises :class:`AutomationManagerError` carrying a
machine-readable ``code`` a wrapper maps to its own vocabulary (an HTTP wrapper
to a status, another client to its own phrasing). It depends only on
:mod:`drumbeat.automation` (the schema), :mod:`drumbeat.fsutil` (atomic writes),
and :func:`drumbeat.scheduler.parse_schedule` (the schedule grammar, imported
read-only) -- never on the engine's HTTP/run/prompt context.

FAIL LOUD, NO FALLBACKS. Every write goes through :func:`_edit_and_validate`:
the surgically-edited file text is re-parsed by the real automation parser
BEFORE a single byte reaches disk. A malformed edit raises and leaves the file
exactly as it was -- a broken automation runs unattended and takes the brain
down, so it must never be written.

Scope shipped in this module today: the read verbs (:func:`list_automations`,
:func:`get_automation`) and the safest write verb (:func:`edit_schedule` --
re-timing an existing schedule). The write path is built around a single spine
(:func:`_edit_and_validate` + a per-field surgical ``transform``) precisely so
that step CRUD and tool (``requires``) CRUD slot in next as new transforms
without reshaping anything here. See ``DONE.md`` for what remains.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drumbeat import fsutil
from drumbeat.automation import (
    Automation,
    AutomationError,
    load_all_tolerant,
    load_from_text,
)
from drumbeat.automation import load as load_automation
from drumbeat.scheduler import parse_schedule

# Error codes a thin wrapper maps into its own transport vocabulary. Kept
# small and stable on purpose -- an HTTP wrapper reads NOT_FOUND -> 404,
# INVALID -> 400, UNSUPPORTED -> 422/409; another client reads them as its own
# phrasings. New write verbs reuse these rather than inventing more.
ERROR_CODES = frozenset({"NOT_FOUND", "INVALID", "UNSUPPORTED"})


class AutomationManagerError(Exception):
    """Raised by any library function; carries a machine-readable ``code``.

    ``code`` is one of :data:`ERROR_CODES`. ``message`` is the human-readable
    reason, safe to surface to an end user through any client.
    """

    def __init__(self, code: str, message: str) -> None:
        if code not in ERROR_CODES:
            # A wrong code is itself a fail-loud bug -- never silently coerce.
            raise ValueError(f"unknown AutomationManagerError code {code!r}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---- surface-agnostic views (each JSON-ready via to_dict) ----


@dataclass(frozen=True)
class TriggerView:
    """When an automation fires. ``expression`` is None for non-schedule triggers."""

    type: str
    expression: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "expression": self.expression}


@dataclass(frozen=True)
class InjectView:
    """One declared ``inject:`` pre-step command."""

    argv: tuple[str, ...]
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "label": self.label}


@dataclass(frozen=True)
class AutomationSummary:
    """The at-a-glance shape for a listing -- no steps, no body."""

    name: str
    slug: str
    enabled: bool
    trigger: TriggerView
    notify: str
    requires: tuple[str, ...]
    step_count: int
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "enabled": self.enabled,
            "trigger": self.trigger.to_dict(),
            "notify": self.notify,
            "requires": list(self.requires),
            "step_count": self.step_count,
            "path": self.path,
        }


@dataclass(frozen=True)
class AutomationDetail:
    """The full shape for one automation -- every sub-piece plus raw content.

    ``content`` is the file verbatim so a raw-text editor in any client can
    round-trip it; the structured fields are for a structured editor. Both
    describe the same file.
    """

    name: str
    slug: str
    enabled: bool
    trigger: TriggerView
    notify: str
    requires: tuple[str, ...]
    steps: tuple[str, ...]
    inject: tuple[InjectView, ...]
    guidance_delivery: str
    step_count: int
    path: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slug": self.slug,
            "enabled": self.enabled,
            "trigger": self.trigger.to_dict(),
            "notify": self.notify,
            "requires": list(self.requires),
            "steps": list(self.steps),
            "inject": [i.to_dict() for i in self.inject],
            "guidance_delivery": self.guidance_delivery,
            "step_count": self.step_count,
            "path": self.path,
            "content": self.content,
        }


@dataclass(frozen=True)
class LoadFailureView:
    """One automation file that did not parse, and why -- surfaced, never hidden."""

    path: str
    problem: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "problem": self.problem}


@dataclass(frozen=True)
class AutomationListing:
    """The whole automations directory: what parsed, and what didn't.

    ``failures`` is non-empty when a file is broken. A listing NEVER refuses
    the whole directory over one bad file (that is the tolerant loader's
    contract) -- it lists every good automation and names every broken one, so
    a client can show "3 automations, 1 needs fixing" instead of a blank
    screen or a lie.
    """

    automations: tuple[AutomationSummary, ...]
    failures: tuple[LoadFailureView, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "automations": [a.to_dict() for a in self.automations],
            "failures": [f.to_dict() for f in self.failures],
        }


def _summary(a: Automation) -> AutomationSummary:
    return AutomationSummary(
        name=a.name,
        slug=a.slug,
        enabled=a.enabled,
        trigger=TriggerView(a.trigger.type, a.trigger.expression),
        notify=a.notify,
        requires=tuple(a.requires),
        step_count=len(a.steps),
        path=str(a.path),
    )


def _detail(a: Automation) -> AutomationDetail:
    content = a.path.read_text(encoding="utf-8")
    return AutomationDetail(
        name=a.name,
        slug=a.slug,
        enabled=a.enabled,
        trigger=TriggerView(a.trigger.type, a.trigger.expression),
        notify=a.notify,
        requires=tuple(a.requires),
        steps=tuple(a.steps),
        inject=tuple(InjectView(argv=s.argv, label=s.label) for s in a.inject),
        guidance_delivery=a.guidance_delivery,
        step_count=len(a.steps),
        path=str(a.path),
        content=content,
    )


# ---- resolution ----


def _resolve(slug: str, automations_dir: Path) -> Automation:
    """Load one automation by slug, distinguishing "missing" from "broken".

    Uses the tolerant loader so one unrelated broken file cannot mask an
    otherwise-loadable target. If the requested slug is not among the files
    that parsed, and a file whose name matches the slug FAILED to parse, that
    is reported as INVALID with the real parse problem -- "get details on X"
    should tell you X is broken and why, not merely "not found". A slug with
    no file at all is NOT_FOUND.
    """
    try:
        automations, failures = load_all_tolerant(Path(automations_dir).expanduser())
    except AutomationError as exc:
        # The tolerant loader only raises for a missing directory; per-file
        # problems come back as ``failures``.
        raise AutomationManagerError("NOT_FOUND", exc.problem) from exc

    for a in automations:
        if a.slug == slug:
            return a
    for f in failures:
        if f.path.stem == slug:
            raise AutomationManagerError(
                "INVALID",
                f"automation {slug!r} exists at {f.path} but does not parse: "
                f"{f.problem}",
            )
    raise AutomationManagerError("NOT_FOUND", f"no automation with slug {slug!r}")


# ---- read verbs ----


def list_automations(automations_dir: Path) -> AutomationListing:
    """Every automation in a directory, with broken files surfaced (not hidden).

    Raises:
        AutomationManagerError(NOT_FOUND): the directory itself is missing.
    """
    try:
        automations, failures = load_all_tolerant(Path(automations_dir).expanduser())
    except AutomationError as exc:
        raise AutomationManagerError("NOT_FOUND", exc.problem) from exc
    return AutomationListing(
        automations=tuple(_summary(a) for a in automations),
        failures=tuple(LoadFailureView(str(f.path), f.problem) for f in failures),
    )


def get_automation(slug: str, automations_dir: Path) -> AutomationDetail:
    """Full detail for one automation.

    Raises:
        AutomationManagerError(NOT_FOUND): no automation with that slug.
        AutomationManagerError(INVALID): the file exists but does not parse.
    """
    return _detail(_resolve(slug, automations_dir))


# ---- the write spine (every sub-piece edit rides this) ----


def _edit_and_validate(
    automation: Automation,
    transform: Callable[[str], str],
    *,
    expect: Callable[[Automation], None],
) -> Automation:
    """Apply a surgical ``transform`` to an automation file, validate, then write.

    The single home for every write in this module -- schedule edit today,
    step/tool CRUD tomorrow. The contract every caller gets for free:

    1. ``transform`` produces the new file text from the old (surgical: it
       changes exactly one sub-piece and preserves everything else -- comments,
       key order, body -- byte-for-byte).
    2. The result is re-parsed by the REAL automation parser
       (:func:`drumbeat.automation.load_from_text`). A parse failure becomes
       ``AutomationManagerError(INVALID)`` and NOTHING is written.
    3. The edit must not change the slug (the slug keys the pin, the run dir,
       every API path -- a rename is a different, unsupported operation here).
    4. ``expect`` asserts the intended change actually took effect on the
       re-parsed automation -- a surgical edit that silently matched the wrong
       line is caught here rather than shipped.
    5. Only then is the file written atomically, preserving a ``.bak``.

    Returns the freshly re-loaded automation (read back from disk, proving the
    bytes that landed parse).
    """
    original = automation.path.read_text(encoding="utf-8")
    candidate = transform(original)

    try:
        parsed = load_from_text(automation.path, candidate)
    except AutomationError as exc:
        raise AutomationManagerError("INVALID", exc.problem) from exc

    if parsed.slug != automation.slug:
        raise AutomationManagerError(
            "INVALID",
            f"edit would change the automation's slug from {automation.slug!r} to "
            f"{parsed.slug!r}; renaming is not supported here",
        )

    expect(parsed)

    fsutil.atomic_write_with_backup(automation.path, candidate)
    return load_automation(automation.path)


# ---- surgical frontmatter transforms ----

_FRONTMATTER_BLOCK_RE = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*\n?)(.*)\Z", re.DOTALL)
_TRIGGER_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)trigger:[ \t]*$")
_EXPRESSION_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)expression:[ \t]*(?P<val>.*?)[ \t]*$"
)


def _set_trigger_expression(text: str, new_expression: str, *, path: Path) -> str:
    """Return ``text`` with the ``expression:`` under ``trigger:`` set to a new value.

    Surgical: only the value on the single ``expression:`` line inside the
    trigger block changes. Its indentation, every other frontmatter line
    (including comments), the delimiters, and the entire body are preserved
    byte-for-byte -- the same discipline as
    ``management_api._toggle_enabled_in_frontmatter``.

    Refuses loudly (raising, so :func:`_edit_and_validate` writes nothing) on
    anything it cannot resolve unambiguously -- a missing frontmatter block, a
    missing or duplicated ``trigger:`` key, or a missing/duplicated
    ``expression:`` child. Guessing here would risk corrupting a policy file
    that runs unattended.
    """
    match = _FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        raise AutomationManagerError(
            "INVALID",
            f"{path.name}: frontmatter block not found while editing schedule",
        )
    open_delim, frontmatter, close_delim, body = match.groups()

    lines = frontmatter.splitlines(keepends=True)

    trigger_idx: int | None = None
    trigger_indent = 0
    for i, line in enumerate(lines):
        tm = _TRIGGER_LINE_RE.match(line)
        if tm:
            if trigger_idx is not None:
                raise AutomationManagerError(
                    "INVALID",
                    f"{path.name}: multiple 'trigger:' keys in frontmatter; "
                    "refusing to guess which schedule to edit",
                )
            trigger_idx = i
            trigger_indent = len(tm.group("indent"))
    if trigger_idx is None:
        raise AutomationManagerError(
            "INVALID", f"{path.name}: could not locate the 'trigger:' block to edit"
        )

    expr_idx: int | None = None
    for j in range(trigger_idx + 1, len(lines)):
        raw = lines[j]
        if raw.strip() == "":
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        if indent <= trigger_indent:
            break  # left the trigger block (hit a sibling/parent key)
        if _EXPRESSION_LINE_RE.match(raw):
            if expr_idx is not None:
                raise AutomationManagerError(
                    "INVALID",
                    f"{path.name}: multiple 'expression:' keys under trigger; "
                    "refusing to guess",
                )
            expr_idx = j
    if expr_idx is None:
        raise AutomationManagerError(
            "INVALID",
            f"{path.name}: no 'expression:' line under the trigger block to edit "
            "(is the trigger written in flow style?); edit the full automation "
            "content instead",
        )

    em = _EXPRESSION_LINE_RE.match(lines[expr_idx])
    assert em is not None  # re-matched from the same line
    indent = em.group("indent")
    newline = "\n" if lines[expr_idx].endswith("\n") else ""
    lines[expr_idx] = f"{indent}expression: {new_expression}{newline}"

    return open_delim + "".join(lines) + close_delim + body


# ---- write verbs ----


def edit_schedule(
    slug: str, automations_dir: Path, *, expression: str
) -> AutomationDetail:
    """Re-time an existing schedule automation. The safest write verb.

    Only changes the cadence of an automation that ALREADY has a ``schedule``
    trigger -- it does not create schedules, change trigger types, or touch any
    other sub-piece. That narrow scope is deliberate: it is the write least able
    to leave an automation in a surprising state.

    The expression is validated twice before anything is written: first against
    the SAME grammar the scheduler runs (:func:`drumbeat.scheduler.parse_schedule`),
    so a value like ``"every other tuesday"`` -- which the automation parser
    would accept as a mere non-empty string and which would then never fire --
    is refused here; then the whole rewritten file is re-parsed by the
    automation parser via :func:`_edit_and_validate`. A malformed edit leaves
    the file untouched.

    Raises:
        AutomationManagerError(INVALID): empty expression, an expression the
            scheduler cannot parse, or a rewrite the automation parser rejects.
        AutomationManagerError(NOT_FOUND): no automation with that slug.
        AutomationManagerError(UNSUPPORTED): the automation's trigger is not a
            schedule (nothing to re-time).
    """
    if not isinstance(expression, str) or not expression.strip():
        raise AutomationManagerError(
            "INVALID", "schedule expression must be a non-empty string"
        )
    expression = expression.strip()

    try:
        parse_schedule(expression)
    except ValueError as exc:
        raise AutomationManagerError("INVALID", str(exc)) from exc

    current = _resolve(slug, automations_dir)
    if current.trigger.type != "schedule":
        raise AutomationManagerError(
            "UNSUPPORTED",
            f"automation {slug!r} has a {current.trigger.type!r} trigger, not a "
            "schedule; edit_schedule only re-times an existing schedule trigger",
        )

    def _transform(text: str) -> str:
        return _set_trigger_expression(text, expression, path=current.path)

    def _expect(parsed: Automation) -> None:
        if parsed.trigger.type != "schedule" or parsed.trigger.expression != expression:
            raise AutomationManagerError(
                "INVALID",
                "schedule edit did not take effect as expected (wanted "
                f"schedule/{expression!r}, got "
                f"{parsed.trigger.type}/{parsed.trigger.expression!r})",
            )

    updated = _edit_and_validate(current, _transform, expect=_expect)
    return _detail(updated)


__all__ = [
    "ERROR_CODES",
    "AutomationDetail",
    "AutomationListing",
    "AutomationManagerError",
    "AutomationSummary",
    "InjectView",
    "LoadFailureView",
    "TriggerView",
    "edit_schedule",
    "get_automation",
    "list_automations",
]
