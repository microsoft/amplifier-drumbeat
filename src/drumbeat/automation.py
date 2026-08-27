"""Parse and validate automation files.

An automation file is markdown with YAML frontmatter that carries the whole
machine surface. Steps are structured frontmatter data -- an ordered
``steps:`` list of ``{id, prompt, label?}`` objects -- not prose the engine
parses out of the document body. The body is a human-facing description that
is never parsed for execution. This module is intentionally strict: it rejects
anything ambiguous rather than guessing, because a bad automation runs
unattended and a silently-wrong parse produces silently-wrong behavior.

The frozen shape lives in ``contracts/automation-file.v1.md``; this module is
its only parser.

File format::

    ---
    automation:
      name: Teams Check
      enabled: true
      trigger:
        type: schedule
        expression: every 30 minutes
      notify: auto
      steps:
        - id: check-source
          label: Check the source
          prompt: First step text, possibly spanning multiple lines.
        - id: report
          prompt: Second step text.
    ---

    Optional human-facing description. Never parsed for execution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from drumbeat import agent_config
from drumbeat.error_log import log_automation_error

# "urgent-only": same auto-notify judgment turn and NOTHING_TO_REPORT
# convention as "auto" -- the difference is entirely in runner.run()'s
# push decision, not in how this automation is parsed. See runner.py's
# push-demotion fix note for why: two automations (channels-check,
# reconcile) produced status-report notifications nobody ever replied to
# (7 and 2 respectively, 0 replies), while their actual findings belong
# in the item ledger and run artifacts either way. This value keeps the
# work and the judgment identical to "auto" and withholds only the push,
# unless the agent's own final reply carries an `URGENT: <reason>`
# marker -- same plain-text-convention-plus-mechanical-parse discipline
# as the quiet-hours marker, not a scored gate.
VALID_NOTIFY_VALUES = {"always", "auto", "never", "urgent-only"}
VALID_TRIGGER_TYPES = {"schedule", "manual", "event"}

# How this automation's required guidance FILES reach the agent each run.
#
# "reference" (default, preferred): the engine injects the workspace-relative
#   PATHS plus a mandatory "read these first" preamble; the agent loads the
#   bodies itself with its file tools. The turn text stays a few hundred bytes
#   no matter how large the guidance grows. Guidance files are the per-user
#   personalization layer, so referencing them -- rather than snapshotting a
#   body into the transcript -- is what keeps a resumed session reading the
#   CURRENT file. That is the whole reason the field exists.
#
# "inline": the engine embeds each guidance body verbatim in the turn text, for
#   an automation that genuinely needs its guidance literally in the
#   transcript. The cost is that the transcript then carries a snapshot: a
#   resumed session keeps reading the body as it was on the run that inlined it.
#
# Executable `requires:` tool cards are unaffected by this knob -- they ride
# inline in both modes (they are pack documentation, not workspace files the
# agent can read by relative path).
VALID_GUIDANCE_DELIVERY = {"reference", "inline"}
DEFAULT_GUIDANCE_DELIVERY = "reference"

# How this automation's amplifier-agent conversation persists ACROSS runs. This
# is per-automation policy; the engine still owns the pin itself (see
# ``drumbeat.session_pins``). Closed vocabulary, refused loudly on anything else
# -- a lifecycle that reads as meaningful and silently does nothing is the worst
# defect shape in a fail-loud project.
#
# "continuous" (default): today's behavior, unchanged. One pinned conversation
#   resumes on every run, abandoned only when the engine's own health signals
#   fire (a context-ceiling hit or contract drift -- see
#   ``drumbeat.session_health``). An automation that never sets this key is a
#   continuous automation, byte-for-byte as before this field existed.
#
# "fresh": a brand-new conversation on every single run. The prior run's pin is
#   rotated through the same flock-guarded rotation path the health signals use,
#   so two runs produce two distinct session ids and nothing accumulates across
#   them. For a run whose value is entirely in the current firing and must carry
#   no memory of the last one.
#
# "daily": one conversation per local calendar day (host timezone). The first
#   run after local midnight rotates; every run within the same local day
#   resumes. Bounds unbounded transcript growth on a predictable, documented
#   boundary rather than waiting for the context ceiling to force the issue.
VALID_CONVERSATION_LIFECYCLES = {"continuous", "fresh", "daily"}
DEFAULT_CONVERSATION_LIFECYCLE = "continuous"

# The governing contract every remedy message points back at. One string so a
# rename is a single edit and every refusal stays consistent.
CONTRACT_REF = "contracts/automation-file.v1.md"

# The closed top-level vocabulary of the `automation:` mapping (contract rule 2;
# the human-facing registry is docs/AUTOMATIONS.md section 2). Any key here that
# is not in this set is refused loudly with a remedy -- never ignored. The
# retired keys (`session`, `session_workspace`, `prompt_caching`) are refused
# earlier with their own specific migration messages, so they never reach the
# generic closed-vocabulary check.
KNOWN_AUTOMATION_KEYS = frozenset(
    {
        "name",
        "enabled",
        "trigger",
        "notify",
        "requires",
        "inject",
        "conversation",
        "agent_config",
        "guidance_delivery",
        "steps",
    }
)

# The exact keys a single step object may carry (contract rule 3). An unknown
# key inside a step is refused the same way as an unknown top-level key.
KNOWN_STEP_KEYS = frozenset({"id", "prompt", "label"})

# A step `id` is a slug: lowercase letters, digits, and single hyphens between
# them. Same filesystem-safe shape as an automation slug (see `_slugify`), so a
# step id can appear in run-record paths and event payloads without escaping.
_STEP_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)

# Detects the RETIRED body-steps shape (contract rule 1, clean cut): a numbered
# list item at column 0 in the markdown body. Steps used to live here as
# ``N. text``; they are now structured frontmatter data and the body is never
# parsed for execution, so a body that still carries numbered steps is refused
# with a pointer to the contract rather than silently ignored.
_BODY_NUMBERED_RE = re.compile(r"^\d+\.\s", re.MULTILINE)

# Surgical-edit regexes for removing the retired session-pin lines from a
# frontmatter block without disturbing anything else in the file. Distinct
# from ``_FRONTMATTER_RE`` above, which is used for parsing and discards the
# delimiters; these keep them so the original text can be reassembled
# byte-for-byte apart from the lines being removed. The write-back twins of
# these (``_set_session_in_frontmatter`` and friends) are GONE: the engine no
# longer writes state into policy files at all -- see ``drumbeat.session_pins``.
_FRONTMATTER_BLOCK_RE = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*\n?)(.*)\Z", re.DOTALL)


class AutomationError(Exception):
    """Raised when an automation file is malformed or fails validation.

    Always carries the file path so a broken automation can be traced back to
    its source without guessing which file the error came from. Every
    construction is durably logged (see ``drumbeat.error_log``) -- this is the
    single choke point for that, so no raise site anywhere in the codebase
    (present or future) can accidentally go unlogged.
    """

    def __init__(self, path: Path, problem: str) -> None:
        self.path = path
        self.problem = problem
        log_automation_error(path, problem)
        super().__init__(f"{path}: {problem}")


@dataclass(frozen=True)
class Trigger:
    """When an automation fires."""

    type: str
    expression: str | None


@dataclass(frozen=True)
class InjectSpec:
    """One declared ``inject:`` entry (see docs/ARCHITECTURE.md section 6).

    ``argv`` is executed before step 1 of every run; its stdout becomes an
    injected turn under the hybrid-sentinel contract (timeout -> exit code
    -> stdout; ``INJECT_IDLE`` skips with a recorded reason; bare-empty
    stdout aborts loud). Both fields are required with no defaults: an
    inject with no label would make its ``inject_skipped`` events
    unattributable (failure class 13).
    """

    argv: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class Step:
    """One structured automation step (contract rule 3).

    A step is judgment plus identity and nothing operational:

    - ``id`` -- a slug, unique within the file. It is identity, not control
      flow: it appears in run records so a run's turns can be tied back to the
      declared step, and it survives an edit to the prompt text.
    - ``prompt`` -- the entirety of the step's behavior, fed as one sequential
      agent turn. Non-empty, stripped.
    - ``label`` -- optional human display name; carries no behavior.

    Scheduling, notification policy, conversation lifecycle, and agent config
    are whole-automation (top-level) concerns -- a step carries none of them.
    """

    id: str
    prompt: str
    label: str | None = None


@dataclass(frozen=True)
class Automation:
    """A fully parsed and validated automation."""

    name: str
    enabled: bool
    trigger: Trigger
    notify: str  # always | auto | never | urgent-only
    requires: list[str]
    steps: list[Step]  # ordered structured steps from frontmatter `steps:`
    path: Path
    slug: str  # kebab-case of name, filesystem-safe
    # Section 7.1 `inject:` -- declared argv pre-step injections, in order.
    # Empty tuple when the frontmatter has no `inject:` key.
    inject: tuple[InjectSpec, ...] = ()
    # How required guidance FILES reach the agent: "reference" (default,
    # preferred -- inject paths + a read-first preamble, turn text stays tiny)
    # or "inline" (embed bodies verbatim). See VALID_GUIDANCE_DELIVERY.
    # Defaulted, so an automation that never names it is a reference-form
    # automation -- which is how a resumed session keeps reading the CURRENT
    # guidance file rather than a snapshot in its transcript.
    guidance_delivery: str = DEFAULT_GUIDANCE_DELIVERY
    # Cross-run conversation lifecycle: "continuous" (default), "fresh", or
    # "daily". See VALID_CONVERSATION_LIFECYCLES. Default "continuous" =
    # unchanged behavior for every automation that never sets it: one pinned
    # conversation, resumed every run, rotated only by the engine's health
    # signals. "fresh" starts a new conversation every run; "daily" starts one
    # per host-local calendar day. Both non-continuous modes rotate through the
    # same flock-guarded rotation path the health signals use.
    conversation: str = DEFAULT_CONVERSATION_LIFECYCLE
    # Per-automation agent-config overlay (see ``drumbeat.agent_config``). The
    # highest-precedence layer merged into the ONE materialized amplifier-agent
    # host config handed to the engine on every turn of this automation. A
    # mapping in the closed top-level vocabulary
    # ``provider | providers | mcp | skills | debug``, validated at parse time
    # so a malformed block surfaces through ``load_all_tolerant`` -> ``doctor``
    # rather than at run time. ``None`` (the default) means this automation
    # contributes no overlay -- its turns are handed whatever the lower layers
    # resolve to, or no host config at all.
    agent_config: dict[str, Any] | None = None
    #
    # NOTE: there is deliberately no ``session`` / ``session_workspace``
    # here. Those were engine state living in a policy file; they now live
    # in ``drumbeat.session_pins``, keyed by slug, under the data dir. An
    # automation file is pure policy and byte-identical across machines.
    # Read a pin with ``session_pins.get(automation.slug, runs_dir=...)``.


def _slugify(name: str) -> str:
    """Convert a name to a kebab-case, filesystem-safe slug.

    Strips anything that isn't alphanumeric or a hyphen (notably ``:``, which
    is illegal in Windows paths), collapses whitespace/hyphen runs, and lowers
    the case.
    """
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    slug = slug.strip("-")
    if not slug:
        raise ValueError(f"name {name!r} produces an empty slug")
    return slug


def _split_frontmatter(path: Path, text: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise AutomationError(
            path,
            "missing YAML frontmatter (file must start with '---' delimited block)",
        )
    raw_frontmatter, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        raise AutomationError(path, f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise AutomationError(path, "frontmatter must be a YAML mapping")
    return data, body


def _refuse_body_steps(path: Path, body: str) -> None:
    """Refuse the RETIRED body-steps shape (contract rule 1, clean cut).

    Steps used to be an ordered list of ``N. text`` items in the markdown
    body. They are structured frontmatter data now (``steps:``), and the body
    is never parsed for execution. A body that still carries a numbered list is
    the classic migration hazard -- the author moved on believing those lines
    still run, while the engine ignores them entirely -- so it is refused
    loudly with a pointer to the contract rather than silently dropped. This is
    a clean cut: the body-steps shape is never dual-read.
    """
    if _BODY_NUMBERED_RE.search(body):
        raise AutomationError(
            path,
            "the markdown body carries a numbered list, which is the RETIRED "
            "body-steps shape -- steps are structured frontmatter data now "
            "(`steps:` in the `automation:` block) and the body is never "
            "parsed for execution. Move each numbered step into a `steps:` "
            f"entry with an `id` and a `prompt` (see {CONTRACT_REF}); the body "
            "is for a human-facing description only. If this numbered list is "
            "genuinely prose, remove the `N.` numbering.",
        )


def _parse_steps(path: Path, raw: object) -> list[Step]:
    """Parse and validate the frontmatter ``steps:`` list (contract rule 3).

    ``steps:`` is a required, ordered list of step objects. Each object has an
    ``id`` (a slug, unique within the file), a non-empty ``prompt``, and an
    optional ``label``. Strict on purpose: an unknown key inside a step, a
    missing or duplicate id, or an empty prompt each makes the automation
    invalid, because a silently-wrong step runs unattended.
    """
    if raw is None:
        raise AutomationError(
            path,
            "automation.steps is required: an ordered list of step objects, "
            f"each with an `id` and a `prompt` (see {CONTRACT_REF}).",
        )
    if not isinstance(raw, list) or not raw:
        raise AutomationError(
            path,
            "automation.steps must be a non-empty list of step mappings "
            f"(each with an `id` and a `prompt`; see {CONTRACT_REF}).",
        )

    steps: list[Step] = []
    seen_ids: dict[str, int] = {}
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise AutomationError(
                path,
                f"automation.steps[{i}] must be a mapping with an `id` and a "
                f"`prompt`, got {type(entry).__name__}.",
            )
        unknown = set(entry) - KNOWN_STEP_KEYS
        if unknown:
            raise AutomationError(
                path,
                f"automation.steps[{i}] has unknown key(s) {sorted(unknown)}; "
                f"only {sorted(KNOWN_STEP_KEYS)} are allowed. A step is "
                "judgment (prompt) plus identity (id) and carries no "
                f"operational config -- see {CONTRACT_REF}.",
            )

        step_id = entry.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise AutomationError(
                path,
                f"automation.steps[{i}].id is required and must be a non-empty "
                "slug string.",
            )
        step_id = step_id.strip()
        if not _STEP_ID_RE.match(step_id):
            raise AutomationError(
                path,
                f"automation.steps[{i}].id {step_id!r} must be a slug "
                "(lowercase letters, digits, and single hyphens between them).",
            )
        if step_id in seen_ids:
            raise AutomationError(
                path,
                f"automation.steps[{i}].id {step_id!r} duplicates "
                f"automation.steps[{seen_ids[step_id]}].id -- step ids must be "
                "unique within the file (they are the step's identity in run "
                "records).",
            )
        seen_ids[step_id] = i

        prompt = entry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise AutomationError(
                path,
                f"automation.steps[{i}].prompt (id {step_id!r}) is required and "
                "must be non-empty text -- it is the entirety of the step's "
                "behavior.",
            )

        label = entry.get("label")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise AutomationError(
                path,
                f"automation.steps[{i}].label (id {step_id!r}), when present, "
                "must be a non-empty string.",
            )

        steps.append(
            Step(
                id=step_id,
                prompt=prompt.strip(),
                label=label.strip() if isinstance(label, str) else None,
            )
        )

    return steps


def _parse_trigger(path: Path, raw: object, *, enabled: bool) -> Trigger:
    if not isinstance(raw, dict):
        raise AutomationError(path, "automation.trigger must be a mapping")
    trigger_type = raw.get("type")
    if not isinstance(trigger_type, str) or trigger_type not in VALID_TRIGGER_TYPES:
        raise AutomationError(
            path,
            f"automation.trigger.type must be one of {sorted(VALID_TRIGGER_TYPES)}, "
            f"got {trigger_type!r}",
        )
    expression = raw.get("expression")
    if expression is not None and not isinstance(expression, str):
        raise AutomationError(path, "automation.trigger.expression must be a string")
    if trigger_type == "schedule" and not expression:
        raise AutomationError(
            path, "automation.trigger.expression is required for type 'schedule'"
        )
    # "event" is kept in VALID_TRIGGER_TYPES deliberately -- it is a real,
    # named future capability (Graph change-notification subscriptions),
    # which a future UI would render as a visibly-disabled, captioned option
    # rather than hide. But nothing anywhere implements it: scheduler.serve() only
    # ever tracks trigger.type == "schedule", so an ENABLED automation
    # declaring trigger: event validated cleanly and then ran forever,
    # silently, never once -- the exact "config that looks accepted and
    # is silently inert" failure this project's first principle (fail
    # loud) exists to prevent. A disabled placeholder is fine (that's the
    # UI's own future authoring path); an enabled one can never do what it
    # claims, so it fails validation now instead of failing silently
    # forever.
    if trigger_type == "event" and enabled:
        raise AutomationError(
            path,
            "automation.trigger.type 'event' has no scheduler support yet "
            "(reserved for future Graph change-notification triggers) -- an "
            "enabled automation with this trigger would validate cleanly and "
            "then never run, silently, forever. Set automation.enabled: false "
            "to keep this as a placeholder, or use trigger.type: schedule or "
            "manual.",
        )
    return Trigger(type=trigger_type, expression=expression)


def _parse_inject(path: Path, raw: object) -> tuple[InjectSpec, ...]:
    """Parse and validate the ``inject:`` frontmatter key (section 7.1).

    Strict on purpose -- an inject entry runs unattended before every run
    of this automation, so a silently-wrong parse produces silently-wrong
    injections. Every field is required with no default.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise AutomationError(
            path, "automation.inject must be a non-empty list of mappings when present"
        )
    specs: list[InjectSpec] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise AutomationError(
                path, f"automation.inject[{i}] must be a mapping with argv and label"
            )
        unknown = set(entry) - {"argv", "label"}
        if unknown:
            raise AutomationError(
                path,
                f"automation.inject[{i}] has unknown key(s) {sorted(unknown)}; "
                "only argv and label are allowed",
            )
        argv = entry.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a.strip() for a in argv)
        ):
            raise AutomationError(
                path,
                f"automation.inject[{i}].argv is required and must be a non-empty "
                "list of non-empty strings",
            )
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise AutomationError(
                path,
                f"automation.inject[{i}].label is required and must be a non-empty "
                "string -- an unlabeled injection's skip events would be "
                "unattributable",
            )
        specs.append(InjectSpec(argv=tuple(argv), label=label.strip()))
    return tuple(specs)


def load(path: Path) -> Automation:
    """Parse and validate a single automation file.

    Raises:
        AutomationError: if the file is missing, malformed, or fails
            validation (no steps, unknown notify value, unknown trigger
            type, missing name).
    """
    path = Path(path).expanduser()
    if not path.is_file():
        raise AutomationError(path, "file not found")

    text = path.read_text(encoding="utf-8")
    return load_from_text(path, text)


def load_all(directory: Path) -> list[Automation]:
    """Parse and validate every ``*.md`` automation file in a directory.

    Files are processed in sorted (deterministic) order. A single malformed
    file aborts the whole load — fail loud, don't silently skip a broken
    automation and continue as if it didn't exist. This is the right
    behavior for a one-shot command (the consumer CLI's ``run``/``validate``,
    ``load_by_slug``) where the caller wants to know about ANY problem
    before proceeding. For a long-running loop where one broken file must
    not take every other, already-working automation down with it, see
    ``load_all_tolerant`` below.
    """
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise AutomationError(directory, "automations directory not found")

    automations = []
    for md_path in sorted(directory.glob("*.md")):
        automations.append(load(md_path))
    return automations


@dataclass(frozen=True)
class AutomationLoadFailure:
    """One automation file that failed to parse, and why.

    Carries the same ``path``/``problem`` shape as ``AutomationError`` so a
    caller can report it identically -- this is the non-raising twin of
    that exception, not a different vocabulary for the same fact.
    """

    path: Path
    problem: str


def load_all_tolerant(
    directory: Path,
) -> tuple[list[Automation], list[AutomationLoadFailure]]:
    """Parse every ``*.md`` automation file in a directory, isolating failures.

    Unlike ``load_all``, one malformed file does NOT abort the batch: a
    scheduler loop must keep running every automation that DOES parse even
    while one unrelated file is broken. A single bad file's blast radius
    must be itself, not the whole fleet -- observed live (2026-08-05): a
    trailing paragraph after the numbered steps in one new automation file
    made ``load_all`` raise on every poll tick, which silently stopped
    teams-check, email-check, reconciliation, and every other
    already-working automation until the one file was fixed. Since the
    system exists so the user can author automations freely, a typo in one
    new file must never punish every other one.

    Each parse failure is still fail-loud, just not fail-everything: every
    ``AutomationError`` raised while attempting a file is durably logged by
    ``drumbeat.error_log`` (via its own constructor, unconditionally, the
    same as any other raise site) and returned here as an
    ``AutomationLoadFailure`` for the caller to surface -- e.g. the
    scheduler names the broken file in its log on every tick it remains
    broken, and the consumer CLI's ``list`` shows it as broken rather than refusing to
    list anything.

    A file that was previously fine and breaks between calls (edited
    mid-run) and a file that was already broken before the caller ever
    loaded this directory are handled identically here -- both simply fail
    to appear in the returned ``automations`` list on whichever call is
    made after the breakage. No separate "was this newly broken" state is
    tracked in this function; callers that need registration bookkeeping
    (the scheduler's ``next_due``) already recompute it from the current
    successfully-parsed set on every tick, so a newly-broken file falls out
    of scheduling the same way a deleted or disabled one does.

    Files are processed in sorted (deterministic) order, matching
    ``load_all``.

    Raises:
        AutomationError: if the directory itself is missing -- there is
            nothing to isolate a per-file failure from when there are no
            files to enumerate at all.
    """
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        raise AutomationError(directory, "automations directory not found")

    automations: list[Automation] = []
    failures: list[AutomationLoadFailure] = []
    by_slug: dict[str, Path] = {}
    for md_path in sorted(directory.glob("*.md")):
        try:
            parsed = load(md_path)
        except AutomationError as exc:
            failures.append(AutomationLoadFailure(path=md_path, problem=exc.problem))
            continue
        # The slug is the key for EVERYTHING downstream: the session pin, the
        # run directory, the scheduler's next_due entry, every management API
        # path. Two files whose `name` slugifies the same collide on all of
        # them silently -- they share one pinned conversation, write runs into
        # one directory, and the first one loaded quietly loses its schedule
        # slot to the second. Nothing raised, nothing logged; the automation
        # simply behaves like the other one.
        #
        # Reported as a load failure on the LATER file (sorted order, so the
        # winner is deterministic) rather than raised: this is the tolerant
        # loader, and one duplicated name must not stop the fleet. The
        # scheduler already names every failure on every tick, so this rides
        # the loudspeaker that exists.
        first = by_slug.get(parsed.slug)
        if first is not None:
            failures.append(
                AutomationLoadFailure(
                    path=md_path,
                    problem=(
                        f"duplicate slug {parsed.slug!r}: {first.name} already "
                        f"claims it (automation.name {parsed.name!r} slugifies to "
                        "the same key). The slug keys the session pin, the run "
                        "directory, and the API path -- two files cannot share "
                        "one. Rename one automation."
                    ),
                )
            )
            continue
        by_slug[parsed.slug] = md_path
        automations.append(parsed)
    return automations, failures


def load_by_slug(slug: str, directory: Path) -> Automation:
    """Find and load a single automation by slug from a directory.

    Used by the notify service to resolve the "chat" automation for
    ``/api/message`` — fails loud (rather than silently falling back to a
    bare ad-hoc session) when the automation is missing or the directory
    itself fails to load.

    Raises:
        AutomationError: if no automation with that slug exists, or the
            directory fails to load at all.
    """
    directory = Path(directory).expanduser()
    for a in load_all(directory):
        if a.slug == slug:
            return a
    raise AutomationError(
        directory, f"no automation with slug {slug!r} found in {directory}"
    )


def load_from_text(path: Path, text: str) -> Automation:
    """Parse and validate automation content already in memory.

    Shares every validation rule with ``load()`` (which just reads the
    file first) -- used by the management API to validate incoming edits
    before they touch disk.
    """
    data, body = _split_frontmatter(path, text)

    section = data.get("automation")
    if not isinstance(section, dict):
        raise AutomationError(path, "frontmatter must contain an 'automation' mapping")

    name = section.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AutomationError(
            path, "automation.name is required and must be a non-empty string"
        )
    name = name.strip()

    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AutomationError(path, "automation.enabled must be a boolean")

    trigger = _parse_trigger(path, section.get("trigger"), enabled=enabled)

    notify = section.get("notify", "auto")
    if notify not in VALID_NOTIFY_VALUES:
        raise AutomationError(
            path,
            f"automation.notify must be one of {sorted(VALID_NOTIFY_VALUES)}, got {notify!r}",
        )

    requires_raw = section.get("requires", [])
    if not isinstance(requires_raw, list) or not all(
        isinstance(item, str) for item in requires_raw
    ):
        raise AutomationError(path, "automation.requires must be a list of strings")
    requires = list(requires_raw)

    guidance_delivery = section.get("guidance_delivery", DEFAULT_GUIDANCE_DELIVERY)
    if guidance_delivery not in VALID_GUIDANCE_DELIVERY:
        raise AutomationError(
            path,
            f"automation.guidance_delivery must be one of "
            f"{sorted(VALID_GUIDANCE_DELIVERY)}, got {guidance_delivery!r}",
        )

    # Retired sugar: ``prompt_caching:`` was a deprecated alias that folded into
    # ``agent_config.provider.config.enable_prompt_caching``. The upstream
    # provider defect it routed around is fixed, so the alias is gone -- and it
    # is REFUSED loudly, never tolerated-and-ignored. An author who still writes
    # ``prompt_caching: false`` believing it disables caching, while the engine
    # silently drops it, is exactly the "enabled, validated, inert" failure this
    # parser exists to prevent (same discipline as the retired session pins
    # below). Set ``agent_config:`` with ``provider.config.enable_prompt_caching``
    # directly instead.
    if "prompt_caching" in section:
        raise AutomationError(
            path,
            "automation.prompt_caching is no longer a recognized key -- the "
            "deprecated caching alias was removed. Set `agent_config:` with "
            "`provider.config.enable_prompt_caching: false` directly instead.",
        )

    # Cross-run conversation lifecycle. Closed vocabulary (see
    # VALID_CONVERSATION_LIFECYCLES); an unknown value is refused loudly rather
    # than coerced, because a lifecycle that validated and then did something
    # other than what it named would silently abandon or silently accumulate a
    # conversation -- the exact fail-silent shape this parser exists to prevent.
    conversation = section.get("conversation", DEFAULT_CONVERSATION_LIFECYCLE)
    if conversation not in VALID_CONVERSATION_LIFECYCLES:
        raise AutomationError(
            path,
            f"automation.conversation must be one of "
            f"{sorted(VALID_CONVERSATION_LIFECYCLES)}, got {conversation!r}",
        )

    # Optional per-automation agent-config overlay. Validated HERE, at parse
    # time, against the same closed vocabulary / null / recursive-credential
    # rules the resolver enforces on every other layer -- so a malformed block
    # surfaces through ``load_all_tolerant`` -> ``doctor`` exactly like any other
    # authoring mistake, rather than blowing up mid-run. See
    # ``drumbeat.agent_config``.
    agent_config_raw = section.get("agent_config")
    if agent_config_raw is None:
        agent_config_value: dict[str, Any] | None = None
    else:
        try:
            agent_config_value = agent_config.validate_config_layer(
                agent_config_raw, source="automation.agent_config"
            )
        except agent_config.AgentConfigError as exc:
            raise AutomationError(path, str(exc)) from exc

    # Retired keys: REFUSE, never tolerate-and-ignore. A key that reads as
    # meaningful and silently does nothing is the worst defect shape in a
    # fail-loud project -- an automation whose frontmatter still names a
    # conversation, while the engine resumes a different one (or none), is
    # exactly "enabled, validated, inert" with a receipt. Session pins moved
    # out of policy-file frontmatter into engine state
    # (<data-dir>/session_pins.json) precisely so a stale frontmatter value
    # could never silently diverge from the session the engine actually
    # resumes; a leftover key is refused rather than read.
    retired_pin_keys = [k for k in ("session", "session_workspace") if k in section]
    if retired_pin_keys:
        raise AutomationError(
            path,
            f"automation.{' and automation.'.join(retired_pin_keys)} "
            f"{'is' if len(retired_pin_keys) == 1 else 'are'} no longer read "
            "from frontmatter: session pins live in engine state "
            "(<data-dir>/session_pins.json); remove the line(s). To control how "
            "the conversation persists across runs, use the `conversation:` key "
            f"({' | '.join(sorted(VALID_CONVERSATION_LIFECYCLES))}) instead.",
        )

    # Closed frontmatter vocabulary (contract rule 2). Every key in the
    # `automation:` mapping is registered (KNOWN_AUTOMATION_KEYS, mirrored by
    # the human registry in docs/AUTOMATIONS.md section 2); an unknown key is
    # refused loudly with a remedy, never ignored. Placed AFTER the retired-key
    # refusals above so `session`/`session_workspace`/`prompt_caching` still get
    # their own specific migration messages rather than this generic one.
    unknown_keys = sorted(set(section) - KNOWN_AUTOMATION_KEYS)
    if unknown_keys:
        raise AutomationError(
            path,
            f"automation has unknown top-level key(s) {unknown_keys}; the "
            f"registered vocabulary is {sorted(KNOWN_AUTOMATION_KEYS)}. An "
            "unknown key is refused rather than ignored so a typo can never "
            f"silently do nothing (see {CONTRACT_REF} and docs/AUTOMATIONS.md).",
        )

    inject = _parse_inject(path, section.get("inject"))

    # The body is a human-facing description, never parsed for execution
    # (contract rule 1). Refuse the retired body-steps shape loudly before
    # reading the structured steps from the frontmatter.
    _refuse_body_steps(path, body)

    steps = _parse_steps(path, section.get("steps"))

    try:
        slug = _slugify(name)
    except ValueError as exc:
        raise AutomationError(path, str(exc)) from exc

    return Automation(
        name=name,
        enabled=enabled,
        trigger=trigger,
        notify=notify,
        requires=requires,
        steps=steps,
        path=path,
        slug=slug,
        inject=inject,
        guidance_delivery=guidance_delivery,
        conversation=conversation,
        agent_config=agent_config_value,
    )


def validate_automation_content(text: str, *, path: Path | None = None) -> Automation:
    """Validate automation file content, raising ``AutomationError`` on any violation.

    The engine's own conformance surface for ``contracts/automation-file.v1.md``
    (see the contract's Conformance section). It is exactly the same validation
    ``load()``/``load_from_text`` apply, exposed under a name the contract and
    its fixtures can call directly. Every frozen-core rule is enforced here:

    - unknown top-level key (closed frontmatter vocabulary, rule 2)
    - unknown step key (rule 3)
    - missing ``steps`` / empty ``steps`` (rules 3, 6)
    - missing or duplicate step ``id`` (rule 3)
    - empty step ``prompt`` (rule 3)
    - the retired body-steps shape (rule 1, clean cut)

    Args:
        text: the full automation file content (frontmatter + body).
        path: optional source path for error attribution; a placeholder is
            used when validating detached content (e.g. an incoming edit).

    Returns:
        The parsed, valid ``Automation``.

    Raises:
        AutomationError: naming the offending key/step and the remedy.
    """
    return load_from_text(path if path is not None else Path("<automation>"), text)
