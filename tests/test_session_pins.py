"""Phase B, step 3: session pins leave frontmatter for engine state.

Each test pins one property of moving session pins out of policy-file
frontmatter into engine state -- a corrupt store is refused rather than read
as empty, and a pin is keyed by automation slug -- and is written to go red
if the mechanism is removed.

The three that matter most:

- ``test_a_corrupt_store_is_refused_not_read_as_empty`` -- make ``_parse``
  swallow a JSONDecodeError and return ``{}`` and this goes red. Read-as-
  empty is a silent MASS rotation: every automation cold-starts, every
  accumulated conversation is abandoned, and nothing anywhere says so.
- ``test_a_failed_pin_write_aborts_the_run`` -- restore the old
  WARNING-and-proceed posture and this goes red. Carried over, a full disk
  became a silent fresh-session fork on every single run.
- ``test_a_pin_key_in_frontmatter_is_refused_by_name`` -- make the parser
  tolerate-and-ignore the retired keys and this goes red. A key that reads
  as meaningful and does nothing is failure class 2 verbatim.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest
from drumbeat.automation import AutomationError, load

from drumbeat import cli, runner, session_pins

_PINNED = """---
automation:
  name: Teams Check
  session: teams-check-20260805T213024Z
  session_workspace: -home-user-dev-x-consumer
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
---

1. Do the thing.
"""

_CLEAN = """---
automation:
  name: Teams Check
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
---

1. Do the thing.
"""


# ----------------------------------------------------------- the store ----


def test_absent_store_reads_as_empty_but_a_corrupt_one_does_not(tmp_path: Path):
    """Absent is a legitimate state (fresh workspace). Corrupt never is."""
    assert session_pins.read_all(tmp_path) == {}


def test_a_corrupt_store_is_refused_not_read_as_empty(tmp_path: Path):
    session_pins.pins_path(tmp_path).write_text("{not json", encoding="utf-8")
    with pytest.raises(session_pins.PinStoreError) as exc:
        session_pins.read_all(tmp_path)
    assert "REFUSING" in str(exc.value)


def test_a_torn_zero_byte_store_is_refused(tmp_path: Path):
    """An interrupted write must not look like a deliberate mass rotation."""
    session_pins.pins_path(tmp_path).write_text("", encoding="utf-8")
    with pytest.raises(session_pins.PinStoreError):
        session_pins.read_all(tmp_path)


def test_an_unknown_pins_format_is_refused(tmp_path: Path):
    session_pins.pins_path(tmp_path).write_text(
        json.dumps({"pins": {}, "pins_format": 99}), encoding="utf-8"
    )
    with pytest.raises(session_pins.PinStoreError) as exc:
        session_pins.read_all(tmp_path)
    assert "pins_format" in str(exc.value)


def test_a_pin_with_no_session_id_is_refused(tmp_path: Path):
    """A pin that names no conversation is worse than no pin: it reads as pinned."""
    session_pins.pins_path(tmp_path).write_text(
        json.dumps({"pins": {"demo": {"session_id": ""}}, "pins_format": 1}),
        encoding="utf-8",
    )
    with pytest.raises(session_pins.PinStoreError):
        session_pins.read_all(tmp_path)


def test_upsert_then_get_roundtrips_including_the_workspace(tmp_path: Path):
    session_pins.upsert(
        "teams-check",
        session_id="teams-check-1",
        session_workspace="-home-x",
        created_by=session_pins.CREATED_BY_RUN,
        runs_dir=tmp_path,
    )
    pin = session_pins.get("teams-check", runs_dir=tmp_path)
    assert pin is not None
    assert pin.session_id == "teams-check-1"
    # Load-bearing: this is what makes a cwd move DETECTABLE instead of a
    # silent cold start (_SessionProbe.WORKSPACE_MISMATCH).
    assert pin.session_workspace == "-home-x"
    assert pin.created_by == session_pins.CREATED_BY_RUN


def test_upsert_does_not_disturb_other_pins(tmp_path: Path):
    for slug in ("a", "b", "c"):
        session_pins.upsert(
            slug,
            session_id=f"{slug}-1",
            session_workspace="-w",
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=tmp_path,
        )
    session_pins.upsert(
        "b",
        session_id="b-2",
        session_workspace="-w",
        created_by=session_pins.CREATED_BY_RUN,
        runs_dir=tmp_path,
    )
    pins = session_pins.read_all(tmp_path)
    assert {s: p.session_id for s, p in pins.items()} == {
        "a": "a-1",
        "b": "b-2",
        "c": "c-1",
    }


def test_delete_returns_the_abandoned_pin(tmp_path: Path):
    """Rotation must never destroy the only record of the id it abandoned."""
    session_pins.upsert(
        "demo",
        session_id="demo-1",
        session_workspace="-w",
        created_by=session_pins.CREATED_BY_RUN,
        runs_dir=tmp_path,
    )
    removed = session_pins.delete("demo", runs_dir=tmp_path)
    assert removed is not None and removed.session_id == "demo-1"
    assert session_pins.get("demo", runs_dir=tmp_path) is None
    assert session_pins.delete("demo", runs_dir=tmp_path) is None


def test_orphans_are_the_set_difference(tmp_path: Path):
    for slug in ("live", "renamed-away"):
        session_pins.upsert(
            slug,
            session_id=f"{slug}-1",
            session_workspace="-w",
            created_by=session_pins.CREATED_BY_RUN,
            runs_dir=tmp_path,
        )
    assert session_pins.orphans(tmp_path, known_slugs={"live"}) == ["renamed-away"]
    assert session_pins.orphans(tmp_path, known_slugs={"live", "renamed-away"}) == []


# ---------------------------------------------------------- the parser ----


def test_a_pin_key_in_frontmatter_is_refused_by_name(tmp_path: Path):
    path = tmp_path / "teams-check.md"
    path.write_text(_PINNED, encoding="utf-8")
    with pytest.raises(AutomationError) as exc:
        load(path)
    problem = str(exc.value)
    # Refusal, not tolerate-and-ignore -- and it must say what to do, or the
    # operator is left guessing why a file that worked yesterday broke.
    assert "session" in problem
    assert "remove the line" in problem


def test_session_workspace_alone_is_also_refused(tmp_path: Path):
    path = tmp_path / "x.md"
    path.write_text(
        _CLEAN.replace(
            "  enabled: true", "  session_workspace: -home-x\n  enabled: true"
        ),
        encoding="utf-8",
    )
    with pytest.raises(AutomationError) as exc:
        load(path)
    assert "session_workspace" in str(exc.value)


def test_a_clean_automation_still_parses(tmp_path: Path):
    path = tmp_path / "teams-check.md"
    path.write_text(_CLEAN, encoding="utf-8")
    a = load(path)
    assert a.slug == "teams-check"
    assert not hasattr(a, "session")


# ------------------------------------------- the runner's write posture ----


class TestRunAbortsWhenThePinCannotBeRecorded(unittest.TestCase):
    """The council's non-negotiable: abort loudly, never warn-and-proceed."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.runs_dir = self.tmp_path / "runs"
        self.runs_dir.mkdir()
        path = self.tmp_path / "teams-check.md"
        path.write_text(_CLEAN, encoding="utf-8")
        self.automation = load(path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_failed_pin_write_aborts_the_run(self) -> None:
        with (
            mock.patch(
                "drumbeat.session_pins.upsert",
                side_effect=session_pins.PinStoreError("disk full"),
            ),
            mock.patch("drumbeat.runner._execute_turn") as turn,
        ):
            result = runner.run(
                self.automation,
                cwd=self.tmp_path,
                runs_dir=self.runs_dir,
                prompts_dir=self.tmp_path / "prompts",
            )

        self.assertTrue(result.failed)
        assert result.error is not None
        self.assertIn("ABORTING", result.error)
        self.assertIn("disk full", result.error)
        # The load-bearing half: it aborted BEFORE doing any work. A run that
        # did the work and then lost the conversation is the silent fork.
        turn.assert_not_called()


# ---------------------------------------------------------- the CLI ------


def test_store_verbs_accept_data_dir():
    """Section 5, round-3 nit 3: the workspace/data-dir pairing is non-default.

    A defaulting rotate at cutover opens the PACKET's empty store and prints
    ten exit-0 "no pinned session to clear" no-ops -- the exact "rotates
    nothing, exits 0" shape this verb exists to replace.
    """
    parser = cli.build_parser()
    for argv in (
        [
            "rotate-session",
            "demo",
            "--workspace",
            "/w",
            "--data-dir",
            "/d",
            "--reason",
            "r",
        ],
        ["sessions", "--workspace", "/w", "--data-dir", "/d"],
    ):
        args = parser.parse_args(argv)
        assert args.data_dir == "/d", argv


def test_rotate_session_requires_a_reason():
    """A rotation that cannot say why is indistinguishable from an accident."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rotate-session", "demo", "--workspace", "/w"])


def test_rotating_nothing_exits_nonzero(tmp_path: Path, capsys):
    """Class 2 with a receipt, closed: exit 0 must not mean 'rotated nothing'."""
    (tmp_path / "automations").mkdir()
    rc = cli.main(
        [
            "rotate-session",
            "never-pinned",
            "--workspace",
            str(tmp_path),
            "--reason",
            "drill",
        ]
    )
    assert rc == 1
    assert "NOTHING WAS ROTATED" in capsys.readouterr().err
