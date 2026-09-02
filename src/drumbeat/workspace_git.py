"""Read-only git facts about the workspace, for ``drumbeat doctor``.

The engine does not know what any particular consumer's policy-packaging
concept is, and never will. What it knows is narrower and useful to every
consumer:
**a workspace may be a git checkout, and if it is, whether that checkout is
drifting away from its archive is an operational fact worth reporting.**

Three numbers, chosen because each names a different way the archive stops
being an archive:

- **dirty count** -- policy edited on this machine that no commit carries.
  The agent's own guidance self-edits and the phone's automation edits land
  here first, so a nonzero count is normal *transiently* and alarming
  *chronically*.
- **last-commit age** -- how long since anything was archived at all. This
  is the number that catches a dead snapshot timer, which is the only
  failure mode of mechanical archival that looks exactly like "nothing
  changed today."
- **unpushed count** -- commits that exist only on this disk. An archive
  that dies with the machine defeats the entire reason for archiving.

**Strictly read-only, and deliberately so.** The engine acquiring git as a
write dependency -- auto-committing its own workspace writes -- was
considered and rejected (design section 4): commit policy is an opinion, and
opinions belong outside the kernel. Snapshotting is a dumb ops timer, visible
in ``systemctl list-timers``; this module is only the check-engine light.

**Never raises for a non-git workspace.** A workspace that is not a checkout
is a completely legal workspace -- most are, on day one. It reports
``is_repo=False`` and doctor prints one line saying so, because an absent
archive that the operator *chose* is not a fault, and reporting it as one
trains people to ignore the block.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Long enough that a slow disk or a cold page cache never turns a health
# check into a hang; short enough that doctor stays a thing you run
# impatiently. A timeout is reported as `error`, never as "clean".
_GIT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class WorkspaceGit:
    """What git says about a workspace directory. All fields read-only."""

    is_repo: bool
    dirty_count: int | None = None
    last_commit_at: str | None = None
    last_commit_age_seconds: float | None = None
    last_commit_subject: str | None = None
    unpushed_count: int | None = None
    upstream: str | None = None
    error: str | None = None
    data_dir_inside_workspace: bool = False


def _git(workspace: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def inspect(workspace: Path, *, data_dir: Path | None = None) -> WorkspaceGit:
    """Git drift facts for ``workspace``, plus the data-dir containment check.

    Args:
        workspace: the policy tree ``drumbeat serve --workspace`` was given.
        data_dir: the resolved engine state root. When it lands *inside* the
            workspace checkout, that is reported -- see the containment note
            on ``WorkspaceGit.data_dir_inside_workspace``.

    Returns:
        A ``WorkspaceGit``. Failure is a populated ``error`` field, never an
        exception and never a silently-clean report: "git blew up" and "the
        tree is clean" must not look the same to a human reading a health
        check at 7am.
    """
    workspace = Path(workspace).expanduser()
    contained = _data_dir_inside(workspace, data_dir)

    try:
        code, out, _err = _git(workspace, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return WorkspaceGit(
            is_repo=False,
            error=f"could not run git: {exc}",
            data_dir_inside_workspace=contained,
        )
    if code != 0 or out != "true":
        return WorkspaceGit(is_repo=False, data_dir_inside_workspace=contained)

    try:
        return _collect(workspace, contained=contained)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return WorkspaceGit(
            is_repo=True,
            error=f"git inspection failed: {exc}",
            data_dir_inside_workspace=contained,
        )


def _collect(workspace: Path, *, contained: bool) -> WorkspaceGit:
    # Dirty count. `--porcelain` is the stable, parse-intended format;
    # untracked files are INCLUDED (no `-uno`) on purpose: an unreviewed
    # stray dropped by an agent turn is exactly what the scoped `git add`
    # refuses to push, so it must be visible somewhere, and this is where.
    _, status_out, _ = _git(workspace, "status", "--porcelain")
    dirty_count = len([line for line in status_out.splitlines() if line.strip()])

    last_commit_at: str | None = None
    age_seconds: float | None = None
    subject: str | None = None
    code, out, _ = _git(workspace, "log", "-1", "--format=%cI%x00%s")
    if code == 0 and out:
        raw_at, _, subject = out.partition("\0")
        last_commit_at = raw_at
        try:
            committed = datetime.fromisoformat(raw_at)
        except ValueError:
            committed = None
        if committed is not None:
            age_seconds = (datetime.now(UTC) - committed).total_seconds()

    # Unpushed. A branch with no upstream is not "0 unpushed" -- it is
    # "nothing is tracking this, every commit here is local forever," which
    # is a worse state wearing a healthier number. Report it as unknown with
    # the reason, rather than a reassuring zero.
    unpushed: int | None = None
    upstream: str | None = None
    code, out, _ = _git(
        workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"
    )
    if code == 0 and out:
        upstream = out
        code, out, _ = _git(workspace, "rev-list", "--count", "@{upstream}..HEAD")
        if code == 0 and out.isdigit():
            unpushed = int(out)

    return WorkspaceGit(
        is_repo=True,
        dirty_count=dirty_count,
        last_commit_at=last_commit_at,
        last_commit_age_seconds=age_seconds,
        last_commit_subject=subject or None,
        unpushed_count=unpushed,
        upstream=upstream,
        data_dir_inside_workspace=contained,
    )


def _data_dir_inside(workspace: Path, data_dir: Path | None) -> bool:
    """Does engine state resolve INSIDE the policy tree?

    Rule 1 of the packet format (design section 10) says state must live
    outside the tree -- but the engine's own *default* data dir
    (``<workspace>/runs``) violates it, kept that way for behavior
    preservation at cutover. So the rule is enforced by observation, not
    refusal: a serve-time abort would be mechanism punishing a legal input.
    This is the observation.
    """
    if data_dir is None:
        return False
    try:
        resolved_data = Path(data_dir).expanduser().resolve()
        resolved_ws = Path(workspace).expanduser().resolve()
    except OSError:
        return False
    return resolved_data == resolved_ws or resolved_ws in resolved_data.parents


def format_block(report: WorkspaceGit, *, workspace: Path) -> list[str]:
    """The doctor block, as lines. Pure formatting -- no I/O, no git."""
    lines = [f"workspace git: {workspace}"]

    if report.error:
        lines.append(f"  UNKNOWN: {report.error}")
        return lines + _containment_lines(report)

    if not report.is_repo:
        lines.append(
            "  not a git checkout -- no archive of this policy exists. That is a "
            "legal workspace; it is also a machine loss away from gone. Put "
            "it under version control if you want an archive."
        )
        return lines + _containment_lines(report)

    dirty = report.dirty_count or 0
    lines.append(
        f"  dirty: {dirty} file(s)"
        + (
            ""
            if dirty == 0
            else "  -- uncommitted policy: edits no archive carries yet"
        )
    )

    if report.last_commit_at is None:
        lines.append("  last commit: none yet (no commit on this branch)")
    else:
        age = report.last_commit_age_seconds
        age_text = "unknown age" if age is None else humanize_age(age)
        lines.append(f"  last commit: {age_text} ago ({report.last_commit_at})")
        if report.last_commit_subject:
            lines.append(f"               {report.last_commit_subject}")

    if report.unpushed_count is None:
        lines.append(
            "  unpushed: UNKNOWN -- this branch has no upstream, so every commit "
            "here is local-only and no push can be behind. An archive that dies "
            "with the machine is not an archive."
        )
    else:
        lines.append(
            f"  unpushed: {report.unpushed_count} commit(s) ahead of "
            f"{report.upstream}"
            + (
                ""
                if report.unpushed_count == 0
                else "  -- archived locally, not remotely"
            )
        )

    return lines + _containment_lines(report)


def _containment_lines(report: WorkspaceGit) -> list[str]:
    if not report.data_dir_inside_workspace:
        return []
    # The danger this names is git-specific, so the wording is too. A
    # non-checkout workspace still violates Rule 1 -- and saying "git-tracked"
    # about a directory git has never heard of would be a false statement in a
    # health check, which is how a block earns the right to be ignored.
    if report.is_repo:
        danger = (
            "    Engine state (runs, outbox, locks, api key, session pins -- "
            "and whatever the consumer keeps there) is one `git clean -fdx` "
            "away from deletion, and ignored files show nothing in "
            "`git status` first."
        )
        headline = (
            "  CONTAINMENT WARNING: the data dir resolves INSIDE this "
            "git-tracked workspace."
        )
    else:
        danger = (
            "    Not dangerous today -- this workspace is not a checkout -- "
            "but it becomes a `git clean -fdx` away from deleting engine "
            "state on the day anyone runs `git init` here."
        )
        headline = (
            "  CONTAINMENT WARNING: the data dir resolves INSIDE the "
            "workspace (packet-format Rule 1: state lives outside the "
            "policy tree)."
        )
    return [headline, danger, "    Point --data-dir outside the tree."]


def humanize_age(seconds: float) -> str:
    """A short, human-readable age (``42s`` / ``17m`` / ``3.2h`` / ``5.1d``).

    Public because ``cli`` renders the failure log's age with it too, and two
    independently-written age formatters on one health page is how the same
    number starts reading two different ways.
    """
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


__all__ = ["WorkspaceGit", "format_block", "humanize_age", "inspect"]
