"""The drumpack contract's negative space (docs/DRUMPACKS.md).

Every test here is red-provable: it names a specific way a drumpack can lie and
asserts the engine REFUSES rather than degrades. That is the whole point of
the contract -- "PATH + card + fail-loud load rules" is only worth anything
if the third clause is mechanically true, and a load rule with no test is
the `requires:`-was-theater lesson repeating itself one layer down.

The positive cases are deliberately thin: the real proof that loading works
is the live capabilities diff and a real scheduled run under the
drumpack-resolved PATH, not a fixture.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from drumbeat import packs

REPO_ROOT = Path(__file__).resolve().parent.parent
MINIMAL_PACK = REPO_ROOT / "tests" / "packs" / "minimal"


def _write_pack(
    directory: Path,
    *,
    name: str = "fixture",
    pack_format: object = 1,
    tools: object = ("fixture-tool",),
    body: str = "The card. Says what the tool does.\n",
    make_executable: bool = True,
    extra_bin: dict[str, bool] | None = None,
) -> Path:
    """Build a drumpack on disk. Every knob here is one way a card can lie."""
    directory.mkdir(parents=True, exist_ok=True)
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)

    tool_list = list(tools) if isinstance(tools, (list, tuple)) else tools
    if isinstance(tool_list, list):
        for tool in tool_list:
            path = bin_dir / str(tool)
            path.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
            if make_executable:
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
    for extra_name, executable in (extra_bin or {}).items():
        path = bin_dir / extra_name
        path.write_text("#!/usr/bin/env bash\necho extra\n", encoding="utf-8")
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    tools_yaml = (
        "\n".join(f"  - {t}" for t in tool_list)
        if isinstance(tool_list, list)
        else f"  {tool_list}"
    )
    (directory / "drumpack.md").write_text(
        "---\n"
        f"pack_format: {pack_format}\n"
        f"name: {name}\n"
        "description: a fixture pack\n"
        "tools:\n"
        f"{tools_yaml}\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return directory


# ---- the shipped minimal test pack (section 7.2: the public inject: exemplar) ----


def test_minimal_test_pack_loads_and_carries_the_public_inject_exemplar():
    """The engine's own test pack must load, and must ship a COPYABLE `inject:`.

    Section 7.2: "The engine's minimal test pack carries a public `inject:`
    exemplar -- its only real user, the ledger pack, stays private (section
    10), and consumers must be able to COPY the pattern, not just read about
    it." A doc paragraph is not a copyable exemplar; a file is.
    """
    pack = packs.load_pack(MINIMAL_PACK)
    assert pack.name == "minimal"
    assert pack.tools == ("minimal-state",)
    assert "INJECT_IDLE" in pack.card
    # It also carries the copyable `activity:` exemplar: consumer-owned progress
    # narration, keyed by subcommand -- the drumpack owns its phrasing.
    assert pack.activity == {"state": "Checking minimal state…"}

    exemplar = MINIMAL_PACK / "automations" / "inject-exemplar.md"
    assert exemplar.is_file(), "the public inject: exemplar automation must ship"
    text = exemplar.read_text(encoding="utf-8")
    assert "inject:" in text
    assert "minimal-state" in text


def test_minimal_pack_tool_honours_the_sentinel_and_the_stderr_rule(tmp_path):
    """`minimal-state` must implement what the card claims, not just describe it."""
    import subprocess

    tool = MINIMAL_PACK / "bin" / "minimal-state"

    idle = subprocess.run(
        [str(tool)],
        capture_output=True,
        text=True,
        env={**os.environ, "MINIMAL_STATE_IDLE": "1"},
        check=False,
    )
    assert idle.returncode == 0
    assert idle.stdout.strip() == "INJECT_IDLE"

    failed = subprocess.run(
        [str(tool)],
        capture_output=True,
        text=True,
        env={**os.environ, "MINIMAL_STATE_FAIL": "1"},
        check=False,
    )
    assert failed.returncode != 0, "a failed read must exit non-zero"
    assert failed.stdout == "", "stdout is the injection channel -- errors go to stderr"
    assert failed.stderr.strip(), "the failure must say something, on stderr"


# ---- load refusals: every way a card can lie ----


def test_missing_drumpack_md_refuses(tmp_path):
    (tmp_path / "bin").mkdir()
    with pytest.raises(packs.PackError, match="no drumpack.md"):
        packs.load_pack(tmp_path)


def test_missing_directory_refuses(tmp_path):
    with pytest.raises(packs.PackError, match="does not exist"):
        packs.load_pack(tmp_path / "nope")


def test_unknown_pack_format_refuses(tmp_path):
    _write_pack(tmp_path, pack_format=2)
    with pytest.raises(packs.PackError, match="unknown pack_format"):
        packs.load_pack(tmp_path)


def test_absent_pack_format_refuses(tmp_path):
    _write_pack(tmp_path)
    text = (tmp_path / "drumpack.md").read_text(encoding="utf-8")
    (tmp_path / "drumpack.md").write_text(
        text.replace("pack_format: 1\n", ""), encoding="utf-8"
    )
    with pytest.raises(packs.PackError, match="pack_format` is required"):
        packs.load_pack(tmp_path)


def test_declared_tool_missing_from_bin_refuses(tmp_path):
    _write_pack(tmp_path, tools=["fixture-tool"])
    (tmp_path / "bin" / "fixture-tool").unlink()
    with pytest.raises(packs.PackError, match="does not exist"):
        packs.load_pack(tmp_path)


def test_declared_tool_without_exec_bit_refuses(tmp_path):
    """The named exec-bit check -- 'theater with a file extension'."""
    _write_pack(tmp_path, tools=["fixture-tool"], make_executable=False)
    with pytest.raises(packs.PackError, match="NOT.*executable"):
        packs.load_pack(tmp_path)


def test_undeclared_executable_in_bin_refuses(tmp_path):
    """A card that lies in EITHER direction is theater."""
    _write_pack(tmp_path, tools=["fixture-tool"], extra_bin={"sneaky": True})
    with pytest.raises(packs.PackError, match="never declares"):
        packs.load_pack(tmp_path)


def test_empty_card_body_refuses(tmp_path):
    _write_pack(tmp_path, body="   \n")
    with pytest.raises(packs.PackError, match="card body is empty"):
        packs.load_pack(tmp_path)


def test_duplicate_tool_across_packs_refuses_naming_both(tmp_path):
    """No precedence order silently deciding (the silent-fallback ban)."""
    a = _write_pack(tmp_path / "a", name="alpha", tools=["shared-tool"])
    b = _write_pack(tmp_path / "b", name="bravo", tools=["shared-tool"])
    with pytest.raises(packs.PackError) as exc:
        packs.load_packs([a, b])
    message = str(exc.value)
    assert "alpha" in message and "bravo" in message
    assert "shared-tool" in message


def test_duplicate_pack_name_refuses_naming_both(tmp_path):
    a = _write_pack(tmp_path / "a", name="same", tools=["tool-a"])
    b = _write_pack(tmp_path / "b", name="same", tools=["tool-b"])
    with pytest.raises(packs.PackError) as exc:
        packs.load_packs([a, b])
    assert str(a) in str(exc.value) and str(b) in str(exc.value)


# ---- the pack list ----


def test_absent_pack_list_is_reported_not_guessed(tmp_path):
    listing = packs.read_pack_list(tmp_path)
    assert listing.declared is False
    assert listing.paths == ()


def test_pack_list_honours_comments_blanks_and_relative_paths(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pack_dir = _write_pack(tmp_path / "somepack", name="somepack", tools=["some-tool"])
    (workspace / packs.PACK_LIST_FILENAME).write_text(
        "# a comment\n\n../somepack   # trailing comment\n", encoding="utf-8"
    )
    listing = packs.read_pack_list(workspace)
    assert listing.declared is True
    assert listing.paths == (pack_dir.resolve(),)


def test_pack_list_refuses_a_repeated_directory(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_pack(tmp_path / "somepack", name="somepack", tools=["some-tool"])
    (workspace / packs.PACK_LIST_FILENAME).write_text(
        "../somepack\n../somepack\n", encoding="utf-8"
    )
    with pytest.raises(packs.PackError, match="already declared"):
        packs.read_pack_list(workspace)


# ---- PATH construction ----


def test_turn_path_is_packs_then_workspace_bin_then_pinned_base(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / "bin").mkdir(parents=True)
    pack_dir = _write_pack(tmp_path / "p", name="p", tools=["p-tool"])
    (workspace / packs.PACK_LIST_FILENAME).write_text("../p\n", encoding="utf-8")

    packs.reset_base_path_for_tests()
    packs.pin_base_path("/pinned/one:/pinned/two")
    try:
        resolved = packs.turn_path(workspace)
    finally:
        packs.reset_base_path_for_tests()

    assert resolved.split(os.pathsep) == [
        str(pack_dir / "bin"),
        str((workspace / "bin").resolve()),
        "/pinned/one",
        "/pinned/two",
    ]


def test_pinned_base_ignores_later_environment_mutation(monkeypatch):
    """The base is a property of the PROCESS, not of whoever mutates os.environ."""
    packs.reset_base_path_for_tests()
    monkeypatch.setenv("PATH", "/first")
    try:
        assert packs.base_path() == "/first"
        monkeypatch.setenv("PATH", "/second")
        assert packs.base_path() == "/first"
    finally:
        packs.reset_base_path_for_tests()


def test_missing_tool_resolves_to_none_never_a_guess(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    packs.reset_base_path_for_tests()
    packs.pin_base_path("/nonexistent-base")
    try:
        assert packs.resolve_tool("definitely-not-a-real-tool", workspace) is None
    finally:
        packs.reset_base_path_for_tests()


# ---- card resolution ----


def test_card_is_resolved_from_the_tool_name_alone(tmp_path):
    a = _write_pack(tmp_path / "a", name="alpha", tools=["a-tool"], body="ALPHA CARD\n")
    b = _write_pack(tmp_path / "b", name="bravo", tools=["b-tool"], body="BRAVO CARD\n")
    loaded = packs.load_packs([a, b])

    assert [p.name for p in packs.cards_for_tools(loaded, ["b-tool"])] == ["bravo"]
    # Declaration order, not mention order -- a turn's text must not depend on
    # how somebody happened to sort a `requires:` block.
    assert [p.name for p in packs.cards_for_tools(loaded, ["b-tool", "a-tool"])] == [
        "alpha",
        "bravo",
    ]
    # A tool no pack provides contributes no card, and is not an error: the
    # workspace bin/ and the pinned base are legitimate sources.
    assert packs.cards_for_tools(loaded, ["gh"]) == []
    assert packs.pack_for_tool(loaded, "gh") is None
