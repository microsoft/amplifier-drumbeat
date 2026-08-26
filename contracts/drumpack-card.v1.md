# Drumpack Card Contract — v1 (DRAFT — drafted from the engine's current behavior; conformance lane must verify every rule against the loader and correct this file where reality differs)

## Who builds against this

- The drumbeat engine (`src/drumbeat/packs.py` — the only loader).
- Drumpack authors: humans and AI agents shipping tool packs for drumbeat workspaces.
- The `drumbeat-drumpack-authoring` skill and `docs/DRUMPACKS.md` (teaching surfaces).
- External pack repos (e.g. the tmux fleet pack) that must remain loadable across engine versions.

## Purpose

A drumpack is how an automation's tools arrive: a directory carrying a
`drumpack.md` card plus a `bin/` of executables, wired into a workspace via
`drumpacks.txt`. The card is the pack's self-description — the machine-facing
frontmatter tells the engine what ships; the body tells the *agent* what
`--help` cannot. This contract freezes the smallest surface that lets a pack be
declared, validated, and put on a turn's PATH — and nothing else.

## Core (the frozen part — small on purpose)

1. **One directory, one card.** A drumpack is a directory containing exactly one
   `drumpack.md` at its root: YAML frontmatter (machine surface) + markdown body
   (agent-facing manual). The body is documentation for the agent; the engine
   never parses it for behavior.

2. **Closed frontmatter vocabulary.** Required keys: `pack_format` (integer
   schema version; this contract describes `1`), `name` (slug), `description`
   (non-empty), `tools` (non-empty list). Optional: `activity` (map of tool or
   subcommand name → present-tense narration string shown while a turn runs it).
   Unknown top-level keys are refused loudly with a remedy.

3. **Tool entries are minimal and real.** Each `tools` entry is an object with
   exactly `name` (slug), `bin` (path relative to the pack root; must exist and
   be executable at load time), and `description` (non-empty). A tool entry
   whose `bin` is missing or non-executable makes the pack invalid.

4. **Wiring is explicit.** A workspace enables packs via `drumpacks.txt` at the
   workspace root: an ordered list of pack directories. Enabled packs' `bin/`
   directories join the turn PATH in list order. Nothing is auto-discovered.

5. **Fail loud with remedy.** An invalid card (missing card, missing/unknown
   key, bad tool entry) is refused naming the pack, the offending key, and the
   fix — never silently skipped. An empty or missing pack list is a visible
   condition, never a silent zero-tools turn.

6. **Tools are self-serving.** Every declared tool must run from a shell with
   no interactive prompts, support `--help`, and fail loud with a remedy on
   missing prerequisites. The engine guarantees PATH presence — the tool
   guarantees its own usability.

## Explicitly backlogged (not in v1)

Promotion trigger: a real pack demonstrates the need; this contract is amended
first with that evidence (dated changelog entry).

- Per-tool capability/permission declarations.
- Pack-declared context injectors (today injectors are workspace-level
  `injectors.yaml`; whether packs may ship injector declarations is open).
- Versioning/compatibility ranges between pack and engine.
- Structured body sections (verb tables, completeness contracts remain
  authoring conventions, not schema).

## Conformance

- Loader-level validation covers every rule in the frozen core; fixture pair
  proves discrimination: `tests/fixtures/drumpack-good/` (loads) and
  `tests/fixtures/drumpack-bad/` (one violation per rule, each named refusal
  asserted).
- A worked example a stranger can follow lives in `examples/` and the
  `drumbeat-drumpack-authoring` skill.

Freeze bar (all four before FROZEN): this spec · the conformance surface above,
green · ≥1 real external pack loading against it · the worked example. Status
stays DRAFT and says why until then.

## Reserved / open questions (NOT frozen)

- `activity` key granularity (per-tool vs per-subcommand) and fallback narration.
- The `INJECT_IDLE` / stderr sentinel conventions used by injector-style tools —
  convention today, possibly schema later.
- Whether rule 5's "missing pack list is visible" should HARD-refuse engine
  start (a known gap: `read_pack_list()` currently returns an empty list
  silently when `drumpacks.txt` is absent — flagged against VISION §4).

## Changelog

- **2026-08-26** — v1 drafted from current engine behavior + the first real
  external pack (tmux fleet). DRAFT with an explicit verification obligation:
  the conformance lane must check each rule against `packs.py` and amend this
  file where the engine's reality differs (this contract captures current
  state; VISION.md leads).
