"""Guidance reaches the agent by REFERENCE, not by inlining it into the turn.

Drumbeat prefers to inject the workspace-relative guidance PATHS plus a
mandatory read-first preamble and let the agent load the bodies with its own
file tools, rather than embedding every required guidance body verbatim into the
turn text. Two reasons survive the move to the isolated per-turn worker (the
prompt now travels to the worker on stdin, so the old OS argv ceiling that this
feature also guarded against is gone): the turn stays small (cheaper, faster),
and the agent reads the CURRENT on-disk body rather than a snapshot frozen at
inject time. Legacy inline delivery stays selectable per-automation.

Every test drives the real production functions directly -- no mocking of the
logic under test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from drumbeat.automation import (
    DEFAULT_GUIDANCE_DELIVERY,
    AutomationError,
    load_from_text,
)
from drumbeat.runner import (
    RequirementCheck,
    RunnerError,
    _build_command,
    check_requirements,
    format_requirements_turn,
)

# A body big enough that inlining it would blow past MAX_ARG_STRLEN on its own.
# The marker line is what a live agent quotes back to prove it actually read the
# file (see EVIDENCE/rig-atmention.py); here it proves the reference turn names
# the file without ever carrying its bytes.
_MARKER = "DRUMBEAT-ATMENTION-MARKER-51d0"
_HUGE_BODY = f"# Guidance\n{_MARKER}\n" + ("filler line to add weight.\n" * 12000)


def _file_check(item: str, content: str) -> RequirementCheck:
    """A satisfied file-kind requirement, exactly as check_requirements emits."""
    return RequirementCheck(
        item=item, kind="file", satisfied=True, detail=f"/abs/{item}", content=content
    )


# ---- reference mode: names the file, never carries its body ----------------


def test_reference_mode_injects_path_and_read_preamble_not_body() -> None:
    check = _file_check("guidance/IDENTITY.md", _HUGE_BODY)
    text = format_requirements_turn([check], mode="reference")
    assert text is not None
    # The path is named...
    assert "guidance/IDENTITY.md" in text
    # ...the agent is told to read it first, with its file tools...
    assert "read" in text.lower()
    assert "before" in text.lower()
    # ...and the body is NOT present.
    assert _MARKER not in text
    assert "filler line to add weight." not in text


def test_reference_mode_stays_tiny_regardless_of_body_size() -> None:
    """The whole point: argv weight is decoupled from guidance size."""
    checks = [
        _file_check("guidance/IDENTITY.md", _HUGE_BODY),
        _file_check("guidance/ATTENTION.md", _HUGE_BODY),
        _file_check("guidance/TEAMS.md", _HUGE_BODY),
    ]
    total_body = sum(len(c.content or "") for c in checks)
    assert total_body > 128 * 1024, (
        "test bodies must exceed the ceiling to be meaningful"
    )

    text = format_requirements_turn(checks, mode="reference")
    assert text is not None
    # Far under the ceiling even though the referenced bodies are far over it.
    assert len(text.encode("utf-8")) < 2000
    # Every file is listed.
    for c in checks:
        assert c.item in text


def test_reference_mode_lists_every_required_file() -> None:
    checks = [
        _file_check("guidance/A.md", "alpha"),
        _file_check("guidance/B.md", "bravo"),
    ]
    text = format_requirements_turn(checks, mode="reference")
    assert text is not None
    assert "- guidance/A.md" in text
    assert "- guidance/B.md" in text


# ---- inline mode: legacy behaviour, still selectable -----------------------


def test_inline_mode_embeds_the_body_verbatim() -> None:
    check = _file_check("guidance/IDENTITY.md", "body-line-one\nbody-line-two")
    text = format_requirements_turn([check], mode="inline")
    assert text is not None
    assert "guidance/IDENTITY.md" in text
    assert "body-line-one" in text
    assert "body-line-two" in text


def test_default_mode_is_reference() -> None:
    check = _file_check("guidance/IDENTITY.md", _HUGE_BODY)
    default_text = format_requirements_turn([check])
    reference_text = format_requirements_turn([check], mode="reference")
    assert default_text == reference_text
    assert default_text is not None
    assert _MARKER not in default_text


def test_unknown_mode_fails_loud() -> None:
    check = _file_check("guidance/IDENTITY.md", "x")
    with pytest.raises(RunnerError, match="mode must be one of"):
        format_requirements_turn([check], mode="inlined")  # typo, not a real mode


def test_nothing_to_inject_returns_none_in_both_modes() -> None:
    assert format_requirements_turn([], mode="reference") is None
    assert format_requirements_turn([], mode="inline") is None
    # An executable-only check contributes no file guidance and no card here.
    exe = RequirementCheck(item="example-cli", kind="executable", satisfied=True, detail="/x")
    assert format_requirements_turn([exe], mode="reference") is None


# ---- the prompt travels via stdin, never argv ------------------------------


def test_build_command_never_carries_the_prompt_in_argv() -> None:
    """The worker command is fixed and small; the prompt reaches the worker on
    stdin, so a turn's text -- at ANY size -- never appears on argv and there is
    no OS per-argument ceiling to breach."""
    huge = "x" * (256 * 1024)  # far past the old 128 KiB argv ceiling
    cmd = _build_command(session_id="s1", fresh=True, cwd=Path("/tmp"), text=huge)
    # It is exactly `python -m drumbeat.agent_worker` -- no prompt, no session id.
    assert cmd == [sys.executable, "-m", "drumbeat.agent_worker"]
    assert huge not in cmd
    assert "s1" not in cmd


def test_build_command_marker_is_present_for_live_turn_detection() -> None:
    """drain/staleness match a live turn by the worker's dotted module name in
    /proc/<pid>/cmdline; the built command must carry it."""
    cmd = _build_command(session_id="s1", fresh=True, cwd=Path("/tmp"), text="hi")
    assert "drumbeat.agent_worker" in " ".join(cmd)


def test_reference_turn_stays_small_at_any_body_size() -> None:
    """Reference form keeps the turn tiny regardless of how large the guidance
    bodies are -- the property the worker's stdin transport no longer *needs*
    but the feature still deliberately provides."""
    checks = [_file_check(f"guidance/F{i}.md", _HUGE_BODY) for i in range(20)]
    text = format_requirements_turn(checks, mode="reference")
    assert text is not None
    assert len(text.encode("utf-8")) < 4000


# ---- automation.guidance_delivery field ------------------------------------

_AUTOMATION_TEMPLATE = """\
---
automation:
  name: Drill
  enabled: false
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: never
{delivery_line}\
  requires:
    - guidance/IDENTITY.md
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""


def _automation_text(delivery: str | None) -> str:
    line = "" if delivery is None else f"  guidance_delivery: {delivery}\n"
    return _AUTOMATION_TEMPLATE.format(delivery_line=line)


def test_guidance_delivery_defaults_to_reference() -> None:
    a = load_from_text(Path("drill.md"), _automation_text(None))
    assert a.guidance_delivery == DEFAULT_GUIDANCE_DELIVERY == "reference"


def test_guidance_delivery_inline_parses() -> None:
    a = load_from_text(Path("drill.md"), _automation_text("inline"))
    assert a.guidance_delivery == "inline"


def test_guidance_delivery_reference_parses() -> None:
    a = load_from_text(Path("drill.md"), _automation_text("reference"))
    assert a.guidance_delivery == "reference"


def test_guidance_delivery_unknown_value_refuses() -> None:
    with pytest.raises(AutomationError, match="guidance_delivery must be one of"):
        load_from_text(Path("drill.md"), _automation_text("inlined"))


# ---- the pre-run gate still fails loud on a missing/empty file --------------


def test_missing_guidance_file_is_still_an_unsatisfied_requirement(
    tmp_path: Path,
) -> None:
    """Referencing (not inlining) must NOT weaken the gate: a missing file is
    still caught BEFORE any turn runs, exactly as before."""
    runs = tmp_path / "runs"
    runs.mkdir()
    checks = check_requirements(
        ["guidance/DOES_NOT_EXIST.md"], cwd=tmp_path, runs_dir=runs
    )
    assert len(checks) == 1
    assert checks[0].satisfied is False
    assert "file not found" in checks[0].detail


def test_empty_guidance_file_is_still_an_unsatisfied_requirement(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (tmp_path / "guidance").mkdir()
    (tmp_path / "guidance" / "EMPTY.md").write_text("   \n", encoding="utf-8")
    checks = check_requirements(["guidance/EMPTY.md"], cwd=tmp_path, runs_dir=runs)
    assert checks[0].satisfied is False
    assert "empty" in checks[0].detail


def test_gate_reads_the_file_then_reference_turn_names_it(tmp_path: Path) -> None:
    """End to end at the seam: the gate reads+verifies the body (fail-loud
    intact), then the reference turn names the path without carrying the body."""
    runs = tmp_path / "runs"
    runs.mkdir()
    (tmp_path / "guidance").mkdir()
    (tmp_path / "guidance" / "IDENTITY.md").write_text(_HUGE_BODY, encoding="utf-8")

    checks = check_requirements(["guidance/IDENTITY.md"], cwd=tmp_path, runs_dir=runs)
    assert checks[0].satisfied is True
    assert checks[0].content is not None and _MARKER in checks[0].content

    text = format_requirements_turn(checks, mode="reference")
    assert text is not None
    assert "guidance/IDENTITY.md" in text
    assert _MARKER not in text  # body verified at the gate, never placed in argv
    assert len(text.encode("utf-8")) < 2000
