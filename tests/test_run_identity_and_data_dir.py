"""Phase B, steps 1-2: run identity at the mint, and the policy/state split.

Each test pins one invariant of run-identity minting and the policy/state
split -- run ids stay distinct even within the same second, and engine state
never lives in policy files -- and is written to go red if the mechanism is
removed.

The two that matter most:

- ``test_ids_minted_in_the_same_second_are_distinct`` -- revert
  ``_run_id_now`` to a bare ``strftime`` and this goes red. It is the exact
  production failure: two automations firing in one wall-clock second minted
  one id, and the delivery seam's dedup-at-mint suppressed the second
  automation's notification against the first's, silently.
- ``test_since_window_still_filters_new_format_ids`` -- revert
  ``_parse_run_id_time`` to a whole-string parse and this goes red. Note the
  failure it guards is *silent*: unparseable ids are fail-open-inclusive, so
  a whole-string parse would not raise -- ``--since`` would simply stop
  filtering while still reporting itself as a window.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from drumbeat import invalid_runs, runner, serve

# ------------------------------------------------------- run id: the mint ----


def test_ids_minted_in_the_same_second_are_distinct():
    """The production bug, as a test: many mints inside one second, no collision."""
    minted = [runner.new_run_id() for _ in range(500)]

    # The precondition -- this test is only meaningful if the loop actually
    # crossed the same-second case. Asserting it means a machine slow enough
    # to spread 500 mints over 500 seconds fails loudly instead of passing
    # vacuously.
    seconds = [run_id.split("-", 1)[0] for run_id in minted]
    assert len(set(seconds)) < len(minted), "loop never minted twice in one second"

    assert len(set(minted)) == len(minted), "two runs minted the same identity"


def test_id_shape_is_timestamp_prefix_plus_entropy():
    run_id = runner.new_run_id()
    stamp, sep, suffix = run_id.partition("-")
    assert sep == "-"
    assert len(stamp) == 16  # YYYYmmddTHHMMSSZ
    # Raises if the prefix drifted. Goes through the engine's own parser so
    # the mint and the one site that reads it are tested against each other.
    assert invalid_runs._parse_run_id_time(run_id) is not None
    assert len(suffix) == 6
    int(suffix, 16)  # raises if the suffix stops being hex


def test_id_survives_filesystem_sanitization():
    """``new_run_id`` sanitizes; the suffix must not be what gets eaten."""
    run_id = runner.new_run_id()
    assert runner._sanitize_run_id(run_id) == run_id


def test_lexicographic_order_is_chronological_across_both_formats():
    """Two live sorters order run dirs by NAME.

    ``management_api`` line 610 and ``session_health`` line 382 both sort run
    directory names -- the latter reverse, to read newest-first. Neither
    parses the name, so neither breaks on the new format, but both silently
    depend on lexicographic order matching time order. The fixed-width
    timestamp prefix is what keeps that true, including for history's
    old-format ids. This test is what makes that dependency deliberate rather
    than lucky.
    """
    ordered = [
        "20260810T021421Z",  # old format, same second as the next two
        "20260810T021421Z-0f0f0f",
        "20260810T021421Z-ffffff",
        "20260810T021422Z",
        "20260810T021500Z-000000",
        "20260811T000000Z-aaaaaa",
    ]
    assert sorted(ordered) == ordered
    assert sorted(ordered, reverse=True) == list(reversed(ordered))


# ---------------------------------------------- run id: the one parser ----


def _write_run(runs_dir: Path, slug: str, run_id: str, *, result: dict | None) -> Path:
    run_dir = runs_dir / slug / run_id
    run_dir.mkdir(parents=True)
    if result is not None:
        (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return run_dir


def test_parses_the_prefix_of_a_new_format_id():
    parsed = invalid_runs._parse_run_id_time("20260810T021421Z-7f3a9c")
    assert parsed == datetime(2026, 8, 10, 2, 14, 21, tzinfo=UTC)


def test_still_parses_a_bare_old_format_id():
    parsed = invalid_runs._parse_run_id_time("20260810T021421Z")
    assert parsed == datetime(2026, 8, 10, 2, 14, 21, tzinfo=UTC)


def test_a_genuinely_undateable_id_is_still_none_not_a_guess():
    assert invalid_runs._parse_run_id_time("manual-test-thing") is None
    assert invalid_runs._parse_run_id_time("") is None


def test_since_window_still_filters_new_format_ids(tmp_path: Path):
    """One sweep, both formats, on both sides of the window."""
    # Outside the window -- must be filtered out by --since despite being
    # invalid (no result.json).
    _write_run(tmp_path, "teams-check", "20260801T120000Z", result=None)
    _write_run(tmp_path, "teams-check", "20260801T120000Z-aaaaaa", result=None)
    # Inside the window -- must be reported.
    _write_run(tmp_path, "email-check", "20260810T120000Z", result=None)
    _write_run(tmp_path, "email-check", "20260810T120000Z-bbbbbb", result=None)

    since = datetime(2026, 8, 9, tzinfo=UTC)
    windowed = invalid_runs.sweep(tmp_path, since=since)

    assert {f.run_id for f in windowed} == {
        "20260810T120000Z",
        "20260810T120000Z-bbbbbb",
    }
    assert len(invalid_runs.sweep(tmp_path)) == 4


def test_undateable_id_is_included_even_inside_a_window(tmp_path: Path):
    """Fail-open-inclusive, unchanged: the sweep never skips what it cannot date."""
    _write_run(tmp_path, "teams-check", "not-a-timestamp", result=None)
    findings = invalid_runs.sweep(tmp_path, since=datetime(2099, 1, 1, tzinfo=UTC))
    assert [f.run_id for f in findings] == ["not-a-timestamp"]


# ----------------------------------------------------- policy/state split ----


def _policy_tree(tmp_path: Path) -> Path:
    workspace = tmp_path / "packet"
    (workspace / "automations").mkdir(parents=True)
    return workspace


def test_data_dir_defaults_to_workspace_runs(tmp_path: Path):
    """Behavior preservation is the point: no flag, no change."""
    workspace = _policy_tree(tmp_path)
    ctx = serve.resolve_workspace(workspace)
    assert ctx.runs_dir == workspace.resolve() / "runs"


def test_explicit_data_dir_moves_state_out_of_the_policy_tree(tmp_path: Path):
    workspace = _policy_tree(tmp_path)
    state = tmp_path / "state"
    ctx = serve.resolve_workspace(workspace, data_dir=state)

    assert ctx.runs_dir == state.resolve()
    # The policy dirs and the turn cwd stay pinned to the workspace -- only
    # state moves.
    assert ctx.automations_dir == workspace.resolve() / "automations"
    assert ctx.prompts_dir == workspace.resolve() / "prompts"
    assert ctx.cwd == workspace.resolve()
    # And the data dir is genuinely outside the tree a `git clean -fdx` would
    # sweep.
    assert workspace.resolve() not in ctx.runs_dir.parents


def test_data_dir_is_expanded_and_resolved(tmp_path: Path):
    """``~`` must expand; every consumer must see one spelling of the path."""
    workspace = _policy_tree(tmp_path)
    ctx = serve.resolve_workspace(workspace, data_dir=Path("~/.drumbeat-test-state"))
    assert ctx.runs_dir == (Path.home() / ".drumbeat-test-state").resolve()

    indirect = tmp_path / "state" / ".." / "state"
    (tmp_path / "state").mkdir()
    ctx2 = serve.resolve_workspace(workspace, data_dir=indirect)
    assert ctx2.runs_dir == (tmp_path / "state").resolve()


def test_data_dir_pointing_at_a_file_is_refused(tmp_path: Path):
    """FAIL LOUD rather than mkdir-explode later, mid-startup."""
    workspace = _policy_tree(tmp_path)
    not_a_dir = tmp_path / "state-file"
    not_a_dir.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="not a directory"):
        serve.resolve_workspace(workspace, data_dir=not_a_dir)


def test_resolve_workspace_creates_nothing(tmp_path: Path):
    """Resolution is a pure question. Only ``serve`` writes."""
    workspace = _policy_tree(tmp_path)
    state = tmp_path / "state"
    serve.resolve_workspace(workspace, data_dir=state)
    assert not state.exists()
    assert not (workspace / "runs").exists()


# ------------------------------------------------------------- the flag ----


def test_every_workspace_command_accepts_data_dir():
    """A flag only ``serve`` understood would let ``doctor``/``drain``/``sweep``
    inspect an empty directory and report health for an engine they were never
    looking at."""
    from drumbeat import cli

    parser = cli.build_parser()
    for command in ("serve", "doctor", "drain", "sweep", "api-key"):
        args = parser.parse_args(
            [command, "--workspace", "/tmp/ws", "--data-dir", "/tmp/state"]
        )
        assert args.data_dir == "/tmp/state", command


def test_data_dir_is_optional_everywhere():
    from drumbeat import cli

    parser = cli.build_parser()
    for command in ("serve", "doctor", "drain", "sweep", "api-key"):
        args = parser.parse_args([command, "--workspace", "/tmp/ws"])
        assert args.data_dir is None, command
