"""Conformance for ``contracts/drumpack-card.v1.md`` -- the frozen card shape.

This is the fixture-backed half of the contract's Conformance section: a
``drumpack-good/`` pack that loads clean, and a ``drumpack-bad/`` tree carrying
one violation per loader-enforceable frozen-core rule, each asserted to REFUSE
with its named remedy. The fixtures live on disk (``tests/fixtures/``) rather
than being written by a helper so a stranger can open them and see exactly what
a conforming card -- and each specific lie -- looks like.

The rules split by what the loader can mechanically enforce:

* Rules 1, 2, 3 are card-level and produce a named refusal -- one bad fixture
  each (rule 1 and rule 3 have two facets apiece, so two fixtures each).
* Rule 4 (explicit wiring) is a pack-LIST property, proven against
  ``read_pack_list``/``path_entries`` with the good fixture, not a bad card.
* Rule 5 (fail loud; a missing/empty pack list is VISIBLE) is proven two ways:
  every bad fixture above IS the "invalid card refused" half, and the
  zero-tools-visibility half is proven against ``pack_list_visibility`` +
  ``load_workspace_packs``'s loud log + ``drumbeat doctor``.
* Rule 6 (tools are self-serving) is only half loader-enforceable: the engine
  guarantees PATH presence (proven here); ``--help``/no-prompts/fail-loud on a
  missing prerequisite are the tool's own obligation, an author convention the
  loader cannot check without becoming the plugin protocol the project refused.
  The good fixture's ``good-tool`` honours it, and that is asserted, but its
  ABSENCE could never be a load refusal -- see the contract changelog.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from drumbeat import cli, packs

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
GOOD = FIXTURES / "drumpack-good"
BAD = FIXTURES / "drumpack-bad"


# ---------------------------------------------------------------------------
# The good fixture loads clean (the positive half of the discriminating pair).
# ---------------------------------------------------------------------------


def test_good_fixture_loads_clean():
    pack = packs.load_pack(GOOD)
    assert pack.name == "drumpack-good"
    assert pack.tools == ("good-tool",)  # rule 3: the declared, real tool
    assert pack.pack_format == 1  # rule 2: required, supported format
    assert pack.description  # rule 2: required, non-empty
    assert pack.card.strip()  # rule 1: the non-empty agent-facing body
    # rule 2: the optional `activity:` map loads (keyed by subcommand).
    assert pack.activity.get("status", "").startswith("Checking status")


# ---------------------------------------------------------------------------
# The bad fixtures each refuse, with the rule's named remedy. One per rule
# facet -- the discriminating negative half.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "rule", "match"),
    [
        # Rule 1 -- one directory, one card; the body is non-empty.
        ("rule1-missing-card", "1 (card must exist)", r"no drumpack\.md"),
        ("rule1-empty-body", "1 (body non-empty)", r"card body is empty"),
        # Rule 2 -- closed vocabulary; required keys.
        ("rule2-unknown-key", "2 (closed vocabulary)", r"unknown frontmatter key"),
        (
            "rule2-missing-required-key",
            "2 (required keys)",
            r"description.*is required",
        ),
        # Rule 3 -- tool entries are minimal and real, in both directions.
        ("rule3-bin-missing", "3 (declared tool must exist)", r"does not exist"),
        ("rule3-not-executable", "3 (declared tool executable)", r"NOT.*executable"),
        ("rule3-undeclared-bin", "3 (no undeclared bin)", r"never declares"),
    ],
)
def test_bad_fixture_refuses_with_named_remedy(fixture, rule, match):
    """Each bad fixture violates frozen-core rule {rule} and is REFUSED, not degraded."""
    with pytest.raises(packs.PackError, match=match):
        packs.load_pack(BAD / fixture)


def test_unknown_key_refusal_names_the_offending_key_and_the_closed_vocabulary():
    """Rule 2's remedy has to be actionable: name the bad key AND the allowed set."""
    with pytest.raises(packs.PackError) as err:
        packs.load_pack(BAD / "rule2-unknown-key")
    message = str(err.value)
    assert "maintainer" in message  # the offending key, named
    assert "pack_format" in message and "activity" in message  # the closed vocabulary
    assert "silently" in message  # says WHY it refuses rather than ignoring


# ---------------------------------------------------------------------------
# Rule 4 -- wiring is explicit: drumpacks.txt only, in list order, nothing
# auto-discovered.
# ---------------------------------------------------------------------------


def test_rule4_only_declared_packs_join_the_path_nothing_auto_discovered(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Wire exactly one pack, by absolute path: its bin/ joins the turn PATH.
    (workspace / packs.PACK_LIST_FILENAME).write_text(f"{GOOD}\n", encoding="utf-8")
    assert packs.path_entries(workspace) == [str((GOOD / "bin").resolve())]
    # Rewire to declare NONE. The good pack still exists on disk, but an unlisted
    # pack is never auto-discovered onto the PATH -- wiring is explicit only.
    (workspace / packs.PACK_LIST_FILENAME).write_text(
        "# nothing wired\n", encoding="utf-8"
    )
    assert packs.path_entries(workspace) == []


# ---------------------------------------------------------------------------
# Rule 5 -- fail loud: a missing or empty pack list is a VISIBLE condition,
# never a silent zero-tools turn. (VISION §4.)
# ---------------------------------------------------------------------------


def test_rule5_missing_pack_list_is_a_loud_visible_condition(tmp_path):
    listing = packs.read_pack_list(tmp_path)
    assert listing.declared is False
    warning = packs.pack_list_visibility(listing)
    assert warning is not None
    assert packs.PACK_LIST_FILENAME in warning
    assert "ZERO drumpack tools" in warning


def test_rule5_empty_pack_list_is_distinct_and_also_loud(tmp_path):
    """A drumpacks.txt that declares nothing is a DIFFERENT fact from an absent one."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / packs.PACK_LIST_FILENAME).write_text(
        "# every line here is a comment\n\n", encoding="utf-8"
    )
    listing = packs.read_pack_list(workspace)
    assert listing.declared is True and listing.paths == ()
    warning = packs.pack_list_visibility(listing)
    assert warning is not None
    assert "declares NO drumpacks" in warning


def test_rule5_load_logs_loudly_at_load_time(tmp_path, caplog):
    """The engine's canonical load path warns loudly -- not a silent []."""
    with caplog.at_level(logging.WARNING, logger="drumbeat.packs"):
        result = packs.load_workspace_packs(tmp_path)
    assert result == ()  # loud-but-tolerant: it still loads (zero packs), never refuses
    assert any(
        "drumpack list" in record.getMessage() for record in caplog.records
    ), "a missing pack list must be logged loudly at load, never swallowed"


def test_rule5_doctor_names_the_zero_tools_condition(tmp_path, capsys):
    """`drumbeat doctor` surfaces the missing/empty pack list -- test-proven end to end.

    A fresh `drumbeat init` scaffolds a comments-only drumpacks.txt (an empty
    declared list), which is exactly the loud-but-tolerant case: doctor must
    NAME it rather than report a healthy engine that would run every turn with
    zero drumpack tools.
    """
    workspace = tmp_path / "ws"
    assert cli.main(["init", str(workspace)]) == 0
    capsys.readouterr()  # discard init output
    cli.main(["doctor", "--workspace", str(workspace)])
    out = capsys.readouterr().out
    assert "drumpacks:" in out
    assert "NO drumpacks" in out or "ZERO drumpack tools" in out


# ---------------------------------------------------------------------------
# Rule 6 -- tools are self-serving. The engine guarantees PATH presence; the
# tool guarantees its own usability.
# ---------------------------------------------------------------------------


def test_rule6_engine_guarantees_path_presence(tmp_path):
    """The engine's half of rule 6: a declared tool resolves ON the turn PATH."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / packs.PACK_LIST_FILENAME).write_text(f"{GOOD}\n", encoding="utf-8")
    packs.reset_base_path_for_tests()
    packs.pin_base_path("/nonexistent-base")
    try:
        resolved = packs.resolve_tool("good-tool", workspace)
    finally:
        packs.reset_base_path_for_tests()
    assert resolved == str((GOOD / "bin" / "good-tool").resolve())


def test_rule6_good_fixture_tool_is_self_serving():
    """The tool's half of rule 6 (a CONVENTION, not a load refusal): --help, exit 0.

    The loader cannot enforce this without executing every declared binary at
    load, so it is the author's obligation -- the good fixture honours it, and
    that is what a conforming tool looks like.
    """
    tool = GOOD / "bin" / "good-tool"
    result = subprocess.run(
        [str(tool), "--help"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,  # no interactive prompt may block it
        env={**os.environ},
        check=False,
    )
    assert result.returncode == 0
    assert "good-tool" in result.stdout
