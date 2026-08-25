"""Tests for ``drumbeat init`` -- workspace scaffolding (drumbeat-w95).

Covers the four acceptance facets:

* scaffold correctness -- the four dirs and every default file land, with the
  content the engine needs;
* the example automation loads through ``automation.load_all`` and the workspace
  passes ``drumbeat doctor``'s structural gate (``serve.resolve_workspace``);
* idempotence by REFUSAL -- a second init writes nothing and names exactly what
  already exists;
* ``--force`` overwrites precisely the scaffold files.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from drumbeat import automation, cli, prompts, serve, workspace_init


class ScaffoldLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # A subdir that does NOT exist yet: init must create the target itself,
        # not just its contents.
        self.ws = Path(self._tmp.name) / "myspace"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_creates_missing_target_dir(self) -> None:
        self.assertFalse(self.ws.exists())
        workspace_init.scaffold(self.ws)
        self.assertTrue(self.ws.is_dir())

    def test_creates_all_four_directories(self) -> None:
        workspace_init.scaffold(self.ws)
        for rel in ("automations", "guidance", "prompts", "runs"):
            self.assertTrue((self.ws / rel).is_dir(), f"{rel}/ missing")

    def test_writes_every_scaffold_file(self) -> None:
        workspace_init.scaffold(self.ws)
        for rel in workspace_init.SCAFFOLD_FILES:
            self.assertTrue((self.ws / rel).is_file(), f"{rel} missing")
        # The specific files the spec names, spelled out (so a template rename
        # can't quietly drop one of them and still pass on the loop above).
        for rel in (
            "prompts/system.md",
            "prompts/auto-notify.md",
            "drumpacks.txt",
            "agent-config.yaml",
            "automations/repo-status-digest.md",
        ):
            self.assertTrue((self.ws / rel).is_file(), f"{rel} missing")

    def test_result_reports_created_dirs_and_files(self) -> None:
        result = workspace_init.scaffold(self.ws)
        self.assertEqual(list(result.created_dirs), list(workspace_init.SCAFFOLD_DIRS))
        self.assertEqual(
            list(result.created_files), list(workspace_init.SCAFFOLD_FILES)
        )
        self.assertEqual(result.overwritten_files, [])

    def test_agent_config_is_commented_placeholder(self) -> None:
        workspace_init.scaffold(self.ws)
        text = (self.ws / "agent-config.yaml").read_text(encoding="utf-8")
        # Commented-out default provider block: every non-blank line is a
        # comment, so nothing is active configuration yet.
        for line in text.splitlines():
            if line.strip():
                self.assertTrue(
                    line.lstrip().startswith("#"),
                    f"agent-config.yaml has an active (non-comment) line: {line!r}",
                )

    def test_drumpacks_uses_new_name_and_is_empty(self) -> None:
        # The NEW drumpack name (drumpacks.txt), and no packs declared yet:
        # every non-blank line is a comment.
        self.assertIn("drumpacks.txt", workspace_init.SCAFFOLD_FILES)
        workspace_init.scaffold(self.ws)
        text = (self.ws / "drumpacks.txt").read_text(encoding="utf-8")
        declared = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(declared, [], f"drumpacks.txt declares packs: {declared}")

    def test_no_consumer_brand_vocabulary(self) -> None:
        # drumbeat-w95 clean-cut rule: zero consumer-brand references in anything
        # scaffolded. The forbidden tokens are assembled from fragments so this
        # test file itself carries none of them literally (a repo-wide brand grep
        # must not trip on the very test that enforces the ban). Word-boundary
        # match so ordinary words that merely contain a fragment (e.g. an
        # "att" + "end"-suffixed everyday word) never false-positive.
        forbidden = ["cor" + "tex", "at" + "tend"]
        brand = re.compile(r"\b(" + "|".join(forbidden) + r")\b", re.IGNORECASE)
        workspace_init.scaffold(self.ws)
        for rel, template in workspace_init.SCAFFOLD_FILES.items():
            self.assertIsNone(brand.search(template), f"brand vocab in template {rel}")
            written = (self.ws / rel).read_text(encoding="utf-8")
            self.assertIsNone(brand.search(written), f"brand vocab written into {rel}")


class ExampleAutomationLoadsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        workspace_init.scaffold(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_all_accepts_the_example(self) -> None:
        # The strict, fail-on-any-bad-file loader accepts the scaffolded
        # automations dir and returns the one example.
        loaded = automation.load_all(self.ws / "automations")
        self.assertEqual(len(loaded), 1)
        example = loaded[0]
        self.assertEqual(example.name, "Repo Status Digest")
        self.assertEqual(example.slug, "repo-status-digest")
        self.assertEqual(example.notify, "auto")
        self.assertEqual(example.trigger.type, "schedule")
        self.assertGreaterEqual(len(example.steps), 1)

    def test_auto_notify_prompt_is_loadable_and_nonempty(self) -> None:
        # notify: auto automations REQUIRE prompts/auto-notify.md to be present
        # and non-empty (the engine ships no built-in copy). require_prompt
        # raises if it isn't -- so this asserts the example can actually run.
        body = prompts.require_prompt("auto-notify", self.ws / "prompts", reason="test")
        self.assertIn("NOTHING_TO_REPORT", body)

    def test_system_prompt_is_valid_and_a_noop_by_default(self) -> None:
        # system.md ships with only its explanatory frontmatter -> empty body ->
        # a valid, supported no-op (load_prompt returns None), not a parse error.
        self.assertIsNone(prompts.load_prompt("system", self.ws / "prompts"))


class DoctorStructuralChecksTests(unittest.TestCase):
    """Acceptance: init then doctor -> workspace passes doctor's STRUCTURAL checks.

    ``doctor``'s structural gate is ``serve.resolve_workspace`` (it SystemExits
    before printing anything when ``automations/`` is absent). A freshly-inited
    workspace clears that gate; doctor's overall exit code is a LIVENESS signal
    (no running engine -> UNKNOWN), which is deliberately not asserted here.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        workspace_init.scaffold(self.ws)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolve_workspace_succeeds(self) -> None:
        ctx = serve.resolve_workspace(self.ws)  # must not SystemExit
        self.assertTrue(ctx.automations_dir.is_dir())
        self.assertEqual(ctx.prompts_dir, self.ws / "prompts")
        self.assertEqual(ctx.runs_dir, self.ws / "runs")
        self.assertEqual(ctx.workspace, self.ws.resolve())

    def test_doctor_clears_structural_gate(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["doctor", "--workspace", str(self.ws)])
        self.assertIsInstance(rc, int)  # completed, no SystemExit from the gate
        text = out.getvalue()
        # A structural-health line only reachable AFTER the workspace resolved
        # and every automation parsed.
        self.assertIn("orphan pins: 0", text)
        self.assertNotIn("no automations/ directory", text)


class IdempotenceAndRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_second_scaffold_refuses_and_lists_existing(self) -> None:
        workspace_init.scaffold(self.ws)
        with self.assertRaises(workspace_init.InitError) as caught:
            workspace_init.scaffold(self.ws)
        err = caught.exception
        self.assertEqual(
            set(err.existing),
            set(workspace_init.SCAFFOLD_FILES),
        )
        # The message names each existing file, so the user can see exactly
        # what is in the way.
        for rel in workspace_init.SCAFFOLD_FILES:
            self.assertIn(rel, str(err))

    def test_refusal_does_not_modify_existing_content(self) -> None:
        workspace_init.scaffold(self.ws)
        edited = self.ws / "prompts" / "auto-notify.md"
        edited.write_text("MY OWN EDITED PROMPT\n", encoding="utf-8")
        with self.assertRaises(workspace_init.InitError):
            workspace_init.scaffold(self.ws)
        # Untouched: refusal is total, it does not clobber the user's edit.
        self.assertEqual(edited.read_text(encoding="utf-8"), "MY OWN EDITED PROMPT\n")

    def test_refusal_is_a_total_no_op(self) -> None:
        # A directory with only ONE scaffold file present must still refuse
        # WITHOUT writing any of the others -- a partial stamp is the failure
        # a fail-loud refusal exists to prevent.
        (self.ws / "prompts").mkdir(parents=True)
        (self.ws / "prompts" / "system.md").write_text("mine\n", encoding="utf-8")
        with self.assertRaises(workspace_init.InitError):
            workspace_init.scaffold(self.ws)
        self.assertFalse((self.ws / "drumpacks.txt").exists())
        self.assertFalse((self.ws / "agent-config.yaml").exists())
        self.assertFalse((self.ws / "prompts" / "auto-notify.md").exists())
        self.assertFalse((self.ws / "automations" / "repo-status-digest.md").exists())

    def test_force_overwrites_scaffold_files(self) -> None:
        workspace_init.scaffold(self.ws)
        edited = self.ws / "drumpacks.txt"
        edited.write_text("STALE\n", encoding="utf-8")
        result = workspace_init.scaffold(self.ws, force=True)
        self.assertEqual(
            set(result.overwritten_files), set(workspace_init.SCAFFOLD_FILES)
        )
        self.assertEqual(result.created_files, [])
        # Restored to the template (no longer the stale content).
        self.assertNotIn("STALE", edited.read_text(encoding="utf-8"))
        self.assertEqual(
            edited.read_text(encoding="utf-8"),
            workspace_init.SCAFFOLD_FILES["drumpacks.txt"],
        )


class CliWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_registered_with_positional_dir_and_force(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["init", str(self.ws)])
        self.assertEqual(args.command, "init")
        self.assertIs(args.func, cli._cmd_init)
        self.assertEqual(args.dir, str(self.ws))
        self.assertFalse(args.force)

        args = parser.parse_args(["init", str(self.ws), "--force"])
        self.assertTrue(args.force)

    def test_init_dir_defaults_to_cwd(self) -> None:
        # Unlike every other verb, init takes no --workspace; its DIR defaults
        # to the current directory.
        parser = cli.build_parser()
        args = parser.parse_args(["init"])
        self.assertEqual(args.dir, ".")

    def test_cli_init_creates_and_reports(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["init", str(self.ws)])
        self.assertEqual(rc, 0)
        self.assertTrue((self.ws / "automations" / "repo-status-digest.md").is_file())
        self.assertIn("Initialized drumbeat workspace", out.getvalue())

    def test_cli_second_init_refuses_nonzero_and_names_files(self) -> None:
        cli.main(["init", str(self.ws)])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main(["init", str(self.ws)])
        self.assertEqual(rc, 1)
        stderr = err.getvalue()
        self.assertIn("refusing to overwrite", stderr)
        self.assertIn("--force", stderr)
        self.assertIn("prompts/auto-notify.md", stderr)

    def test_cli_init_force_returns_zero(self) -> None:
        cli.main(["init", str(self.ws)])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["init", str(self.ws), "--force"])
        self.assertEqual(rc, 0)
        self.assertIn("overwritten", out.getvalue())


if __name__ == "__main__":
    unittest.main()
