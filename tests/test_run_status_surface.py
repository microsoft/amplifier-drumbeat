"""Wiring two computed-but-never-served run-health mechanisms into the
management API and the CLI.

Both mechanisms were fully computed before this change and served nowhere:

Mechanism 2 -- "last run" must be the latest ATTEMPT, with its outcome.
    ``management_api._iter_run_records`` (reference:
    ``~/dev/amplifier-attention-manager/drumbeat/src/drumbeat/management_api.py:644``)
    already reads every persisted ``result.json`` and sorts newest-attempt-
    first, but ``_automation_summary`` served only file-derived fields --
    ``list_automations``/``get_automation_detail`` served NO last-run
    information at all, leaving a client to compute (or mis-compute) it
    itself. The exact failure this guards: a failing automation whose
    reported "last run" is silently its last SUCCESS, which reads as
    healthy.

Mechanism 4 -- consecutive-failure count must be visible.
    ``session_health._scan_recent_runs`` (reference:
    ``~/dev/amplifier-attention-manager/drumbeat/src/drumbeat/session_health.py:368``)
    computes the consecutive-failure count and a ceiling-hit verdict for a
    pinned session, but ``session_health.health_for`` -- built on top of it
    -- had ZERO callers anywhere in src/ before this change (grep-provable).
    A repeatedly-failing automation therefore read as merely "failed once"
    everywhere it was surfaced.

Same discipline as ``test_soft_launch_gates.py``: real production functions
are driven directly (``management_api.list_automations``,
``management_api.get_automation_detail``, ``cli.build_parser`` +
``session_health.run_health``/``health_for``). Only inputs are
constructed -- a temp workspace and real ``result.json`` files written to
disk in the exact shape ``runner._persist_run`` writes them (see
``runner.py:3881-3888`` and ``tests/test_run_identity_and_data_dir.py`` for
the confirmed on-disk shape).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from drumbeat import session_health, session_pins
from drumbeat.management_api import (
    EngineContext,
    get_automation_detail,
    list_automations,
)

_AUTOMATION_TEXT = """---
automation:
  name: Demo
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
  steps:
    - id: first-step
      prompt: First step.
    - id: second-step
      prompt: Second step.
---
"""

_SLUG = "demo"


def _workspace(root: Path) -> Path:
    for sub in ("automations", "prompts", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _ctx(root: Path) -> EngineContext:
    return EngineContext(
        automations_dir=root / "automations",
        prompts_dir=root / "prompts",
        runs_dir=root / "runs",
        cwd=root,
    )


def _write_automation(root: Path, *, text: str = _AUTOMATION_TEXT) -> None:
    (root / "automations" / f"{_SLUG}.md").write_text(text, encoding="utf-8")


def _write_run(
    runs_dir: Path,
    run_id: str,
    *,
    session_id: str,
    started_at: str,
    finished_at: str,
    failed: bool,
    error: str | None = None,
) -> None:
    """Write a real ``result.json`` in the exact shape ``runner._persist_run``
    produces (see ``runner.py:3881-3888``): ``run_id``, ``session_id``,
    ``started_at``, ``finished_at``, ``failed``, ``error``.
    """
    run_dir = runs_dir / _SLUG / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "automation": "Demo",
                "session_id": session_id,
                "started_at": started_at,
                "finished_at": finished_at,
                "failed": failed,
                "error": error,
                "steps": [],
            }
        ),
        encoding="utf-8",
    )


def _pin(slug: str, session_id: str, *, runs_dir: Path) -> None:
    session_pins.upsert(
        slug,
        session_id=session_id,
        session_workspace=None,
        created_by=session_pins.CREATED_BY_RUN,
        runs_dir=runs_dir,
    )


def _entry(entries: list[dict[str, Any]], slug: str) -> dict[str, Any]:
    for entry in entries:
        if entry["slug"] == slug:
            return entry
    raise AssertionError(f"no entry for slug {slug!r} in {entries!r}")


class TestLastRunIsTheLatestAttempt(unittest.TestCase):
    """Mechanism 2: the served ``last_run`` must be the latest ATTEMPT, not
    a cached/filtered last SUCCESS.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _workspace(Path(self._tmp.name))
        _write_automation(self.root)
        self.ctx = _ctx(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_later_failure_supersedes_an_earlier_success(self) -> None:
        """The stale-last-run regression, as a test.

        An EARLIER successful run and a LATER failed run exist for the same
        automation. The served ``last_run`` must be the FAILED one -- its
        timestamp, its failed=True, and its error message -- never the
        earlier success's timestamp. This is the exact bug: a failing
        automation displaying its last success and looking healthy.
        """
        _write_run(
            self.ctx.runs_dir,
            "20260807T000000Z",
            session_id="demo-1",
            started_at="2026-08-07T00:00:00Z",
            finished_at="2026-08-07T00:00:05Z",
            failed=False,
        )
        _write_run(
            self.ctx.runs_dir,
            "20260807T010000Z",
            session_id="demo-1",
            started_at="2026-08-07T01:00:00Z",
            finished_at="2026-08-07T01:00:05Z",
            failed=True,
            error="provider refused the prompt",
        )

        entry = _entry(list_automations(self.ctx), _SLUG)
        last_run = entry["last_run"]
        assert last_run is not None
        self.assertEqual(last_run["started_at"], "2026-08-07T01:00:00Z")
        self.assertTrue(last_run["failed"])
        self.assertEqual(last_run["error"], "provider refused the prompt")
        # Explicitly not the earlier success's timestamp.
        self.assertNotEqual(last_run["started_at"], "2026-08-07T00:00:00Z")

    def test_get_automation_detail_serves_the_same_last_run_fields(self) -> None:
        """Both endpoints share the fix -- not just the list view."""
        _write_run(
            self.ctx.runs_dir,
            "20260807T000000Z",
            session_id="demo-1",
            started_at="2026-08-07T00:00:00Z",
            finished_at="2026-08-07T00:00:05Z",
            failed=False,
        )
        _write_run(
            self.ctx.runs_dir,
            "20260807T010000Z",
            session_id="demo-1",
            started_at="2026-08-07T01:00:00Z",
            finished_at="2026-08-07T01:00:05Z",
            failed=True,
            error="boom",
        )

        detail = get_automation_detail(_SLUG, self.ctx)
        last_run = detail["last_run"]
        assert last_run is not None
        self.assertEqual(last_run["started_at"], "2026-08-07T01:00:00Z")
        self.assertTrue(last_run["failed"])
        self.assertEqual(last_run["error"], "boom")

    def test_never_run_automation_serves_last_run_none(self) -> None:
        """No fabricated entry for an automation that has never run."""
        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertIsNone(entry["last_run"])


class TestConsecutiveFailureCountIsVisible(unittest.TestCase):
    """Mechanism 4: a repeatedly-failing automation must read as visibly
    degraded/dead, not merely "failed once".
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _workspace(Path(self._tmp.name))
        _write_automation(self.root)
        self.ctx = _ctx(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_consecutive_failures_on_one_session_are_dead(self) -> None:
        for i, run_id in enumerate(
            ("20260807T000000Z", "20260807T010000Z", "20260807T020000Z")
        ):
            _write_run(
                self.ctx.runs_dir,
                run_id,
                session_id="demo-1",
                started_at=f"2026-08-07T0{i}:00:00Z",
                finished_at=f"2026-08-07T0{i}:00:05Z",
                failed=True,
                error="boom",
            )
        _pin(_SLUG, "demo-1", runs_dir=self.ctx.runs_dir)

        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertEqual(entry["consecutive_failures"], 3)
        self.assertEqual(entry["session_status"], "dead")

    def test_a_newer_success_resets_the_consecutive_counter(self) -> None:
        _write_run(
            self.ctx.runs_dir,
            "20260807T000000Z",
            session_id="demo-1",
            started_at="2026-08-07T00:00:00Z",
            finished_at="2026-08-07T00:00:05Z",
            failed=True,
            error="boom",
        )
        _write_run(
            self.ctx.runs_dir,
            "20260807T010000Z",
            session_id="demo-1",
            started_at="2026-08-07T01:00:00Z",
            finished_at="2026-08-07T01:00:05Z",
            failed=True,
            error="boom",
        )
        _write_run(
            self.ctx.runs_dir,
            "20260807T020000Z",
            session_id="demo-1",
            started_at="2026-08-07T02:00:00Z",
            finished_at="2026-08-07T02:00:05Z",
            failed=False,
        )
        _pin(_SLUG, "demo-1", runs_dir=self.ctx.runs_dir)

        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["session_status"], "healthy")

    def test_exactly_one_recent_failure_is_degraded(self) -> None:
        """Proves the three states are distinguishable, not just healthy/dead."""
        _write_run(
            self.ctx.runs_dir,
            "20260807T000000Z",
            session_id="demo-1",
            started_at="2026-08-07T00:00:00Z",
            finished_at="2026-08-07T00:00:05Z",
            failed=True,
            error="boom",
        )
        _pin(_SLUG, "demo-1", runs_dir=self.ctx.runs_dir)

        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertEqual(entry["consecutive_failures"], 1)
        self.assertEqual(entry["session_status"], "degraded")

    def test_a_predecessors_failures_are_not_charged_to_a_fresh_pin(self) -> None:
        """The guard already inside ``_scan_recent_runs`` must survive the wrapper.

        Failures recorded under an OLD session_id must not count against a
        freshly-rotated pin -- a rotation that still reports the automation
        as degraded/dead is indistinguishable from a rotation that did not
        work (see ``session_health._scan_recent_runs``'s own docstring).
        """
        for i, run_id in enumerate(
            ("20260807T000000Z", "20260807T010000Z", "20260807T020000Z")
        ):
            _write_run(
                self.ctx.runs_dir,
                run_id,
                session_id="demo-OLD",
                started_at=f"2026-08-07T0{i}:00:00Z",
                finished_at=f"2026-08-07T0{i}:00:05Z",
                failed=True,
                error="boom",
            )
        _pin(_SLUG, "demo-NEW", runs_dir=self.ctx.runs_dir)

        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["session_status"], "healthy")

    def test_unpinned_automation_is_healthy_not_unknown(self) -> None:
        """Nothing is wrong with an automation that has simply never been pinned."""
        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertEqual(entry["consecutive_failures"], 0)
        self.assertEqual(entry["session_status"], "healthy")


class TestCorruptPinStoreIsUnknownNotHealthy(unittest.TestCase):
    """Hard constraint: fail loud, but never let one corrupt file 500 the
    whole listing, and never let it masquerade as "healthy" either.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _workspace(Path(self._tmp.name))
        _write_automation(self.root)
        self.ctx = _ctx(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_corrupt_store_reports_unknown_and_does_not_raise(self) -> None:
        session_pins.pins_path(self.ctx.runs_dir).parent.mkdir(
            parents=True, exist_ok=True
        )
        session_pins.pins_path(self.ctx.runs_dir).write_text(
            "{not json", encoding="utf-8"
        )

        entry = _entry(list_automations(self.ctx), _SLUG)
        self.assertEqual(entry["session_status"], "unknown")
        self.assertEqual(entry["consecutive_failures"], 0)


class TestRunHealthWrapper(unittest.TestCase):
    """Direct coverage of ``session_health.run_health`` -- the public wrapper
    that makes the state rule (healthy/degraded/dead) reusable outside
    ``management_api``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ceiling_hit_is_dead_even_with_one_failure(self) -> None:
        run_dir = self.runs_dir / _SLUG / "20260807T000000Z"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(
            json.dumps(
                {
                    "run_id": "20260807T000000Z",
                    "session_id": "demo-1",
                    "started_at": "2026-08-07T00:00:00Z",
                    "finished_at": "2026-08-07T00:00:05Z",
                    "failed": True,
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "stderr.log").write_text(
            "prompt is too long: 219685 tokens > 200000 maximum", encoding="utf-8"
        )

        health = session_health.run_health(
            _SLUG, session_id="demo-1", runs_dir=self.runs_dir
        )
        self.assertEqual(health.consecutive_failures, 1)
        self.assertIsNotNone(health.ceiling_hit)
        self.assertEqual(health.state, "dead")

    def test_no_runs_is_healthy(self) -> None:
        health = session_health.run_health(
            _SLUG, session_id="demo-1", runs_dir=self.runs_dir
        )
        self.assertEqual(health.consecutive_failures, 0)
        self.assertIsNone(health.ceiling_hit)
        self.assertEqual(health.state, "healthy")


class TestSessionHealthCliSubcommand(unittest.TestCase):
    """Makes ``session_health.health_for`` reachable at all -- see cli.py's
    ``_cmd_session_health``. Before this command existed, ``health_for`` had
    zero callers anywhere in src/.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = _workspace(Path(self._tmp.name))
        _write_automation(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_registered_and_accepts_workspace_and_data_dir(self) -> None:
        from drumbeat import cli

        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "session-health",
                "--workspace",
                str(self.root),
                "--data-dir",
                str(self.root / "runs"),
            ]
        )
        self.assertEqual(args.command, "session-health")
        self.assertIs(args.func, cli._cmd_session_health)

    def test_prints_a_row_for_the_unpinned_automation(self) -> None:
        import io
        from contextlib import redirect_stdout

        from drumbeat import cli

        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "session-health",
                "--workspace",
                str(self.root),
                "--data-dir",
                str(self.root / "runs"),
            ]
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli._cmd_session_health(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("Demo", buf.getvalue())
        self.assertIn("CONSEC FAILS", buf.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
