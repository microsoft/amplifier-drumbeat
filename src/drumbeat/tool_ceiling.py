"""Bound a tool result BEFORE it enters the conversation.

contracts/agent-binding.v1.md section 10. A tool result is not a transient
display artifact: it is appended to the session transcript and **re-sent, in
full, on every subsequent turn**. One oversized result therefore poisons the
session permanently, and no later turn can undo it.

Measured on the originating deployment (2026-09-07): a single MCP tool result
of 46,464,072 bytes entered ``teams-check``'s pinned session, after which the
provider refused every later request (``string_above_max_length:
input[37].output > 10,485,760``). The identical run failed again four hours
later on the same pinned session. Manual ``drumbeat rotate-session`` was the
only remedy.

The agent library does not bound this for us, and that is deliberate on its
side: measured on ``amplifier-agent 1.0.0a1``, the built-in ``bash`` tool's
``CapturedBash`` subclass overrides ``_truncate_output`` to return the output
UNCHANGED, and the library never compacts.

WHAT THIS MODULE IS, AND IS NOT
-------------------------------
This is the pure mechanism: run a command, bound a string, write the overflow
somewhere a human can still read it. It imports nothing from the agent library
-- ``drumbeat.agent_worker`` is the only module allowed to do that (section 1)
-- so the ceiling can be tested, and reasoned about, with no library present.

It is NOT a claim that every tool result is bounded. It reaches exactly the
results drumbeat's own process produces (its ``caller`` tools). Built-in and
MCP results are executed inside the library and cannot be reached at all:
a caller tool named ``bash`` is REFUSED at construction
(``AgentError('invalid_input', 'Duplicate tool name: bash.')``), ``AgentOptions``
is a closed list with no tool-filtering field, and an approvals ``deny`` is
terminal. That boundary is recorded in the contract's "Known gap" and proven by
``tests/test_v1_tool_shadowing.py`` against the real library.

FAIL LOUD, BUT NEVER FAIL OPEN
------------------------------
An unwritable run directory means the full output cannot be preserved. It must
NOT mean the result is passed through unbounded: the note says the file is
unavailable and why, and the truncation still happens. Losing the overflow is
an inconvenience; letting 46 MB reach the provider is the incident.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The engine default, in bytes. 256 KiB.
#
# WHY THIS NUMBER: it is calibrated off what a turn can afford to CARRY FOREVER,
# not off what a single result costs once. At roughly 4 bytes/token this is
# ~65k tokens -- already a third of a 200k context window for ONE result, and
# well beyond any legitimate tool answer observed on the originating deployment
# (every rollup automation there stayed under a megabyte across a whole run).
# It is generous enough that no honest tool call trips it, and small enough
# that even a handful of maxed-out results cannot reach the 150,000-token
# rotation gate in one turn.
#
# WHY NOT SMALLER: truncation costs the model information it may need, and a
# ceiling that fires on ordinary output would train an automation author to
# raise it fleet-wide -- which is the same as not having one.
DEFAULT_CEILING_BYTES = 262_144

# The deployment-level override. Same env-var seam as
# ``$DRUMBEAT_SESSION_ROTATE_TOKENS``; the per-automation key
# (``agent_config.tool_result_ceiling_bytes``) wins over it.
ENV_CEILING_VAR = "DRUMBEAT_TOOL_RESULT_CEILING_BYTES"

# Where the full, untruncated output lands, under the run directory.
TOOL_OUTPUT_DIRNAME = "tool-output"

# The name of the caller tool drumbeat registers. NOT ``bash``: that name is
# reserved by the library's built-in and a duplicate is refused at
# construction (see the module docstring).
TOOL_NAME = "run_command"

# The library's own bounds for the built-in bash tool's ``timeout``, mirrored
# so ``run_command`` behaves identically where it can.
DEFAULT_TIMEOUT_S = 30
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 120


class CeilingConfigError(ValueError):
    """A configured ceiling that cannot be honored. Refused, never guessed at."""


@dataclass(frozen=True)
class Bounded:
    """One tool result after the ceiling was applied.

    ``text`` is what enters the conversation. ``truncated`` says whether
    anything was dropped, ``total_bytes``/``kept_bytes`` say how much, and
    ``full_output_path`` is where the whole thing was written (``None`` when
    nothing was truncated, or when the write failed -- ``write_error`` then
    says why).
    """

    text: str
    truncated: bool
    total_bytes: int
    kept_bytes: int
    full_output_path: Path | None = None
    write_error: str | None = None


def coerce_ceiling(value: object, *, source: str) -> int:
    """One declared ceiling, validated. Refuses anything that is not positive.

    ``0`` is REFUSED rather than read as "unlimited". There is no unlimited:
    an unbounded result is the exact defect this whole module exists to bound,
    so a config that asks for one must fail at load rather than at 46 MB.

    Args:
        value: the raw declared value (from frontmatter, or already an int).
        source: what to name in the refusal, e.g.
            ``"agent config: tool_result_ceiling_bytes"``.

    Raises:
        CeilingConfigError: naming the value and the remedy.

    Example:
        >>> coerce_ceiling(1024, source="x")
        1024
        >>> coerce_ceiling(0, source="x")
        Traceback (most recent call last):
        drumbeat.tool_ceiling.CeilingConfigError: x must be a positive integer...
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise CeilingConfigError(
            f"{source} must be a positive integer number of bytes, got "
            f"{value!r} ({type(value).__name__}) -- there is no 'unlimited', "
            "because an unbounded tool result is the failure this key bounds"
        )
    if value <= 0:
        raise CeilingConfigError(
            f"{source} must be a positive integer number of bytes, got "
            f"{value!r} -- 0 does not mean 'unlimited', it means a ceiling "
            "nothing can satisfy; remove the key to use the engine default "
            f"({DEFAULT_CEILING_BYTES})"
        )
    return value


def ceiling_bytes(override: int | None = None) -> int:
    """The ceiling in force: per-automation override, else env, else default.

    FAIL LOUD: an unusable ``$DRUMBEAT_TOOL_RESULT_CEILING_BYTES`` is reported
    on stderr and the documented default is used -- never silently honored as
    "no ceiling", which is the one outcome this module must never produce.
    The per-automation ``override`` is already validated at config load
    (``coerce_ceiling``), so it is trusted here.
    """
    if override is not None:
        return override
    raw = os.environ.get(ENV_CEILING_VAR)
    if not raw:
        return DEFAULT_CEILING_BYTES
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value > 0:
        return value
    print(
        f"[drumbeat] {ENV_CEILING_VAR}={raw!r} is not a positive integer -- "
        f"ignoring it and using the default {DEFAULT_CEILING_BYTES} bytes.",
        file=sys.stderr,
    )
    return DEFAULT_CEILING_BYTES


def _truncate_utf8(text: str, limit_bytes: int) -> tuple[str, int]:
    """Keep the longest UTF-8 prefix of ``text`` that fits in ``limit_bytes``.

    Cuts on a CHARACTER boundary: a partial multi-byte sequence at the cut is
    dropped rather than emitted broken, so the model never receives mojibake it
    then reasons from.

    Returns:
        ``(kept_text, total_bytes)``.
    """
    raw = text.encode("utf-8")
    total = len(raw)
    if total <= limit_bytes:
        return text, total
    # ``errors="ignore"`` drops exactly the trailing partial sequence.
    return raw[:limit_bytes].decode("utf-8", errors="ignore"), total


def full_output_path(output_dir: Path | str, call_id: str) -> Path:
    """Where ``call_id``'s untruncated output belongs under a run directory.

    ``call_id`` is the library's own correlation id (``ToolContext.call_id``),
    so the note in the transcript and the file on disk name the same call.
    Sanitized to a filename: an id carrying a separator could otherwise escape
    the run directory.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in call_id) or "call"
    return Path(output_dir).expanduser() / TOOL_OUTPUT_DIRNAME / f"{safe}.txt"


def apply_ceiling(
    text: str,
    *,
    limit_bytes: int,
    call_id: str,
    output_dir: Path | str | None,
) -> Bounded:
    """Bound one tool result, writing the overflow beside the run.

    At or below the ceiling the result is returned BYTE-IDENTICAL: no note, no
    file, nothing written. Above it, the first ``limit_bytes`` bytes are kept
    and the mandatory note is appended:

        ``\\n[drumbeat: output truncated -- kept N of M bytes; full output: <path>]``

    A truncated result that did not say so would be a lie the model then
    reasons from, so the note is not optional and not configurable.

    ``output_dir`` may be ``None`` (no run directory available -- an
    interactive turn); the note then reports the full output as unavailable
    and says why. The truncation happens either way: see the module docstring's
    "never fail open".
    """
    kept_text, total = _truncate_utf8(text, limit_bytes)
    if total <= limit_bytes:
        return Bounded(text=text, truncated=False, total_bytes=total, kept_bytes=total)

    kept = len(kept_text.encode("utf-8"))
    path: Path | None = None
    write_error: str | None = None
    if output_dir is None:
        write_error = "no run directory for this turn"
    else:
        path = full_output_path(output_dir, call_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            write_error = str(exc)
            path = None
            print(
                f"[drumbeat] tool result for call {call_id!r} was truncated at "
                f"{limit_bytes} bytes, but the full {total}-byte output could "
                f"NOT be written: {exc}. The overflow is lost; the truncation "
                "still happened.",
                file=sys.stderr,
            )

    where = str(path) if path is not None else f"unavailable -- {write_error}"
    note = (
        f"\n[drumbeat: output truncated -- kept {kept} of {total} bytes; "
        f"full output: {where}]"
    )
    return Bounded(
        text=kept_text + note,
        truncated=True,
        total_bytes=total,
        kept_bytes=kept,
        full_output_path=path,
        write_error=write_error,
    )


def coerce_timeout(value: object) -> int:
    """One ``run_command`` timeout, clamped to the library's own 1-120s bounds.

    The built-in ``bash`` tool accepts 1-120 seconds and defaults to 30
    (``docs/concepts/tools.md``). ``run_command`` mirrors that exactly, so a
    model that learned one has not learned a different thing about the other.
    A value outside the range is clamped rather than refused: this is a model's
    argument, not an operator's config, and failing a turn over it would be a
    false failure with no defect behind it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_TIMEOUT_S
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, value))


def run_command(
    command: str,
    *,
    cwd: Path | str,
    env: dict[str, str] | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> tuple[str, int | None]:
    """Run one shell command exactly as the built-in ``bash`` tool would.

    ``/bin/bash -c <command>``, in its own process group, under the turn's own
    working directory and environment (which on drumbeat's worker is already
    the pack-augmented ``PATH``). Returns the rendered result text and the exit
    code (``None`` on timeout).

    A timeout kills the whole process tree and reports what was captured
    before it -- the same shape the library reports, and for the same reason:
    an effect may already have landed, so the output is evidence, not noise.
    """
    try:
        completed = subprocess.run(
            ["/bin/bash", "-c", command],
            # A non-zero exit is DATA here, not an exception: the model is
            # entitled to see the command failed and why (the built-in tool
            # reports the same shape).
            check=False,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return (
            _render(out, err, None, timed_out_after=timeout_s),
            None,
        )
    return _render(completed.stdout, completed.stderr, completed.returncode), completed.returncode


def _render(
    stdout: str, stderr: str, returncode: int | None, *, timed_out_after: int | None = None
) -> str:
    """One command's outcome as the plain text the model reads.

    Deliberately plain and labelled rather than JSON: this string is what the
    ceiling then bounds, and a truncated JSON document is unparseable garbage
    while a truncated labelled transcript still reads.
    """
    parts: list[str] = []
    if timed_out_after is not None:
        parts.append(
            f"[timed out after {timed_out_after}s -- the process tree was killed; "
            "any effect it had may already have happened]"
        )
    else:
        parts.append(f"exit code: {returncode}")
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)


# The preference drumbeat states in ``AgentOptions.instructions``. ADVISORY and
# known to be advisory: the built-in ``bash`` cannot be removed or shadowed
# (see the module docstring), so this is steering, not enforcement. It is
# written as a reason rather than a rule because a model that understands WHY
# follows it on the calls that actually matter -- the ones with big output.
TOOL_PREFERENCE_INSTRUCTIONS = (
    f"Shell commands: prefer the `{TOOL_NAME}` tool over the built-in `bash` tool. "
    f"`{TOOL_NAME}` takes the same arguments and bounds its result, writing any "
    "overflow to a file it names in the reply. The built-in `bash` tool does not "
    "bound its result, and an oversized result is re-sent on every later turn in "
    "this conversation until the provider refuses the request outright."
)


__all__ = [
    "DEFAULT_CEILING_BYTES",
    "DEFAULT_TIMEOUT_S",
    "ENV_CEILING_VAR",
    "MAX_TIMEOUT_S",
    "MIN_TIMEOUT_S",
    "TOOL_NAME",
    "TOOL_OUTPUT_DIRNAME",
    "TOOL_PREFERENCE_INSTRUCTIONS",
    "Bounded",
    "CeilingConfigError",
    "apply_ceiling",
    "ceiling_bytes",
    "coerce_ceiling",
    "coerce_timeout",
    "full_output_path",
    "run_command",
]
