---
name: drumbeat-automation-authoring
description: >-
  Author and validate drumbeat automation files — the closed-vocabulary YAML
  frontmatter contract. Covers the registered top-level keys, structured `steps:`
  objects ({id, prompt, label?} — steps live in the frontmatter, never the
  markdown body), the schedule grammar, `notify:` policy with its
  NOTHING_TO_REPORT and URGENT: sentinels, `conversation:` lifecycles
  (continuous/fresh/daily), the `requires:` pre-run gate and `inject:` state
  turns, `agent_config:` host-config overlays and the workspace baseline, loud
  refusal semantics, the guidance-file loop convention, and how to write steps
  that hold up unattended — with one complete worked example. Use when writing,
  editing, reviewing, or debugging a drumbeat `automations/*.md` file.
metadata:
  project: drumbeat
  contract: https://github.com/microsoft/amplifier-drumbeat/blob/main/contracts/automation-file.v1.md
---

# Authoring drumbeat automations

An automation is **one markdown file**. The YAML frontmatter is the entire
machine surface — *when it runs, whether anyone hears about it, and what it
does*. The markdown body is a human-facing description and is **never parsed for
execution**. This skill is the how-to; the frozen contract is
[`contracts/automation-file.v1.md`](https://github.com/microsoft/amplifier-drumbeat/blob/main/contracts/automation-file.v1.md)
and the full reference is
[`docs/AUTOMATIONS.md`](https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/AUTOMATIONS.md).

**Two load-bearing rules.** (1) The frontmatter vocabulary is **closed**: every
key is registered, and an unknown or retired key is refused loudly with a remedy
at save/load — never ignored. (2) **Steps are structured frontmatter data**,
`{id, prompt, label?}` objects — a numbered list left in the body is the retired
shape and is refused with a pointer to the contract.

The filename stem is the automation's **slug** (`email-check.md` →
`email-check`), used in run-artifact paths and API routes.

## 1 · The frontmatter (closed vocabulary)

| Key | Required | Value |
|---|---|---|
| `name` | yes | Human-readable name, used in reports/notifications |
| `enabled` | no (default `true`) | Boolean. `false` = parsed and listed, never scheduled |
| `trigger` | yes | Mapping; see §3 |
| `steps` | yes | Ordered list of `{id, prompt, label?}` objects; see §2 |
| `notify` | no (default `auto`) | `always` · `auto` · `urgent-only` · `never`; see §4 |
| `requires` | no (default `[]`) | List of tool names and/or workspace-relative file paths; see §5 |
| `inject` | no | List of `{argv, label}`; see §6 |
| `conversation` | no (default `continuous`) | `continuous` · `fresh` · `daily`; see §7 |
| `guidance_delivery` | no (default `reference`) | `reference` · `inline` — how required guidance FILES reach the agent |
| `agent_config` | no | Per-automation host-config overlay (provider, model, mcp, skills, debug); see §8 |

There is **no `session:` key** — session pins are engine state in
`<data-dir>/session_pins.json`, and nothing writes to your file. Every key not
in this table is refused at parse time.

## 2 · Steps — structured, minimal, one concern each

`steps:` is a **required, ordered list**. Each step object carries exactly three
keys and no others:

| Key | Required | Value |
|---|---|---|
| `id` | yes | A slug (lowercase letters, digits, single hyphens), **unique within the file**. Identity, not control flow — it appears in run records and survives an edit to the prompt |
| `prompt` | yes | Non-empty text; the entirety of the step's behavior, fed as one sequential agent turn |
| `label` | no | Human display name; carries no behavior |

Steps execute in array order, one agent turn per step, all within the
automation's conversation. **A step is judgment (prompt) plus identity (id) and
nothing operational** — scheduling, notify policy, conversation lifecycle, and
agent config are whole-automation concerns, never per-step. There is no
branching, looping, or variables: write a conditional as prose in the prompt and
let the agent apply judgment.

An unknown step key, a missing or duplicate `id`, or an empty `prompt` is refused
loudly. The rotation fingerprint (see §7) covers the ordered step **prompts**
only — editing an `id`, a `label`, or surrounding frontmatter does not abandon
the conversation.

## 3 · Triggers and the schedule grammar

```yaml
trigger:
  type: schedule
  expression: every 30 minutes
```

| `type` | Meaning |
|---|---|
| `schedule` | Fires on `expression` (**required**) |
| `manual` | Never fires on its own; run via API or CLI |
| `event` | **Reserved** — refuses to load when `enabled: true` (it would validate then never run) |

Schedule expressions are free text in one of these forms:

```
every N minutes      every minute      every N hours      every hour
daily at HH:MM       every day at HH:MM
```

`HH:MM` is a 24-hour clock in the **server's local timezone**, recomputed each
evaluation (a DST shift does not silently move your 07:00). Interval forms fire
relative to the last run's finish, not a wall-clock grid. An unparseable
expression skips that automation with a loud log line every tick — it never
crashes the scheduler or silently disables itself.

## 4 · Notify policy and its sentinels

The engine evaluates the policy and emits a **verdict with a reason**; it never
delivers anything itself (a consuming service does).

| Value | Behavior |
|---|---|
| `always` | Final reply always delivered; no judgment turn |
| `auto` | An extra judgment turn decides whether there is anything worth saying |
| `urgent-only` | Identical work/judgment to `auto`, but delivery only if the reply carries an `URGENT:` marker |
| `never` | Never delivered; the run and artifacts still exist |

- **`auto` uses the `NOTHING_TO_REPORT` sentinel.** After the last step the
  engine appends a turn asking whether anything is worth surfacing; a reply of
  exactly `NOTHING_TO_REPORT` withholds the run with that gate recorded. **State
  the sentinel in your own step text** (don't rely on a guidance file that may be
  pruned), and **say that a quiet run is a success** — automations that treat
  silence as failure learn to manufacture findings.
- **`urgent-only` uses the `URGENT:` marker.** Delivery happens only if the final
  reply carries `URGENT: <one-line reason>` (tolerant of surrounding markup like
  `**URGENT: …**`). Write into the steps that *the work is never optional* (the
  report is still saved) and that *most passes should not carry the marker*.

`NOTHING_TO_REPORT` (agent, inside a turn) and `INJECT_IDLE` (tool, on stdout —
see §6) are deliberately different tokens; never swap them.

## 5 · `requires:` — the pre-run gate

Each entry is a **tool name** or a **workspace-relative file path**, checked
*before step 1*. A file must exist and is injected **verbatim, re-read every
run** (edit it and the next run sees the new text — no restart, no cache). A tool
must resolve on the constructed turn PATH, and its pack card is injected
verbatim. **An unsatisfied requirement aborts the run** with a reasoned record —
it does not run your steps with a missing tool.

**Requiring the guidance file a step says to "load" is not optional.** A step
that says "load and follow `guidance/EMAIL.md`" without a matching `requires:`
entry produces a run where the agent proceeds without the policy and nothing says
so.

## 6 · `inject:` — durable state on every run

An `inject:` entry runs a tool before step 1 and turns its **stdout into a
turn** — how consumer-owned state reaches every run mechanically.

```yaml
inject:
  - argv: ["example-cli", "state"]
    label: "current state"
```

Classification order is fixed: **timeout → exit code → stdout.** Times out or
exits non-zero → **aborts the run**, voiced. Exits 0 with stdout byte-exactly
`INJECT_IDLE` → injects nothing, run proceeds (a reasoned `inject_skipped` event
is written). Exits 0 with bare-empty stdout → **aborts, loud**. Anything else →
injects stdout verbatim. (Writing the tool? See the sibling
`drumbeat-drumpack-authoring` skill.)

## 7 · Conversation lifecycle (`conversation:`)

By default an automation keeps **one conversation forever**, resuming it every
run — right for a check that should remember what it saw last time.

| Value | Meaning |
|---|---|
| `continuous` | **Default.** One conversation, resumed each run. Abandoned only on a health signal — a provider context-ceiling hit or a change to the step prompts |
| `fresh` | A **new conversation every run**; two runs share no memory |
| `daily` | **One conversation per local calendar day**; first run after local midnight starts fresh, later runs that day resume it |

Any other value is refused at parse time. Rotation is identical across triggers:
the pin is cleared, a line is written to `<data-dir>/session_rotations.jsonl`, and
a `session_rotated` event is emitted — whether caused by a ceiling hit, a steps
rewrite, or a `fresh`/`daily` boundary. Rotation never deletes a transcript.
Note `conversation:` (does this run reuse the last conversation?) is independent
of the `daily at HH:MM` *schedule* (when a run fires).

## 8 · `agent_config:` — per-automation host config

`agent_config:` shapes the single host config `amplifier-agent run --config`
reads, for this automation's turns only. The top-level vocabulary is **closed to
`provider · providers · mcp · skills · debug`**; anything else (including
`approval`) is refused.

```yaml
agent_config:
  provider:
    module: openai            # provider short-name; omit to keep the bundle default
    config:
      default_model: gpt-5-mini
```

The engine resolves ONE config per turn by merging up to three layers,
**lowest precedence first**: (1) `$AMPLIFIER_AGENT_CONFIG` operator file, folded
in as the base; (2) the workspace baseline `agent-config.yaml` `default:`; (3)
this automation's `agent_config:`. Merge rules: two mappings recurse; a scalar or
list **replaces** wholesale; a `null` **anywhere is refused loudly** (omit the
key instead). The workspace baseline sets a `default:` merged into every
automation, while `profiles:` in that file is reserved for interactive/API turns
(the scheduled path reads only `default:`).

**Credentials are refused anywhere in the block** — any `api_key` / `apiKey` /
`token` / `secret` / `authorization` at any depth fails loud and names the path.
Credentials belong in the engine's **environment**, never a config file. This is
the one rule to memorize. Changing the **provider module** rotates the pinned
session automatically (provider-specific transcript state); changing only the
model does not.

## 9 · The guidance-file loop (a convention, not schema)

The pattern that turns a static automation into one that improves: a first step
loads a guidance file, and a final step prunes/updates it.

```yaml
requires:
  - guidance/EMAIL.md
steps:
  - id: load-guidance
    prompt: Load and follow guidance/EMAIL.md.
  # … the work …
  - id: update-guidance
    prompt: >-
      Update guidance/EMAIL.md with anything durable you learned. This file is
      injected verbatim every run and cannot grow unbounded — use judgement about
      when to revise, prune, or consolidate.
```

The **pruning clause is not optional** — without it the file grows until it
crowds out everything else in the context window. Put criteria (judgment) in the
guidance file where they can grow; keep the step pointed at the judgment, not a
hard-coded threshold. Cross-domain learnings go in an `ATTENTION.md`; domain
files stay domain-specific. Your workspace's `guidance/` is yours — there is no
overlay mechanism, and none is coming; a `git`-tracked workspace is the history
and restore path.

## Writing steps that hold up

- **One concern per step** — the step boundary is the unit you will edit later.
- **State the negative space** — the single highest-value sentence is often what
  *not* to do: "This is a read-only run: do NOT mark anything read, do NOT send,
  do NOT edit files."
- **Demand an honest failure** — "If some part of this cannot be carried out with
  the tools you have, say so explicitly rather than approximating." "Nothing
  found" and "could not look" are different answers.
- **Recording is not telling** — if the run writes to a ledger, say explicitly
  that writing the record is not the same as surfacing it.
- **Ask for judgment, not a threshold** — thresholds need editing every time
  reality shifts; judgment plus a guidance file grows.

## Complete worked example

```markdown
---
automation:
  name: Email Check
  enabled: false                    # exemplars ship disabled; flip to true to run
  trigger:
    type: schedule
    expression: every 45 minutes
  notify: auto
  requires:                         # tool names are placeholders — use your packs'
    - mail-cli
    - guidance/EMAIL.md
  inject:
    - argv: ["items-cli", "state"]
      label: "open items"
  steps:
    - id: load-guidance
      label: Load guidance
      prompt: Load and follow guidance/EMAIL.md as written — no override.
    - id: check-inbox
      prompt: >-
        Check the inbox for mail new or newly relevant since your last check in
        this conversation, and tell me what needs my attention using the triage
        rules in guidance/EMAIL.md. Check each listing's own completeness field;
        if a folder or page could not be read, report it as a coverage gap rather
        than treating it as empty. This is a read-only run: do NOT mark anything
        read, do NOT draft, do NOT send. If this run has nothing worth surfacing,
        reply with exactly NOTHING_TO_REPORT — a quiet run is a success.
    - id: update-guidance
      prompt: >-
        Update guidance/EMAIL.md with anything durable you learned; prune or
        consolidate rather than only appending. If some part of this cannot be
        carried out with the tools you have, say so explicitly.
---

Triages the inbox against your guidance. This body is a human-facing description
only; the steps above are what runs.
```

## Validate before you trust it

Save-time validation refuses an invalid file loudly. To check without a running
engine, the engine's own `validate_automation_content()` is the conformance
surface (rejects unknown top-level/step keys, missing/duplicate step `id`, empty
`prompt`, missing `steps`, and steps left in the body). Prefer copying a working
exemplar from
[`examples/automations/`](https://github.com/microsoft/amplifier-drumbeat/tree/main/examples/automations)
and editing it over starting from scratch.
