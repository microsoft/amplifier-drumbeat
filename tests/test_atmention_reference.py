"""Guidance reaches the agent by REFERENCE, not by inlining it into argv.

Drumbeat used to embed every required guidance body verbatim in the turn text,
which is the last argv element handed to ``amplifier-agent``. A single argv
element over Linux's ``MAX_ARG_STRLEN`` (131072 bytes = 128 KiB) fails
``execve`` with E2BIG -- silently, before the agent boots, with no run record.
That is exactly how channels-check died (IDENTITY.md was inlined).

The fix, verified against the real installed ``amplifier-agent`` binary (which
does NOT auto-load @-mentions in turn text -- see EVIDENCE/verify-atmention.md):
inject the workspace-relative guidance PATHS plus a mandatory read-first
preamble, and let the agent load the bodies with its own file tools. argv stays
a few hundred bytes no matter how large the guidance grows. Legacy inline
delivery stays selectable per-automation during migration, and a turn-size belt
guard turns any breach into a named failure instead of the kernel's opaque
E2BIG.

Every test drives the real production functions directly -- no mocking of the
logic under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drumbeat.automation import (
    DEFAULT_GUIDANCE_DELIVERY,
    AutomationError,
    load_from_text,
)
from drumbeat.runner import (
    MAX_ARG_STRLEN,
    RequirementCheck,
    RunnerError,
    TurnTooLargeError,
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
    exe = RequirementCheck(item="m365", kind="executable", satisfied=True, detail="/x")
    assert format_requirements_turn([exe], mode="reference") is None


# ---- the turn-size belt guard ----------------------------------------------


def test_build_command_rejects_a_turn_at_the_ceiling() -> None:
    text = "x" * MAX_ARG_STRLEN  # exactly the failing size measured on-host
    with pytest.raises(TurnTooLargeError) as excinfo:
        _build_command(session_id="s1", fresh=True, cwd=Path("/tmp"), text=text)
    assert excinfo.value.nbytes == MAX_ARG_STRLEN
    assert "reference" in str(excinfo.value)


def test_build_command_accepts_a_turn_just_under_the_ceiling() -> None:
    text = "x" * (MAX_ARG_STRLEN - 1)  # 131071 execs on-host; 131072 fails
    cmd = _build_command(session_id="s1", fresh=True, cwd=Path("/tmp"), text=text)
    assert cmd[-1] == text


def test_belt_measures_utf8_bytes_not_characters() -> None:
    # A 2-byte char must count as 2 toward the ceiling, or a multibyte turn
    # could slip past the guard and hit E2BIG anyway.
    text = "\u00e9" * (
        MAX_ARG_STRLEN // 2
    )  # 'é' is 2 bytes in UTF-8 -> exactly the limit
    assert len(text) < MAX_ARG_STRLEN  # fewer CHARS than the ceiling...
    with pytest.raises(TurnTooLargeError):  # ...but not fewer BYTES
        _build_command(session_id="s1", fresh=True, cwd=Path("/tmp"), text=text)


def test_reference_turn_is_never_rejected_by_the_belt() -> None:
    """Reference form + the belt = argv is safe by construction, at any body size."""
    checks = [_file_check(f"guidance/F{i}.md", _HUGE_BODY) for i in range(20)]
    text = format_requirements_turn(checks, mode="reference")
    assert text is not None
    # Would raise if it were anywhere near the ceiling; it is not.
    cmd = _build_command(session_id="s1", fresh=True, cwd=Path("/tmp"), text=text)
    assert cmd[-1] == text


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
---

1. Do the thing.
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
