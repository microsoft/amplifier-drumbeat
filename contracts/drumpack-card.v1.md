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
   never parses it for behavior — but it **must be non-empty** (loader-enforced):
   a card that says nothing puts a tool on PATH with no knowledge attached, the
   one thing the card exists to prevent. (Verified 2026-08-26; the empty-body
   refusal was already live in `packs.py` and is now written into the rule.)

2. **Closed frontmatter vocabulary.** Required keys: `pack_format` (integer
   schema version; this contract describes `1`), `name` (slug), `description`
   (non-empty), `tools` (non-empty list). Optional: `activity` (map of tool or
   subcommand name → present-tense narration string shown while a turn runs it).
   The vocabulary is exactly `{pack_format, name, description, tools, activity}`;
   an unknown top-level key is refused loudly with a remedy — never silently
   ignored, because a swallowed key is a card that lies about what it declares
   (VISION §1/§4). (Amended 2026-08-26: this was a claim the loader did NOT
   enforce — unknown keys were silently dropped — so the engine was fixed to
   refuse them, rather than the rule being weakened to match a silent failure.)

3. **Tool entries are minimal and real.** A `tools` entry is **either** a bare
   string (the tool name) **or** a mapping carrying at least `name` (slug);
   both shapes are accepted so a card can migrate to the richer shape without a
   flag day that takes every other drumpack down with it. The loader reads
   `name` and locates the executable at `bin/<name>` relative to the pack root:
   it must exist, be a file, and have the exec bit set at load time, or the pack
   is invalid. Exhaustive **in both directions** — an executable in `bin/` the
   card never declares is equally a refusal. On the mapping shape, `bin` and
   `description` are card documentation this contract owns; the loader reads
   only `name` and never resolves an arbitrary `bin` path. (Amended 2026-08-26:
   the draft described a single `{name, bin, description}` object with a
   loader-resolved `bin` path; the loader accepts both shapes, resolves at
   `bin/<name>`, and treats the sub-keys as documentation — the rule now
   captures that reality.)

4. **Wiring is explicit.** A workspace enables packs via `drumpacks.txt` at the
   workspace root: an ordered list of pack directories. Enabled packs' `bin/`
   directories join the turn PATH in list order. Nothing is auto-discovered.

5. **Fail loud with remedy.** An invalid card (missing card, missing/unknown
   key, bad tool entry) is refused naming the pack, the offending key, and the
   fix — never silently skipped. An empty or missing pack list is a visible
   condition, never a silent zero-tools turn: it is surfaced **loud-but-tolerant**
   — logged at load (`load_workspace_packs`) and named by `drumbeat doctor` —
   rather than hard-refusing engine start, because a fresh `drumbeat init`
   legitimately begins with a comments-only `drumpacks.txt`. (Amended
   2026-08-26: `read_pack_list()` returned `[]` silently on a missing/empty list
   — a VISION §4 silent-zero — so the engine was fixed to make the condition
   loud and visible, and the open question below is resolved to loud-but-tolerant.)

6. **Tools are self-serving.** Every declared tool must run from a shell with
   no interactive prompts, support `--help`, and fail loud with a remedy on
   missing prerequisites. The engine guarantees PATH presence — the tool
   guarantees its own usability. **Only the PATH-presence half is
   loader-enforced**; the tool-side obligations (`--help`, no prompts, fail-loud
   on a missing prerequisite) are author **conventions** the loader cannot check
   without executing every declared binary at load — the same Rules-vs-Conventions
   split docs/DRUMPACKS.md draws. Conformance therefore proves the engine half
   (a declared tool resolves on the turn PATH) with a loader test, and the tool
   half with the good fixture's own `good-tool`, never with a load refusal.
   (Amended 2026-08-26 to name which half the loader can and cannot enforce.)

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
- ~~Whether rule 5's "missing pack list is visible" should HARD-refuse engine
  start.~~ **Resolved 2026-08-26 → loud-but-tolerant**, not a hard refusal: a
  fresh `drumbeat init` scaffolds a comments-only `drumpacks.txt`, so refusing
  start on an empty list would break the first-run flow. The condition is now
  logged at load and named by `drumbeat doctor` (see rule 5 and the changelog).

## Changelog

- **2026-08-26** — Conformance verification pass (the draft's explicit
  obligation, discharged). Every frozen-core rule checked against
  `src/drumbeat/packs.py`:
  - **Rule 1** — verified; the already-live empty-body refusal written into the
    rule (amendment).
  - **Rule 2** — the "unknown keys refused" claim was NOT enforced (unknown
    top-level keys were silently dropped). A silent drop is a VISION §1/§4
    failure, so the **engine was fixed** to refuse unknown keys loudly with a
    remedy, rather than weakening the rule to match the silent behavior. The
    closed vocabulary is `{pack_format, name, description, tools, activity}`.
  - **Rule 3** — amended to reality: the loader accepts a bare-string OR a
    `name`-carrying mapping, resolves the executable at `bin/<name>`, and treats
    a mapping's `bin`/`description` as documentation (not a loader-resolved path).
  - **Rule 4** — verified: explicit `drumpacks.txt` wiring only, bins in list
    order, nothing auto-discovered.
  - **Rule 5** — a missing/empty pack list returned `[]` silently (VISION §4
    silent-zero). **Engine fixed** loud-but-tolerant: `load_workspace_packs`
    logs it at load and `drumbeat doctor` names it; start is NOT hard-refused,
    because a fresh `drumbeat init` legitimately begins with a comments-only
    `drumpacks.txt` (see the resolved open question).
  - **Rule 6** — amended to name which half the loader can enforce (PATH
    presence) vs. which is an author convention (`--help`/no-prompts/fail-loud).
  - Conformance fixtures added: `tests/fixtures/drumpack-good/` (loads) and
    `tests/fixtures/drumpack-bad/` (one violation per loader-enforceable rule,
    each named refusal asserted in `tests/test_drumpack_card_contract.py`).
  - Status stays **DRAFT**: the freeze bar's real-external-pack and
    worked-example items are the owner's to stamp, not this lane's.
- **2026-08-26** — v1 drafted from current engine behavior + the first real
  external pack (tmux fleet). DRAFT with an explicit verification obligation:
  the conformance lane must check each rule against `packs.py` and amend this
  file where the engine's reality differs (this contract captures current
  state; VISION.md leads).
