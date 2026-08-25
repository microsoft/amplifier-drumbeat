"""The capabilities endpoint must report the workspace turns ACTUALLY run in.

Every test here defends one property: the workspace a report is computed
against is the same workspace the runner uses, derived the same way, from
the same input. When those two drifted apart the endpoint did not fail --
it answered confidently about a directory nothing runs in, which is the
worst possible failure mode for a surface whose entire job is telling a
phone what is real.

The specific drift this file was written for (2026-08-16, live):
``resolve_tools``/``resolve_packs`` called ``.resolve()`` on
``automations_dir`` before taking ``.parent``. A workspace's
``automations/`` is routinely a symlink to policy kept elsewhere -- e.g. a
workspace's ``automations/`` pointing at a shared policy checkout -- so
resolving walked out of the workspace and computed the turn PATH against the
POLICY repo's ``bin/`` instead of the workspace's own. Four tools that were
installed, on PATH, and passing their scheduled runs were reported to the
client as "not installed on this box".
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from drumbeat import capabilities, packs
from drumbeat.management_api import EngineContext
from drumbeat.paths import workspace_for_automations_dir

# A base with nothing real on it, so a "resolved" tool in any assertion
# below can only have come from a directory this test built. Inheriting the
# host PATH here would let a same-named binary on the developer's machine
# make a broken resolver look correct.
_EMPTY_BASE = "/nonexistent-base-bin"


def _executable(path: Path, body: str = "#!/usr/bin/env bash\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _automation(directory: Path, slug: str, requires: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    required = "\n".join(f"    - {name}" for name in requires)
    path = directory / f"{slug}.md"
    path.write_text(
        "---\n"
        "automation:\n"
        f"  name: {slug}\n"
        "  enabled: false\n"
        "  trigger:\n"
        "    type: schedule\n"
        "    expression: every 30 minutes\n"
        "  notify: never\n"
        "  requires:\n"
        f"{required}\n"
        "  steps:\n"
        "    - id: do-the-thing\n"
        "      prompt: Do the thing.\n"
        "---\n",
        encoding="utf-8",
    )
    return path


def _symlinked_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """The live shape: a workspace whose automations/ points at a policy repo.

    Returns ``(workspace, policy)``. The workspace has its own ``bin/`` with
    ``lane-tool``; the policy repo has a ``bin/`` with ``decoy-tool``. Only
    one of those two is on a correctly-derived turn PATH, which is what
    makes this rig able to fail.
    """
    workspace = tmp_path / "state" / "drumbeat"
    policy = tmp_path / "share" / "mind"

    _automation(policy / "automations", "check", ["lane-tool", "decoy-tool"])
    _executable(workspace / "bin" / "lane-tool")
    _executable(policy / "bin" / "decoy-tool")

    (workspace / "automations").symlink_to(policy / "automations")
    return workspace, policy


def _pinned_empty_base():
    packs.reset_base_path_for_tests()
    packs.pin_base_path(_EMPTY_BASE)


# ---- the derivation itself ----


def test_workspace_derivation_does_not_follow_the_automations_symlink(tmp_path):
    workspace, policy = _symlinked_workspace(tmp_path)

    derived = workspace_for_automations_dir(workspace / "automations")

    assert derived == workspace
    assert derived != policy, (
        "derivation followed the symlink into the policy repo -- every path "
        "built from this (above all bin/, and therefore the turn PATH) now "
        "addresses a directory turns never run in"
    )


def test_runner_side_and_report_side_derive_the_same_workspace(tmp_path):
    """The two must be one implementation, not two that agree today."""
    workspace, _ = _symlinked_workspace(tmp_path)
    automations_dir = workspace / "automations"

    ctx = EngineContext(
        automations_dir=automations_dir,
        prompts_dir=workspace / "prompts",
        runs_dir=workspace / "runs",
        cwd=workspace,
    )

    assert ctx.workspace == workspace_for_automations_dir(automations_dir)
    assert ctx.workspace == ctx.cwd, (
        "the workspace the runner runs turns in and the one derived from "
        "automations_dir disagree -- capabilities would report a PATH no "
        "turn ever gets"
    )


# ---- what the endpoint reports ----


def test_resolve_tools_finds_the_workspaces_own_bin_through_a_symlink(tmp_path):
    """The regression, stated as the property: own bin/, not the link target's."""
    workspace, policy = _symlinked_workspace(tmp_path)

    _pinned_empty_base()
    try:
        tools = {
            t["name"]: t for t in capabilities.resolve_tools(workspace / "automations")
        }
    finally:
        packs.reset_base_path_for_tests()

    lane = tools["lane-tool"]
    assert lane["resolved"] is True, (
        "a tool sitting in the workspace's own bin/ was reported unresolvable "
        "-- this is the live defect: four running tools shown to the "
        "app as 'not installed on this box'"
    )
    assert lane["path"] == str(workspace / "bin" / "lane-tool")
    assert str(policy) not in lane["path"]

    decoy = tools["decoy-tool"]
    assert decoy["resolved"] is False, (
        "a binary in the SYMLINK TARGET's bin/ resolved -- the search path "
        "is still being built from the policy repo, so this test would pass "
        "for the wrong reason if the workspace bin happened to match too"
    )


def test_resolve_packs_reports_the_workspaces_own_path_entries(tmp_path):
    workspace, policy = _symlinked_workspace(tmp_path)

    _pinned_empty_base()
    try:
        report = capabilities.resolve_packs(workspace / "automations")
    finally:
        packs.reset_base_path_for_tests()

    assert report["pack_list"] == str(workspace / "drumpacks.txt")
    assert report["path_prepended"] == [str(workspace / "bin")]
    assert str(policy / "bin") not in report["turn_path"], (
        "the reported turn PATH contains the policy repo's bin/ -- the "
        "endpoint that exists to expose a wrong PATH is computing it the "
        "same wrong way, which is how this stayed invisible"
    )
    assert report["turn_path"].split(os.pathsep)[0] == str(workspace / "bin")


def test_a_plain_workspace_is_unaffected(tmp_path):
    """No symlink involved: the fix must not change the ordinary case."""
    workspace = tmp_path / "ws"
    _automation(workspace / "automations", "check", ["lane-tool"])
    _executable(workspace / "bin" / "lane-tool")

    _pinned_empty_base()
    try:
        tools = {
            t["name"]: t for t in capabilities.resolve_tools(workspace / "automations")
        }
    finally:
        packs.reset_base_path_for_tests()

    assert tools["lane-tool"]["resolved"] is True
    assert tools["lane-tool"]["path"] == str(workspace / "bin" / "lane-tool")
