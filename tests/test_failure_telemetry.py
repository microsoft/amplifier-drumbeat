"""Proof that failure telemetry is honestly named, fresh, and never truncated.

Three measured defects, one theme -- a surface that LOOKED healthy while the
thing it described was not:

1. **A name that lied.** ``runs/automation_errors.jsonl`` is a CONFIG-LINT log
   (schema ``{time, path, problem}``, written when an automation FILE fails to
   parse). Its last write was 08-27, because nobody had broken an automation
   file since 08-27. Anything watching a file whose name contained "automation"
   and "errors" therefore read a flatline straight through two days that each
   produced a hundred run failures. Nothing malfunctioned; the name did. The
   file is now ``automation_lint.jsonl``.

2. **Zero-byte run records.** Two ``result.json`` files on the owner's box were
   zero bytes -- runs that died mid-write. A zero-byte record is not a loud
   failure, it is an ABSENCE: every consumer that counts parseable records
   simply does not see that run.

3. **No liveness signal.** Nothing on the health surface said when the REAL
   failure log last moved, so a dead monitoring pipe and a healthy system were
   the same picture.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from drumbeat import cli, error_log, runner
from drumbeat.automation import load
from drumbeat.paths import derive_workspace_slug

_AUTOMATION = """---
automation:
  name: Fleet Check
  enabled: true
  trigger:
    type: manual
  notify: never
  steps:
    - id: do-the-thing
      prompt: Do the thing.
---
"""


class TestTheLintLogIsNamedForWhatItIs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        error_log.set_log_data_dir(self.data_dir)
        self.addCleanup(error_log.set_log_data_dir, Path.cwd() / "runs")

    def test_automation_lint_records_land_in_the_lint_log(self) -> None:
        error_log.log_automation_error(
            Path("/ws/automations/broken.md"), "automation.trigger must be a mapping"
        )
        lint_path = self.data_dir / "automation_lint.jsonl"
        self.assertTrue(lint_path.is_file())
        record = json.loads(lint_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(set(record), {"time", "path", "problem"})
        self.assertIn("broken.md", record["path"])

        # The old name is never written again -- a clean cut, not a dual write
        # that would leave the misleading file looking alive.
        self.assertFalse((self.data_dir / "automation_errors.jsonl").exists())

    def test_the_old_name_is_only_ever_referenced_as_legacy(self) -> None:
        self.assertEqual(error_log.AUTOMATION_LINT_FILENAME, "automation_lint.jsonl")
        self.assertEqual(
            error_log.LEGACY_AUTOMATION_LINT_FILENAME, "automation_errors.jsonl"
        )
        self.assertEqual(
            error_log.automation_lint_path(), self.data_dir / "automation_lint.jsonl"
        )
        self.assertEqual(
            error_log.legacy_automation_lint_path(),
            self.data_dir / "automation_errors.jsonl",
        )

    def test_no_live_code_still_uses_the_old_name(self) -> None:
        """Grep-proven: the rename is applied consistently.

        The invariant is about CODE, not prose. ``error_log.py`` owns the
        legacy constant, and several modules carry post-mortem comments naming
        the old file -- those are the record of why the rename happened and
        must survive. What must not survive is any executable line elsewhere
        that still reaches for the old name.
        """
        repo_root = Path(__file__).resolve().parent.parent
        offenders: list[str] = []
        for path in list((repo_root / "src").rglob("*.py")) + list(
            (repo_root / "tests").rglob("*.py")
        ):
            if path.name in {"error_log.py", "test_failure_telemetry.py"}:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "automation_errors" not in line:
                    continue
                stripped = line.strip()
                if stripped.startswith(("#", '"')):
                    continue  # a comment or docstring line: prose, not a reference
                offenders.append(f"{path.relative_to(repo_root)}:{lineno}: {stripped}")
        self.assertEqual(offenders, [], "live code still uses automation_errors")

    def test_the_repo_documents_both_logs(self) -> None:
        """A reader must be able to learn WHICH file carries run failures and
        WHICH carries config lint without reading the source."""
        docs = Path(__file__).resolve().parent.parent / "docs"
        architecture = (docs / "ARCHITECTURE.md").read_text(encoding="utf-8")
        automations = (docs / "AUTOMATIONS.md").read_text(encoding="utf-8")
        for text in (architecture, automations):
            self.assertIn("failures.log", text)
            self.assertIn("automation_lint.jsonl", text)


class TestRunRecordsAreAtomic(unittest.TestCase):
    """A zero-byte ``result.json`` must be structurally impossible, not merely
    unlikely. The proof is not "the file looks right afterwards" -- it is that
    NO partial state is ever observable AT THE FINAL PATH, which is what a
    consumer reads."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        self.workspace = self.tmp_path / "workspace"
        for sub in ("automations", "guidance", "prompts"):
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
        self.prompts_dir = self.workspace / "prompts"
        self.runs_dir = self.tmp_path / "runs"
        self.runs_dir.mkdir()
        self.agent_home = self.tmp_path / "agent-home"

        env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFIER_AGENT_HOME": str(self.agent_home),
                "AMPLIFIER_AGENT_WORKSPACE": "",
                "CONTEXT_INTELLIGENCE_PERSONAL": "",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

        automation_path = self.workspace / "automations" / "fleet-check.md"
        automation_path.write_text(_AUTOMATION, encoding="utf-8")
        self.automation = load(automation_path)
        self.workspace_slug = derive_workspace_slug(self.workspace)

    def _result_path(self, run_id: str) -> Path:
        return self.runs_dir / self.automation.slug / run_id / "result.json"

    def test_a_write_killed_mid_flight_leaves_no_partial_result_json(self) -> None:
        """Simulate the exact measured shape: the process dies during the
        result.json write. The final path must hold either the previous good
        record or nothing -- never a truncated or zero-byte file."""
        real_write = runner.fsutil.atomic_write
        boom = RuntimeError("killed mid-write")

        def die_on_result_json(path: Path, content: str) -> None:
            if Path(path).name == "result.json":
                # Reproduce what a dying writer really does: the temp file has
                # been created and partially written, and then nothing more.
                partial = Path(path).parent / "half-written.tmp"
                partial.write_text(content[: len(content) // 2], encoding="utf-8")
                raise boom
            real_write(path, content)

        with (
            mock.patch.object(
                runner,
                "_submit_turn",
                return_value=runner._TurnOutcome(reply="ok", duration_ms=1),
            ),
            mock.patch.object(runner.fsutil, "atomic_write", die_on_result_json),
            redirect_stdout(io.StringIO()),
            self.assertRaises(RuntimeError),
        ):
            runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )

        # Whatever run dirs exist, NOT ONE holds a partial result.json.
        for result_path in self.runs_dir.rglob("result.json"):
            raw = result_path.read_text(encoding="utf-8")
            self.assertNotEqual(raw.strip(), "", f"{result_path} is zero-byte")
            json.loads(raw)  # raises if truncated

    def test_a_healthy_run_writes_a_complete_parseable_record(self) -> None:
        with (
            mock.patch.object(
                runner,
                "_submit_turn",
                return_value=runner._TurnOutcome(reply="ok", duration_ms=1),
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )
        raw = self._result_path(result.run_id).read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw)["run_id"], result.run_id)
        self.assertTrue(raw.endswith("\n"))

    def test_no_tmp_files_are_left_behind_by_a_clean_run(self) -> None:
        with (
            mock.patch.object(
                runner,
                "_submit_turn",
                return_value=runner._TurnOutcome(reply="ok", duration_ms=1),
            ),
            redirect_stdout(io.StringIO()),
        ):
            runner.run(
                self.automation,
                cwd=self.workspace,
                runs_dir=self.runs_dir,
                prompts_dir=self.prompts_dir,
            )
        self.assertEqual(list(self.runs_dir.rglob("*.tmp")), [])


class TestFailureLogRecency(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs_dir = Path(self._tmp.name)

    def _write(self, *lines: str) -> None:
        runner.failure_log_path(self.runs_dir).write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )

    def test_absent_log_is_reported_as_absent_not_as_zero_failures(self) -> None:
        status = runner.failure_log_status(self.runs_dir)
        self.assertFalse(status.exists)
        self.assertIsNone(status.problem)
        self.assertIsNone(status.last_entry_at)

    def test_recency_comes_from_the_last_entry(self) -> None:
        older = (datetime.now(UTC) - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        newer = (datetime.now(UTC) - timedelta(minutes=7)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._write(
            f"{older} automation=Old Check run_id=r1 session_id=s1 error='x'",
            f"{newer} automation=Fleet-Check run_id=r2 session_id=s2 error='y'",
        )
        status = runner.failure_log_status(self.runs_dir)
        self.assertTrue(status.exists)
        self.assertIsNone(status.problem)
        self.assertEqual(status.last_entry_at, newer)
        self.assertEqual(status.last_automation, "Fleet-Check")
        assert status.last_entry_age_seconds is not None
        self.assertLess(abs(status.last_entry_age_seconds - 7 * 60), 120)

    def test_an_unreadable_tail_is_a_problem_not_a_silence(self) -> None:
        """Refusing to invent a time is the whole point: an unparseable tail
        must not render as 'no recent failures'."""
        self._write("this line has no timestamp at all")
        status = runner.failure_log_status(self.runs_dir)
        self.assertIsNotNone(status.problem)
        self.assertIsNone(status.last_entry_at)

    def test_an_empty_file_is_a_problem_not_a_clean_bill(self) -> None:
        self._write()
        status = runner.failure_log_status(self.runs_dir)
        self.assertEqual(status.problem, "exists but holds no entries")

    def test_only_the_tail_is_read(self) -> None:
        """A health check must not cost proportional to the history it
        summarizes."""
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        filler = "2026-01-01T00:00:00Z automation=Noise run_id=r session_id=s error='z'"
        runner.failure_log_path(self.runs_dir).write_text(
            "".join(f"{filler}\n" for _ in range(20_000))
            + f"{stamp} automation=Latest run_id=rN session_id=sN error='q'\n",
            encoding="utf-8",
        )
        size = runner.failure_log_path(self.runs_dir).stat().st_size
        self.assertGreater(size, runner._FAILURE_LOG_TAIL_BYTES)
        status = runner.failure_log_status(self.runs_dir)
        self.assertEqual(status.last_entry_at, stamp)
        self.assertEqual(status.last_automation, "Latest")


class TestDoctorNamesTheRealFailureLog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.workspace = self.tmp_path / "ws"
        for sub in ("automations", "guidance", "prompts"):
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.tmp_path / "runs"
        self.runs_dir.mkdir()

    def _doctor(self) -> str:
        args = mock.Mock()
        args.workspace = str(self.workspace)
        args.data_dir = str(self.runs_dir)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._cmd_doctor(args)
        return buf.getvalue()

    def test_doctor_names_failures_log_and_separates_the_lint_log(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        runner.failure_log_path(self.runs_dir).write_text(
            f"{stamp} automation=Fleet-Check run_id=r1 session_id=s1 error='boom'\n",
            encoding="utf-8",
        )
        out = self._doctor()

        # The real failure log, by path, with its recency.
        self.assertIn("run failures", out)
        self.assertIn(str(runner.failure_log_path(self.runs_dir)), out)
        self.assertIn(stamp, out)
        self.assertIn("Fleet-Check", out)

        # ...and the lint log, named and explicitly disclaimed, so the two can
        # never be conflated again.
        self.assertIn("config lint", out)
        self.assertIn("automation_lint.jsonl", out)
        self.assertIn("says nothing about whether runs are failing", out)

    def test_doctor_flags_a_leftover_pre_rename_lint_file(self) -> None:
        (self.runs_dir / "automation_errors.jsonl").write_text("", encoding="utf-8")
        out = self._doctor()
        self.assertIn("PRE-RENAME", out)
        self.assertIn("its age means nothing", out)

    def test_doctor_reports_an_unreadable_failure_log_as_an_outage(self) -> None:
        runner.failure_log_path(self.runs_dir).write_text(
            "no timestamp here\n", encoding="utf-8"
        )
        out = self._doctor()
        self.assertIn("run failures", out)
        self.assertIn("UNKNOWN", out)
        self.assertIn("NOT an absence of failures", out)

    def test_doctor_says_none_recorded_when_there_is_no_log(self) -> None:
        out = self._doctor()
        self.assertIn("none recorded yet", out)


if __name__ == "__main__":
    unittest.main()
