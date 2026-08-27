"""``drumbeat init`` -- scaffold a fresh consumer workspace.

Today a new user hand-creates four directories and two prompt files before the
engine will start (the README quickstart's manual ``mkdir -p`` recipe). This
module turns that into one command: it writes the section-7.2 workspace layout
-- ``automations/``, ``guidance/``, ``prompts/``, ``runs/`` -- plus generic,
ready-to-edit defaults (the two prompt files the engine ships no built-in copy
of, an empty drumpack list, a placeholder agent config, and one working example
automation).

FAIL LOUD, NEVER CLOBBER: scaffolding is idempotent by refusal, not by
overwrite. If any file this command would write already exists, it writes
NOTHING and names every existing file, so a second ``drumbeat init`` in a
workspace someone has started editing can never silently stamp a template over
real work. ``--force`` is the one, explicit way to overwrite -- and it
overwrites exactly the scaffold files, nothing else in the directory.

Everything written here is generic. The engine owns zero policy (README's "Not
a policy owner"): these files are a starting point the user edits, not defaults
the engine falls back to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The four workspace directories the engine resolves (serve.resolve_workspace
# refuses to start without automations/; the rest are the policy tree a run
# reads). mkdir is exist_ok -- a directory already present is not a conflict,
# only a file the scaffold would overwrite is.
SCAFFOLD_DIRS: tuple[str, ...] = ("automations", "guidance", "prompts", "runs")


# ---- file templates (all generic; the engine owns no policy) ----

# prompts/system.md -- the optional identity/persona turn. Empty body is the
# real, supported default (the runner sends no extra turn when it is empty), so
# the scaffold ships the explanatory frontmatter note and an empty body: on,
# ready to edit, doing nothing until the user writes into it.
_SYSTEM_PROMPT = """\
---
note: >
  This file is yours. If it has content (below the frontmatter), that content
  is sent as the very first turn of an automation's session -- before step 1 --
  using --fresh. It is the place for identity/persona/capabilities framing: who
  the agent is, how it should describe itself, house style -- anything you want
  established once, up front, before the automation's own steps start running.

  EMPTY IS THE DEFAULT AND IS A REAL, SUPPORTED STATE, not a placeholder waiting
  to be filled in. When the body below is empty (or this file is missing), the
  runner sends NO extra turn -- step 1 of the automation is the first turn of
  the session. Nothing is invented on your behalf.

  Add text below the frontmatter to turn it on. Remove it (or empty the body
  again) to turn it back off. Takes effect on the next run -- for a session that
  already exists, on the next run after that session rotates.
---
"""

# prompts/auto-notify.md -- the final "is any of this worth surfacing?" turn for
# notify: auto / urgent-only automations. The engine ships NO built-in copy, so
# this file must be present and non-empty for the example automation below (and
# any notify: auto automation) to run; scaffolding it is what saves the user
# from meeting that abort by hand. Generic: it defers every judgment to the
# guidance the automation loaded, and keeps the one mechanically-matched
# NOTHING_TO_REPORT contract.
_AUTO_NOTIFY_PROMPT = """\
---
note: >
  This file is yours. It is sent to the agent, verbatim, as the final turn of an
  automation whose file sets notify: auto or notify: urgent-only -- after all of
  that automation's own steps have run, in the same conversation, with all of
  that conversation's context still in view.

  Editing this file changes what gets asked on the very next run. There is no
  built-in copy of this prompt anywhere in the engine -- if this file is missing
  or empty, notify: auto automations fail loudly rather than silently using some
  other text.

  The one thing you should not remove: the instruction to reply with the exact
  word NOTHING_TO_REPORT (and nothing else) when there is genuinely nothing to
  surface. The runner matches on that exact string to decide whether to emit a
  delivery intent. Everything else below is yours to rewrite.
---
Consider everything that has happened in this conversation so far, including any
guidance documents you loaded and followed as part of the steps above.

Decide whether there is something worth proactively telling the user about,
using the guidance's own rules for what counts as worth surfacing, how long an
item stays worth surfacing, and how often to surface it. Do not invent a freshness
or novelty test of your own, and do not invent a suppression rule of your own --
the loaded guidance decides both.

If, applying that guidance, there is something worth surfacing: reply with a
concise, ready-to-send notification message for the user.

If, applying that guidance, there is genuinely nothing to surface: reply with
exactly the single word `NOTHING_TO_REPORT` and nothing else -- no punctuation,
no explanation, no leading or trailing whitespace.
"""

# drumpacks.txt -- the ordered list of pack directories this workspace declares
# (one path per line, blank lines and # comments ignored). Empty by default: a
# fresh workspace declares no packs, and an automation that requires a pack tool
# aborts loudly at the requirements gate until one is listed here.
_DRUMPACKS_TXT = """\
# drumpacks.txt -- one drumpack directory per line (relative to this workspace,
# or absolute). Blank lines and # comments are ignored.
#
# A drumpack is a directory with a drumpack.md card and a bin/ of executables;
# listing it here puts its bin/ on the turn PATH and injects its card into every
# automation that requires one of its tools. No packs are declared yet -- add a
# line pointing at a drumpack directory to bring tools to your automations.
"""

# agent-config.yaml -- placeholder. The agent-config subsystem (drumbeat
# resolving the ONE host config the engine is handed for every turn) lands in a
# later release; this is a commented-out default provider block so the file
# exists in the layout without changing any behavior. Nothing reads it yet.
_AGENT_CONFIG_YAML = """\
# agent-config.yaml -- host config drumbeat will hand amplifier-agent for every
# turn. This is a PLACEHOLDER: the agent-config subsystem arrives in a later
# release, and until then nothing reads this file. Leave it as-is for now.
#
# When it is wired up, uncomment and fill in a provider block to pin the model
# your automations run with. Until then, amplifier-agent uses its own configured
# provider and keys from the environment the engine starts in.
#
# provider:
#   name: anthropic
#   config:
#     model: <your-model-id>
"""

# automations/repo-status-digest.md -- one working, generic example automation.
# notify: auto (so it exercises prompts/auto-notify.md above), requires nothing
# (uses the agent's own shell -- no drumpack needed), and ships DISABLED like
# every exemplar: flip enabled to true once a turn is verified to run.
_EXAMPLE_AUTOMATION_FILENAME = "automations/repo-status-digest.md"
_EXAMPLE_AUTOMATION = """\
---
automation:
  name: Repo Status Digest
  # Ships disabled, like every exemplar. Flip to true once you have verified a
  # turn actually runs (see the README quickstart's "run it now" step).
  enabled: false
  trigger:
    type: schedule
    # `daily at HH:MM` is a 24-hour clock in the engine's local timezone,
    # recomputed on every evaluation -- a daylight-saving shift does not
    # silently move it.
    expression: daily at 09:00
  # `auto`: after the steps run, the agent is asked -- via prompts/auto-notify.md
  # -- whether anything here is worth telling you about. Nothing is delivered
  # unless it decides there is.
  notify: auto
  # This example needs no tools; it uses the agent's own shell. Add tool names
  # here (and declare their drumpack in drumpacks.txt) when an automation needs
  # one.
  requires: []
  # Steps are structured frontmatter data (contracts/automation-file.v1.md): an
  # ordered list, each with an `id` (identity in run records), a `prompt` (the
  # whole behavior), and an optional `label`. The body below is for humans and
  # is never parsed for execution.
  steps:
    - id: confirm-fire-time
      label: Confirm the fire time
      prompt: |-
        Report the current wall-clock time and confirm this run fired at or
        after the scheduled time (`daily at 09:00`). Say plainly whether the
        fire time matched. A scheduled job that quietly drifts is worth
        catching on its own.
    - id: gather-digest
      label: Gather the git status digest
      prompt: |-
        For the git repository in the current working directory, gather a short
        status digest as your reply to THIS step:

        - **Branch** -- the current branch, and whether it is ahead of or behind
          its upstream.
        - **Working tree** -- how many files are modified, staged, or untracked;
          name the most recent few rather than listing everything.
        - **Recent commits** -- the last handful of commits (short hash +
          subject) so the digest shows what just landed.

        Use read-only git commands only. Do not commit, push, pull, or change
        any file -- this automation reports, it does not act.
    - id: emit-digest
      label: Emit the complete digest
      prompt: |-
        End this step's reply with the complete digest: restate the sections
        above, unchanged, as one message. If a section has nothing in it, say so
        plainly and say what you checked -- never skip a section silently. This
        combined reply IS the digest, and this is the LAST step.
---

A working, generic example automation: a daily read-only git status digest that
needs no tools. It ships disabled -- flip `enabled` to true once you have
verified a turn actually runs. Every step lives in the frontmatter `steps:` list
above; this body is a human-facing description only.
"""

# Ordered so refusal and success output read top-to-bottom the same way every
# time. A dict preserves insertion order.
SCAFFOLD_FILES: dict[str, str] = {
    "prompts/system.md": _SYSTEM_PROMPT,
    "prompts/auto-notify.md": _AUTO_NOTIFY_PROMPT,
    "drumpacks.txt": _DRUMPACKS_TXT,
    "agent-config.yaml": _AGENT_CONFIG_YAML,
    _EXAMPLE_AUTOMATION_FILENAME: _EXAMPLE_AUTOMATION,
}


class InitError(Exception):
    """Scaffolding refused because it would overwrite existing files.

    Carries the resolved ``target`` and the workspace-relative paths of the
    files that already exist, so the CLI can name exactly what is in the way
    (and the caller can re-run with ``force=True`` to overwrite precisely
    those).
    """

    def __init__(self, target: Path, existing: list[str]) -> None:
        self.target = target
        self.existing = existing
        listing = "\n".join(f"  {rel}" for rel in existing)
        super().__init__(
            f"refusing to overwrite existing files in {target} -- drumbeat init "
            "is idempotent and will not clobber your work:\n"
            f"{listing}\n"
            "Re-run with --force to overwrite exactly these files, or delete "
            "them first."
        )


@dataclass(frozen=True)
class ScaffoldResult:
    """What ``scaffold`` created, for the CLI to render.

    ``created_dirs`` are directories that did not exist before this call
    (already-present ones are not conflicts and are simply omitted).
    ``created_files`` were newly written; ``overwritten_files`` existed and were
    replaced (only possible with ``force=True``). All paths are
    workspace-relative and in ``SCAFFOLD_DIRS`` / ``SCAFFOLD_FILES`` order.
    """

    target: Path
    created_dirs: list[str]
    created_files: list[str]
    overwritten_files: list[str]


def existing_scaffold_files(target: Path) -> list[str]:
    """Workspace-relative scaffold files that already exist under ``target``.

    In ``SCAFFOLD_FILES`` order. This is the exact set ``scaffold`` refuses on
    (without ``force``) -- separated out so a caller can preview the conflict.
    """
    target = Path(target).expanduser()
    return [rel for rel in SCAFFOLD_FILES if (target / rel).exists()]


def scaffold(target: Path, *, force: bool = False) -> ScaffoldResult:
    """Create the workspace layout and default files under ``target``.

    Creates ``target`` itself if missing. Directories are created idempotently
    (an existing one is fine). Files are NEVER overwritten unless ``force`` is
    true: if any scaffold file already exists and ``force`` is false, this
    raises :class:`InitError` having written nothing at all -- the check runs
    before any write, so a refusal leaves the directory exactly as it was.

    Args:
        target: workspace directory to scaffold (created if it does not exist).
        force: overwrite the scaffold files that already exist.

    Returns:
        A :class:`ScaffoldResult` describing what was created/overwritten.

    Raises:
        InitError: a scaffold file exists and ``force`` is false.
    """
    target = Path(target).expanduser().resolve()

    existing = existing_scaffold_files(target)
    if existing and not force:
        # Refuse BEFORE creating anything -- not even the directories -- so a
        # refused init is a total no-op on disk.
        raise InitError(target, existing)

    target.mkdir(parents=True, exist_ok=True)

    created_dirs: list[str] = []
    for rel in SCAFFOLD_DIRS:
        d = target / rel
        if not d.is_dir():
            created_dirs.append(rel)
        d.mkdir(parents=True, exist_ok=True)

    created_files: list[str] = []
    overwritten_files: list[str] = []
    for rel, content in SCAFFOLD_FILES.items():
        path = target / rel
        already = path.exists()
        # Parent (e.g. prompts/, automations/) is guaranteed by the dir loop
        # above for every template, but mkdir here keeps this robust if the
        # template set ever grows a new subdirectory.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if already:
            overwritten_files.append(rel)
        else:
            created_files.append(rel)

    return ScaffoldResult(
        target=target,
        created_dirs=created_dirs,
        created_files=created_files,
        overwritten_files=overwritten_files,
    )


__all__ = [
    "SCAFFOLD_DIRS",
    "SCAFFOLD_FILES",
    "InitError",
    "ScaffoldResult",
    "existing_scaffold_files",
    "scaffold",
]
