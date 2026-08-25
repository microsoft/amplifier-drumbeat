"""An automation's surfaced "last run" must report the real
FAILURE time when a run dies from an *unexpected* exception, never a stale
prior SUCCESS.

The gap this guards (confirmed on disk before the fix):

``management_api._iter_run_records`` -- the read path behind the ``last_run``
field served by ``list_automations``/``get_automation_detail`` -- consults
ONLY each run's ``result.json``. Every failure path *inside* ``runner.run``
already persists one (``failed: true``). But an exception that ESCAPES all of
that handling used to leave no ``result.json`` at all: the scheduler recorded
it only in memory, and the management API's "run now" wrote a ``status.json``
that ``_iter_run_records`` ignores. So a run that died that way left the
automation's surfaced "last run" pointing at its previous success -- a failing
automation reading as healthy, its real failure time recorded nowhere the app
looks.

``runner.run`` now persists a canonical failed ``result.json`` for such an
escaped exception (at the one place ``run_id``/``started_at`` are known) and
THEN re-raises -- fail loud, but no longer silent about the record. These tests
drive the REAL production functions (``runner.run``, ``runner.run_automation``
via ``management_api.run_automation_async``, ``management_api.list_automations``)
and construct only on-disk inputs, same discipline as
``test_run_status_surface.py``.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from drumbeat import runner
from drumbeat.automation import load as load_automation
from drumbeat.management_api import (
    EngineContext,
    get_automation_detail,
    list_automations,
    run_automation_async,
)

_AUTOMATION_TEXT = """---
automation:
  name: Demo
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
---

1. First step.
"""

_SLUG = "demo"

# Deliberately far in the past so that a run minted "now" (real wall clock)
# always sorts strictly after it, with no dependence on the test's own timing.
_PRIOR_SUCCESS_STARTED = "2020-01-01T00:00:00Z"


def _workspace(root: Path) -> EngineContext:
    for sub in ("automations", "prompts", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "automations" / f"{_SLUG}.md").write_text(
        _AUTOMATION_TEXT, encoding="utf-8"
    )
    return EngineContext(
        automations_dir=root / "automations",
        prompts_dir=root / "prompts",
        runs_dir=root / "runs",
        cwd=root,
    )


def _write_prior_success(ctx: EngineContext) -> None:
    """A canonical, older, SUCCESSFUL result.json -- the record that used to
    be served as ``last_run`` even after a newer run had failed."""
    ok_dir = ctx.runs_dir / _SLUG / "20200101T000000Z"
    ok_dir.mkdir(parents=True)
    (ok_dir / "result.json").write_text(
        json.dumps(
            {
                "run_id": "20200101T000000Z",
                "automation": "Demo",
                "session_id": "demo-old",
                "started_at": _PRIOR_SUCCESS_STARTED,
                "finished_at": "2020-01-01T00:00:05Z",
                "failed": False,
                "error": None,
                "steps": [],
            }
        ),
        encoding="utf-8",
    )


class TestEscapedExceptionIsRecordedAsLatestFailure(unittest.TestCase):
    """``runner.run`` itself -- the canonical home shared by the scheduler and
    the manual "run now" path -- must record an escaped exception as a failed
    run so every read path sees it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = _workspace(Path(self._tmp.name))
        self.automation = load_automation(self.ctx.automations_dir / f"{_SLUG}.md")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_prior_success_is_superseded_by_the_escaped_failure(self) -> None:
        _write_prior_success(self.ctx)

        boom = RuntimeError("provider library blew up mid-run")
        # Force an exception that escapes run()'s internal fail-loud aborts:
        # resolving the pinned session is the very first thing the body does.
        # Fail loud is preserved: the original exception still propagates.
        with (
            mock.patch.object(runner.session_pins, "get", side_effect=boom),
            self.assertRaises(RuntimeError),
        ):
            runner.run(
                self.automation,
                cwd=self.ctx.cwd,
                runs_dir=self.ctx.runs_dir,
                prompts_dir=self.ctx.prompts_dir,
            )

        # ...but it is now ALSO recorded. The run-status API reports the
        # FAILURE as the most recent run, not the earlier success.
        entry = next(e for e in list_automations(self.ctx) if e["slug"] == _SLUG)
        last_run = entry["last_run"]
        self.assertIsNotNone(last_run)
        assert last_run is not None  # for type-checkers
        self.assertTrue(
            last_run["failed"],
            f"stale success surfaced instead of the failure: {last_run}",
        )
        self.assertNotEqual(last_run["started_at"], _PRIOR_SUCCESS_STARTED)
        self.assertIsNotNone(last_run["finished_at"])
        self.assertIn("provider library blew up", last_run["error"] or "")

        # Both surfaces agree (detail endpoint shares the same computation).
        detail_last_run = get_automation_detail(_SLUG, self.ctx)["last_run"]
        self.assertEqual(detail_last_run, last_run)

    def test_first_ever_run_failing_is_recorded_not_lost(self) -> None:
        """No prior success at all: an escaped exception on the first run must
        still leave a real, failed last_run -- not ``None`` (which reads as
        'never run')."""
        boom = RuntimeError("kaboom before any turn")
        with (
            mock.patch.object(runner.session_pins, "get", side_effect=boom),
            self.assertRaises(RuntimeError),
        ):
            runner.run(
                self.automation,
                cwd=self.ctx.cwd,
                runs_dir=self.ctx.runs_dir,
                prompts_dir=self.ctx.prompts_dir,
            )

        entry = next(e for e in list_automations(self.ctx) if e["slug"] == _SLUG)
        last_run = entry["last_run"]
        self.assertIsNotNone(last_run)
        assert last_run is not None
        self.assertTrue(last_run["failed"])
        self.assertIn("kaboom before any turn", last_run["error"] or "")

    def test_dry_run_never_writes_a_record(self) -> None:
        """A dry run that raises must not fabricate a persisted failure -- a
        preview is not a run."""
        boom = RuntimeError("boom during preview")
        with (
            mock.patch.object(runner.session_pins, "get", side_effect=boom),
            self.assertRaises(RuntimeError),
        ):
            runner.run(
                self.automation,
                cwd=self.ctx.cwd,
                runs_dir=self.ctx.runs_dir,
                prompts_dir=self.ctx.prompts_dir,
                dry_run=True,
            )

        entry = next(e for e in list_automations(self.ctx) if e["slug"] == _SLUG)
        self.assertIsNone(entry["last_run"])


class TestManualRunNowSurfacesTheFailureTime(unittest.TestCase):
    """The manual "run now" surface end to end: a background run that dies
    from an escaped exception must leave the automation's ``last_run``
    reporting THAT failure, not a stale prior success."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = _workspace(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _await_result(self, run_id: str, *, timeout_s: float = 5.0) -> Path:
        run_dir = self.ctx.runs_dir / _SLUG / run_id
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if (run_dir / "result.json").is_file():
                return run_dir
            time.sleep(0.02)
        raise AssertionError(
            f"the failed manual run left no canonical result.json in {run_dir} "
            f"within {timeout_s}s -- its failure time is unrecorded"
        )

    def test_background_failure_records_a_canonical_result(self) -> None:
        _write_prior_success(self.ctx)

        # Force the failure INSIDE runner.run (not in run_automation_async's
        # own pin lookup) by making the run body raise; the real run() wrapper
        # then persists the canonical failed record.
        with mock.patch.object(
            runner, "_run_body", side_effect=RuntimeError("worker died")
        ):
            started = run_automation_async(_SLUG, self.ctx)
            run_dir = self._await_result(started["run_id"])
        self.assertTrue((run_dir / "result.json").is_file())

        entry = next(e for e in list_automations(self.ctx) if e["slug"] == _SLUG)
        last_run = entry["last_run"]
        self.assertIsNotNone(last_run)
        assert last_run is not None
        self.assertEqual(last_run["run_id"], started["run_id"])
        self.assertTrue(
            last_run["failed"],
            f"stale success surfaced instead of the failure: {last_run}",
        )
        self.assertNotEqual(last_run["started_at"], _PRIOR_SUCCESS_STARTED)
        self.assertIn("worker died", last_run["error"] or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
