"""Tests for the soft-launch gate fixes (2026-08-11).

Every test here guards a behavior that was RED-PROVEN broken first, against
this exact codebase, before the fix was written. The proof is recorded in
each class docstring, because a test whose failure mode was never observed
guards a guess.

Same discipline as test_placement_corrections.py: the real production
functions are driven directly. Nothing under test is mocked -- only its
inputs (a temp workspace, a PATH with the agent removed).
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from drumbeat.automation import load_all_tolerant
from drumbeat.management_api import (
    EngineContext,
    ManagementError,
    validate_automation_content,
)
from drumbeat.prompts import KNOWN_PROMPTS

from drumbeat import capabilities, drain, packs, runner, staleness

_AUTOMATION = """---
automation:
  name: {name}
  enabled: true
  trigger:
    type: schedule
    expression: {expression}
  notify: {notify}
---

1. Say something.
"""


def _workspace(tmp: str) -> Path:
    root = Path(tmp)
    for sub in ("automations", "guidance", "prompts", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


class TestAgentCommandPreflight(unittest.TestCase):
    """RED-PROVEN: with `amplifier-agent` off PATH, a scheduled run logged
    `Probe: run raised an unhandled exception` and left NOTHING -- no run
    directory, no failures.log, no engine-events.jsonl, and
    `GET /api/automations/probe/runs` still answered `{"runs": [], "count":
    0}`. Failure class 1 in the engine that exists to prevent it.
    """

    def test_missing_agent_is_detected_on_the_turn_path(self) -> None:
        # Neutralize the SIBLING source (the co-installed agent in this test
        # venv, which the dependency now pulls in) so this exercises the
        # turn-PATH half in isolation: agent nowhere -> None.
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            with (
                mock.patch.object(runner, "_sibling_agent_command", return_value=None),
                mock.patch.object(packs, "turn_path", return_value="/nonexistent-bin"),
            ):
                self.assertIsNone(runner.check_agent_command(root))

    def test_present_agent_resolves_to_its_real_path(self) -> None:
        # Sibling neutralized, so this asserts the turn-PATH fallback resolves
        # a bring-your-own agent to its real path.
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            agent = fake_bin / "amplifier-agent"
            agent.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agent.chmod(0o755)
            with (
                mock.patch.object(runner, "_sibling_agent_command", return_value=None),
                mock.patch.object(packs, "turn_path", return_value=str(fake_bin)),
            ):
                self.assertEqual(runner.check_agent_command(root), str(agent))

    def test_sibling_agent_is_preferred_over_the_turn_path(self) -> None:
        """The single-install path: uv co-installs amplifier-agent into the
        engine's own tool venv but keeps it OFF the user PATH, so the runner
        must resolve it as a sibling of sys.executable -- and prefer it over
        any unrelated agent that happens to also be on the turn PATH.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            other_bin = root / "bin"
            other_bin.mkdir()
            other = other_bin / "amplifier-agent"
            other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            other.chmod(0o755)
            sibling = "/opt/drumbeat-toolvenv/bin/amplifier-agent"
            with (
                mock.patch.object(
                    runner, "_sibling_agent_command", return_value=sibling
                ),
                mock.patch.object(packs, "turn_path", return_value=str(other_bin)),
            ):
                self.assertEqual(runner.check_agent_command(root), sibling)

    def test_install_hint_is_unpinned_with_no_dead_httpx_workaround(self) -> None:
        # The `--with httpx --with httpx-sse` workaround is DEAD (amplifier-agent
        # declares httpx itself) and the v0.9.3 pin is ancient. The dependency is
        # deliberately unpinned, so the hint must carry neither.
        self.assertNotIn("--with httpx", runner.AGENT_INSTALL_HINT)
        self.assertNotIn("--with httpx-sse", runner.AGENT_INSTALL_HINT)
        self.assertNotIn("@v0.9.3", runner.AGENT_INSTALL_HINT)
        self.assertNotIn("0.9.3", runner.AGENT_INSTALL_HINT)
        # Still names the unpinned git URL as the direct-install fallback.
        self.assertIn(
            "git+https://github.com/microsoft/amplifier-agent",
            runner.AGENT_INSTALL_HINT,
        )

    def test_build_command_uses_sibling_path_and_keeps_the_drain_marker(self) -> None:
        """argv[0] is the resolved sibling ABSOLUTE path, and the command line
        still carries the ``amplifier-agent run --session-id`` substring that
        drain/staleness match against -- so process detection is unaffected by
        resolving the agent by locus instead of by bare name.
        """
        sibling = "/opt/drumbeat-toolvenv/bin/amplifier-agent"
        with mock.patch.object(runner, "_sibling_agent_command", return_value=sibling):
            cmd = runner._build_command(
                session_id="s1", fresh=True, cwd=Path("/tmp"), text="hi"
            )
        self.assertEqual(cmd[0], sibling)
        cmdline = " ".join(cmd)
        self.assertIn(drain.AGENT_TURN_MARKER, cmdline)
        self.assertIn(staleness._AGENT_TURN_MARKER, cmdline)

    def test_find_agent_turns_detects_a_real_process_carrying_the_marker(self) -> None:
        """Live-turn detection survives the SDK cut, proven against a REAL
        process -- not just a synthetically joined argv string.

        The runner no longer spawns the agent with its own ``subprocess.Popen``;
        the amplifier-agent-py SDK does, via
        ``create_subprocess_exec(binary, *assemble_argv(...))``. That child's
        ``/proc/<pid>/cmdline`` is ``<...>/amplifier-agent run --session-id
        <id> ...`` -- carrying ``drain.AGENT_TURN_MARKER`` exactly as the old
        hand-rolled command did (argv[0] basename ``amplifier-agent``, then
        ``run`` then ``--session-id``, all adjacent). ``drain`` and ``staleness``
        detect a live turn by substring-matching that marker in ``/proc``, and
        that answer gates whether it is safe to stop the scheduler -- a false
        all-clear is the failure this guards.

        Spawn a stand-in whose argv reproduces that exact cmdline shape (the
        trailing tokens are inert ``sys.argv`` to a sleeper) and assert
        ``drain.find_agent_turns`` finds it by pid. Read-only: ``drain`` never
        signals anything, and this test kills only its own child.
        """
        import subprocess
        import sys
        import time

        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                "amplifier-agent",
                "run",
                "--session-id",
                "probe-marker-test",
            ],
        )
        try:
            deadline = time.time() + 5.0
            found = False
            while time.time() < deadline:
                if any(t.pid == proc.pid for t in drain.find_agent_turns()):
                    found = True
                    break
                time.sleep(0.05)
            self.assertTrue(
                found,
                "drain.find_agent_turns did not detect a live process carrying "
                "the SDK-spawned agent cmdline marker",
            )
            # staleness answers the same question through the same reader.
            self.assertGreaterEqual(staleness.count_agent_turns_in_flight() or 0, 1)
        finally:
            proc.kill()
            proc.wait()

    def test_spawn_failure_returns_a_step_result_not_a_raise(self) -> None:
        """The load-bearing half: a spawn death must ride the ordinary abort
        path (StepResult with .error) so `_persist_run` writes a run record
        and emits automation_error. Raising skipped both.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            runs = root / "runs"
            with (
                mock.patch.object(
                    runner,
                    "_submit_turn",
                    return_value=runner._TurnOutcome(
                        spawn_failed=True,
                        error="amplifier-agent binary not found: install it",
                    ),
                ),
                redirect_stderr(io.StringIO()),
            ):
                result, stderr_text = runner._execute_turn(
                    session_id="test-session",
                    fresh=True,
                    cwd=root,
                    text="hello",
                    index=1,
                    runs_dir=runs,
                    wait_seconds=None,
                )
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("amplifier-agent", result.error)
        # The hint rides the failure; assert its stable, unpinned shape.
        self.assertIn("uv tool install", result.error)
        self.assertNotIn("--with httpx", result.error)
        self.assertEqual(stderr_text, "")


class TestExemplarPromptsShip(unittest.TestCase):
    """RED-PROVEN: zero prompt files shipped in any repo, so a `notify: auto`
    automation aborted with `prompts/auto-notify.md: prompt file is missing or
    empty` -- and that abort raised straight out of run(), leaving no
    failures.log line and no run record either.
    """

    def test_every_known_prompt_has_an_exemplar(self) -> None:
        examples = Path(__file__).resolve().parent.parent / "examples" / "prompts"
        for name in KNOWN_PROMPTS:
            self.assertTrue(
                (examples / f"{name}.md").is_file(),
                f"examples/prompts/{name}.md must ship: require_prompt names it",
            )

    def test_auto_notify_exemplar_keeps_the_protocol_token(self) -> None:
        examples = Path(__file__).resolve().parent.parent / "examples" / "prompts"
        body = (examples / "auto-notify.md").read_text(encoding="utf-8")
        self.assertIn(runner.NOTHING_TO_REPORT, body)

    def test_auto_notify_exemplar_is_publish_clean(self) -> None:
        examples = Path(__file__).resolve().parent.parent / "examples" / "prompts"
        for name in KNOWN_PROMPTS:
            body = (examples / f"{name}.md").read_text(encoding="utf-8")
            _consumer = "att" + "end"
            _owner = "bkra" + "bach"
            for leaked in (f"{_consumer}-items", f"{_consumer}-availability", _owner):
                self.assertNotIn(leaked, body, f"{name}.md leaks {leaked!r}")


class TestScheduleValidatedOnSave(unittest.TestCase):
    """RED-PROVEN: `validate_automation_content` returned
    `{'valid': True, ...}` for `expression: every other tuesday-ish`. The file
    saved green, showed Active, and failed every scheduler tick forever.
    """

    def _ctx(self, root: Path) -> EngineContext:
        return EngineContext(
            automations_dir=root / "automations",
            prompts_dir=root / "prompts",
            runs_dir=root / "runs",
            cwd=root,
        )

    def test_unparseable_expression_is_a_400_naming_the_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(_workspace(tmp))
            content = _AUTOMATION.format(
                name="Bad", expression="every other tuesday-ish", notify="always"
            )
            with self.assertRaises(ManagementError) as caught:
                validate_automation_content(content, ctx)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("every N minutes", caught.exception.message)
        self.assertIn("daily at HH:MM", caught.exception.message)
        self.assertIn("(line 7)", caught.exception.message)

    def test_every_supported_form_still_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(_workspace(tmp))
            for expression in (
                "every 30 minutes",
                "every hour",
                "every 2 hours",
                "daily at 07:00",
                "every day at 23:45",
            ):
                content = _AUTOMATION.format(
                    name="Fine", expression=expression, notify="always"
                )
                self.assertTrue(validate_automation_content(content, ctx)["valid"])

    def test_manual_trigger_is_not_schedule_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(_workspace(tmp))
            content = (
                "---\nautomation:\n  name: Manual\n  enabled: true\n"
                "  trigger:\n    type: manual\n  notify: always\n---\n\n1. Do it.\n"
            )
            self.assertTrue(validate_automation_content(content, ctx)["valid"])


class TestLeadingProseIsRefused(unittest.TestCase):
    """RED-PROVEN twice. Against the parser: a body opening with a prose
    paragraph validated `{'valid': True}` with the paragraph absent from
    steps -- author-written instruction, silently dropped. Against the live
    deployment: the reference packet's reconciliation automation opened with
    exactly such a paragraph, unread on every run (the council's exemplar
    sweep missed it; the halt rule caught it before the refusal shipped and
    stopped a running automation).
    """

    def _body(self, body: str) -> str:
        return (
            "---\nautomation:\n  name: Prose Drill\n  enabled: true\n"
            "  trigger:\n    type: schedule\n    expression: every 30 minutes\n"
            "  notify: always\n---\n\n" + body
        )

    def test_leading_prose_is_refused_naming_the_fix(self) -> None:
        from drumbeat.automation import AutomationError, load_from_text

        with self.assertRaises(AutomationError) as caught:
            load_from_text(
                Path("drill.md"),
                self._body(
                    "Instructions the agent will never see.\n\n1. Do a thing.\n"
                ),
            )
        self.assertIn("before step 1", caught.exception.problem)
        self.assertIn("frontmatter", caught.exception.problem)  # the fix, named

    def test_trailing_twin_still_refuses(self) -> None:
        from drumbeat.automation import AutomationError, load_from_text

        with self.assertRaises(AutomationError) as caught:
            load_from_text(
                Path("drill.md"),
                self._body("1. Do a thing.\n\nA trailing paragraph.\n"),
            )
        self.assertIn("inside step list", caught.exception.problem)

    def test_clean_body_is_untouched(self) -> None:
        from drumbeat.automation import load_from_text

        automation = load_from_text(
            Path("drill.md"),
            self._body("1. Do a thing.\n\n2. Do another, with\n   a continuation.\n"),
        )
        self.assertEqual(len(automation.steps), 2)


class TestDuplicateSlugIsALoadFailure(unittest.TestCase):
    """RED-PROVEN: two files both named `Twin` loaded as two automations with
    ONE slug and zero failures. They share the session pin, the run directory
    and the API path; the second silently takes the first's schedule slot.
    """

    def test_second_claimant_becomes_a_named_load_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            for stem in ("alpha", "bravo"):
                (root / "automations" / f"{stem}.md").write_text(
                    _AUTOMATION.format(
                        name="Twin", expression="every 30 minutes", notify="always"
                    ),
                    encoding="utf-8",
                )
            automations, failures = load_all_tolerant(root / "automations")

        self.assertEqual([a.path.name for a in automations], ["alpha.md"])
        self.assertEqual([f.path.name for f in failures], ["bravo.md"])
        self.assertIn("duplicate slug 'twin'", failures[0].problem)
        self.assertIn("alpha.md", failures[0].problem)

    def test_distinct_names_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            for stem, name in (("a", "First"), ("b", "Second")):
                (root / "automations" / f"{stem}.md").write_text(
                    _AUTOMATION.format(
                        name=name, expression="every 30 minutes", notify="always"
                    ),
                    encoding="utf-8",
                )
            automations, failures = load_all_tolerant(root / "automations")

        self.assertEqual(len(automations), 2)
        self.assertEqual(failures, [])

    def test_a_parse_failure_does_not_reserve_a_slug(self) -> None:
        """A broken file has no slug to claim, so it must not block a good one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            (root / "automations" / "broken.md").write_text(
                "no frontmatter here", encoding="utf-8"
            )
            (root / "automations" / "good.md").write_text(
                _AUTOMATION.format(
                    name="Good", expression="every 30 minutes", notify="always"
                ),
                encoding="utf-8",
            )
            automations, failures = load_all_tolerant(root / "automations")

        self.assertEqual([a.name for a in automations], ["Good"])
        self.assertEqual([f.path.name for f in failures], ["broken.md"])


class TestBaselineToolsAreNotConsumerBranded(unittest.TestCase):
    """RED-PROVEN: `_BASELINE_TOOLS` hardcoded two of the first consumer's own
    ledger tool names -- a consumer's vocabulary inside an engine that claims
    to ship zero tools. Both are declared by that consumer's own pack, so the
    resolved set is unchanged without them (measured).
    """

    def test_no_consumer_branded_names_remain(self) -> None:
        consumer_prefix = "att" + "end-"
        for name in capabilities._BASELINE_TOOLS:
            self.assertFalse(
                name.startswith(consumer_prefix),
                f"{name!r} names the first consumer; packs declare their own tools",
            )

    def test_pack_declared_tools_still_fold_in(self) -> None:
        """The mechanism that makes dropping them safe, asserted directly."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _workspace(tmp)
            pack = packs.Pack(
                name="ledger",
                directory=root / "packs" / "ledger",
                bin_dir=root / "packs" / "ledger" / "bin",
                tools=("some-consumer-tool",),
                description="a consumer's own domain tools",
                card="",
                pack_format=1,
            )
            names = capabilities._candidate_tool_names(root / "automations", (pack,))
        self.assertIn("some-consumer-tool", names)


class TestSourceTreeCarriesNoConsumerBrand(unittest.TestCase):
    """The permanent whole-source brand guard the councils demanded.

    Case-insensitive scan of every ``.py`` under ``src/drumbeat`` for any
    consumer brand or pack vocabulary that must not survive into the
    standalone engine -- the token set is ``_BRAND_TOKENS`` below, built from
    fragments so this guard never itself plants a scannable brand literal.
    Strings, comments, docstrings -- all of it. Zero is the contract.

    The token must not be FLANKED BY ASCII LETTERS, rather than a bare
    substring or a plain word boundary -- and each of those two refinements
    earns its place:

    * A bare substring flags ordinary English words that merely contain a
      token (honest prose that appears across the tree) and would make this
      guard cry wolf.
    * A plain word boundary would then MISS the references that matter most:
      it does not fire between a letter and ``_``, so it never matches an
      UPPER_SNAKE env-var name that embeds a brand token -- exactly the
      env-var brand leaks this guard exists to catch.

    "Not flanked by letters" gets both right: underscore, hyphen, digit and
    string edges are all real boundaries, so it catches ``BRAND-tool``,
    ``BRAND_ENV`` and ``brand-1a2b`` while skipping a token embedded inside a
    longer English word.

    SCOPED TO ``src/drumbeat`` (the repo's own source tree). It never scans
    ``tests/`` -- which, together with the fragment-built token list, is why
    this test file does not flag itself.

    ``_KNOWN_BRAND_DEBT`` is a shrinking allowlist of source files that still
    carry pack-vocabulary example names (platform/connector illustrations)
    outside the scope of the consumer-brand cut. Every file NOT on the list is
    held to zero. The list only ever shrinks; an empty set is the intended
    terminal state, at which point this is a pure whole-source zero-tolerance
    gate. See workspace-root handoff-notes.md.
    """

    # Built from fragments so the guard file itself carries no scannable brand
    # literal (verified by the whole-repo sweep the docs-sweep lane enforces).
    _BRAND_TOKENS = ("cor" + "tex", "att" + "end", "m3" + "65", "mux" + "plex", "gs" + "uite")
    _PATTERN = re.compile(
        r"(?<![A-Za-z])(" + "|".join(_BRAND_TOKENS) + r")(?![A-Za-z])",
        re.IGNORECASE,
    )

    # Allowlist emptied: every ``.py`` under ``src/drumbeat`` is now held to
    # zero. runner.py's last pack-vocabulary example names (the platform and
    # connector illustrations) were genericized to neutral tool names, so this
    # is now a pure whole-source zero-tolerance gate. Do NOT add to this to
    # make a new leak pass -- fix the leak instead.
    _KNOWN_BRAND_DEBT = frozenset()

    @staticmethod
    def _src_root() -> Path:
        return Path(__file__).resolve().parent.parent / "src" / "drumbeat"

    def test_no_consumer_brand_in_swept_source(self) -> None:
        root = self._src_root()
        self.assertTrue(root.is_dir(), f"source tree not found at {root}")
        offenders: list[str] = []
        for py in sorted(root.rglob("*.py")):
            if py.name in self._KNOWN_BRAND_DEBT:
                continue
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), start=1
            ):
                match = self._PATTERN.search(line)
                if match:
                    offenders.append(
                        f"{py.name}:{lineno}: {match.group(0)!r} in {line.strip()!r}"
                    )
        self.assertEqual(
            offenders,
            [],
            "consumer brand / pack vocabulary leaked into swept source "
            "(remove the term; do NOT extend the allowlist):\n" + "\n".join(offenders),
        )


class TestInFlightCountNeedsNoExternalBinary(unittest.TestCase):
    """RED-PROVEN by the clean-container walk: in `python:3.13-slim` (no
    `procps`, so no `ps`), `drumbeat doctor` printed `agent turns in flight:
    UNKNOWN (check failed)` -- the number that decides whether a restart is
    safe -- while `drain.find_agent_turns()` on the same machine read /proc
    and returned the real answer.
    """

    def test_counts_without_shelling_out(self) -> None:
        # Any `ps` invocation would blow up on this patch. The real reader
        # touches /proc only.
        with mock.patch.object(
            staleness.subprocess,
            "run",
            side_effect=AssertionError("must not shell out to ps"),
        ):
            count = staleness.count_agent_turns_in_flight()
        self.assertIsInstance(count, int)

    def test_agrees_with_the_drain_check(self) -> None:
        """One question, one answer. Two readers is how they diverged."""
        self.assertEqual(
            staleness.count_agent_turns_in_flight(), len(drain.find_agent_turns())
        )

    def test_unreadable_proc_is_none_not_zero(self) -> None:
        """'I could not tell' must never render as a confident zero."""
        with mock.patch.object(
            drain, "find_agent_turns", side_effect=OSError("no /proc")
        ):
            self.assertIsNone(staleness.count_agent_turns_in_flight())


class TestExampleConsumerShips(unittest.TestCase):
    """The engine emits intents and stops. Nothing shipped that showed the
    other half, so 'copy and own it' had nothing to copy.
    """

    def test_tailer_exists_and_is_stdlib_only(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "examples"
            / "consumers"
            / "tail_intents.py"
        )
        self.assertTrue(path.is_file())
        source = path.read_text(encoding="utf-8")
        self.assertIn("example, not product", source.lower())
        # Reads the same top-level field names the engine actually emits
        # (verified against a real engine-events.jsonl).
        for field in ("delivery_intent", "automation_error", "verdict", "gate"):
            self.assertIn(field, source)
        self.assertTrue(os.access(path, os.X_OK), "must be executable")


class TestNoRegressionInExemplars(unittest.TestCase):
    """Every shipped exemplar and drill must still parse under the stricter
    loader. The duplicate-slug rule is the kind of change that can quietly
    disqualify a file nobody re-checked.
    """

    def test_examples_and_drills_load_clean(self) -> None:
        root = Path(__file__).resolve().parent.parent / "examples"
        automations, failures = load_all_tolerant(root / "automations")
        self.assertEqual(failures, [])
        self.assertEqual(len(automations), 7)

        _, drill_failures = load_all_tolerant(root / "drills")
        # README.md is the one expected non-automation in that directory.
        self.assertEqual([f.path.name for f in drill_failures], ["README.md"])

    def test_every_exemplar_declares_a_parseable_schedule(self) -> None:
        from drumbeat.scheduler import parse_schedule

        root = Path(__file__).resolve().parent.parent / "examples" / "automations"
        automations, _ = load_all_tolerant(root)
        for automation in automations:
            if automation.trigger.type == "schedule":
                parse_schedule(automation.trigger.expression or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
