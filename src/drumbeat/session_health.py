"""When a pinned amplifier-agent session must be abandoned -- mechanism only.

A pinned session accumulates conversation forever. Two things go wrong with
that, and *only* two have ever been observed to actually break a run here.
Both are mechanically detectable with zero judgment, which is why they live
in code; everything else about session growth is judgment, which is why it
lives in ``automations/session-growth-check.md`` instead.

A third, pre-emptive trigger -- the TRANSCRIPT SIZE GATE -- lives in
``runner`` rather than here, because it needs nothing from this module's
state stores: it is a ``stat()`` of the pinned session's transcript against
a byte gate, taken before the first turn. See ``NOT A PREDICTOR, BUT A
GATE`` below and ``runner._DEFAULT_SESSION_ROTATE_BYTES``.

Trigger 1 -- CEILING HIT (permanent, self-inflicted deadlock)
    The provider rejects the request outright::

        prompt is too long: 219685 tokens > 200000 maximum

    This is not a bad day. amplifier-agent's vendored context-simple bundle
    compacts at ``0.8 x 300000 = 240000`` tokens; the provider refuses at
    ``200000``. A session whose prompt lands in that 200k-240k window can
    never compact its way out, because compaction never fires. Measured on
    this project's own run history: ``channels-check-20260804T221148Z`` hit
    it and then failed **12 consecutive times over 27 hours**;
    ``agent-sessions-check-20260730T111014Z`` hit it and failed twice before
    a human rotated it by hand. **Zero recoveries, ever.** So the first
    ceiling hit is a zero-false-positive signal that the session is dead,
    and the cost of acting on it immediately is exactly one run -- the one
    that already failed.

Trigger 2 -- CONTRACT DRIFT (the session argues against its own automation)
    When an automation's steps are rewritten, the pinned session still
    carries every *old* instruction as conversation history, and keeps
    obeying it. Measured: ``daily-rollup-20260805T202538Z`` answered the
    bare sentinel ``NOTHING_TO_REPORT`` to a step that asks no question, for
    **15 consecutive runs**, and reasoned about a schedule its automation no
    longer declared. Fingerprinting the *steps* (the contract) and comparing
    on resume catches exactly that, before the bad run rather than after 15.

TRANSCRIPT SIZE ON DISK -- NOT A PREDICTOR, BUT A GATE
    Size cannot tell you how close a prompt is to the ceiling. Measured
    against the two runs whose true token counts the provider told us:

        ============================================  =======  ==========  =========
        session                                        on disk  true tokens  bytes/tok
        ============================================  =======  ==========  =========
        channels-check-20260804T221148Z                10.4 MB     219,685      ~50
        agent-sessions-check-20260730T111014Z          33.0 MB     201,361     ~172
        ============================================  =======  ==========  =========

    The *smaller* file produced the *larger* prompt, and the implied
    bytes-per-token differs by 3.4x. Compaction has already discarded an
    unknown prefix, and the file carries megabytes of thinking-block
    signatures that are never sent to the provider. No byte count can
    therefore *predict* a ceiling hit, and the consumer CLI's
    ``session-health`` verb still reports size as an *aside* for that reason.

    What a byte count CAN do is bound the region a session occupies.
    Measured over an 8-day production window (4,133 runs carrying a
    ``session_transcript_bytes_at_start``, 157 sessions): all 41 observed
    ``ContextLengthError`` runs started from at least 5,586,751 bytes, while
    1,138 runs started at or below 5,000,000 bytes and none of them hit the
    ceiling. So the gate is calibrated to keep sessions inside the region
    where no crash was ever observed -- not to guess where the ceiling is.
    It is enforced in ``runner``, ahead of the first turn, and rotates
    through the same single path every trigger here uses.

Rotation is safe here, and that is a measured claim rather than a hope.
``runner.run()`` re-injects the durable state into **every** run --
``format_requirements_turn`` (the ``guidance/*.md`` policy files, verbatim,
every firing) and the consumer's `inject:` state turn (its durable state plus the
recently-discharged window, with ids, stakes, notify counts and ages).
Checked across all five rotations this project has performed: 71/71 item ids
survived Daily Rollup's boundary, 27/27 survived Agent Sessions Check's, and
no post-rotation run contained an "I don't have that context" phrase. The
transcript is sediment, not memory.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from drumbeat.automation import Step

from drumbeat import session_pins
from drumbeat.fsutil import atomic_write

# The provider's own refusal text. Kept deliberately loose about the numbers
# (any provider, any ceiling) and strict about the shape, so a future model
# with a different limit still trips it. Matched against a run's captured
# stderr, which is where amplifier-agent surfaces the provider error --
# verified against runs/channels-check/*/stderr.log.
CEILING_RE = re.compile(
    r"prompt is too long:\s*(\d+)\s*tokens?\s*>\s*(\d+)\s*maximum",
    re.IGNORECASE,
)

# Contract fingerprints live beside every other durable operational record
# this project keeps (failures.log, session_rotations.jsonl,
# automation_lint.jsonl) -- one place to look, greppable, gitignored.
CONTRACT_STORE_NAME = "session_contracts.json"


@dataclass(frozen=True)
class CeilingHit:
    """A provider refusal that proves the pinned session is unrecoverable."""

    prompt_tokens: int
    limit_tokens: int

    @property
    def detail(self) -> str:
        return (
            f"prompt is too long: {self.prompt_tokens} tokens > "
            f"{self.limit_tokens} maximum"
        )


@dataclass(frozen=True)
class SessionHealth:
    """Read-only health of one automation's pinned session.

    ``ceiling_hit`` and ``contract_drifted`` are the only two fields that
    ever justify a rotation. ``transcript_bytes`` is an aside -- see this
    module's docstring for why it is not a threshold.
    """

    automation: str
    slug: str
    session_id: str | None
    enabled: bool
    transcript_bytes: int | None
    transcript_lines: int | None
    ceiling_hit: CeilingHit | None
    contract_drifted: bool
    contract_recorded: bool
    consecutive_failures: int
    detail: str


def detect_ceiling_hit(text: str) -> CeilingHit | None:
    """Return the provider's context-ceiling refusal in ``text``, if any.

    Args:
        text: captured stderr from one or more turns. Empty/None-ish input
            is fine and yields ``None``.

    Returns:
        The ``CeilingHit`` with the largest prompt size found (a run may
        retry and log more than one), or ``None`` if the text carries no
        ceiling refusal at all.

    Example:
        >>> hit = detect_ceiling_hit("prompt is too long: 219685 tokens > 200000 maximum")
        >>> hit.prompt_tokens, hit.limit_tokens
        (219685, 200000)
        >>> detect_ceiling_hit("some unrelated failure") is None
        True
    """
    if not text:
        return None
    hits = [
        CeilingHit(prompt_tokens=int(m.group(1)), limit_tokens=int(m.group(2)))
        for m in CEILING_RE.finditer(text)
    ]
    if not hits:
        return None
    return max(hits, key=lambda h: h.prompt_tokens)


def contract_fingerprint(steps: Sequence[Step]) -> str:
    """Stable sha256 over an automation's ordered step **prompts**.

    Deliberately covers the step prompts only -- never the frontmatter, and
    never a step's ``id`` or ``label``. The prompt text is the contract the
    pinned session obeys; the ``id`` is engine-facing identity and the
    ``label`` is human display, both of which must be editable without
    abandoning the conversation. Hashing the prompt text (and only that) is
    also what makes this fingerprint stable across the steps-to-frontmatter
    migration: a step whose prompt is unchanged keeps its fingerprint even
    though it moved from the markdown body into ``steps:``, so an existing
    pinned session is not needlessly rotated by the format change alone.

    (Historically the steps-only scope mattered doubly: the runner used to
    write ``session:``/``session_workspace:`` back into the frontmatter, so a
    whole-file hash would have reported drift against itself on the very next
    run and rotated forever. Pins are engine state now -- see
    ``drumbeat.session_pins`` -- but the prompt-only scope is unchanged.)

    Args:
        steps: the automation's ordered ``Step`` objects.

    Returns:
        Lowercase hex sha256 digest.

    Example:
        >>> from types import SimpleNamespace as S
        >>> contract_fingerprint([S(prompt="a"), S(prompt="b")]) == \
        ...     contract_fingerprint([S(prompt="a"), S(prompt="b")])
        True
        >>> contract_fingerprint([S(prompt="a"), S(prompt="b")]) == \
        ...     contract_fingerprint([S(prompt="a"), S(prompt="c")])
        False
    """
    h = hashlib.sha256()
    for step in steps:
        h.update(step.prompt.strip().encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def contract_store_path(runs_dir: Path) -> Path:
    """Path to the session -> contract-fingerprint store under ``runs_dir``."""
    return Path(runs_dir).expanduser() / CONTRACT_STORE_NAME


@contextlib.contextmanager
def _contract_store_lock(runs_dir: Path):
    """Exclusive cross-process lock for session_contracts.json read-modify-write.

    Migration requirement (two processes, one workspace): during the
    hybrid window engine code runs in TWO processes (drumbeat serve and
    notify-serve's in-process engine library), and per-session flocks do not
    arbitrate this file's RMW. Sidecar-.lock flock, same convention as the
    outbox.
    """
    path = contract_store_path(Path(runs_dir).expanduser())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_store(runs_dir: Path) -> dict[str, dict[str, object]]:
    path = contract_store_path(runs_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        # FAIL LOUD, but never block a run: an unreadable store means drift
        # is *unknown*, which read_contract renders as "not recorded" -- and
        # an unrecorded contract never triggers a rotation. Silence here
        # would be the deferred mystery this project keeps paying for.
        print(
            f"[session-health] cannot read contract store {path}: {exc} -- "
            "contract drift cannot be evaluated this run",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        print(
            f"[session-health] contract store {path} is not valid JSON: {exc} -- "
            "contract drift cannot be evaluated this run",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        print(
            f"[session-health] contract store {path} is not a JSON object -- "
            "contract drift cannot be evaluated this run",
            file=sys.stderr,
        )
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def record_contract(
    *,
    session_id: str,
    automation_slug: str,
    fingerprint: str,
    recorded_at: str,
    runs_dir: Path,
    provider_module: str | None = None,
) -> None:
    """Remember the contract a freshly-created session was started under.

    Called once, at session creation. A write failure is printed and
    swallowed: it must never fail the run it is annotating, and the
    consequence is only that drift for this one session stays undetectable
    (which ``session-health`` reports explicitly as ``contract_recorded``
    False, rather than as "no drift").

    ``provider_module`` is recorded BESIDE the contract fingerprint (see
    ``claim_provider_rotation``): the effective provider module the session was
    created under, so a later change to it auto-rotates the pin. It is a
    separate axis from the steps-only contract fingerprint and never affects
    contract-drift semantics.
    """
    runs_dir = Path(runs_dir).expanduser()
    try:
        with _contract_store_lock(runs_dir):
            store = _read_store(runs_dir)
            store[session_id] = {
                "slug": automation_slug,
                "fingerprint": fingerprint,
                "recorded_at": recorded_at,
                "provider": provider_module,
            }
            atomic_write(
                contract_store_path(runs_dir),
                json.dumps(store, indent=2, sort_keys=True) + "\n",
            )
    except OSError as exc:
        print(
            f"[session-health] failed to record contract fingerprint for "
            f"{session_id!r}: {exc} -- contract drift will be undetectable "
            "for this session",
            file=sys.stderr,
        )


def read_contract(session_id: str, *, runs_dir: Path) -> str | None:
    """The fingerprint ``session_id`` was created under, or ``None`` if unknown."""
    entry = _read_store(Path(runs_dir).expanduser()).get(session_id)
    if not entry:
        return None
    fingerprint = entry.get("fingerprint")
    return fingerprint if isinstance(fingerprint, str) and fingerprint else None


def contract_drift(
    *, session_id: str, steps: Sequence[Step], runs_dir: Path
) -> tuple[bool, str | None]:
    """Has this session's automation been rewritten since it was created?

    Returns:
        ``(drifted, recorded_fingerprint)``. ``drifted`` is True only when a
        fingerprint was recorded **and** differs from the current steps --
        an unrecorded session is never treated as drifted, because "we don't
        know" must not masquerade as "yes".
    """
    recorded = read_contract(session_id, runs_dir=runs_dir)
    if recorded is None:
        return False, None
    return recorded != contract_fingerprint(steps), recorded


def forget_contract(session_id: str, *, runs_dir: Path) -> None:
    """Drop a rotated session's fingerprint so the store stays bounded."""
    runs_dir = Path(runs_dir).expanduser()
    try:
        with _contract_store_lock(runs_dir):
            store = _read_store(runs_dir)
            if session_id not in store:
                return
            del store[session_id]
            atomic_write(
                contract_store_path(runs_dir),
                json.dumps(store, indent=2, sort_keys=True) + "\n",
            )
    except OSError as exc:
        print(
            f"[session-health] failed to prune contract fingerprint for "
            f"{session_id!r}: {exc}",
            file=sys.stderr,
        )


# ---- conversation lifecycle sidecar (drumbeat-5wt) -------------------------
#
# The ``conversation:`` automation key (continuous | fresh | daily) needs ONE
# per-session fact recorded at creation and read on resume: the host-local
# calendar day the session's ``daily`` clock is anchored to. It rides as a
# NAMESPACED, additive sub-object on the SAME ``session_contracts.json`` entry
# the contract fingerprint uses, under the entry-local key
# ``LIFECYCLE_ENTRY_KEY`` -- so it composes with any other lane's additive
# field on that entry without collision, and ``forget_contract``'s whole-entry
# delete prunes it on rotation for free (no separate cleanup path).

LIFECYCLE_ENTRY_KEY = "conversation"


def record_lifecycle(
    *,
    session_id: str,
    mode: str,
    anchor_day: str,
    runs_dir: Path,
) -> None:
    """Record a fresh session's conversation-lifecycle sidecar.

    Merges a namespaced ``LIFECYCLE_ENTRY_KEY`` sub-object into the session's
    existing ``session_contracts.json`` entry. Additive: it preserves the
    contract fingerprint written by ``record_contract`` (which MUST run first,
    since it replaces the entry) and any other lane's additive field on the
    same entry.

    Best-effort, exactly like ``record_contract``: a write failure is printed
    and swallowed. It must never fail the run it annotates; the only
    consequence is that a ``daily`` session with no recorded anchor never
    lifecycle-rotates -- the same "unknown must not read as yes" posture the
    drift check takes.
    """
    runs_dir = Path(runs_dir).expanduser()
    try:
        with _contract_store_lock(runs_dir):
            store = _read_store(runs_dir)
            entry = dict(store.get(session_id, {}))
            entry[LIFECYCLE_ENTRY_KEY] = {"mode": mode, "anchor_day": anchor_day}
            store[session_id] = entry
            atomic_write(
                contract_store_path(runs_dir),
                json.dumps(store, indent=2, sort_keys=True) + "\n",
            )
    except OSError as exc:
        print(
            f"[session-health] failed to record conversation lifecycle for "
            f"{session_id!r}: {exc} -- daily rotation will be undetectable for "
            "this session",
            file=sys.stderr,
        )


def read_lifecycle_anchor_day(session_id: str, *, runs_dir: Path) -> str | None:
    """The host-local calendar day ``session_id`` is anchored to, or ``None``.

    ``None`` means no anchor was recorded (a session created before this
    feature, or one whose lifecycle write failed). A ``daily`` automation
    treats that as "do not rotate this run" rather than abandoning the
    conversation on missing data.
    """
    entry = _read_store(Path(runs_dir).expanduser()).get(session_id)
    if not entry:
        return None
    conversation = entry.get(LIFECYCLE_ENTRY_KEY)
    if not isinstance(conversation, dict):
        return None
    anchor = conversation.get("anchor_day")
    return anchor if isinstance(anchor, str) and anchor else None


# ---- provider-change rotation (built-in, owner decision: always rotate) ----
#
# A pinned session accumulates a transcript under ONE provider. Resuming it
# under a DIFFERENT provider module is not safe the way resuming under a new
# model is: the transcript carries provider-specific artifacts (thinking-block
# signatures, cache-control breakpoints, a provider-specific tokenization the
# next provider never produced) that the new provider can reject outright. So a
# provider-module change ALWAYS rotates the pin -- the same "leave the sediment,
# start fresh, re-seed durable state" move contract drift and a ceiling hit
# already make. The provider module is recorded BESIDE the steps-only contract
# fingerprint (a separate axis; it never touches contract semantics).


def read_provider(session_id: str, *, runs_dir: Path) -> str | None:
    """The effective provider module ``session_id`` was recorded under, or ``None``.

    ``None`` means "not recorded" -- an older session created before provider
    recording existed, or a path that recorded no provider. As with an
    unrecorded contract fingerprint, "we don't know" must never masquerade as a
    change, so callers treat ``None`` as "do not rotate".
    """
    entry = _read_store(Path(runs_dir).expanduser()).get(session_id)
    if not entry:
        return None
    provider = entry.get("provider")
    return provider if isinstance(provider, str) and provider else None


def provider_drift(
    *, session_id: str, current_provider: str, runs_dir: Path
) -> tuple[bool, str | None]:
    """Read-only: has this session's effective provider module changed?

    Returns ``(changed, recorded_provider)``. ``changed`` is True only when a
    provider was recorded AND differs from ``current_provider``. An unrecorded
    session is never "changed" (same rule as ``contract_drift``). Used for the
    dry-run "WOULD rotate" message; the real run path uses
    ``claim_provider_rotation``, which also claims the decision under lock.
    """
    recorded = read_provider(session_id, runs_dir=runs_dir)
    if recorded is None:
        return False, None
    return recorded != current_provider, recorded


def claim_provider_rotation(
    *, session_id: str, current_provider: str, runs_dir: Path
) -> tuple[bool, str | None]:
    """Atomically decide-and-claim whether THIS caller rotates for a provider change.

    Returns ``(should_rotate, previous_provider)``. The whole read-decide-write
    happens under the contract-store flock so two concurrent triggers (drumbeat
    serve and notify-serve's in-process engine both resuming the same session)
    cannot both rotate the same pin -- the documented concurrent-trigger race.

    Cases:

    - Unknown session (no recorded contract entry): ``(False, None)``. "We don't
      know" never rotates -- same rule as contract drift.
    - No provider recorded yet: record ``current_provider`` now and return
      ``(False, None)``. First observation BACKFILLS, never rotates, so the
      existing fleet is not rotated en masse the first time this ships.
    - Recorded equals current: ``(False, recorded)`` -- no change, no write.
    - Recorded differs from current: advance the stored provider to
      ``current_provider`` (the CLAIM) and return ``(True, recorded)``. A
      concurrent caller that takes the lock afterwards sees recorded == current
      and returns ``(False, current)``, so the pin is never rotated twice for
      the same transition.

    A write failure is announced and treated as "do not rotate" -- never fatal
    to the run it is annotating.
    """
    runs_dir = Path(runs_dir).expanduser()
    try:
        with _contract_store_lock(runs_dir):
            store = _read_store(runs_dir)
            entry = store.get(session_id)
            if not entry:
                return False, None
            raw = entry.get("provider")
            recorded = raw if isinstance(raw, str) and raw else None
            if recorded == current_provider:
                return False, recorded
            entry["provider"] = current_provider
            store[session_id] = entry
            atomic_write(
                contract_store_path(runs_dir),
                json.dumps(store, indent=2, sort_keys=True) + "\n",
            )
            # First observation (recorded is None) backfills without rotating;
            # a real transition (recorded is a different string) claims + rotates.
            return recorded is not None, recorded
    except OSError as exc:
        print(
            f"[session-health] provider-rotation decision for {session_id!r} "
            f"could not be persisted: {exc} -- not rotating this run",
            file=sys.stderr,
        )
        return False, None


def _transcript_path(session_id: str, workspace: str, *, agent_home: Path) -> Path:
    return (
        Path(agent_home).expanduser()
        / "state"
        / "workspaces"
        / workspace
        / "sessions"
        / session_id
        / "transcript.jsonl"
    )


def _transcript_stats(path: Path) -> tuple[int | None, int | None]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    try:
        with path.open(encoding="utf-8") as f:
            lines = sum(1 for _ in f)
    except OSError:
        return size, None
    return size, lines


def _scan_recent_runs(
    slug: str, *, session_id: str, runs_dir: Path, limit: int = 24
) -> tuple[CeilingHit | None, int]:
    """Ceiling hit (if any) and consecutive-failure count for ONE session.

    Scoped to ``session_id`` deliberately. A ceiling hit belongs to the
    session that hit it, and a rotation is precisely the act of leaving it
    behind -- so counting a predecessor's failures against the fresh
    session reports a healthy automation as dead. That is not hypothetical:
    the first version of this function scanned by slug alone and reported
    ``channels-check`` as DEAD minutes after its rotation, reading a
    ceiling hit out of the *previous* session's stderr.

    Reads at most ``limit`` most-recent run directories, newest first, and
    stops counting failures at the first success -- bounded work, same
    read-backward discipline as every other transcript read here.
    """
    auto_dir = Path(runs_dir).expanduser() / slug
    try:
        run_dirs = sorted((d for d in auto_dir.iterdir() if d.is_dir()), reverse=True)
    except OSError:
        return None, 0

    ceiling: CeilingHit | None = None
    consecutive = 0
    counting = True
    for run_dir in run_dirs[:limit]:
        result_path = run_dir / "result.json"
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("session_id") != session_id:
            continue
        failed = bool(data.get("failed"))
        if counting and failed:
            consecutive += 1
        elif counting:
            counting = False
        if failed and ceiling is None:
            try:
                stderr_text = (run_dir / "stderr.log").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                stderr_text = ""
            ceiling = detect_ceiling_hit(stderr_text)
    return ceiling, consecutive


@dataclass(frozen=True)
class RunHealth:
    """Public, minimal session-health verdict for one automation's pinned session.

    Wraps ``_scan_recent_runs`` above rather than reimplementing it, so the
    predecessor-session guard documented on that function (a rotation must
    actually clear a DEAD verdict) applies here too. Exists because
    ``_scan_recent_runs`` -- and the ``health_for`` report built on it --
    had **zero callers anywhere in src/** before this: the consecutive-
    failure counter was computed on every call and then thrown away. See
    ``~/dev/amplifier-attention-manager/drumbeat/src/drumbeat/session_health.py:368``
    for the reference implementation this mirrors.

    ``state`` is a 3-way verdict rather than a bare integer because a served
    status needs to let a client visually distinguish "one bad run" from
    "this has been dead for hours" without redoing the threshold arithmetic
    itself at every call site.
    """

    consecutive_failures: int
    ceiling_hit: CeilingHit | None
    state: str  # "healthy" | "degraded" | "dead"


def run_health(
    slug: str, *, session_id: str, runs_dir: Path, limit: int = 24
) -> RunHealth:
    """Public wrapper around ``_scan_recent_runs`` for callers outside this module.

    State rule (thresholds are measured, not arbitrary):

    - ``"dead"`` if a ceiling hit is present -- this module's own docstring
      records zero recoveries, ever, from a ceiling hit -- OR
      ``consecutive_failures >= 3``. Real run history has two
      measured cases of a drifted/ceiling-hit session going unnoticed for
      12 and 15 consecutive failures before anyone looked (see Triggers 1
      and 2 above); 3 is the smallest count that cannot be a transient
      blip -- one bad run, or even two in a row, still happens to healthy
      automations, but three in a row against the SAME pinned session is
      not noise.
    - ``"degraded"`` if there is at least one recent failure, so a single
      bad run is visible immediately rather than waiting for a third one
      to cross the "dead" line.
    - ``"healthy"`` otherwise.

    Args:
        slug: the automation's slug (its run directory name).
        session_id: the pinned session to scope the scan to -- see
            ``_scan_recent_runs`` for why this must be the CURRENT pin, not
            the slug alone.
        runs_dir: the engine's data directory.
        limit: most-recent run directories to read; see ``_scan_recent_runs``.
    """
    ceiling, consecutive = _scan_recent_runs(
        slug, session_id=session_id, runs_dir=runs_dir, limit=limit
    )
    if ceiling is not None or consecutive >= 3:
        state = "dead"
    elif consecutive >= 1:
        state = "degraded"
    else:
        state = "healthy"
    return RunHealth(consecutive_failures=consecutive, ceiling_hit=ceiling, state=state)


def health_for(
    automations: Iterable[object],
    *,
    runs_dir: Path,
    agent_home: Path,
    workspace: str,
) -> list[SessionHealth]:
    """Read-only health report for every automation's pinned session.

    Args:
        automations: parsed ``drumbeat.automation.Automation`` objects.
        runs_dir: the engine's data directory (the pin store lives here).
        agent_home: amplifier-agent's home (see ``paths.amplifier_agent_home``).
        workspace: fallback workspace slug for pins recorded before the
            ``session_workspace`` field existed.

    Returns:
        One ``SessionHealth`` per automation, in the order given. Purely
        observational -- this function never rotates anything.

    Raises:
        session_pins.PinStoreError: if the pin store is corrupt. A health
            report that read a broken store as "nothing is pinned" would
            report the entire fleet as healthy-and-unpinned, which is the
            single most misleading answer this function could give.
    """
    runs_dir = Path(runs_dir).expanduser()
    pins = session_pins.read_all(runs_dir)
    reports: list[SessionHealth] = []
    for automation in automations:
        slug = getattr(automation, "slug", "")
        pin = pins.get(slug)
        session_id = pin.session_id if pin else None
        steps = getattr(automation, "steps", []) or []
        if not session_id:
            reports.append(
                SessionHealth(
                    automation=getattr(automation, "name", slug),
                    slug=slug,
                    session_id=None,
                    enabled=bool(getattr(automation, "enabled", False)),
                    transcript_bytes=None,
                    transcript_lines=None,
                    ceiling_hit=None,
                    contract_drifted=False,
                    contract_recorded=False,
                    consecutive_failures=0,
                    detail="no pinned session -- next run creates a fresh one",
                )
            )
            continue

        ws = (pin.session_workspace if pin else None) or workspace
        size, lines = _transcript_stats(
            _transcript_path(session_id, ws, agent_home=agent_home)
        )
        ceiling, consecutive = _scan_recent_runs(
            slug, session_id=session_id, runs_dir=runs_dir
        )
        drifted, recorded = contract_drift(
            session_id=session_id, steps=steps, runs_dir=runs_dir
        )

        if ceiling is not None:
            detail = (
                f"DEAD -- provider refused the prompt ({ceiling.detail}). "
                "This never recovers on its own; the next run auto-rotates."
            )
        elif drifted:
            detail = (
                "DRIFTED -- the automation's steps changed since this session "
                "was created, so it is still carrying the old contract. The "
                "next run auto-rotates."
            )
        elif recorded is None:
            detail = (
                "healthy so far; contract not fingerprinted (session predates "
                "drift detection) -- drift cannot be evaluated for it"
            )
        else:
            detail = "healthy"

        reports.append(
            SessionHealth(
                automation=getattr(automation, "name", slug),
                slug=slug,
                session_id=session_id,
                enabled=bool(getattr(automation, "enabled", False)),
                transcript_bytes=size,
                transcript_lines=lines,
                ceiling_hit=ceiling,
                contract_drifted=drifted,
                contract_recorded=recorded is not None,
                consecutive_failures=consecutive,
                detail=detail,
            )
        )
    return reports


__all__ = [
    "CEILING_RE",
    "CONTRACT_STORE_NAME",
    "LIFECYCLE_ENTRY_KEY",
    "CeilingHit",
    "RunHealth",
    "SessionHealth",
    "claim_provider_rotation",
    "contract_drift",
    "contract_fingerprint",
    "contract_store_path",
    "detect_ceiling_hit",
    "forget_contract",
    "health_for",
    "provider_drift",
    "read_contract",
    "read_lifecycle_anchor_day",
    "read_provider",
    "record_contract",
    "record_lifecycle",
    "run_health",
]
