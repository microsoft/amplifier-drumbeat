"""Phase B, step 6: the doctor's workspace-git block.

Each test pins one rule of the doctor's workspace-git block -- that drift
from the policy archive (uncommitted edits, unpushed commits, or no checkout
at all) is reported as an operational fact -- and is written to go red if the
mechanism is removed.

The two that matter most:

- ``test_no_upstream_is_unknown_not_zero`` -- return 0 instead of None for a
  branch with no upstream and this goes red. The failure it guards is the
  reassuring kind: "0 commits unpushed" and "nothing is tracking this repo,
  so every commit is local forever" are opposite states, and reporting the
  worse one with the healthier number is how a check-engine light stops
  meaning anything.
- ``test_containment_fires_when_data_dir_is_inside_the_workspace`` -- drop
  the containment check and this goes red. It is the only observable for a
  mispointed data dir: ignored files show NOTHING in ``git status``, so the
  packet's ``.gitignore`` is a backstop, never the alarm.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from drumbeat import workspace_git


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test")


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")


# ------------------------------------------------- the not-a-repo branch ----


def test_a_workspace_that_is_not_a_checkout_is_reported_not_failed(
    tmp_path: Path,
) -> None:
    """No archive is a legal state, not an error.

    Most workspaces are not checkouts on day one. Reporting that as a fault
    would train an operator to skip the block, which costs more than the
    block is worth.
    """
    report = workspace_git.inspect(tmp_path)
    assert report.is_repo is False
    assert report.error is None
    lines = "\n".join(workspace_git.format_block(report, workspace=tmp_path))
    assert "not a git checkout" in lines


def test_a_missing_directory_reports_error_never_clean(tmp_path: Path) -> None:
    """A failed inspection must never be indistinguishable from a clean one."""
    report = workspace_git.inspect(tmp_path / "does-not-exist")
    assert report.is_repo is False
    assert report.dirty_count is None


# ------------------------------------------------------------- the numbers --


def test_clean_repo_reports_zero_dirty_and_a_commit_age(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")

    report = workspace_git.inspect(repo)
    assert report.is_repo is True
    assert report.dirty_count == 0
    assert report.last_commit_at is not None
    assert report.last_commit_age_seconds is not None
    assert report.last_commit_age_seconds < 120
    assert report.last_commit_subject == "add policy.md"


def test_untracked_strays_count_as_dirty(tmp_path: Path) -> None:
    """Untracked files are INCLUDED in the dirty count, on purpose.

    The snapshot's ``git add`` is scoped to the policy paths precisely so an
    unreviewed stray -- a debug dump, a token-bearing temp file dropped by an
    agent turn into its own cwd -- cannot ride an auto-push into remote
    history. Excluding untracked files here would make that stray invisible
    in both places at once.
    """
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")
    (repo / "stray-debug-dump.txt").write_text("oops\n", encoding="utf-8")

    assert workspace_git.inspect(repo).dirty_count == 1


def test_modified_tracked_file_counts_as_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")
    (repo / "policy.md").write_text("edited by the agent\n", encoding="utf-8")

    assert workspace_git.inspect(repo).dirty_count == 1


# ------------------------------------------------------------- the upstream --


def test_no_upstream_is_unknown_not_zero(tmp_path: Path) -> None:
    """A branch nothing tracks is not "0 unpushed" -- it is worse than that."""
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")

    report = workspace_git.inspect(repo)
    assert report.unpushed_count is None
    assert report.upstream is None
    lines = "\n".join(workspace_git.format_block(report, workspace=repo))
    assert "unpushed: UNKNOWN" in lines
    assert "dies with the machine" in lines


def test_unpushed_commits_are_counted_against_the_upstream(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")

    assert workspace_git.inspect(repo).unpushed_count == 0

    _commit(repo, "more-policy.md", "world\n")
    report = workspace_git.inspect(repo)
    assert report.unpushed_count == 1
    assert report.upstream == "origin/main"
    assert "archived locally, not remotely" in "\n".join(
        workspace_git.format_block(report, workspace=repo)
    )


# ----------------------------------------------------------- containment ----


def test_containment_fires_when_data_dir_is_inside_the_workspace(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")

    report = workspace_git.inspect(repo, data_dir=repo / "runs")
    assert report.data_dir_inside_workspace is True
    lines = "\n".join(workspace_git.format_block(report, workspace=repo))
    assert "CONTAINMENT WARNING" in lines
    assert "git clean -fdx" in lines


def test_containment_silent_when_data_dir_is_outside(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")

    report = workspace_git.inspect(repo, data_dir=tmp_path / "state")
    assert report.data_dir_inside_workspace is False
    assert "CONTAINMENT" not in "\n".join(
        workspace_git.format_block(report, workspace=repo)
    )


def test_containment_wording_does_not_claim_git_for_a_non_checkout(
    tmp_path: Path,
) -> None:
    """Rule 1 is violated either way; only the danger is git-specific.

    Saying "git-tracked workspace" about a directory git has never heard of
    is a false statement inside a health check, and a block that says false
    things earns the right to be ignored.
    """
    report = workspace_git.inspect(tmp_path, data_dir=tmp_path / "runs")
    assert report.data_dir_inside_workspace is True
    lines = "\n".join(workspace_git.format_block(report, workspace=tmp_path))
    assert "CONTAINMENT WARNING" in lines
    assert "git-tracked workspace" not in lines
    assert "git init" in lines


def test_data_dir_equal_to_workspace_is_contained(tmp_path: Path) -> None:
    report = workspace_git.inspect(tmp_path, data_dir=tmp_path)
    assert report.data_dir_inside_workspace is True


# ------------------------------------------------------- read-only posture --


def test_inspect_never_writes_to_the_workspace(tmp_path: Path) -> None:
    """The engine reads git. It does not commit, stage, or clean.

    Auto-committing the engine's own workspace writes was considered and
    rejected (design section 4): commit policy is an opinion, and this
    module must never grow one.
    """
    repo = tmp_path / "ws"
    _init_repo(repo)
    _commit(repo, "policy.md", "hello\n")
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")

    before = sorted(p.name for p in repo.iterdir())
    _, status_before, _ = _porcelain(repo)

    workspace_git.inspect(repo, data_dir=repo / "runs")

    assert sorted(p.name for p in repo.iterdir()) == before
    _, status_after, _ = _porcelain(repo)
    assert status_after == status_before


def _porcelain(repo: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr
