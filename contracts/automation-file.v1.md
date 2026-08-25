# Automation File Contract — v1 (DRAFT — implementation does not yet pass; steps still live in the markdown body)

## Who builds against this

- The drumbeat engine itself (`src/drumbeat/automation.py` — the only parser).
- Automation authors: humans and AI agents writing `automations/*.md` files.
- The `drumbeat-automation-authoring` skill and `docs/AUTOMATIONS.md` (teaching surfaces).
- Consumers migrating existing automation sets (see the migration doc for the current consumer).

## Purpose

One automation is one markdown file. The machine-facing surface is structured,
closed-vocabulary YAML frontmatter that the engine validates loudly on save and
load; the body is for humans. This contract freezes the smallest surface that
lets an automation be authored, validated, and executed unattended — and nothing
else.

## Core (the frozen part — small on purpose)

1. **One file, two layers.** An automation is a single markdown file: YAML
   frontmatter (the machine surface) and an optional markdown body (human-facing
   description). **The body is never parsed for execution.**

2. **Closed frontmatter vocabulary.** Every top-level frontmatter key is
   registered (the registry lives in `docs/AUTOMATIONS.md` §2). An unknown or
   retired key is **refused loudly with a remedy** at validation time — never
   ignored, never warned-and-continued.

3. **Steps are structured data.** `steps:` is a required, ordered list in the
   frontmatter. Each step is an object with exactly these keys:
   - `id` (required) — a slug, unique within the file
   - `prompt` (required) — non-empty text; the entirety of the step's behavior
   - `label` (optional) — human display name
   An unknown key inside a step object is refused the same way as an unknown
   top-level key.

4. **Execution semantics.** Steps execute in array order, one agent turn per
   step, all within the automation's (lifecycle-governed) session. Step `id`
   appears in run records; it is identity, not control flow.

5. **Steps carry zero configuration.** Scheduling, notification policy,
   conversation lifecycle, agent config/profile selection, and activity labels
   are top-level frontmatter concerns that apply to the whole automation. A step
   is judgment (prompt) plus identity (id) — nothing operational.

6. **Fail loud with remedy.** Any violation of 1–5 makes the automation invalid:
   save is rejected and the scheduler refuses the file, in both cases naming the
   offending key/step and the remedy. No silent degraded modes.

## Explicitly backlogged (not in v1)

Promotion trigger for all of these: a real automation demonstrates the need, and
this contract is amended first with that evidence (dated changelog entry).

- Per-step control flow: `condition`, `retry`, `timeout`, `on_error`, branching.
- Inter-step output binding (`inputs`/`outputs`); today the shared session is the
  data pipe, by design.
- Per-step capability/tool declarations.
- Per-step config overrides (model, notify, etc.).

## Conformance

The kit is the engine's own validation surface plus fixtures:

- `validate_automation_content()` rejects: unknown top-level key, unknown step
  key, missing/duplicate step `id`, empty `prompt`, missing `steps`, steps in
  the markdown body (retired shape — refused with a pointer to this contract).
- Fixture pair proves discrimination: `tests/fixtures/automation-good.md` passes;
  `tests/fixtures/automation-bad.md` (one violation per rule above) fails with
  the named refusals.
- A worked example a stranger can follow lives in `examples/automations/`.

Freeze bar (all four required before this contract is marked FROZEN): this spec ·
the conformance surface above, green · at least one real automation set running
against it · the worked example. Status stays DRAFT and says why until then.

## Reserved / open questions (NOT frozen)

- The guidance-file loop (a step that loads `guidance/*.md`, a closing step that
  prunes/updates it) is an authoring **convention**, deliberately not schema.
  Whether any part of it deserves schema is open.
- Step-level `activity` labeling for observability.
- Whether `label` earns its keep or dies in v2.

## Changelog

- **2026-08-25** — v1 drafted. Encodes the negotiated decision: steps move from
  markdown body to frontmatter as minimal `{id, label?, prompt}` objects
  (Scout-informed: prompts as data, zero control-flow schema, config stays
  run-level). Clean cut: the body-steps shape is retired, not dual-read.
