"""Load user-editable prompt text from markdown files.

Prompts are the mechanism-not-policy principle applied to our own turns: the
*text* the runner sends the agent is not a policy decision for developers to
make and bake into code — it is data the user owns, in plain markdown, next
to the automations and guidance they already edit. See ``prompts/*.md`` for
the actual prompt content; this module only knows how to load it.

A prompt file is plain markdown body text, with an optional leading YAML
frontmatter block (delimited by ``---`` lines) for metadata. The frontmatter,
if present, is parsed only far enough to validate it and then discarded —
the loader returns body text only.

FAIL LOUD, NO FALLBACKS: this loader never substitutes built-in prompt text
for a missing or malformed file. That was the exact defect this module
exists to fix — a hardcoded prompt silently overriding the user's own
written policy. Two states are treated as "no-op" (this prompt contributes
nothing, proceed as if it weren't configured): the file doesn't exist, or
its body is empty/whitespace-only after stripping frontmatter. Every other
problem (unparsable frontmatter, frontmatter that isn't a mapping) raises
``PromptError`` naming the file. Callers that require a prompt (rather than
treating it as optional) use ``require_prompt``, which also raises by name
when the prompt is a no-op.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PROMPTS_DIR = Path("prompts")

# Every prompt file the consumer CLI's `prompts` verb knows to report on. Not a plugin
# registry — just the fixed, small set this codebase currently sends turns
# from. Adding a new prompt file means adding its name here too.
KNOWN_PROMPTS: tuple[str, ...] = ("auto-notify", "system")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)


class PromptError(Exception):
    """Raised when a prompt file is malformed, or missing when required.

    Always carries the file path so a broken or missing prompt can be traced
    back to its source without guessing which file the error came from.
    """

    def __init__(self, path: Path, problem: str) -> None:
        self.path = path
        self.problem = problem
        super().__init__(f"{path}: {problem}")


@dataclass(frozen=True)
class PromptStatus:
    """Display-only status of one known prompt file, for the consumer CLI's
    `prompts` verb."""

    name: str
    path: Path
    status: str  # "present" | "empty" | "missing" | "malformed: <problem>"


def _strip_frontmatter(path: Path, text: str) -> str:
    """Return the markdown body, validating and discarding a leading frontmatter block.

    If the text doesn't start with a ``---`` delimited block, it is returned
    unchanged (frontmatter is optional — a prompt file may be pure body text).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text

    raw_frontmatter, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as exc:
        raise PromptError(path, f"invalid YAML frontmatter: {exc}") from exc
    if data is not None and not isinstance(data, dict):
        raise PromptError(path, "frontmatter must be a YAML mapping")
    return body


def load_prompt(name: str, prompts_dir: Path) -> str | None:
    """Return the body text of ``prompts_dir/{name}.md``, or None if it's a no-op.

    A prompt is a no-op in exactly two cases: the file does not exist, or its
    body (after stripping optional YAML frontmatter) is empty or
    whitespace-only. In both cases the caller should behave exactly as if
    this prompt were never configured — no injected text, no extra turn.

    Any other problem (unparsable frontmatter, frontmatter that isn't a
    mapping) raises ``PromptError`` naming the file. This function never
    substitutes a built-in default for a bad file.

    Args:
        name: prompt file stem, e.g. "auto-notify" (without ".md").
        prompts_dir: directory containing prompt markdown files.

    Returns:
        The stripped body text, or None if the prompt is a no-op.

    Raises:
        PromptError: if the file exists but its frontmatter is malformed.
    """
    prompts_dir = Path(prompts_dir).expanduser()
    path = prompts_dir / f"{name}.md"

    if not path.is_file():
        return None

    text = path.read_text(encoding="utf-8")
    body = _strip_frontmatter(path, text)

    stripped = body.strip()
    if not stripped:
        return None

    return stripped


def require_prompt(name: str, prompts_dir: Path, *, reason: str) -> str:
    """Like ``load_prompt``, but raise ``PromptError`` if the prompt is a no-op.

    Use this for prompts whose text is required for an automation to run
    correctly (e.g. the auto-notify check when ``notify: auto``). The
    prompt file is the single source of truth — there is no built-in
    fallback text to silently substitute when it's missing or emptied out.

    Args:
        name: prompt file stem, e.g. "auto-notify".
        prompts_dir: directory containing prompt markdown files.
        reason: human-readable explanation of why this prompt is required,
            included in the error so the user knows what triggered it.

    Returns:
        The stripped, non-empty body text.

    Raises:
        PromptError: if the file is missing, empty, or malformed.
    """
    prompt = load_prompt(name, prompts_dir)
    if prompt is None:
        prompts_dir = Path(prompts_dir).expanduser()
        path = prompts_dir / f"{name}.md"
        raise PromptError(
            path,
            f"prompt file is missing or empty, but is required: {reason}. "
            "This file is authoritative — there is no built-in fallback text.",
        )
    return prompt


def list_prompts(prompts_dir: Path) -> list[PromptStatus]:
    """Report the status of every known prompt file, for display purposes.

    Never raises: a malformed prompt is reported as ``"malformed: ..."``
    rather than propagating, since this is used purely for the consumer CLI's `
    prompts` listing command.
    """
    prompts_dir = Path(prompts_dir).expanduser()
    statuses: list[PromptStatus] = []
    for name in KNOWN_PROMPTS:
        path = prompts_dir / f"{name}.md"
        if not path.is_file():
            statuses.append(PromptStatus(name=name, path=path, status="missing"))
            continue
        try:
            body = load_prompt(name, prompts_dir)
        except PromptError as exc:
            statuses.append(
                PromptStatus(name=name, path=path, status=f"malformed: {exc.problem}")
            )
            continue
        statuses.append(
            PromptStatus(name=name, path=path, status="present" if body else "empty")
        )
    return statuses


__all__ = [
    "DEFAULT_PROMPTS_DIR",
    "KNOWN_PROMPTS",
    "PromptError",
    "PromptStatus",
    "list_prompts",
    "load_prompt",
    "require_prompt",
]
