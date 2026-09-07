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
| `inject` | no | List of `{argv, label, expect_prefix?}`; see §6 |
| `conversation` | no (default `continuous`) | `continuous` · `fresh` · `daily` — how the conversation persists across runs; see §2.1 |
| `guidance_delivery` | no (default `reference`) | `reference` · `inline` — how required guidance FILES reach the agent; see §5 |
| `priority` | no (default `normal`) | `high` · `normal` — dispatch order among automations due at the same tick; see §2.3 |
| `agent_config` | no | Mapping: a per-automation agent-config overlay (provider, model, effort, MCP, skills, tool-result ceiling); see §10 |

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
boundary. Rotation never deletes anything — it leaves the old session's storage
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

### 2.3 · Dispatch priority (`priority:`)

```yaml
priority: high        # high | normal (default: normal)
```

When several automations come due on the same tick, `high` ones are dispatched
before `normal` ones. That is the entire behavior.

**Read the next paragraph before you set this key on anything.** It changes
**who waits**, not how much gets done. It adds no concurrency, preempts no
running turn, and reorders nothing within a tier. On the reference deployment
the fleet demands ~412 runs/day and completes ~127 (31%); 30-minute automations
attain 52–72% of their declared cadence. This key does not move those numbers.
It exists because a schedule expression had become a bid in an auction nobody
clears, and the auction had no priority — so the one notify-capable path to the
owner starved on exactly equal terms with bulk background checks. Fixing the
throughput is a separate decision (run fewer automations, or change the
engine's concurrency); this key only decides who goes first with the throughput
you have.

Consequences worth stating plainly:

- **Marking everything `high` marks nothing high.** The tier is only meaningful
  while it is scarce.
- **Absent means `normal`,** and a fleet where nothing declares `priority:`
  dispatches in byte-identical order to before this key existed.
- **An unknown value is refused loudly** at parse time, naming the allowed
  vocabulary — the same closed-vocabulary discipline as `notify:` and
  `conversation:`.
- **The owner still outranks every tier.** A due automation whose session the
  owner is actively using is deferred to the next tick regardless of its
  priority (the owner-priority latch). `high` orders scheduled work *beneath*
  the owner, never alongside them.

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

### A crashed pass is not a quiet pass

Every policy above except `never` means an absence of output can be *read as a
judgment*: "nothing needed you." A run that crashes mid-pass also delivers
nothing — so without help, the two are the same observable, and the more
alarming one hides inside the reassuring one.

The engine keeps them apart in two places, and neither needs anything in your
automation file:

- **`<data-dir>/failed_passes.json`** holds at most one record per automation:
  *its most recent run failed, and nothing has succeeded since*. One small
  read answers "which automations are currently in a crashed state" — no
  walking `runs/<slug>/<run_id>/`, no outbox cursor. A successful run clears
  the record, so it is a live state and not a growing pile.
- **The next run of that automation is told**, in plain language, on its first
  turn: when the previous run failed, which run it was, and the recorded
  error. It is told to treat that interval as **unchecked** rather than quiet,
  and explicitly *not* to reconstruct or guess at what the failed pass would
  have said.

Write your steps so that notice is usable: an automation whose output includes
an honest accounting line ("checked X through Y") has somewhere to put it.

`notify: never` automations are deliberately outside this. Their failures are
still recorded in `failures.log` and still emit an `automation_error` event —
that is a safety property and bypasses notify policy entirely — but their
silence was never going to be mistaken for a verdict, so there is nothing to
disambiguate.

### Which log to watch when something looks wrong

| File (in the data dir) | Carries | A quiet file means |
|---|---|---|
| `failures.log` | **Runs that failed.** One line each. This is the failure telemetry | nothing is failing |
| `failed_passes.json` | Which notify-capable automations are *currently* in a crashed state | every automation's latest run succeeded |
| `automation_lint.jsonl` | **Automation files that would not parse.** Config lint, not run health | nobody has broken an automation file |

Watch `failures.log`, not the lint log. This distinction cost eight days once:
the lint log was called `automation_errors.jsonl`, went quiet because nobody
had broken an automation file, and was read as "nothing is failing" through two
days of a hundred failures each. `drumbeat doctor` now prints the failure log's
path and the age of its last entry, so a dead monitoring pipe is visible from
the health surface instead of looking like calm.

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
| Exits 0, `expect_prefix` declared, stdout does not start with it | **Aborts, loud** — malformed inject content is never fed to the model |
| Anything else | **Injects stdout verbatim** as a turn |

`expect_prefix` (optional, sibling to `argv`/`label`) declares the exact string
a healthy tool's stdout must start with. Declared, it turns a stray diagnostic
sentence on stdout — which would otherwise be trusted verbatim as state — into
a loud abort. Unset (the default), exit-0 stdout keeps today's verbatim-trust
behavior unchanged.

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

## 10. `agent_config:` — per-automation agent config

Every turn runs under ONE resolved agent config — the authoritative source for
provider selection, model, reasoning effort, MCP servers, and skills.
`agent_config:` is how an automation shapes that config for its own turns
without touching any other automation.

```yaml
agent_config:
  provider:
    module: openai            # provider ID; omit to keep the library's default
    config:
      model_class: fast       # fast | standard -- resolved to a concrete model
      reasoning_effort: high  # minimal | low | medium | high | xhigh
```

`provider.module` carries a provider **ID** — `openai`, `anthropic`,
`azure-openai`, `gemini`, `ollama`, `vllm`, `github-copilot` — handed to the
agent library unchanged. It is not a module path and not a filesystem path: the
library ships every provider in-process, and drumbeat never enumerates, injects,
or mounts one.

You never write the resolved config by hand. The engine resolves ONE per turn by
merging up to three layers, **lowest precedence first**, and materializes the
result under the data dir:

1. **`agent-config.yaml` `default:`** — the workspace baseline for every
   automation here (see below).
2. **a named `profile`** — used by interactive/API turns (see the last section
   of this chapter); a scheduled run passes none.
3. **this automation's `agent_config:`** — the block above, the highest
   precedence layer.

An operator's own `$AMPLIFIER_AGENT_CONFIG` file is **not** one of those layers.
It belongs to the agent library and speaks the library's own five-key host-config
vocabulary (`provider · model · storage · workspace · extra_request_params`),
not this one. It is folded in as the **base** of the host config drumbeat derives
and hands the turn, where the two speak the same language: drumbeat's
per-automation policy wins key by key, and an operator's unrelated
`extra_request_params` survive untouched. Overriding it wholesale would silently
defeat an operator who set it, which is exactly the failure this seam is built to
avoid.

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
scheduled-automation path reads only `default:`. A third block, `models:`,
holds engine policy rather than a config layer — see "Model classes" below.

### Model classes: `fast` / `standard` instead of a model id

Model ids rotate; tiers don't. Write the tier and let the engine resolve the id:

```yaml
agent_config:
  provider:
    module: openai
    config:
      model_class: fast       # -> default_model: gpt-5.6-luna
```

| `provider.module` | `fast` | `standard` |
| --- | --- | --- |
| `anthropic` | `claude-haiku-4-5-20251001` | `claude-sonnet-4-6` |
| `openai` | `gpt-5.6-luna` | `gpt-5.6-terra` |

Rules worth knowing, all fail-loud:

- The class is resolved **at materialization**, against the `provider.module`
  the *merged* config selects. `model_class` is a **drumbeat** key, not an
  agent-library field: it is resolved away and never reaches the library.
- A `model_class` with **no `provider.module`** anywhere in the merge cannot be
  resolved (a tier is a tier *within* a provider) and is refused, naming the
  known modules. So is an unknown module, and so is a class outside
  `fast | standard`.
- **An explicit `default_model` wins**, and drumbeat **warns** naming the
  shadowed `model_class` rather than dropping it silently. Precedence here is
  *key-level, not layer-level*: a `default_model` in the workspace `default:`
  block shadows a `model_class` in an automation's own (higher-precedence)
  block. If you set tiers per automation, don't also set a workspace-wide
  `default_model` — the warning will tell you when you have.
- The run's resolved model and its **source** (`"model_class:fast"` vs
  `"default_model"`) are recorded on the resolved config, so you can always
  tell which knob decided.

### Overriding the tables: `models:` in `agent-config.yaml`

```yaml
# <workspace>/agent-config.yaml
models:
  classes:                    # override the tier table, per provider module
    openai:
      fast: gpt-5.6-mercury   # partial: openai.standard is untouched
  deny:                       # models this workspace refuses to run
    - gpt-5.6-sol
```

`models:` is a registered top-level key beside `default:` and `profiles:` (the
vocabulary is closed to those three; inside `models:` it is closed to
`classes | deny`). It is a *sibling* of `default:`, not a key inside it, because
`default:` is a config **layer** and engine policy is not a layer.

`classes:` merges per module and per tier. `deny:` **replaces** the default list
(`["gpt-5.6-sol"]`) wholesale — the same "a list replaces, never concatenates"
rule the config merge itself uses.

### The model deny-list rejects the automation at load — it never runs

The deny-list is checked against the **resolved** model (whether it came from
`default_model` or a `model_class`). A denied automation is refused through the
ordinary config-lint path — named on every scheduler tick and by
`drumbeat doctor`, exactly like a malformed block — so it **never runs at all**.
That is deliberate: a model the deployment refuses must not reach a turn and
fail there.

### `reasoning_effort`

`provider.config.reasoning_effort` takes one of
`minimal | low | medium | high | xhigh`; a value outside that set is an
authoring-time refusal naming the set, rather than a mid-turn provider error.

Effort reaches a provider request through exactly one channel: the per-turn
host-config file the engine materializes and points the turn's worker at with
`$AMPLIFIER_AGENT_CONFIG`. There, it is written as
`extra_request_params.<provider-id>.reasoning.effort` — the library keys request
parameters **by provider**, which is why an effort declared with **no
`provider.module`** to scope it under is refused at load: written unscoped it
would validate and then do nothing.

### `skills:` and `mcp:`

Both are top-level blocks beside `provider:`, and both are handed to the library
**in code** rather than through the host-config file (its vocabulary has no key
for either):

```yaml
agent_config:
  skills:
    - skills                  # directory of skill subdirectories; relative to the workspace
  mcp:
    my-server:                # server name -> settings
      transport: stdio        # transport | command | args | env | url | headers
      command: my-mcp-server
      args: ["--flag"]
```

`skills:` is a list of directory paths, each holding skill subdirectories;
relative entries resolve against the workspace. `mcp:` is a mapping of server
name to `{transport, command?, args?, env?, url?, headers?}` — the inner
vocabulary is closed, and an unknown key inside a server entry is refused naming
it.

### `tool_result_ceiling_bytes:` — how big one tool result may be

```yaml
agent_config:
  tool_result_ceiling_bytes: 262144   # the default; a positive integer
```

A tool result is not a transient display artifact: it is appended to this
automation's conversation and **re-sent, in full, on every later turn**. One
oversized result therefore poisons the session permanently, and no later turn
can undo it. Measured on the originating deployment: a single 46,464,072-byte
tool result entered a pinned session, after which the provider refused every
subsequent request and manual rotation was the only remedy.

So drumbeat bounds every tool result its own process produces. Over the ceiling,
the first N bytes are kept and a note is appended naming the file that holds the
whole thing:

```
[drumbeat: output truncated -- kept 262144 of 46464072 bytes;
 full output: <run dir>/tool-output/<call-id>.txt]
```

Absent, the engine default is **262144** bytes (256 KiB), itself overridable per
deployment with `$DRUMBEAT_TOOL_RESULT_CEILING_BYTES`. Set the key when this
automation's tools legitimately return more (or much less) than that. A value
that is not a positive integer is refused at load; **`0` does not mean
"unlimited"** — there is no unlimited, because an unbounded result is the
failure this key exists to bound.

**Two limits, honestly stated.** The ceiling reaches the shell tool drumbeat
supplies (`run_command`) and nothing else: the agent library's own built-in
tools and any MCP server's results are produced inside the library, and it
offers no way to bound or replace them (a caller tool named `bash` is refused at
construction; see
[`../contracts/agent-binding.v1.md`](../contracts/agent-binding.v1.md) §10 and
its "Known gap"). Every turn is told to prefer `run_command` over the built-in
`bash`, but that is **advice, not enforcement**. The backstop for what slips
through is automatic: a provider refusal on input size rotates the pinned
session, so a poisoned conversation self-heals on the next run instead of
failing forever (§10 of that same contract, and the rotation section below).

### What's allowed, and what is refused loudly

The top-level vocabulary is **closed** to
`provider · mcp · skills · tool_result_ceiling_bytes`. Anything else is refused
at parse time, and four names are refused *by name*, each with its own reason:

- **`debug`** — raw provider request/response capture has no equivalent in the
  agent library, which exposes turn events rather than wire payloads. There is
  nothing behind the key, so it is refused rather than accepted and ignored. See
  the "Known gap" section of
  [`../contracts/agent-binding.v1.md`](../contracts/agent-binding.v1.md): a
  provider-level forensic question cannot be answered from a run's artifacts,
  and the ask is recorded upstream rather than worked around here.
- **`providers`** — the library ships every provider in-process and selects
  exactly one by id, so a provider *catalog* selects nothing. Name the one
  provider under `provider.module`.
- **`approval`** — the engine runs unattended, so an approval block is a silent
  no-op.
- **`allowProtocolSkew`** — not an automation-tunable knob.

**Credentials are refused anywhere in the block** — any `api_key` / `apiKey` /
`token` / `secret` / `authorization` key, at any depth, fails loud and names the
full path (e.g. `provider.config.api_key`). Credentials belong in the engine's
**environment**, never a config file: a committed value would leak, and the
agent library takes credentials from the environment only — it has no field a
config file could set. This is the one rule worth memorizing.

A malformed `agent_config:` block does not take the fleet down: it is reported
as a load failure (named on every scheduler tick and by `drumbeat doctor`) while
every other automation keeps running.

### Provider changes rotate the pinned session automatically

Each automation resumes one conversation across runs (§2). That conversation is
built under one **provider id**. If your config later selects a *different*
provider, the engine **rotates the pin automatically** — abandons the old
conversation (logged, with a reason) and starts fresh — because a resumed
conversation carries provider-specific state (thinking-block signatures, cache
breakpoints, tokenization) the new provider can reject outright. This is built in
and not configurable; it is the same "leave the sediment, re-seed durable state,
start fresh" move a contract change or a context-ceiling hit already makes.
Changing only the *model* (same provider) does **not** rotate.

### Nothing set? Nothing changes

An automation with no `agent_config:` and no workspace `agent-config.yaml` runs
with **no drumbeat-side policy at all** — every turn runs on the library's own
defaults (and on an operator's `$AMPLIFIER_AGENT_CONFIG` file, if one is set,
unchanged). The materialized config's path
and sha256 are recorded in each run's
`result.json` (`effective_config_path` / `effective_config_sha`), so a run can
always be tied back to the exact policy it executed under; both are `null` when
no config was handed down.

### Session-init module failures

`result.json` (and each turn's own entry under `steps:`, and the
`RUN_COMPLETED` event) carries a `module_failures` list — deduped, sorted
`"<type>:<module_id>"` entries (`type` is one of `provider`, `tool`, `hook`)
naming every module that failed to load or failed its own validation while
this run's session was booting. Empty for the overwhelming majority of runs.

**A non-empty `module_failures` means `failed: true`**, and the run's `error`
names the modules that did not load. Booting a session tolerates a provider,
tool, or hook that fails to load — the library keeps going with a reduced
module set and the turn can still emit text — but a turn that answered without
the modules it was configured to have did not run on the engine you asked for,
and the record says so rather than reporting a success with a footnote.
Measured on a real deployment, 2026-08-28: 96 of 96 runs in one morning
carried the same module-load warning on stderr while every run's own record
read `"failed": false, "error": null`. This field is what makes that
degradation visible on the record itself, and the verdict is what makes it
count. See `docs/ARCHITECTURE.md` §4 ("A turn with no working brain is a
failure") for the mechanism — including the companion rule, that a reply which
is *itself* a statement of provider unavailability fails the run (anchored at
the start of the reply, so an automation quoting the phrase is unaffected).

A run that instead hits a genuinely **unhandled** exception during session
init is unaffected by any of this — it still records `failed: true` with the
real exception text in `error`, the same as any other turn failure.

### Interactive/API turns do NOT read this automation's `agent_config:`

**Everything above describes the SCHEDULED-run resolver (`agent_config.resolve`).**
An interactive turn submitted via `POST /api/turns` — either a fresh turn naming
`automation_slug` (e.g. a manual-trigger, chat-style automation's first message)
or a reply that names an existing `session_id` — is resolved by a **separate,
narrower function** (`agent_config.resolve_turn`) that merges only two layers:
the workspace `agent-config.yaml` `default:` block, and the request's own
`profile:` (looked up in that same file's `profiles:` block). **It has no
`automation_config` parameter at all, so an `agent_config:`
block written into that automation's frontmatter is silently inert for every
interactive/API turn against it** — including every turn a `trigger: manual`
automation ever runs, since such an automation is *never* invoked any other way.
To make a manual-trigger automation's turns select a model, add a named
`profile:` in `agent-config.yaml` and have the caller pass
`"profile": "<name>"` on `POST /api/turns` — an `agent_config:` block on the
automation file itself only a *scheduled* run of that automation would read.

