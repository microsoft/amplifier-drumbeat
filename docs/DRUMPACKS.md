# Authoring a drumbeat drumpack

This is the **contract**, not advice. Everything under "Rules" is enforced by
the loader and refuses at startup when violated; everything under
"Conventions" is on you, because the engine cannot enforce it without
becoming the plugin protocol we deliberately refused.

> **The frozen shape lives in the contract.**
> [`contracts/drumpack-card.v1.md`](../contracts/drumpack-card.v1.md) is the
> governing card contract — the smallest frozen surface (one card, closed
> frontmatter vocabulary, real tool entries, explicit wiring, fail-loud load,
> self-serving tools) plus its conformance fixtures. This page is the how-to
> layer that teaches against that shape; it does not re-freeze it. When a rule
> here and the contract ever disagree, the contract governs the machine
> surface and VISION.md leads — so read them together, and don't trust a copy
> over the source.

A drumpack is a **directory** -- normally a git repo:

```
drumpack.md      frontmatter + the card
bin/             executables; each MUST self-document via --help
guidance/        optional: exemplar policy files for this source
automations/     optional: exemplar automations requiring this drumpack's tools
```

There is no registry, no install step, no manifest beyond `drumpack.md`'s
frontmatter, no version solver, no per-tool JSON schema, and no semver
constraint. Drumpacks are checkouts; git is the version mechanism; consumers
pin by submodule. Each of those was considered and rejected: they are contract
surfaces nothing consumes yet.

The whole contract is **PATH + card + fail-loud load rules.**

## Declaring drumpacks (the consumer side)

The consumer workspace hands the engine an ordered list of drumpack directories
in `drumpacks.txt` at the workspace root -- one path per line, relative to the
workspace or absolute, `#` comments and blank lines ignored:

```
drumpacks/ledger          # a private, in-repo drumpack
../drumbeat-pack-mail     # a sibling checkout
../drumbeat-pack-workers
```

Order is for deterministic **reporting** only. It never decides tool
resolution: a duplicate tool name across drumpacks refuses to load rather than
letting position pick a winner.

## Rules (the loader enforces these; violation = refuse to start)

1. **`drumpack.md` must exist**, with YAML frontmatter.
2. **`pack_format` is required, with no default.** Unknown versions are
   refused loudly. A future format will mean something this engine cannot
   know; guessing is how a card starts lying.
3. **`name`, `description`, and `tools` are required.** `tools` is the
   **exhaustive** list of `bin/` entries.
4. **Every declared tool must exist in `bin/`, be a file, and have the exec
   bit set.** A card that names a tool the agent cannot run is class-2
   theater with a file extension.
5. **Every executable in `bin/` must be declared.** A card that lies in
   *either* direction is theater: an undeclared binary on PATH is an
   undocumented weapon.
6. **The card body must be non-empty.** A drumpack whose card says nothing puts
   a tool on PATH with no knowledge attached -- the one thing the card exists
   to prevent.
7. **Duplicate tool names across drumpacks refuse to start, naming both
   drumpacks.** There is no precedence rule. Silently picking a winner is the
   silent-fallback ban applied to namespaces, and the loser's card would
   still be injected -- documentation for a binary that never runs.
8. **Duplicate drumpack names refuse to start, naming both directories.**

Note what rule 7 does *not* cover: the engine cannot know what the base PATH
already holds. Not shadowing an existing system binary is **your** job --
see the name-prefix convention below.

## Progress narration -- the optional `activity:` map

While a tool runs, a chat/reply turn shows a human phrase ("Checking your
calendar…") instead of an opaque spinner. That phrasing is **mechanism, not
policy**: the engine hardcodes NO consumer's vocabulary. Each drumpack declares
the phrasing for its OWN tools, in an optional `activity:` map in the
frontmatter -- a mapping of **subcommand -> narration label**:

```yaml
activity:
  calendar: "Checking your calendar…"
  mail: "Checking email…"
  send-mail: "Sending an email…"
```

- The map is **keyed by subcommand** (the first argument after the tool name).
  When the agent runs `your-cli calendar --json`, the engine looks up
  `calendar` in the drumpack that declares `your-cli` and narrates its label.
- It applies to every tool the drumpack declares; a drumpack with several CLIs
  can map all their verbs in one map (keep the subcommand names distinct).
- **Absent is fine.** A tool with no `activity:` entry -- or a whole drumpack
  with no map -- narrates with a generic "Running a command…". Undeclared is a
  legal, quiet state, not an error.
- **The label is the only thing shown.** No part of the raw command line (which
  may carry a session id, chat id, or path) ever reaches the UI -- the engine
  renders your fixed label or the generic fallback, never the argument.
- Present-but-malformed is a loud refusal, same discipline as every other
  field: keys and values must be non-empty strings.

`tests/packs/minimal/drumpack.md` carries a copyable `activity:` example.

## What may my tool assume is on PATH?

**The pinned base list, and nothing more.**

The turn PATH is constructed as:

```
<drumpack 1 bin>  <drumpack 2 bin>  ...  <workspace bin>  <PINNED BASE>
```

The pinned base is captured **once at engine startup**, logged there, and
echoed in `GET /api/capabilities` under `packs.path_base_pinned`. It is
identical for every turn regardless of how the service was launched -- that
is the point. Undeclared dependence on whoever happened to start the process
is a defect this project has paid for twice.

Read the actual list from `/api/capabilities` rather than assuming; it is a
property of the host the engine runs on, not of this document. In practice
it gives you `bash`, `python3`, `git`, `jq`, `curl` and the usual system
binaries.

A tool needing anything beyond the base **declares it in its card and fails
loud without it**. The shipped example: a meeting-transcript digest tool whose
default mode needs a summarizer CLI on PATH — it says so in its card, and
exits non-zero naming it when it is absent, rather than degrading to a
different answer.

**Hermetic PATH is not shipped, deliberately.** Every current `bin/` tool
across all three drumpacks is a `#!/usr/bin/env bash` shim; a hermetic PATH
today kills 100% of tools at the shebang. It is a named post-topology commit,
gated on a run-every-tool verification.

## `inject:` tools -- the rules are contract, not style

An `inject:` tool runs before step 1 of every run and its **stdout becomes a
turn**. The engine classifies the result in a fixed order: **timeout -> exit
code -> stdout.**

| Your tool does | The engine does |
|---|---|
| Times out | **Aborts the run**, voiced to the consumer |
| Exits non-zero | **Aborts the run**, voiced |
| Exits 0, stdout is byte-exactly `INJECT_IDLE` | **Injects nothing; the run proceeds.** A reasoned `inject_skipped` event is recorded |
| Exits 0, stdout is bare-empty | **Aborts, loud** |
| Anything else | **Injects your stdout verbatim** as a turn |

Therefore, as contract:

- **Exit non-zero on any failed read.** Do not print a partial answer and
  exit 0 -- a half-read state file injected as a turn is a silent fallback
  wearing your tool's name.
- **Errors go to stderr, never stdout.** stdout is the injection channel. A
  stack trace on stdout becomes a turn the agent tries to act on.
- **When you have nothing to say, print `INJECT_IDLE`** -- exactly that,
  alone, and exit 0. Silence is never a contract value: a crashed pipe and a
  genuinely idle state must not share an observable.
- The sentinel match is **byte-exact on the whole stripped stdout** -- not a
  prefix, not a regex.
- `INJECT_IDLE` is deliberately distinct from `NOTHING_TO_REPORT` (which is a
  value the *agent* emits inside turns). Distinct meanings get distinct
  tokens; this project's worst bugs were semantic overloads.

**Copyable exemplar:** `tests/packs/minimal/` -- `bin/minimal-state`
implements all four behaviours above in 12 lines of bash, and
`automations/inject-exemplar.md` shows the `inject:` declaration. Copy those,
do not paraphrase this table.

```yaml
inject:
  - argv: ["minimal-state"]
    label: "minimal state"
```

## Conventions (yours to keep; the engine cannot enforce them)

### Name prefixes

- **Repos:** `<engine>-pack-<source>` -- e.g. `drumbeat-pack-mail`,
  `drumbeat-pack-workers`. Provenance and contract version are legible from
  the name.
- **Tools:** prefix distinctly enough that cross-drumpack collisions (which
  refuse at load) stay rare in practice, *and* that you do not shadow a
  binary already on the base PATH. A real example from a shipped drumpack: its
  tool is named `<source>-cli`, **not** `<source>`, because a binary of that
  bare name already existed on the host. Rule 7 would not have caught that --
  only you can.

### Completeness contracts

Every list-returning tool should answer **"is this everything?"** in the
response itself. The shipped conventions:

- A paged API client returns a `_paging` block on every list: `pages_fetched`,
  `item_count`, `complete`, `capped`, `limited_by_top`, `next_link`.
- A capped-window reader returns `_completeness`: `lines_requested`,
  `lines_returned`, `complete`, plus the server's own limits.
- Tri-state where a boolean would lie: `at_prompt` is
  `"yes"`/`"no"`/`"uncertain"`, never a bare bool, because an honest "not
  sure" beats a confident wrong answer.

A tool that cannot tell its caller whether they saw the whole world lets a
partial view be mistaken for a complete one. The engine cannot enforce the
shape of your stdout without becoming the plugin protocol we refused, so
this one is genuinely on you -- which is exactly why both shipped drumpacks
implement it and document it in their cards.

### What belongs in the card

The card is injected **verbatim** into every automation whose `requires:`
names one of your tools. Write it for a reader who has never seen the tool
and cannot ask you a question.

- What exists, at the level of "these are the verbs".
- Invocation shapes for anything non-obvious.
- **Quirks that `--help` cannot express** -- the server-side filter that
  silently uses a different field, the flag that is not a state but a bell,
  the default window that gets applied when you pass nothing.
- **Negative space** -- what the tool deliberately will *not* do, and why.
  This is the part authors skip and readers need most: it is what stops an
  agent inventing a workaround for a boundary you drew on purpose.
- The completeness convention your tools implement.
- What your tool assumes is on PATH beyond the pinned base.

Do not put consumer policy in the card. "Record what you surface in the item
ledger" is the consumer's guidance, not your tool's documentation.

### Credentials are drumpack-private

Login, refresh, and the token store belong to your drumpack. The engine never
sees, stores, or proxies a credential, and there is no plan for it to.

## Exemplar guidance and automations

`guidance/` and `automations/` are **exemplars, and they are documentation**.
The consumer copies them into its own workspace; the consumer's copies are
live, the drumpack's copies are not. Ownership transfers at copy.

Two obligations follow:

1. An exemplar automation should run **as-is** after a copy -- if it
   `requires:` a guidance file or an `inject:` tool your drumpack does not
   ship, a copier gets an automation that fails its own requirements gate on
   first use, which teaches exactly the wrong lesson about fail-loud.
2. If your exemplars encode a real person's real policy, **scrub before
   publish** -- and say so in your README, in a section somebody skimming
   will actually see.
