"""The status a typing turn shows is richer than "working" AND truthful.

``_TurnProgressTracker`` is the whole richer-status mechanism: it consumes
amplifier-agent's NDJSON event stream and emits a ``ProgressEvent`` per
recognized event, so a text thread can show "Thinking…", a consumer-declared
tool phrase, or a safe generic fallback instead of an opaque spinner.

These tests pin the properties demanded together:

  RICHER   -- the sequence moves through thinking and per-tool activities, and
              ``step`` advances on every event so the turn reads as alive.
  TRUTHFUL -- the activity NEVER echoes a raw command / session id / argument;
              ``tool`` is set only while a tool runs and cleared otherwise; and
              an event type the tracker does not recognize is dropped, never
              guessed at (no fabricated activity, no phantom step).

The tool phrasing is MECHANISM, NOT POLICY: the engine hardcodes no consumer
vocabulary. A drumpack declares an ``activity:`` map for its own tools'
subcommands (``drumbeat.packs``); the tracker consumes that map. An undeclared
tool -- or an undeclared subcommand, or no map at all -- falls back to a generic
phrase that still never leaks the raw command. Delete the drumpack-driven
lookup, the thinking/tool transitions, or the unknown-event guard and a test
here goes red.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from drumbeat import packs
from drumbeat.runner import ProgressEvent, _TurnProgressTracker

# A stand-in for a consumer drumpack's declared narration: program name ->
# {subcommand: label}. No engine code knows these words -- the drumpack owns
# them. `acme` is a stand-in consumer CLI, not anything the engine ships.
_ACTIVITY: dict[str, dict[str, str]] = {
    "acme": {
        "calendar": "Checking your calendar…",
        "mail": "Checking email…",
    }
}


def _ndjson(method: str, **params) -> str:
    return json.dumps({"method": method, "params": params})


def _drive(
    lines: list[str],
    activity_by_tool: dict[str, dict[str, str]] | None = None,
) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    tracker = _TurnProgressTracker(events.append, activity_by_tool)
    for line in lines:
        tracker.observe_line(line)
    return events


# A realistic, live-shaped turn: think, check the calendar, check mail, answer.
_LIVE_SHAPED = [
    _ndjson("thinking/delta", text="Let me look at the schedule"),
    _ndjson("tool/started", name="bash", args={"command": "acme calendar --json"}),
    _ndjson("tool/completed", name="bash"),
    _ndjson("thinking/delta", text="now the inbox"),
    _ndjson("tool/started", name="bash", args={"command": "acme mail --unread"}),
    _ndjson("tool/completed", name="bash"),
    _ndjson("result/delta", text="You have"),
    _ndjson("result/final", text="You have 2 meetings and 1 unread email."),
]


def test_declared_activity_narrates_through_thinking_and_tools() -> None:
    events = _drive(_LIVE_SHAPED, _ACTIVITY)
    activities = [e.activity for e in events]
    assert activities == [
        "Thinking…",
        "Checking your calendar…",
        "Checking your calendar…",  # tool/completed closes out, activity held
        "Thinking…",
        "Checking email…",
        "Checking email…",  # tool/completed
        "Checking email…",  # result/delta narrates, activity unchanged
        "Checking email…",  # result/final
    ]


def test_step_advances_on_every_recognized_event() -> None:
    events = _drive(_LIVE_SHAPED, _ACTIVITY)
    # Monotonic, one per recognized line, starting at 1 -- the liveness signal.
    assert [e.step for e in events] == list(range(1, len(_LIVE_SHAPED) + 1))


def test_tool_is_set_while_a_tool_runs_and_cleared_otherwise() -> None:
    events = _drive(_LIVE_SHAPED, _ACTIVITY)
    tools = [e.tool for e in events]
    assert tools == [
        None,  # thinking
        "bash",  # tool/started
        "bash",  # tool/completed keeps the last-known tool label
        None,  # thinking clears it
        "bash",  # tool/started
        "bash",  # tool/completed
        "bash",  # result/delta (activity/tool unchanged from last tool)
        "bash",  # result/final
    ]


def test_activity_never_leaks_the_raw_command() -> None:
    # THE truthfulness guarantee: no part of the raw command string (which could
    # be a session id, chat id, or full command line) ever reaches the activity.
    for event in _drive(_LIVE_SHAPED, _ACTIVITY):
        assert "acme" not in event.activity
        assert "--json" not in event.activity
        assert "--unread" not in event.activity


def test_undeclared_tool_falls_back_to_generic_without_leaking() -> None:
    # A tool no drumpack declares narrates generically -- and STILL never leaks
    # the command, because the fallback is a fixed literal, not the command.
    (event,) = _drive(
        [
            _ndjson(
                "tool/started",
                name="bash",
                args={"command": "mystery-cli secret-token"},
            )
        ],
        _ACTIVITY,
    )
    assert event.activity == "Running a command…"
    assert "mystery-cli" not in event.activity
    assert "secret-token" not in event.activity


def test_declared_tool_undeclared_subcommand_falls_back_to_generic() -> None:
    # The drumpack declares `acme` but not the `teleport` subcommand -- generic,
    # never a guess.
    (event,) = _drive(
        [_ndjson("tool/started", name="bash", args={"command": "acme teleport"})],
        _ACTIVITY,
    )
    assert event.activity == "Running a command…"


def test_no_activity_map_still_narrates_generically() -> None:
    # The engine ships zero consumer vocabulary: with no drumpack map at all, a
    # CLI invocation is a generic "Running a command…", never a leak.
    (event,) = _drive(
        [_ndjson("tool/started", name="bash", args={"command": "acme calendar"})]
    )
    assert event.activity == "Running a command…"


def test_thinking_sets_thinking_and_no_tool() -> None:
    (event,) = _drive([_ndjson("thinking/final", text="pondering")])
    assert event.activity == "Thinking…"
    assert event.tool is None


def test_error_event_surfaces_the_message_without_a_tool() -> None:
    (event,) = _drive([_ndjson("error", message="provider refused")])
    assert event.activity == "Error: provider refused"
    assert event.tool is None


def test_unknown_event_type_is_dropped_never_guessed() -> None:
    # A method outside the canonical 9-type taxonomy must produce NO event and
    # NOT advance the step -- "don't count it, don't guess".
    events = _drive([_ndjson("telemetry/heartbeat", note="ignore me")])
    assert events == []


def test_unparseable_line_is_ignored() -> None:
    assert _drive(["not json at all", "", "{ broken"]) == []


def test_a_generic_builtin_tool_gets_a_human_phrase() -> None:
    # read_file is the ENGINE's own tool, generic to every consumer -- its phrase
    # lives in the engine, not in any drumpack.
    (event,) = _drive(
        [_ndjson("tool/started", name="read_file", args={"path": "/etc/x"})]
    )
    assert event.activity == "Reading a file…"
    assert event.tool == "read_file"
    assert "/etc/x" not in event.activity


# ---- end-to-end: a real drumpack's declared labels reach the narration ----


def _write_drumpack(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "drumpack.md").write_text(
        "---\n"
        "pack_format: 1\n"
        "name: acme\n"
        "description: a test drumpack that declares its own activity labels\n"
        "tools:\n"
        "  - acme\n"
        "activity:\n"
        '  calendar: "Checking your calendar…"\n'
        '  mail: "Checking email…"\n'
        "---\n\n"
        "# acme\n\nThe acme test drumpack.\n",
        encoding="utf-8",
    )
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / "acme"
    tool.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory.resolve()


def test_a_declared_drumpack_narrates_its_own_label_end_to_end(tmp_path) -> None:
    # Acceptance shape: a workspace with drumpacks.txt + a drumpack.md declaring
    # activity labels -> the tool RESOLVES and the DECLARED label narrates.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pack_dir = _write_drumpack(workspace / "acme-pack")
    (workspace / "drumpacks.txt").write_text(f"{pack_dir}\n", encoding="utf-8")

    loaded = packs.load_workspace_packs(workspace)
    assert loaded[0].activity == {
        "calendar": "Checking your calendar…",
        "mail": "Checking email…",
    }
    # "tools resolve": the declared tool is found on the turn PATH.
    assert packs.resolve_tool("acme", workspace, loaded) is not None

    activity_by_tool = packs.activity_by_tool(loaded)
    (event,) = _drive(
        [
            _ndjson(
                "tool/started", name="bash", args={"command": "acme calendar --json"}
            )
        ],
        activity_by_tool,
    )
    assert event.activity == "Checking your calendar…"
    assert "acme" not in event.activity
