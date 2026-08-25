"""The invalid-run sweep: runs the engine failed to account for.

Section 6 states the invariant this enforces:

    **A run record without a delivery-intent event is an invalid run.**

That is the closing move on failure class 1 -- "ran, delivered nothing,
silently." Before the seam existed, three independent gates could each zero
a run's output with no record that they had. Now every run must end with
exactly one classified, reasoned delivery intent: deliver, withhold, or
demote. A run that finished without one is not "a run that decided to stay
quiet"; it is a run whose decision was never recorded, which is
indistinguishable from a run whose output was lost.

This sweep also reports the other shape of the same problem: a run that
never reached a terminal state at all -- ``status.json`` says running, no
``result.json`` ever appeared. That is what a process killed mid-turn leaves
behind, which makes this sweep the natural verification step after any
scheduler handoff (section 12 step 3, gate 5).

FAIL LOUD: findings are returned as findings. This module never repairs,
never deletes, and never decides that an unaccounted run was "probably
fine."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drumbeat import engine_events

# Files at the top of runs/ that are state, not per-automation run dirs.
_NON_AUTOMATION_ENTRIES = frozenset({".session-locks", "webapp", "withheld"})


@dataclass(frozen=True)
class InvalidRun:
    """One run that the engine cannot fully account for."""

    automation: str
    run_id: str
    path: str
    problem: str
    started_at: str | None = None
    finished_at: str | None = None


def _parse_run_id_time(run_id: str) -> datetime | None:
    """The UTC-second prefix of a run id, or None -- never a guess.

    Run ids are ``YYYYMMDDTHHMMSSZ-<6 hex>`` since the collision fix; history
    holds the older bare ``YYYYMMDDTHHMMSSZ`` form. Both parse here, because
    the timestamp is a fixed-width *prefix* in both.

    Why this must parse the prefix rather than the whole string: unparseable
    ids are deliberately fail-open-inclusive below (a run id this function
    cannot date is always *examined*, never skipped). So a whole-string parse
    would not have broken loudly against new ids -- it would have returned
    None for every one of them, and ``--since`` would have **silently become a
    no-op**, quietly widening every windowed sweep to the whole history while
    reporting itself as windowed. Silent degradation, not a visible break,
    which is why the prefix parse ships in the same commit as the new mint.
    """
    stamp = run_id.split("-", 1)[0]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def run_ids_with_delivery_intent(runs_dir: Path) -> set[str]:
    """Every run id the outbox carries a ``delivery_intent`` for.

    Reads from byte 0 deliberately: this is an audit, not a consumer, so it
    has no cursor and must not advance anyone else's.
    """
    events, _ = engine_events.read_since(Path(runs_dir).expanduser(), 0)
    found: set[str] = set()
    for event in events:
        if event.event_type is not engine_events.EventType.DELIVERY_INTENT:
            continue
        if event.run_id:
            found.add(event.run_id)
    return found


def sweep(
    runs_dir: Path,
    *,
    since: datetime | None = None,
) -> list[InvalidRun]:
    """Find every run in the window that the engine cannot account for.

    ``since``: when given, only runs whose id timestamp is at or after it are
    examined -- the cut-over window, typically. A run id that cannot be
    parsed as a timestamp is **always** examined rather than skipped: the
    alternative is a sweep that silently ignores exactly the malformed cases
    it exists to surface.
    """
    runs_dir = Path(runs_dir).expanduser()
    findings: list[InvalidRun] = []
    if not runs_dir.is_dir():
        return findings

    intents = run_ids_with_delivery_intent(runs_dir)

    for automation_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        if automation_dir.name in _NON_AUTOMATION_ENTRIES:
            continue
        for run_dir in sorted(p for p in automation_dir.iterdir() if p.is_dir()):
            run_id = run_dir.name
            when = _parse_run_id_time(run_id)
            if since is not None and when is not None and when < since:
                continue

            result_path = run_dir / "result.json"
            status_path = run_dir / "status.json"

            if not result_path.is_file():
                status = _read_json(status_path) if status_path.is_file() else None
                if status is None:
                    findings.append(
                        InvalidRun(
                            automation=automation_dir.name,
                            run_id=run_id,
                            path=str(run_dir),
                            problem=(
                                "no result.json and no readable status.json -- the run "
                                "directory exists but nothing recorded what happened"
                            ),
                        )
                    )
                    continue
                state = str(status.get("status", "<unrecorded>"))
                if state in {"failed", "error"}:
                    continue  # terminal, and honestly recorded as such
                findings.append(
                    InvalidRun(
                        automation=automation_dir.name,
                        run_id=run_id,
                        path=str(run_dir),
                        started_at=status.get("started_at"),
                        problem=(
                            f"NON-TERMINAL: status.json says {state!r} and no "
                            "result.json was ever written -- this run's process "
                            "died mid-turn or is still in flight"
                        ),
                    )
                )
                continue

            result = _read_json(result_path)
            if result is None:
                findings.append(
                    InvalidRun(
                        automation=automation_dir.name,
                        run_id=run_id,
                        path=str(run_dir),
                        problem="result.json exists but is unreadable/corrupt",
                    )
                )
                continue

            if run_id not in intents:
                findings.append(
                    InvalidRun(
                        automation=automation_dir.name,
                        run_id=run_id,
                        path=str(run_dir),
                        started_at=result.get("started_at"),
                        finished_at=result.get("finished_at"),
                        problem=(
                            "INVALID: run completed but the outbox carries no "
                            "delivery_intent for it -- its delivery decision was "
                            "never recorded (section 6)"
                        ),
                    )
                )

    return findings


def render(findings: list[InvalidRun], *, since: datetime | None = None) -> str:
    window = (
        f" since {since.strftime('%Y-%m-%dT%H:%M:%SZ')}" if since else " (all time)"
    )
    if not findings:
        return f"invalid-run sweep{window}: CLEAN — 0 findings"
    lines = [f"invalid-run sweep{window}: {len(findings)} finding(s)"]
    for finding in findings:
        lines.append(f"  {finding.automation}/{finding.run_id}: {finding.problem}")
        lines.append(f"    {finding.path}")
    return "\n".join(lines)


__all__ = ["InvalidRun", "render", "run_ids_with_delivery_intent", "sweep"]
