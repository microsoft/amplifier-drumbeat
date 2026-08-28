# Authoring automations

An automation is a markdown file. The frontmatter is the whole machine surface:
**when it runs, whether anyone hears about it, and what it does** -- the steps
are a structured `steps:` list (see [`../contracts/automation-file.v1.md`](../contracts/automation-file.v1.md)).
The markdown body is a human-facing description and is **never parsed for
execution**.

This is the reference for the format and the conventions that make one work.
For the design behind it, see [`ARCHITECTURE.md`](ARCHITECTURE.md). For how to
make one actually good over time, see [`TUNING.md`](TUNING.md).

Working starting points live in [`../examples/`](../examples) — copy one and
edit it rather than starting from this page.

---

## 1. The file

```markdown
---
automation:
  name: Example Check
  enabled: true
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
  requires:
    - example-cli
    - guidance/EXAMPLE.md
  inject:
    - argv: ["example-cli", "state"]
      label: "current state"
  steps:
    - id: load-guidance
      prompt: Load and follow guidance/EXAMPLE.md.
    - id: check-source
      prompt: >-
        Check the source and tell me what needs my attention since your last
        check.
    - id: read-only-guard
      prompt: >-
        This is a read-only run: do not send anything, do not modify anything.
        If some part of this cannot be carried out with the tools you have, say
        so explicitly rather than approximating.
---

Optional human-facing description. This body is never parsed for execution.
```

The filename's stem is the automation's **slug** (`example-check.md` →
`example-check`), used in run artifact paths and API routes.

The parser is strict: anything ambiguous is refused loudly with the file path
and the offending value. A broken automation is logged and listed as broken —
it never takes down the other automations' schedules.

---

## 2. Frontmatter reference

| Key | Required | Value |
|---|---|---|
| `name` | yes | Human-readable name, used in reports and notifications |
| `enabled` | no (default `true`) | Boolean. `false` = parsed and listed, never scheduled |
| `trigger` | yes | Mapping; see §3 |
| `steps` | yes | Ordered list of step objects (`{id, prompt, label?}`); see §2.2 and §7 |
| `notify` | no (default `auto`) | `always` · `auto` · `urgent-only` · `never`; see §4 |
| `requires` | no (default `[]`) | List of strings: tool names and/or workspace-relative file paths; see §5 |
| `inject` | no | List of `{argv, label}`; see §6 |
| `conversation` | no (default `continuous`) | `continuous` · `fresh` · `daily` — how the conversation persists across runs; see §2.1 |
| `guidance_delivery` | no (default `reference`) | `reference` · `inline` — how required guidance FILES reach the agent; see §5 |
| `agent_config` | no | Mapping: a per-automation host-config overlay (provider, model, MCP, skills, debug); see §10 |

This vocabulary is **closed** (contract rule 2): every key above is registered,
and an unknown or retired top-level key is refused loudly with a remedy at parse
time — never ignored. There is no `session:` key. **The frontmatter is yours;
the engine never writes to it** — see below.

### Session pins are engine state, not part of your file

Each automation resumes the same conversation across runs. The id of that
conversation lives in `<data-dir>/session_pins.json`, written by the engine.
**Nothing writes to your automation file, ever.** Two commands are all you need
day to day:

```
drumbeat sessions --workspace <dir>                          # what each one resumes
drumbeat rotate-session <slug> --workspace <dir> --reason R  # abandon one, start fresh
```

Rotation takes a **required reason** and writes a durable log entry: "start
this one over" is a decision worth a record.

One consequence to know before you rename anything: **renaming an automation
starts a fresh conversation.** The pin store is keyed by slug, so `git mv
teams-check.md teams-check-v2.md` leaves the old conversation behind as an
*orphan pin* (reported by `drumbeat doctor`) and starts the new slug cold.

Carrying an older workspace forward, or curious why it works this way? See
[Appendix A](#appendix-a-migrating-a-pre-020-workspace).

### 2.1 · Conversation lifecycle (`conversation:`)

By default an automation keeps **one conversation forever**, resuming it on
every run. That is what you want for a check that should remember what it saw
last time. But a long-lived conversation also grows without bound, and some
automations want a clean slate on a schedule. The `conversation:` key chooses
between three lifecycles:

| Value | Meaning |
|---|---|
| `continuous` | **Default.** One conversation, resumed on every run. Abandoned only when a health signal fires — a provider context-ceiling hit, or a change to the automation's steps (see [§ session health](TUNING.md)). An automation with no `conversation:` key behaves exactly this way. |
| `fresh` | A **new conversation on every run.** The previous run's conversation is left behind and a fresh session is started. Two runs produce two distinct sessions that share no memory. |
| `daily` | **One conversation per local calendar day.** The first run after local midnight starts a fresh session; every run within the same day resumes it. The boundary is the **host's local timezone**, matching the `daily at HH:MM` schedule form. |

```yaml
automation:
  name: Morning Digest
  conversation: daily
```

Any value other than these three is refused at parse time — the same closed
vocabulary discipline as `notify:` and `trigger.type:`.

**How rotation happens is identical across all triggers.** `fresh` and `daily`
abandon the old conversation through the *same* mechanism the health signals
use: the pin is cleared, one line is written to
`<data-dir>/session_rotations.jsonl`, and a `session_rotated` event is emitted.
So `drumbeat sessions` and the rotation log read the same whether a session was
rotated by a ceiling hit, by a steps rewrite, or by a `fresh`/`daily`
boundary. Rotation never deletes a transcript — it leaves the old one on disk
untouched and starts the next run clean.

**Choosing:**

| Situation | Value |
|---|---|
| A check that should remember prior runs (most automations) | `continuous` |
| A run that must carry no memory of the last one | `fresh` |
| A conversation you want bounded on a predictable daily line | `daily` |

Note the difference between `conversation:` and the `daily at HH:MM` *schedule
expression* (§3): the schedule decides **when a run fires**, while
`conversation:` decides **whether that run reuses the previous conversation**.
They are independent — a `daily` conversation on an `every 2 hours` schedule
runs many times a day but rotates its conversation only on the first run after
midnight.

### 2.2 · Step anatomy (`steps:`)

`steps:` is a **required, ordered list** of step objects. Each step carries
exactly three keys, and no others:

| Key | Required | Value |
|---|---|---|
| `id` | yes | A slug (lowercase letters, digits, single hyphens), **unique within the file**. It is identity, not control flow — it appears in run records so a run's turns tie back to the declared step, and it survives an edit to the prompt text |
| `prompt` | yes | Non-empty text; the entirety of the step's behavior, fed as one sequential agent turn |
| `label` | no | Human display name; carries no behavior |

```yaml
steps:
  - id: load-guidance
    label: Load guidance
    prompt: Load and follow guidance/EXAMPLE.md.
  - id: check-source
    prompt: Check the source and tell me what needs my attention.
```

Steps execute in array order, one agent turn per step, all within the
automation's conversation. **A step is judgment (prompt) plus identity (id) and
nothing operational** — scheduling, notification policy, conversation lifecycle,
and agent config are whole-automation frontmatter concerns, never per-step. An
unknown key inside a step, a missing or duplicate `id`, or an empty `prompt` is
refused loudly with a remedy, exactly like an unknown top-level key.

The contract fingerprint that decides session rotation (see §2.1 and
[`TUNING.md`](TUNING.md)) covers the ordered step **prompts** only — editing a
step's `id` or `label`, or the frontmatter around the steps, does not abandon
the conversation.

---

## 3. Triggers

```yaml
trigger:
  type: schedule
  expression: every 30 minutes
```

| `type` | Meaning |
|---|---|
| `schedule` | Fires on the expression. `expression` is **required** |
| `manual` | Never fires on its own; run it via the API or CLI |
| `event` | **Reserved.** Refuses to load when `enabled: true` — see below |

### Schedule expressions

Free text, in one of these forms:

```
every N minutes          every minute
every N hours            every hour
daily at HH:MM           every day at HH:MM
```

`HH:MM` is a 24-hour clock in the **server's local timezone**, recomputed on
every evaluation rather than captured at startup — so a daylight-saving shift
does not silently move your 07:00 rollup.

Interval forms fire relative to the last run, not to a wall-clock grid.
`every 2 hours` means "two hours after the last one finished," which is what
you want for a check whose duration varies.

**Automations sharing an identical expression are deterministically
staggered.** Two automations both declaring `every 2 hours`, registered on the
same poll tick, would otherwise fire in lockstep forever — every reschedule is
`now + interval`, so they never drift apart. The stagger is derived from the
automation's identity, not from randomness, so the same automation staggers the
same way across restarts.

An unparseable expression skips that automation with a loud log line every
tick. It does not crash the scheduler and does not silently disable itself.

### Why `event` refuses to load when enabled

The scheduler only ever tracks `type: schedule`. An enabled automation
declaring `type: event` would validate cleanly and then **never run, silently,
forever** — the exact class of failure this project keeps designing against. So
it is refused at parse time with a message telling you to set `enabled: false`
if you want a placeholder. A disabled one is an honest placeholder; an enabled
one can never do what it claims.

---

## 4. Notify policy

The engine evaluates the policy and emits a **verdict with a reason**. It never
delivers anything itself — a consuming service does that. See
[`ARCHITECTURE.md` §7](ARCHITECTURE.md#7-the-delivery-seam--the-engine-never-touches-a-transport).

| Value | Behavior |
|---|---|
| `always` | The final reply is always delivered. No judgment turn, no gating |
| `auto` | An extra judgment turn runs; the agent decides whether it has anything worth saying |
| `urgent-only` | **Identical work and judgment to `auto`**, but delivery happens only if the reply carries an `URGENT:` marker |
| `never` | Never delivered. The run and its artifacts still exist |

### `auto` and the `NOTHING_TO_REPORT` sentinel

After the last step, the engine appends one more turn asking the agent whether
this run produced anything worth surfacing. If the reply is exactly
`NOTHING_TO_REPORT`, the run is withheld with that gate recorded.

Two rules make this work in practice, and both belong in your step text:

- **State the sentinel in the automation itself**, so the agent knows the exact
  token. Do not rely on it being remembered from a guidance file that might be
  pruned later.
- **A quiet run is a success, not a failure.** Say so. Automations that treat
  silence as an unsatisfying outcome learn to manufacture findings.

### `urgent-only` and the `URGENT:` marker

Use this when an automation's *work* is valuable but its *typical output* is
not worth an interruption — a sweep that usually reports "nothing new."

Delivery happens only if the final reply carries an `URGENT: <one-line reason>`
marker. The match is deliberately tolerant of surrounding markup: a finding
rendered as `**URGENT: …**` or `## URGENT: …` counts. An earlier, stricter
anchor required a bare undecorated line, and a genuinely urgent finding was
demoted for formatting — the automation did its job and the parser lost it.

Two things to write into any `urgent-only` automation's steps:

1. **The work is never optional.** The report is saved to the run's artifacts
   and any records the agent keeps are written, whether or not anything is
   delivered. Never let "this one doesn't push" become "this one doesn't
   bother."
2. **Most passes should not carry the marker.** If you are unsure whether
   something clears the bar, it does not.

### Choosing

| Situation | Policy |
|---|---|
| A periodic status report whose whole point is to arrive | `always` |
| A check that usually finds nothing, occasionally finds something real | `auto` |
| A sweep whose findings belong in a record, not in an interruption | `urgent-only` |
| A run whose value is entirely its side effects | `never` |

---

## 5. `requires:` — the pre-run gate

Each entry is either a **tool name** or a **workspace-relative file path**.
The engine checks all of them *before step 1*:

- **A file** must exist, and its content is injected **verbatim, re-read on
  every single run**. Edit a guidance file and the next run sees the new text.
  No restart, no cache.
- **A tool** must resolve to an executable on the constructed turn PATH, and
  its pack card is injected verbatim.

**An unsatisfied requirement aborts the run** with a reasoned record. It does
not run your steps with a missing tool and let the agent report that it could
not do the job — because that message names the wrong cause, and you will spend
an afternoon debugging your prompt when the real problem was the PATH.

```yaml
requires:
  # Tool names come from whatever packs the consumer installs -- these are
  # placeholders. Substitute the tools your own packs actually provide.
  - example-cli
  # File paths are workspace-relative and injected verbatim, every run.
  - guidance/EXAMPLE.md
```

**Requiring the guidance file you tell the agent to "load" is not optional.**
A step that says "load and follow `guidance/EXAMPLE.md`" without a matching
`requires:` entry produces a run where the agent cheerfully proceeds without
the policy, and nothing anywhere says so.

---

## 6. `inject:` — durable state on every run

An `inject:` entry runs a tool before step 1 and turns its **stdout into a
turn**. This is how consumer-owned state (an open-work ledger, a roster, a
queue) reaches every run mechanically, rather than depending on the agent
remembering to go look.

```yaml
inject:
  - argv: ["example-cli", "state"]
    label: "current state"
```

**Classification order is fixed: timeout → exit code → stdout.**

| Tool result | Engine behavior |
|---|---|
| Times out | **Aborts the run**, voiced |
| Exits non-zero | **Aborts the run**, voiced |
| Exits 0, stdout (whole, stripped) is byte-exactly `INJECT_IDLE` | **Injects nothing; run proceeds.** A reasoned `inject_skipped` event is written |
| Exits 0, stdout bare-empty | **Aborts, loud** |
| Anything else | **Injects stdout verbatim** as a turn |

If you are writing the tool, the contract is in
[`DRUMPACKS.md`](DRUMPACKS.md#inject-tools--the-rules-are-contract-not-style). The short
version: **errors to stderr, never stdout** (stdout is the injection channel),
**exit non-zero on any failed read** (a half-read state file injected as a turn
is a silent fallback wearing your tool's name), and **print `INJECT_IDLE` when
you have nothing to say** — silence is never a contract value.

`INJECT_IDLE` (a tool sentinel, on stdout) and `NOTHING_TO_REPORT` (an agent
sentinel, inside a turn) are deliberately different tokens. Do not use one
where the other belongs.

Copyable exemplar: [`../tests/packs/minimal/`](../tests/packs/minimal).

---

## 7. Writing the steps

The `steps:` list is the ordered set of natural-language steps (§2.2). Each
step's `prompt` is fed as one sequential user turn in the same conversation.
There is no branching, no looping, no variables, no conditionals — if you want
a conditional, write the condition into the prompt's prose and let the agent
apply judgment.

### One concern per step

The step boundary is the unit you will edit later. When a run goes wrong, you
want to change one step's `prompt`, not untangle a paragraph that does four
things. A stable `id` per step also means the run records name *which* step
produced each turn, so a failure points at the step you need to edit.

A shape that works, from automations that have run for months:

```yaml
steps:
  - id: load-guidance      # policy, no action
    prompt: Load and follow the guidance.
  - id: check-source       # the actual work
    prompt: Look at the source; report what needs attention.
  - id: cleanup            # mutation, scoped
    prompt: Take the safe cleanup actions the guidance authorizes.
  - id: self-maintain      # self-maintenance
    prompt: Update the guidance file with anything durable you learned.
```

### State the negative space

The single highest-value sentence in most automations is the one saying what
*not* to do:

> This is a read-only run: do NOT mark anything read, do NOT send anything,
> do NOT edit any files.

An agent with capable tools will find a helpful-looking action you did not
intend. Naming the boundary is cheaper than discovering it was crossed.

### Demand an honest failure

Put this, or something like it, in any step that could partially succeed:

> If some part of this cannot be carried out with the tools you have, say so
> explicitly rather than approximating.

And where a source can be incompletely read:

> Check the command's own completeness field. If anything could not be
> inspected, report it explicitly as a coverage gap — never say a source was
> quiet when it could not actually be checked.

"Nothing found" and "could not look" are different answers. An automation that
cannot tell them apart will report a silence you have no reason to trust.

### Recording is not telling

If your automation writes to a record — a ledger, a file, a tracker — say
explicitly that writing the record is **not** the same as surfacing it.

This is a real, measured failure: a check minted four brand-new records, then
reasoned *"already tracked, therefore nothing to report"* — the records had
existed for zero minutes. Being written down was never evidence anyone had been
told. Decide whether something is worth surfacing on its own merits, never
because it has a record.

### Ask for judgment, not for a threshold

Steps that work say *"use your judgement about what actually warrants
interrupting me versus routine activity that can proceed unattended."* Steps
that fail try to encode the threshold as a number and then need editing every
time reality shifts. Put the *criteria* in a guidance file where they can grow;
keep the step pointed at the judgment.

### Let the automation maintain its own guidance

A final step of *"update `guidance/EXAMPLE.md` with anything durable you
learned; this file cannot grow unbounded, so use judgement about when to
revise, prune, or consolidate"* is what turns a static prompt into something
that improves. The pruning clause is not optional — without it the file grows
until it crowds out everything else in the context window.

---

## 8. The `guidance/` convention

Automation steps and guidance files reference each other by **workspace-relative
path**:

```yaml
requires:
  - guidance/EXAMPLE.md
steps:
  - id: load-guidance
    prompt: Load and follow guidance/EXAMPLE.md.
```

The file is injected verbatim at the top of every run. The convention that has
held up:

| File | Holds |
|---|---|
| `ATTENTION.md` | Cross-domain triage discipline that applies everywhere |
| `<DOMAIN>.md` | One per source: `EMAIL.md`, `MESSAGING.md`, `MEETINGS.md` … |
| `IDENTITY.md` | Who you are and who you work with, as evidence for attribution |
| `TOOLS.md` | What tooling exists beyond what the pack cards already say |

**Cross-domain learnings go in `ATTENTION.md`; domain files stay
domain-specific.** Put that instruction *in* the files, at the top, or every
file slowly becomes a copy of every other file.

### Templates now, personal policy later

[`../examples/guidance/`](../examples/guidance) ships **templates**: they work
blandly out of the box and are explicitly marked as files you replace. Real
guidance encodes real preferences about real people, so it belongs to you and
not to this repo.

The convention for that is simple and available today: **your workspace's
`guidance/` is yours.** Copy a template in, then edit it — by hand or by
letting the automation's own final step do it. A run reads whatever is on disk
at that moment.

**There is no overlay mechanism, and none is coming.** An earlier version of
this page promised layered personal overrides on top of shipped templates as
a coming feature; that promise is withdrawn, deliberately, and the reasoning
is worth keeping because it is the same reasoning that makes the convention
above sufficient.

Runtime layering means two copies of a policy file exist and something must
decide which one loaded. Every consequence of that is bad here: "which file
actually ran" becomes a runtime question needing new machinery; an agent's
own guidance self-edit — the correct-it-in-conversation loop that is this
system's most-loved behavior — lands in the shadowed copy and changes nothing,
succeeding loudly and doing nothing; and a resolution layer that silently
substitutes a default contradicts this engine's own fail-loud contract, where
a missing prompt file is an abort, not a quiet fallback.

**Your workspace is the override.** Copy a template in, edit it, and put the
workspace under git if you want history and a restore path. Layering, done
this way, is a `git clone` — and the layer mechanism is one you already know
how to debug.

---

## 9. What a healthy automation looks like after a month

- Its guidance file has been edited more often than its steps have.
- It reports "nothing to report" most runs, and that is fine.
- When it does surface something, the reason is specific enough to act on
  without opening the run artifact.
- Its coverage gaps are named out loud rather than rendered as quiet.
- Its steps still fit on one screen.

If instead it has grown to twelve steps and its guidance file has not changed
since you wrote it, the judgment is living in the wrong file. Move the criteria
into guidance and shrink the steps back to concerns.

[`TUNING.md`](TUNING.md) is the loop for getting there.

---

## 10. `agent_config:` — per-automation host config

Every turn is handed ONE host config — the authoritative source for provider
selection, model, MCP servers, skills, and debug knobs the engine runs under.
`agent_config:` is how an automation shapes that config for its own turns
without touching any other automation.

```yaml
agent_config:
  provider:
    module: openai            # provider short-name; omit to keep the bundle default
    config:
      default_model: gpt-5-mini
      base_url: http://192.168.1.7:8081/v1
```

You never write the host config by hand. The engine resolves ONE per turn by
merging up to three layers, **lowest precedence first**, and materializes the
result under the data dir:

1. **`$AMPLIFIER_AGENT_CONFIG`** — an operator's own debug/host config file, if
   that variable is set, folded in as the BASE. (The host config a turn is
   handed outranks the environment, so folding it in is what keeps that
   variable from being silently defeated.)
2. **`agent-config.yaml` `default:`** — the workspace baseline for every
   automation here (see below).
3. **this automation's `agent_config:`** — the block above, the highest
   precedence layer.

Merge rules are deliberately boring so you can predict the result:

- two mappings at the same key **recurse**;
- a scalar or a list **replaces** wholesale — no list concatenation;
- a `null` value **anywhere is refused** (loudly). v1 has no "unset this"
  semantics; omit the key instead.

### The workspace baseline: `agent-config.yaml`

A single file at the workspace root sets a `default:` merged into **every**
automation:

```yaml
# <workspace>/agent-config.yaml
default:
  provider:
    config:
      default_model: claude-sonnet-4
```

An automation's own `agent_config:` overrides it key-by-key. `profiles:` is
reserved in this file for named profiles used by interactive/API turns; the
scheduled-automation path reads only `default:`.

### What's allowed, and what is refused loudly

The top-level vocabulary is **closed** to
`provider · providers · mcp · skills · debug`. Anything else is refused at parse
time — including `approval` (the engine always runs `-y`, so an approval block
is a silent no-op) and `allowProtocolSkew`.

**Credentials are refused anywhere in the block** — any `api_key` / `apiKey` /
`token` / `secret` / `authorization` key, at any depth, fails loud and names the
full path (e.g. `provider.config.api_key`). Credentials belong in the engine's
**environment**, never a config file: a committed value would leak, and
amplifier-agent re-asserts credentials from the environment and ignores the file
anyway. This is the one rule worth memorizing.

A malformed `agent_config:` block does not take the fleet down: it is reported
as a load failure (named on every scheduler tick and by `drumbeat doctor`) while
every other automation keeps running.

### Provider changes rotate the pinned session automatically

Each automation resumes one conversation across runs (§2). That transcript is
built under one **provider module**. If your config later selects a *different*
provider module, the engine **rotates the pin automatically** — abandons the old
conversation (logged, with a reason) and starts fresh — because a transcript
carries provider-specific state (thinking-block signatures, cache breakpoints,
tokenization) the new provider can reject outright. This is built in and not
configurable; it is the same "leave the sediment, re-seed durable state, start
fresh" move a contract change or a context-ceiling hit already makes. Changing
only the *model* (same provider) does **not** rotate.

### Disabling provider prompt caching

To turn provider prompt caching off for an automation's turns, set it directly
in the `agent_config:` block:

```yaml
automation:
  # ...
  agent_config:
    provider:
      config:
        enable_prompt_caching: false
```

`provider.config.enable_prompt_caching` is amplifier-agent's own host-config
field, forwarded verbatim in the host config — drumbeat does not interpret it.
There is no separate shorthand for it.

### Nothing set? Nothing changes

An automation with no `agent_config:`, no workspace `agent-config.yaml`, and no
`$AMPLIFIER_AGENT_CONFIG` runs with **no host config at all** — every turn runs
on the engine's own defaults. The materialized config's path
and sha256 are recorded in each run's
`result.json` (`effective_config_path` / `effective_config_sha`), so a run can
always be tied back to the exact policy it executed under; both are `null` when
no config was handed down.

### Interactive/API turns do NOT read this automation's `agent_config:`

**Everything above describes the SCHEDULED-run resolver (`agent_config.resolve`).**
An interactive turn submitted via `POST /api/turns` — either a fresh turn naming
`automation_slug` (e.g. a manual-trigger, chat-style automation's first message)
or a reply that names an existing `session_id` — is resolved by a **separate,
narrower function** (`agent_config.resolve_turn`) that merges only three layers:
`$AMPLIFIER_AGENT_CONFIG`, the workspace `agent-config.yaml` `default:` block,
and the request's own `profile:` (looked up in that same file's `profiles:`
block). **It has no `automation_config` parameter at all, so an `agent_config:`
block written into that automation's frontmatter is silently inert for every
interactive/API turn against it** — including every turn a `trigger: manual`
automation ever runs, since such an automation is *never* invoked any other way.
To make a manual-trigger automation's turns select a model, add a named
`profile:` in `agent-config.yaml` and have the caller pass
`"profile": "<name>"` on `POST /api/turns` — an `agent_config:` block on the
automation file itself only a *scheduled* run of that automation would read.

