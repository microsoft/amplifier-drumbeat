"""A manual run must be TRACKABLE, in every state a run can be in.

The bug, reported from the field on 2026-08-17 and reproduced below: pressing
"Run Now" on any automation and watching the run produced

    run 20260817T233501Z-4a00af: tracking failed -- required field 'automation_name' is absent

for EVERY automation, while the same automation's "Last run" line rendered
fine. The run itself was not necessarily failing -- the client could not
parse the run-status document it polls, so it could not track the run at all
and reported that as a failure of the run.

Why the two disagreed: the client decodes a run record with ONE strict
decoder, used for both the history list and the single-run poll (the API client's
``AutomationsApi.swift`` -- ``RunSummary.init(raw:)``, hard-requiring
``run_id``, ``automation``, ``automation_name``, ``failed`` and ``notified``;
``runs(slug:limit:)`` at the list route and ``runDetail(slug:runId:)`` at
``GET /v1/automations/<slug>/runs/<run_id>``, which the gateway proxies
verbatim to this service's ``GET /api/runs/<slug>/<run_id>``). Engine-side,
only the LIST assembled that shape. ``get_run_detail`` served the raw on-disk
bookkeeping document:

* ``status.json`` (in flight) has no ``automation_name``, no ``failed``,
  no ``notified`` -- THREE required fields absent.
* ``result.json`` (finished) has no ``automation_name``, and carries the
  display NAME under ``automation`` where the list carries the SLUG.

So the list decoded and the poll threw -- exactly the split the owner saw.

``_StrictRunRecordDecoder`` below is that client decoder's parse rule,
transcribed: same five required keys, same "required field 'x' is absent"
message. Every test drives the REAL production functions the client's two
requests reach -- ``management_api.run_automation_async`` (the "Run Now"
POST) and ``management_api.get_run_detail`` / ``list_runs`` (the polls) --
and decodes the actual returned payload through it.

Only inputs are constructed. ``result.json`` fixtures are built by
``dataclasses.asdict`` over a real ``runner.RunResult``, the same call
``runner._persist_run`` makes (``runner.py:3925``), so a fixture cannot
drift from the shape production writes.

``test_the_on_disk_documents_still_lack_the_field`` is the anti-vacuity
guard (law 6: a check that cannot fail proves nothing): it asserts the
underlying files STILL do not carry ``automation_name``, so the passing
assertions above it can only be passing because the endpoint supplies it.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from drumbeat import management_api, runner
from drumbeat.management_api import (
    EngineContext,
    get_run_detail,
    list_runs,
    run_automation_async,
)

_AUTOMATION_TEXT = """---
automation:
  name: Calendar Check
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
  steps:
    - id: first-step
      prompt: First step.
---
"""

_SLUG = "calendar-check"
_NAME = "Calendar Check"

# Transcribed from the API client's `AutomationsApi.swift` `RunSummary.init(raw:)`
# -- the five keys read through `.required(...)` rather than `jsonOptional`.
_CLIENT_REQUIRED_FIELDS = (
    "run_id",
    "automation",
    "automation_name",
    "failed",
    "notified",
)


class MissingRequiredField(Exception):
    """The client's decode failure, verbatim in wording.

    `AutosView.swift` renders this as
    ``run <id>: tracking failed -- <error>`` -- the string the owner saw.
    """


class _StrictRunRecordDecoder:
    """The client's run-record parse rule. Absent or wrong-typed => throw.

    Deliberately as unforgiving as the real one: `absent != null != wrong
    type` (a shared pin). A present-but-null required field is a decode
    failure too, not a tolerated blank -- serving `automation_name: null`
    would break the client exactly as omitting it does.
    """

    @staticmethod
    def decode(raw: dict[str, Any]) -> dict[str, Any]:
        for key in _CLIENT_REQUIRED_FIELDS:
            if key not in raw:
                raise MissingRequiredField(f"required field {key!r} is absent")
            if raw[key] is None:
                raise MissingRequiredField(f"required field {key!r} is null")
        for key in ("run_id", "automation", "automation_name"):
            if not isinstance(raw[key], str) or not raw[key]:
                raise MissingRequiredField(
                    f"required field {key!r} is not a non-empty string: {raw[key]!r}"
                )
        for key in ("failed", "notified"):
            if not isinstance(raw[key], bool):
                raise MissingRequiredField(
                    f"required field {key!r} is not a bool: {raw[key]!r}"
                )
        return raw

    @staticmethod
    def phase(raw: dict[str, Any]) -> str:
        """`RunPhase.phase(for:)` -- landed vs running, from timestamps only.

        Never consults ``failed``; that is why ``failed: False`` on a run
        still in flight cannot be misread as success.
        """
        if raw.get("finished_at"):
            return "landed"
        if raw.get("started_at"):
            return "running"
        return "queued"


class _InlineThread:
    """A ``threading.Thread`` stand-in that runs the target on ``start()``.

    Makes the background half of ``run_automation_async`` deterministic
    without a sleep: by the time ``run_automation_async`` returns, the run
    has already reached its terminal state.
    """

    def __init__(
        self, target: Callable[[], None], name: str = "", daemon: bool = False
    ) -> None:
        self._target = target

    def start(self) -> None:
        self._target()


class _NeverStartsThread:
    """A ``threading.Thread`` stand-in that never runs the target.

    Freezes a run at the synchronously-written "starting" record -- the
    exact document a client polling immediately after the 202 reads first.
    """

    def __init__(
        self, target: Callable[[], None], name: str = "", daemon: bool = False
    ) -> None:
        self._target = target

    def start(self) -> None:
        return None


class _RunTrackingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for sub in ("automations", "prompts", "runs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "automations" / f"{_SLUG}.md").write_text(
            _AUTOMATION_TEXT, encoding="utf-8"
        )
        self.ctx = EngineContext(
            automations_dir=self.root / "automations",
            prompts_dir=self.root / "prompts",
            runs_dir=self.root / "runs",
            cwd=self.root,
        )
        self._restore: list[tuple[Any, str, Any]] = []

    def tearDown(self) -> None:
        for obj, attr, original in reversed(self._restore):
            setattr(obj, attr, original)
        self._tmp.cleanup()

    def _patch(self, obj: Any, attr: str, value: Any) -> None:
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def _write_result_json(
        self,
        run_id: str,
        *,
        failed: bool,
        notified: bool,
        final_reply: str = "nothing on the calendar in the next two hours",
    ) -> None:
        """A faithful ``result.json``: ``asdict`` over a real ``RunResult``."""
        result = runner.RunResult(
            automation=_NAME,
            run_id=run_id,
            session_id=f"{_SLUG}-{run_id}",
            started_at="2026-08-17T23:35:01Z",
            finished_at="2026-08-17T23:35:44Z",
            steps=[],
            final_reply=final_reply,
            notified=notified,
            failed=failed,
        )
        run_dir = self.ctx.runs_dir / _SLUG / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(
            json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8"
        )


class TestTheClientCanTrackAManualRun(_RunTrackingTestCase):
    """The reported bug, in each state a manual run passes through."""

    def test_a_just_started_run_decodes(self) -> None:
        """The exact document the client polls first, after the 202.

        Before the fix this document was served raw and was missing THREE
        required fields (``automation_name``, ``failed``, ``notified``) --
        the client threw on the first one and reported "tracking failed".
        """
        self._patch(management_api.threading, "Thread", _NeverStartsThread)
        self._patch(runner, "run", lambda *a, **k: None)

        accepted = run_automation_async(_SLUG, self.ctx)
        polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)

        self.assertEqual(polled["status"], "starting")
        record = _StrictRunRecordDecoder.decode(polled)
        self.assertEqual(record["automation_name"], _NAME)
        self.assertEqual(record["automation"], _SLUG)
        self.assertEqual(record["run_id"], accepted["run_id"])
        self.assertFalse(record["failed"])
        self.assertFalse(record["notified"])
        # In flight, and readable as such -- not mistaken for a success.
        self.assertEqual(_StrictRunRecordDecoder.phase(record), "running")

    def test_an_in_flight_run_decodes(self) -> None:
        """The "running" record, written by the real background thread."""
        released = threading.Event()
        reached = threading.Event()

        def _blocking_run(*_args: Any, **_kwargs: Any) -> None:
            reached.set()
            released.wait(timeout=30)

        self._patch(runner, "run", _blocking_run)
        accepted = run_automation_async(_SLUG, self.ctx)
        try:
            self.assertTrue(reached.wait(timeout=30), "background run never started")
            polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)
            self.assertEqual(polled["status"], "running")
            record = _StrictRunRecordDecoder.decode(polled)
            self.assertEqual(record["automation_name"], _NAME)
            self.assertEqual(record["automation"], _SLUG)
            self.assertFalse(record["failed"])
            self.assertEqual(_StrictRunRecordDecoder.phase(record), "running")
        finally:
            released.set()

    def test_a_completed_run_decodes(self) -> None:
        """``result.json`` -- missing ``automation_name`` before the fix."""
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _succeeding_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(kwargs["run_id"], failed=False, notified=True)

        self._patch(runner, "run", _succeeding_run)

        accepted = run_automation_async(_SLUG, self.ctx)
        polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)

        record = _StrictRunRecordDecoder.decode(polled)
        self.assertEqual(record["automation_name"], _NAME)
        self.assertEqual(record["automation"], _SLUG)
        self.assertFalse(record["failed"])
        self.assertTrue(record["notified"])
        self.assertEqual(_StrictRunRecordDecoder.phase(record), "landed")
        # The run's own content still rides along untouched.
        self.assertEqual(
            polled["final_reply"], "nothing on the calendar in the next two hours"
        )

    def test_a_completed_but_failed_run_decodes_and_reads_as_failed(self) -> None:
        """A genuinely failed run must be tracked as FAILED, not as untrackable.

        The two were indistinguishable to the owner before this: every run
        read "tracking failed" whether it succeeded or not.
        """
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _failing_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(kwargs["run_id"], failed=True, notified=False)

        self._patch(runner, "run", _failing_run)

        accepted = run_automation_async(_SLUG, self.ctx)
        record = _StrictRunRecordDecoder.decode(
            get_run_detail(_SLUG, accepted["run_id"], self.ctx)
        )
        self.assertTrue(record["failed"])
        self.assertEqual(record["automation_name"], _NAME)
        self.assertEqual(_StrictRunRecordDecoder.phase(record), "landed")

    def test_a_run_that_dies_before_writing_a_result_decodes_and_lands(self) -> None:
        """The background thread raised: no ``result.json`` ever exists.

        Two things must hold. The status record must decode (it carried
        none of the three fields before). And it must LAND -- without a
        ``finished_at`` the client reads "running..." until its own poll
        ceiling expires minutes later, and the real error is never shown.
        """
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _raising_run(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("amplifier-agent not found on PATH")

        self._patch(runner, "run", _raising_run)

        accepted = run_automation_async(_SLUG, self.ctx)
        polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)

        self.assertEqual(polled["status"], "failed")
        record = _StrictRunRecordDecoder.decode(polled)
        self.assertEqual(record["automation_name"], _NAME)
        self.assertTrue(record["failed"])
        self.assertEqual(_StrictRunRecordDecoder.phase(record), "landed")
        self.assertIn("amplifier-agent not found on PATH", polled["error"])

    def test_started_at_does_not_creep_forward_across_status_writes(self) -> None:
        """One run, one start time -- not "whenever we last wrote a status".

        Each of the three status writes used to stamp a fresh ``_iso_now()``
        under ``started_at``, so a run's recorded start time crept forward
        to its most recent write: the elapsed time a client computes from it
        drifted toward zero as the run went on, and the failure record
        always claimed the run started at the instant it died.

        The clock is replaced with a counter so the creep is visible rather
        than hidden behind three writes that land in the same wall-clock
        second. Writes here are starting(T1) -> running(T2) -> failed(T3).
        """
        ticks = iter(
            [
                "2026-08-17T23:35:01Z",
                "2026-08-17T23:35:02Z",
                "2026-08-17T23:35:03Z",
                "2026-08-17T23:35:04Z",
            ]
        )
        self._patch(management_api, "_iso_now", lambda: next(ticks))
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _raising_run(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("amplifier-agent not found on PATH")

        self._patch(runner, "run", _raising_run)
        accepted = run_automation_async(_SLUG, self.ctx)

        polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)
        # The FIRST tick -- when the run actually started. Before the fix
        # the starting/running/failed writes each took their own tick, so
        # this read back as the THIRD one.
        self.assertEqual(polled["started_at"], "2026-08-17T23:35:01Z")
        # And exactly two clock reads happen across a whole run now -- one
        # when it starts, one when it ends -- so the finish is the SECOND
        # tick, not the fourth.
        self.assertEqual(polled["finished_at"], "2026-08-17T23:35:02Z")
        self.assertGreater(polled["finished_at"], polled["started_at"])


class TestFinalReplyPreviewExposesDeadBrainReplies(_RunTrackingTestCase):
    """The dead-brain outage class: a run can exit 0 (``failed: False``)
    while its reply is itself a statement of failure ("Error: No providers
    available"), and a consumer reading only ``failed``/``notified`` off the
    run-record contract has no way to tell. ``final_reply_preview`` closes
    that on every surface that carries the run-record contract -- the runs
    API row (list and detail) and the per-automation ``last_run`` summary.
    """

    def test_a_completed_run_carries_a_bounded_preview(self) -> None:
        self._patch(management_api.threading, "Thread", _InlineThread)
        long_reply = "Error: No providers available. " * 20  # > 200 chars

        def _succeeding_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(
                kwargs["run_id"], failed=False, notified=True, final_reply=long_reply
            )

        self._patch(runner, "run", _succeeding_run)

        accepted = run_automation_async(_SLUG, self.ctx)
        polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)

        self.assertIn("final_reply_preview", polled)
        preview = polled["final_reply_preview"]
        self.assertTrue(preview.startswith("Error: No providers available."))
        self.assertLessEqual(len(preview), 200)
        # The dead-brain signal survives truncation -- a consumer scanning
        # only the preview can still classify this run as suspicious even
        # though ``failed`` reads False.
        self.assertIn("Error: No providers available", preview)
        # The detail endpoint's full, untouched reply still rides along too
        # (unchanged behavior) -- the preview is additive, not a replacement.
        self.assertEqual(polled["final_reply"], long_reply)

        listed = list_runs(limit=10, automation_filter=_SLUG, ctx=self.ctx)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["final_reply_preview"], preview)
        # The listing must never carry the raw, unbounded reply -- only the
        # bounded preview derived from it.
        self.assertNotIn("final_reply", listed[0])

    def test_a_run_aborted_before_any_turn_gets_the_no_reply_marker(self) -> None:
        """``final_reply == ""`` (the empty-but-real, aborted-before-any-turn
        case) must read as the explicit marker, never an empty string a
        consumer could misread as "the model replied with nothing".
        """
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _aborted_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(
                kwargs["run_id"], failed=True, notified=False, final_reply=""
            )

        self._patch(runner, "run", _aborted_run)

        accepted = run_automation_async(_SLUG, self.ctx)
        polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)
        self.assertEqual(polled["final_reply_preview"], management_api._NO_REPLY_MARKER)

        listed = list_runs(limit=10, automation_filter=_SLUG, ctx=self.ctx)
        self.assertEqual(
            listed[0]["final_reply_preview"], management_api._NO_REPLY_MARKER
        )

    def test_an_in_flight_run_gets_the_no_reply_marker(self) -> None:
        """``status.json`` has no ``final_reply`` key at all -- the marker,
        not a KeyError and not an empty string.
        """
        released = threading.Event()
        reached = threading.Event()

        def _blocking_run(*_args: Any, **_kwargs: Any) -> None:
            reached.set()
            released.wait(timeout=30)

        self._patch(runner, "run", _blocking_run)
        accepted = run_automation_async(_SLUG, self.ctx)
        try:
            self.assertTrue(reached.wait(timeout=30), "background run never started")
            polled = get_run_detail(_SLUG, accepted["run_id"], self.ctx)
            self.assertEqual(polled["status"], "running")
            self.assertEqual(
                polled["final_reply_preview"], management_api._NO_REPLY_MARKER
            )
        finally:
            released.set()

    def test_last_run_summary_also_carries_the_preview(self) -> None:
        """The per-automation ``last_run`` field (``list_automations``) is
        the other surface a doctor-style consumer scans across every
        automation at once -- it must carry the same signal.
        """
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _succeeding_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(
                kwargs["run_id"],
                failed=False,
                notified=True,
                final_reply="Error: No providers available",
            )

        self._patch(runner, "run", _succeeding_run)
        run_automation_async(_SLUG, self.ctx)

        automations = management_api.list_automations(self.ctx)
        entry = next(a for a in automations if a["slug"] == _SLUG)
        self.assertEqual(
            entry["last_run"]["final_reply_preview"], "Error: No providers available"
        )


class TestBothEndpointsAgree(_RunTrackingTestCase):
    """One decoder parses both routes, so both must serve one shape."""

    def test_list_and_detail_serve_the_same_identity_for_the_same_run(self) -> None:
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _succeeding_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(kwargs["run_id"], failed=False, notified=True)

        self._patch(runner, "run", _succeeding_run)
        accepted = run_automation_async(_SLUG, self.ctx)

        listed = _StrictRunRecordDecoder.decode(
            list_runs(limit=50, automation_filter=_SLUG, ctx=self.ctx)[0]
        )
        detail = _StrictRunRecordDecoder.decode(
            get_run_detail(_SLUG, accepted["run_id"], self.ctx)
        )

        for key in _CLIENT_REQUIRED_FIELDS:
            self.assertEqual(
                listed[key],
                detail[key],
                f"{key!r} disagrees between the list and the detail endpoint",
            )
        # `automation` is the slug on BOTH. It used to be the slug in the
        # list and the display name in the detail -- one field, two meanings.
        self.assertEqual(detail["automation"], _SLUG)
        self.assertEqual(detail["automation_name"], _NAME)


class TestTheNameIsRealNeverAPlaceholder(_RunTrackingTestCase):
    """`automation_name` must be the automation's actual name."""

    def test_a_renamed_automation_keeps_the_name_the_run_actually_had(self) -> None:
        """The persisted name wins over the current on-disk one.

        A run that happened under the old name is reported under the old
        name -- the record is what happened, not what the file says today.
        """
        self._write_result_json("20260817T233501Z-4a00af", failed=False, notified=True)
        (self.root / "automations" / f"{_SLUG}.md").write_text(
            _AUTOMATION_TEXT.replace("name: Calendar Check", "name: Diary Sweep"),
            encoding="utf-8",
        )
        record = _StrictRunRecordDecoder.decode(
            get_run_detail(_SLUG, "20260817T233501Z-4a00af", self.ctx)
        )
        self.assertEqual(record["automation_name"], _NAME)

    def test_a_record_with_no_persisted_name_resolves_it_from_disk(self) -> None:
        """Second authority: the automation itself. Still a real name."""
        run_dir = self.ctx.runs_dir / _SLUG / "20260817T233501Z-4a00af"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(
            json.dumps(
                {
                    "run_id": "20260817T233501Z-4a00af",
                    "session_id": "s-1",
                    "started_at": "2026-08-17T23:35:01Z",
                    "finished_at": "2026-08-17T23:35:44Z",
                    "failed": False,
                    "notified": False,
                    "steps": [],
                }
            ),
            encoding="utf-8",
        )
        record = _StrictRunRecordDecoder.decode(
            get_run_detail(_SLUG, "20260817T233501Z-4a00af", self.ctx)
        )
        self.assertEqual(record["automation_name"], _NAME)

    def test_an_unnameable_run_is_a_loud_500_never_a_placeholder(self) -> None:
        """No name anywhere: say so. Do not invent one, do not serve null.

        A stand-in string here would be indistinguishable to the client
        from a real name, and would hide a corrupt record forever.
        """
        run_dir = self.ctx.runs_dir / "ghost" / "20260817T233501Z-4a00af"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text(
            json.dumps({"run_id": "20260817T233501Z-4a00af", "failed": False}),
            encoding="utf-8",
        )
        with self.assertRaises(management_api.ManagementError) as caught:
            get_run_detail("ghost", "20260817T233501Z-4a00af", self.ctx)
        self.assertEqual(caught.exception.status, 500)
        self.assertIn("automation_name", caught.exception.message)

    def test_an_unnameable_run_is_skipped_by_the_listing_not_500(self) -> None:
        """One corrupt record must not take out the whole history view.

        Skipped, not served with a null name -- a null would break the
        client's decode of the entire list, which is the same outage in a
        different costume.
        """
        self._write_result_json("20260817T233501Z-4a00af", failed=False, notified=True)
        ghost_dir = self.ctx.runs_dir / "ghost" / "20260817T233502Z-000000"
        ghost_dir.mkdir(parents=True)
        (ghost_dir / "result.json").write_text(
            json.dumps(
                {
                    "run_id": "20260817T233502Z-000000",
                    "failed": False,
                    "started_at": "2026-08-17T23:35:02Z",
                }
            ),
            encoding="utf-8",
        )

        served = list_runs(limit=50, automation_filter=None, ctx=self.ctx)

        self.assertEqual([r["automation"] for r in served], [_SLUG])
        for raw in served:
            _StrictRunRecordDecoder.decode(raw)


class TestTheGuardIsNotVacuous(_RunTrackingTestCase):
    """Law 6: a check that cannot fail proves nothing."""

    def test_the_on_disk_documents_still_lack_the_field(self) -> None:
        """The endpoint supplies ``automation_name``; the files never had it.

        If this ever fails, the assertions above stopped proving anything --
        they would be passing because the fixture handed them the field.
        """
        self._patch(management_api.threading, "Thread", _InlineThread)

        def _succeeding_run(*_args: Any, **kwargs: Any) -> None:
            self._write_result_json(kwargs["run_id"], failed=False, notified=True)

        self._patch(runner, "run", _succeeding_run)
        accepted = run_automation_async(_SLUG, self.ctx)
        run_dir = self.ctx.runs_dir / _SLUG / accepted["run_id"]

        on_disk = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertNotIn("automation_name", on_disk)
        with self.assertRaises(MissingRequiredField):
            _StrictRunRecordDecoder.decode(on_disk)

        self.assertIn(
            "automation_name", get_run_detail(_SLUG, accepted["run_id"], self.ctx)
        )

    def test_the_in_flight_document_lacks_three_required_fields(self) -> None:
        """``status.json`` was missing ``automation_name`` AND the two bools.

        Proves a fix that added only ``automation_name`` would have moved
        the owner's error message to ``'failed' is absent``, not cleared it.
        """
        self._patch(management_api.threading, "Thread", _NeverStartsThread)
        self._patch(runner, "run", lambda *a, **k: None)
        accepted = run_automation_async(_SLUG, self.ctx)
        status_path = self.ctx.runs_dir / _SLUG / accepted["run_id"] / "status.json"

        on_disk = json.loads(status_path.read_text(encoding="utf-8"))
        for absent in ("automation_name", "failed", "notified"):
            self.assertNotIn(absent, on_disk)
        with self.assertRaises(MissingRequiredField):
            _StrictRunRecordDecoder.decode(on_disk)

        _StrictRunRecordDecoder.decode(
            get_run_detail(_SLUG, accepted["run_id"], self.ctx)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
