"""Turn-context injectors: owner-declared commands whose stdout is prepended
to a turn as a labeled preamble block.

THE PROBLEM this closes. Sometimes the brain needs standing context a turn's
own text does not carry -- working state, a recall of durable facts, a summary
of what is open right now. That context lives OUTSIDE drumbeat, behind a
command only the owner knows how to run. drumbeat should be able to run that
command and fold its output into the turn, without drumbeat knowing anything
about what the command is or what it returns.

THE DESIGN CONSTRAINT this respects. This is pure MECHANISM. drumbeat names no
service, embeds no vendor, and hardcodes no command: the owner declares, in a
workspace file they read and edit, a list of ``{argv, label, apply_to}``
injectors. For each turn, every injector whose ``apply_to`` includes the turn's
profile is run; its stdout becomes a labeled preamble block prepended to the
turn text (config order top-to-bottom, the person's own message always last and
most salient). A profile with no configured injector -- and a turn that names
no profile at all -- runs unchanged, with zero subprocess I/O; absence is a
legal, quiet, unchanged-behavior state.

The ``apply_to`` names are OPEN vocabulary: they are profile names the owner
picked in ``agent-config.yaml`` (``profiles:``), the same names a turn request
carries as ``profile``. drumbeat imposes no fixed set of interaction modes;
``apply_to`` is matched as plain strings against the turn's profile name.

THE CONTRACT (mirrors the automation-level ``inject:`` mechanism). An injector
runs with the turn environment (the same constructed PATH the agent's own tool
calls get, so an injector tool resolves exactly like a pack tool), and its
result is classified in a FIXED order:

- the binary cannot be spawned    -> loud refusal (``InjectorError``)
- it times out                    -> loud refusal
- it exits non-zero               -> loud refusal (stderr tail named)
- stdout is exactly ``INJECT_IDLE`` -> the injector opts out of THIS turn, no block
- stdout is empty (not the sentinel) -> loud refusal (silence is never a
  contract value -- a crashed pipe and an idle source must not share an
  observable)
- anything else                   -> the stdout becomes this turn's block

Fail-loud on every failure is deliberate: a configured injector that silently
contributes nothing is a turn that lies about the context the owner declared it
needs. The turn is recorded failed with the remedy, never run with a silently
smaller context.

The policy file lives in the workspace root (beside ``drumpacks.txt``), is re-read
every turn (no cache -- editing it takes effect on the next turn, same
discipline as ``packs.py`` and every markdown in this system), and every way it
can be malformed is a loud refusal that names the file and the problem.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# The owner-editable policy file, read from the workspace root. Absent is legal
# (no injectors run); present-but-malformed is a loud refusal.
POLICY_FILENAME = "injectors.yaml"

# The whole-stdout sentinel an injector prints when it has nothing to add this
# turn. Byte-exact against the stripped stdout -- an injector that prints this
# and then keeps talking is NOT idle. Shared verbatim with the automation-level
# ``inject:`` tool contract so a single tool can honor both.
IDLE_SENTINEL = "INJECT_IDLE"

# Short timeout: an injector is a local context read, generous at 60s for any
# honest read and small against the minutes a hung command would otherwise
# silently cost every turn.
_INJECTOR_TIMEOUT_SECONDS = 60.0


class InjectorPolicyError(Exception):
    """The injectors policy file could not be loaded or is malformed.

    Always names ``POLICY_FILENAME`` and the specific problem -- a policy the
    owner cannot see the shape of is a policy that lies about what runs.
    """


class InjectorError(Exception):
    """A configured injector could not be run to a usable result this turn.

    Raised (never swallowed) on a missing binary, a timeout, a non-zero exit,
    or empty stdout. Names the injector's label and argv[0] so the failure is
    attributable.
    """


@dataclass(frozen=True)
class Injector:
    """One declared injector.

    ``argv`` is executed with the turn environment; its stdout becomes a
    labeled preamble block. ``label`` heads that block (and names the injector
    in any refusal). ``apply_to`` is the set of profile names this injector runs
    for -- a turn whose profile is not in the set never runs it.
    """

    argv: tuple[str, ...]
    label: str
    apply_to: frozenset[str]


@dataclass(frozen=True)
class InjectorOutcome:
    """Classified result of one injector execution.

    Exactly one of two live shapes on a successful run: ``idle`` True (the
    injector opted out of this turn), or ``text`` set (the rendered block to
    prepend). Every failure raises ``InjectorError`` instead of returning.
    """

    label: str
    text: str | None = None
    idle: bool = False


def render_block(label: str, stdout: str) -> str:
    """Render one injector's stdout as a labeled preamble block.

    The label is fenced so the brain can tell where one block ends and the
    next (or the person's own message) begins. Surrounding whitespace on the
    body is trimmed; the interior is left verbatim.
    """
    return f"--- {label} ---\n{stdout.strip()}"


def _policy_path(workspace: Path) -> Path:
    return Path(workspace).expanduser() / POLICY_FILENAME


def load_policy(workspace: Path) -> tuple[Injector, ...]:
    """Read and fully validate the workspace's injectors policy.

    Returns the declared injectors in file order. A missing file is legal and
    returns ``()`` (no injectors run). Any structural fault is an
    ``InjectorPolicyError`` naming the file and the problem, never a silent
    best-effort guess.
    """
    path = _policy_path(workspace)
    if not path.exists():
        return ()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InjectorPolicyError(
            f"{POLICY_FILENAME}: could not read/parse: {exc}"
        ) from exc

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise InjectorPolicyError(
            f"{POLICY_FILENAME}: top level must be a mapping with an "
            f"'injectors' key, got {type(raw).__name__}"
        )

    entries = raw.get("injectors")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise InjectorPolicyError(
            f"{POLICY_FILENAME}: 'injectors' must be a list of mappings, "
            f"got {type(entries).__name__}"
        )

    injectors: list[Injector] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InjectorPolicyError(
                f"{POLICY_FILENAME}: injectors[{i}] must be a mapping with "
                "argv, label, and apply_to"
            )
        unknown = set(entry) - {"argv", "label", "apply_to"}
        if unknown:
            raise InjectorPolicyError(
                f"{POLICY_FILENAME}: injectors[{i}] has unknown key(s) "
                f"{sorted(unknown)}; only argv, label, and apply_to are allowed"
            )

        argv = entry.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a.strip() for a in argv)
        ):
            raise InjectorPolicyError(
                f"{POLICY_FILENAME}: injectors[{i}].argv is required and must be "
                "a non-empty list of non-empty strings"
            )

        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            raise InjectorPolicyError(
                f"{POLICY_FILENAME}: injectors[{i}].label is required and must be "
                "a non-empty string -- an unlabeled block would be unattributable"
            )

        apply_to = entry.get("apply_to")
        if (
            not isinstance(apply_to, list)
            or not apply_to
            or not all(isinstance(m, str) and m.strip() for m in apply_to)
        ):
            raise InjectorPolicyError(
                f"{POLICY_FILENAME}: injectors[{i}].apply_to is required and must "
                "be a non-empty list of non-empty profile-name strings (the names "
                "declared in agent-config.yaml 'profiles:', matched against a "
                "turn's requested profile)"
            )

        injectors.append(
            Injector(
                argv=tuple(argv),
                label=label.strip(),
                apply_to=frozenset(apply_to),
            )
        )

    return tuple(injectors)


def run_injector(
    injector: Injector, *, cwd: Path, env: Mapping[str, str]
) -> InjectorOutcome:
    """Execute one injector and classify it (timeout -> exit -> stdout).

    Runs ``injector.argv`` (no shell) with the supplied turn ``env`` from
    ``cwd``. Returns an idle or a rendered-block outcome; raises
    ``InjectorError`` on any failure. See the module docstring for the fixed
    classification order.
    """
    argv = list(injector.argv)
    label = injector.label
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_INJECTOR_TIMEOUT_SECONDS,
            cwd=str(cwd),
            env=dict(env),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InjectorError(
            f"injector {label!r} ({argv[0]}) timed out after "
            f"{_INJECTOR_TIMEOUT_SECONDS:.0f}s -- refusing to run the turn "
            "without the context it declared it needs"
        ) from exc
    except OSError as exc:
        raise InjectorError(
            f"injector {label!r} ({argv[0]}) could not be executed: {exc} "
            "-- refusing to run the turn without its declared context"
        ) from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-500:]
        raise InjectorError(
            f"injector {label!r} ({argv[0]}) exited {completed.returncode} "
            f"-- refusing to run the turn without its declared context. "
            f"stderr: {stderr_tail or '(empty)'}"
        )

    stdout = completed.stdout or ""
    stripped = stdout.strip()
    if stripped == IDLE_SENTINEL:
        return InjectorOutcome(label=label, idle=True)
    if not stripped:
        raise InjectorError(
            f"injector {label!r} ({argv[0]}) exited 0 with EMPTY stdout -- an "
            f"injector with nothing to add must say {IDLE_SENTINEL}; silence is "
            "never a contract value (a crashed pipe and an idle source must not "
            "share an observable)"
        )
    return InjectorOutcome(label=label, text=render_block(label, stdout))


def collect_preamble(
    workspace: Path,
    profile: str | None,
    *,
    env: Mapping[str, str],
) -> tuple[str, ...]:
    """Run every injector applying to ``profile`` and return their blocks.

    Blocks are returned in policy (file) order, ready to prepend to the turn
    text top-to-bottom. A ``None`` profile, a missing policy file, or no
    matching injector returns ``()`` with zero subprocess I/O. An idle injector
    contributes no block. Any injector failure raises ``InjectorError`` (the
    caller records the turn failed) -- never a silently smaller context.
    """
    if profile is None:
        return ()
    workspace = Path(workspace).expanduser()
    blocks: list[str] = []
    for injector in load_policy(workspace):
        if profile not in injector.apply_to:
            continue
        outcome = run_injector(injector, cwd=workspace, env=env)
        if outcome.idle:
            continue
        assert outcome.text is not None  # non-idle outcomes always carry text
        blocks.append(outcome.text)
    return tuple(blocks)
