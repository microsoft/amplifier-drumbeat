"""Conformance surface for `contracts/automation-file.v1.md`.

The contract's Conformance section names `validate_automation_content()` as the
engine's own validation surface and requires a discriminating fixture pair:
`tests/fixtures/automation-good.md` passes; a bad file fails with a named
refusal. Each frozen-core rule (1-6) is proven here by mutating the good fixture
to introduce exactly one violation and asserting the specific, remedy-carrying
refusal -- because a single parse stops at the first error, so one file cannot
exhibit every violation at once.

Every test drives the real parser (`automation.validate_automation_content`),
not a stand-in.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from drumbeat.automation import AutomationError, validate_automation_content

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_GOOD = (_FIXTURES / "automation-good.md").read_text(encoding="utf-8")
_BAD = (_FIXTURES / "automation-bad.md").read_text(encoding="utf-8")


def _frontmatter(steps_block: str, *, extra: str = "", body: str = "A description.") -> str:
    """A valid automation skeleton with a caller-supplied steps block/body.

    Used to introduce exactly one violation at a time against an otherwise
    valid file.
    """
    return (
        "---\n"
        "automation:\n"
        "  name: Demo\n"
        "  trigger:\n"
        "    type: manual\n"
        "  notify: never\n"
        f"{extra}"
        f"{steps_block}"
        "---\n\n" + body + "\n"
    )


_GOOD_STEPS = (
    "  steps:\n"
    "    - id: step-one\n"
    "      prompt: Do the first thing.\n"
    "    - id: step-two\n"
    "      prompt: Do the second thing.\n"
)


class TestGoodFixturePasses(unittest.TestCase):
    def test_good_fixture_validates(self) -> None:
        automation = validate_automation_content(_GOOD, path=Path("automation-good.md"))
        self.assertEqual(automation.name, "Repo Status Digest")
        self.assertEqual(
            [s.id for s in automation.steps],
            ["confirm-fire-time", "gather-digest", "emit-digest"],
        )
        # The optional `label` is carried where present and None where absent.
        self.assertEqual(automation.steps[0].label, "Confirm the fire time")
        self.assertIsNone(automation.steps[1].label)
        # Every prompt is non-empty (rule 3).
        self.assertTrue(all(s.prompt.strip() for s in automation.steps))


class TestBadFixtureFails(unittest.TestCase):
    def test_bad_fixture_is_refused_pointing_at_the_contract(self) -> None:
        with self.assertRaises(AutomationError) as caught:
            validate_automation_content(_BAD, path=Path("automation-bad.md"))
        problem = caught.exception.problem
        self.assertIn("body-steps", problem)
        self.assertIn("automation-file.v1", problem)


class TestEachFrozenCoreRuleHasANamedRefusal(unittest.TestCase):
    """One violation per frozen-core rule, each asserted by its named refusal."""

    def _refusal(self, text: str) -> str:
        with self.assertRaises(AutomationError) as caught:
            validate_automation_content(text, path=Path("mutant.md"))
        return caught.exception.problem

    def test_unknown_top_level_key(self) -> None:  # rule 2
        problem = self._refusal(_frontmatter(_GOOD_STEPS, extra="  bogus: 1\n"))
        self.assertIn("unknown top-level key", problem)
        self.assertIn("bogus", problem)

    def test_unknown_step_key(self) -> None:  # rule 3
        problem = self._refusal(
            _frontmatter(
                "  steps:\n    - id: step-one\n      prompt: One.\n      bogus: 1\n"
            )
        )
        self.assertIn("unknown key", problem)
        self.assertIn("bogus", problem)

    def test_missing_step_id(self) -> None:  # rule 3
        problem = self._refusal(_frontmatter("  steps:\n    - prompt: One.\n"))
        self.assertIn("id is required", problem)

    def test_duplicate_step_id(self) -> None:  # rule 3
        problem = self._refusal(
            _frontmatter(
                "  steps:\n    - id: dup\n      prompt: One.\n"
                "    - id: dup\n      prompt: Two.\n"
            )
        )
        self.assertIn("duplicates", problem)
        self.assertIn("unique", problem)

    def test_empty_prompt(self) -> None:  # rule 3
        problem = self._refusal(
            _frontmatter('  steps:\n    - id: step-one\n      prompt: "   "\n')
        )
        self.assertIn("prompt", problem)
        self.assertIn("non-empty", problem)

    def test_missing_steps(self) -> None:  # rules 3, 6
        problem = self._refusal(_frontmatter(""))
        self.assertIn("automation.steps is required", problem)

    def test_retired_body_steps_shape(self) -> None:  # rule 1 (clean cut)
        problem = self._refusal(
            _frontmatter(_GOOD_STEPS, body="1. A step left behind in the body.")
        )
        self.assertIn("body-steps", problem)
        self.assertIn("automation-file.v1", problem)

    def test_step_id_must_be_a_slug(self) -> None:  # rule 3
        problem = self._refusal(
            _frontmatter("  steps:\n    - id: Bad_ID\n      prompt: One.\n")
        )
        self.assertIn("slug", problem)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
