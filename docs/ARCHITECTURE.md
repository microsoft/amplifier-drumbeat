# Architecture

What drumbeat is, what it deliberately is not, and the contracts that hold at
each boundary.

This document is standalone. It assumes no knowledge of any particular
consumer, and every claim in it is either a contract the code enforces or a
measurement taken from a real deployment. Where a number appears, it was
observed, not estimated.

---

## 1. What the engine is

**drumbeat runs markdown automations as sequential turns in a long-lived agent
session, on a schedule, unattended, and emits a reasoned record of everything
it decided.**

That is the whole product. Five nouns:

| Noun | What it is |
|---|---|
| **Automation** | A markdown file: YAML frontmatter (trigger, notify policy, requirements) plus an ordered list of natural-language steps |
| **Turn** | One step, executed in its own OS process — a worker that imports the agent's engine library — against a pinned session |
| **Drumpack** | A directory of `bin/` executables plus a `drumpack.md` card, brought by the consumer. The engine ships no tools |
| **Delivery intent** | A durable, reasoned event saying "this run's output should / should not reach a human, and here is why" |
| **Run artifact** | Per-run directory holding the result, every step's output, and stderr |

Everything else in this repo exists to make those five reliable when nobody is
watching.

### The one-sentence version of the design

**Policy is markdown the consumer owns; mechanism is Python the engine owns;
and every gate that can silence output must write down why it fired.**

---

## 2. Topology

```
   ┌──────────────────────────────────────────────────────────┐
   │ consumer workspace                                        │
   │   automations/   guidance/   prompts/   drumpacks.txt     │   markdown — policy,
   │   drumpacks/<name>/  (drumpack.md + bin/)                 │   owned by the consumer
   └───────────────────────────┬──────────────────────────────┘
                               │ --workspace <dir>
   ┌───────────────────────────▼──────────────────────────────┐
   │ drumbeat serve      (one instance per consumer)           │
   │   scheduler · runner · pinned sessions · pre-run gate     │
   │   inject turns · rotation · session health · artifacts    │
   │   HTTP API on 127.0.0.1:<port>  (X-API-Key on writes)     │
   │   └── spawns: engine-library worker  (one process/turn)   │
   └───────┬──────────────────────────────────┬───────────────┘
           │ durable event outbox             │ POST /api/turns
           │ runs/engine-events.jsonl         │ (reply → exact session;
           │ (tailed by the consumer)         │  423 locked, 404 unknown)
   ┌───────▼──────────────────────────────────▼───────────────┐
   │ the consuming service                                     │
   │   delivery worker · transport (push/mail/chat/…) ·        │
   │   quiet hours · notification store · reply routing · UI   │
   └──────────────────────────────────────────────────────────┘
```

**One engine instance per consuming project.** Own port, own data directory,
own lock. Multi-tenancy was considered and rejected: it buys nothing at small
N and costs per-tenant scoping, authorization, and a shared daemon whose
failure takes down every tenant at once. The engine is a service you run for
your project — not a platform you register with.

**Auth.** The engine binds loopback only. **Every mutating endpoint requires
`X-API-Key`; there is no loopback bypass on writes.** A loopback bypass makes
an engine authless to every local process — including a pack binary running
inside some *other* engine instance's turn, which could then inject turns into
your pinned sessions. Read-only endpoints may keep the loopback convenience.
Writes never do.

---

## 3. The automation format

A minimal automation:

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
  steps:
    - id: load-guidance
      prompt: Load and follow guidance/EXAMPLE.md.
    - id: check-source
      prompt: Check the source and tell me what needs my attention.
    - id: read-only-guard
      prompt: This is a read-only run; do not send or modify anything.
---

Optional human-facing description. Never parsed for execution.
```

`steps:` is an **ordered list of structured step objects** (`{id, prompt,
label?}`) in the frontmatter — see [`../contracts/automation-file.v1.md`](../contracts/automation-file.v1.md).
Each `prompt` is one freeform natural-language turn; there is no branching, no
looping, no variables, no per-step types, and no expression language. That is
deliberate — see §8. The markdown body is a human-facing description and is
never parsed for execution.

Full authoring reference: [`AUTOMATIONS.md`](AUTOMATIONS.md).

### The parser is strict on purpose

An automation runs unattended. A silently-wrong parse produces silently-wrong
behavior for days before anyone notices. So the parser refuses rather than
guesses: unknown `notify` values, unknown trigger types, a `schedule` trigger
with no expression, a non-boolean `enabled`, a `requires` that is not a list of
strings — each is a loud refusal with the file path and the offending value.

A broken automation is recorded in a durable error log and **listed as broken**,
rather than causing the engine to refuse to start. One bad file must not take
down every other automation's schedule.

---

## 4. The turn execution model

### One OS process per turn

Each step becomes one isolated worker process:

```
python -m drumbeat.agent_worker      # task spec arrives on stdin as JSON:
                                     #   {prompt, session_id, agent_session_id,
                                     #    turn_id, cwd, storage, provider, model,
                                     #    skills, mcp, resume}
```

The worker imports the agent **library** — `amplifier_agent`, and this worker is
the only module in the engine that imports it — and drives its entire embedding
surface, which is four calls:

1. `create_agent(AgentOptions(...))` — one agent;
2. `agent.create_session(SessionOptions(session_id=…, persistence="durable"))`,
   or `agent.resume_session(id)` for a continuing session;
3. `session.start_turn(TurnInput(content=[TextPart(prompt)]))` — one turn;
4. consume `turn.events()` until the one `terminal` event.

That is the whole surface. The library ships every provider in-process, so there
is nothing to prepare, cache, mount, enumerate, or install before a turn runs: no
bundle, no provider injection, no run-time `uv pip install`, no warm-up step.
Results come back over the worker's stdout: display events as NDJSON while the
turn runs, then one terminal envelope carrying the reply and the library's own
token and cost counts.

One thing reaches the turn outside that spec: the **host config**. The runner
sets `$AMPLIFIER_AGENT_CONFIG` on the worker process **only**, pointing at a
per-turn file the engine materializes (see
[`AUTOMATIONS.md` §10](AUTOMATIONS.md)). That file is the single channel through
which reasoning effort can reach a provider request. Skills and MCP servers
travel in the task spec instead, because the library takes both in code.

Because the task spec travels on **stdin**, the prompt is never an OS argument,
so a turn's text can be arbitrarily large.

The first turn of a session's life starts fresh; every turn after it resumes.
The conversation is durable on disk in that session's own storage root (below),
and the library replays it between turns. The engine
holds no in-memory conversation state — which is why an engine restart mid-day
loses nothing but the currently-executing turn.

One turn per process is the contract, not an implementation shortcut: it keeps
the library's process-global state harmless by construction, and it means a
wedged or runaway turn dies alone.

This also means **the engine is not a fork of the agent runtime.** It uses the
stock library through its documented embedding surface. Nothing in the engine
depends on a modified agent underneath, which is the property that keeps it
portable.

### A turn with no working brain is a failure

Booting a session mounts every provider, tool, and hook the library declares.
One that fails to load, or fails its own module validation, does not raise:
the library keeps booting with a reduced module set and logs the failure once
(`Failed to load <type> '<module_id>': <reason>`). Every turn's stderr is
scanned for exactly that line, and the matches are recorded — deduped, sorted,
as `"<type>:<module_id>"` — on the turn's own run-record entry and (aggregated
across the whole run) at the run's top level, persisted to `result.json` and
the `RUN_COMPLETED` event as `module_failures`. See `docs/AUTOMATIONS.md` §10
("Session-init module failures") for the consumer-facing field reference.

**A non-empty `module_failures` fails the run**, and the error names the
modules that did not load. A turn that answered without the modules it was
configured to have did not run on the engine anyone asked for, and the record
has to say so. The measurement that settles it: 96 of 96 runs in one morning
carried exactly this warning on their stderr while every one of those runs
recorded `"failed": false, "error": null` — an independent watcher scanning
stderr correctly flagged the degradation; the engine's own verdict never did.
Visibility that nothing acts on is not visibility.

The same rule closes the other shape of the same defect: **a reply that is
itself a statement of provider unavailability fails the run** — the engine
answering that it has no brain to think with. Measured on the same deployment:
34 runs between 20:30Z and 23:44Z recorded `"failed": false, "error": null`
while their reply said the engine had no provider. The match is **anchored** at
the start of the reply, never a substring search, so an automation that merely
*quotes* the phrase — one reporting on its own fleet's health does exactly that
— is never failed for saying it.

An **unhandled** exception during session init (one the library does not catch
internally) is a different, unrelated path: it still produces `failed: true`
with the real exception text in `error`, exactly like any other turn failure
(§10) — this section covers the two failure modes that do *not* raise.

### Dispatch order among due automations

Runs are dispatched sequentially. On a tick where several automations are due,
something decides who goes first, and for most of this engine's life that
something was list order — which meant a schedule expression was a **bid in an
auction nobody clears, and the auction had no priority**. Measured on the
reference deployment: the fleet demands ~412 runs/day and completes ~127 (31%);
30-minute automations attain 52–72% of their declared cadence, the 20-minute one
46%. The single notify-capable path to the owner starved on exactly equal terms
with bulk background checks.

An automation may now declare `priority: high` (see
[`AUTOMATIONS.md` §2.3](AUTOMATIONS.md)); absent means `normal`. Due `high`
automations are dispatched before due `normal` ones. The sort is **stable**, so
within a tier the prior order survives untouched and a fleet where nothing
declares the key dispatches in byte-identical order to before it existed.

**State the limit as loudly as the feature:** this changes *who waits*, not how
much gets done. No concurrency is added, no running turn is preempted, and
nothing is dropped or deferred beyond what the backlog already imposes. It
cannot move the 31%. The capacity fix — more concurrency, or fewer automations
— is a separate architecture decision this key deliberately does not make.
A knob that reads as a throughput fix while ordering a queue would be exactly
the reassuring-but-inert surface this project refuses.

**Owner precedence is above all of it.** The owner-priority latch is consulted
per automation at dispatch: a due automation whose session the owner is
actively using is deferred to the next tick and left due, regardless of tier.
`priority:` orders scheduled work strictly *beneath* the owner, never alongside.

### Sessions are pinned, not per-run

Each automation resumes **one long-lived conversation**, across runs, for weeks.
That is the single most consequential design choice in the engine, and it is
what makes "review activity **since your last check**" a meaningful
instruction: the prior check is literally in the conversation.

A fresh session per run would be simpler and would be wrong — it would make
every run a cold start with no memory of what it already said, which is the
behavior the whole design exists to avoid.

**Where the pin lives.** The session id is engine state and is stored as engine
state: `<data-dir>/session_pins.json` (default `<workspace>/runs/`), written
atomically under a lock. It is **not** in the automation file. Earlier versions
of the engine wrote it back into the automation's own frontmatter (`session:`);
that key is now **refused** by the parser rather than ignored, with the
migration remediation printed.

The reason is the boundary this whole document is about: an automation file is
**policy** — authored by a person, versioned, copied between machines. A
session id is **machine-local runtime state**. State living inside policy meant
the engine wrote to files it does not own, that every archive of a workspace
carried conversation ids meaningful on exactly one machine, and that restoring
such an archive somewhere else produced automations pinned to conversations
that do not exist. Server state belongs in the server's state directory. See
`docs/AUTOMATIONS.md` for the operator surface (`drumbeat sessions` and
`drumbeat rotate-session`).

**Where the conversation lives.** Each pinned session gets its **own**
agent-storage root under the data dir — `<data-dir>/agent-storage/<agent
session id>/` — handed to the library as the storage location for that session
and nothing else. The engine owns that path and stats it; it never reads inside
it, because the library declares its internal layout private. There is no
shared agent home directory anywhere on the machine, and no per-session file the
engine parses: everything a session *is* lives under the workspace's own data
dir, which is why a renamed or moved project carries its sessions with it and
orphans nothing.

**Session ids are translated at that boundary.** The library accepts ids
matching `[a-z0-9][a-z0-9-]{7,63}`. An engine session id that already matches is
used **verbatim** — the id a human reads in the pin file is the id the library
stores. Any other is lowercased, non-conforming characters become `-`, and a
`-<sha256[:8] of the original id>` suffix is appended, so two distinct engine
ids can never collapse onto one conversation. The translation is pure and
deterministic: the same engine id always yields the same agent id, in any
process, so a resume in a later process finds the same session.

**Existence is a stat of that root.** Present means resumable; absent means the
session is gone and safe to recreate; an I/O error is `UNKNOWN`, and the engine
aborts rather than guessing — silently recreating a session is exactly the
silent-drop defect the pin mechanism exists to prevent. A **fresh** turn removes
the session's storage root before running, so a fresh turn can never resume a
stale conversation.

### The pre-run gate

Before step 1, the engine checks every `requires:` entry:

- A **file** requirement must exist and is injected **verbatim, re-read on
  every single run**. Guidance edited between runs takes effect on the next run
  with no restart.
- A **tool** requirement must resolve to an executable on the constructed turn
  PATH. Its pack card is injected verbatim.

**An unsatisfied requirement aborts the run** with a reasoned record. It does
not run the steps with a missing tool and let the agent report "I could not do
that" — the run never starts, and the failure names the requirement.

### Injections, and the two the engine owns

The engine performs exactly two built-in injections, both domain-free:

1. **`requires:` file content** — verbatim, every run.
2. **The resolved local time, timezone, and UTC offset** — because an agent
   cannot reliably know what time it is, and every "since your last check"
   instruction depends on it.

Everything else the consumer wants present on every run comes through
`inject:` (§6), which is domain-blind. The engine does not know what an "item",
a "notification", or a "subscription" is, and it never will.

### The turn PATH is pinned

```
<pack 1 bin> : <pack 2 bin> : … : <workspace bin> : <PINNED BASE>
```

The base list is captured **once at engine startup**, logged there, and echoed
in `GET /api/capabilities`. It is identical for every turn regardless of how
the service was launched.

This is a scar. Tools once worked only because whoever started the process
happened to have the right directory exported; a restart from a different shell
made every tool vanish, and the agent faithfully reported "I could not do that"
while the real cause was the launcher. Undeclared dependence on the launching
environment is not a contract.

---

## 5. Session lifecycle: when a pinned session must be abandoned

A pinned session accumulates conversation forever. Every rotation trigger in
the code is **mechanically detectable with zero judgment** — no heuristic, no
model call, no operator in the loop — and every one of them rotates through a
single function, so a rotation can never happen without landing in
`session_rotations.jsonl` and never happens silently. The three below are the
health triggers: the failure modes that have actually broken runs.

### Trigger 1 — ceiling hit

The provider rejects the request outright:

```
prompt is too long: 219685 tokens > 200000 maximum
```

This is not a bad day; it is a permanent deadlock. The agent library does not
compact, so a session whose prompt has crossed the provider's hard limit has no
mechanism to shrink itself back under it: every subsequent turn sends at least
as much as the one the provider just refused.

Measured on real run history: one session hit the ceiling and then failed **12
consecutive times over 27 hours**; another hit it and failed twice before a
human rotated it by hand. **Zero recoveries, ever.** A first ceiling hit is
therefore a zero-false-positive death signal, and acting on it immediately
costs exactly one run — the one that already failed.

### Trigger 2 — contract drift

When an automation's steps are rewritten, the pinned session still carries
every *old* instruction as conversation history — and keeps obeying it.

Measured: one session answered a bare sentinel to a step that asks no question,
for **15 consecutive runs**, reasoning about a schedule its automation no
longer declared. The engine fingerprints the *steps* (the contract) and
compares on resume, catching this before the bad run rather than after fifteen.

### Trigger 3 — prompt-token gate

Trigger 1 is correct and, by construction, always one failed run late: it can
only fire *after* the provider has already refused a prompt. Trigger 3 is the
pre-emptive half. Before the first turn of a resumed run, the engine reads the
largest **prompt-token** count recorded for that session's most recent run — the
agent library's own reported `tokens_in`, read back off the run record — and if
it is **over 150,000 tokens**, the session is rotated first and the run then
proceeds on the fresh session.

**The gate measures the failing quantity, not a proxy for it.** Prompt tokens
are the unit the provider refuses on (`prompt is too long: 219685 tokens >
200000 maximum`), and they are the unit the library itself reports and the
engine already records on every step. Nothing has to be inferred from a
session's on-disk representation for the gate to be right — which matters,
because the engine deliberately never reads inside that storage at all.

**Where 150,000 comes from, stated honestly: it is calibrated off the ceiling,
not off a crash distribution.** The two sessions whose true prompt sizes the
provider ever reported crashed at **219,685** and **201,361** tokens. The
smallest observed refusal is therefore 201,361, and 150,000 sits about a quarter
below it — inside a region where no refusal has ever been observed, with room
for one more turn's growth. The gate is deliberately not asked to *predict* the
next turn's size; it is asked to keep a session inside that region.

**Why tokens are the unit, and stored size is not.** For those same two
sessions, the stored bytes behind each prompt token differed by **3.4×** (10.4
MB of stored conversation → a 219,685-token prompt; 33.0 MB → 201,361 — the
*smaller* store produced the *larger* prompt). A prompt does not carry
everything a session stores, and a session stores plenty a prompt never carries,
so any size-derived threshold is a coin flip between a false alarm and a missed
failure with no way to tell which one you got. That spread is the reason this
gate counts what the provider counts.

**Why not lower:** rotation is not free — it abandons accumulated conversation
memory directly. A lower gate rotates healthy sessions and buys no additional
observed crashes.

**An unmeasured session is not an over-threshold one.** When no run record for
that session carries a count, the gate does not fire: "unknown" must never read
as "yes" and abandon a live conversation, the same posture the drift and
lifecycle checks take.

The gate is an engine deployment knob, overridable via
`$DRUMBEAT_SESSION_ROTATE_TOKENS` (a positive integer; an unusable value is
reported on stderr and the default is used, so the mechanism cannot be
disabled by a typo). There is deliberately **no automation frontmatter key** —
the automation file's vocabulary is closed, and this is a property of the
deployment, not per-automation policy.

The count a run started from is recorded on the run record as
`session_prompt_tokens_at_start`, and `drumbeat session-health` reports the same
measurement as `prompt_tokens`.

Trigger 1 stays exactly as it is. This gate reduces how often the ceiling is
reached; it never replaces the backstop for the sessions that get there anyway.

### Rotation is safe, and that is measured

Rotation abandons the conversation, not the state. `run()` re-injects the durable
state on **every** run — the `requires:` guidance files verbatim, plus the
consumer's `inject:` state turn. Checked across every rotation performed in the
originating deployment: **71/71 tracked ids survived one automation's rotation
boundary, 27/27 survived another's**, and no post-rotation run contained an "I
don't have that context" phrase.

An accumulated conversation is sediment, not memory. What must survive a
rotation is re-injected on every run anyway — which is exactly why `inject:`
exists.

### There is no compaction to tune, and rotation is why that is survivable

The agent library does not compact and exposes no context-pressure signal
(measured; see [`../contracts/agent-binding.v1.md`](../contracts/agent-binding.v1.md)
§9). A pinned session therefore grows monotonically until something abandons it,
and the only two things that ever do are the triggers above: the pre-emptive
prompt-token gate, and the provider's own refusal behind it.

That is a design constraint this engine states rather than works around. It has
no code that computes a context budget, a target size, or a protected-content
window, and it will not grow one: an engine that second-guessed the library's
context handling would be a fork of the agent runtime in all but name, which is
precisely what §4 rules out. What it does instead is stop depending on any
single turn surviving a long conversation at all — an automation's `inject:`
state is carried forward on every subsequent turn of the same run (see
`inject_recap_blocks` in `runner._run_body`), so a run's correctness never rests
on how much of an earlier turn is still in context.

---

## 6. `inject:` — the only aperture for consumer state

An `inject:` entry names an argv. Before step 1 of every run, the engine
executes it with the turn environment and constructed PATH, and **its stdout
becomes a turn**.

```yaml
inject:
  - argv: ["example-cli", "state"]
    label: "current state"
```

That is the entire aperture. The engine does not know what the tool returns and
does not parse it. This is what lets a consumer put arbitrary durable state in
front of every run without the engine ever acquiring the consumer's domain.

### The hybrid-sentinel contract

**Classification order is fixed: timeout → exit code → stdout.**

| Tool result | Engine behavior |
|---|---|
| Times out | **Abort the run**, voiced through the intent path |
| Exits non-zero | **Abort the run**, voiced |
| Exits 0, stdout (whole, stripped) is byte-exactly `INJECT_IDLE` | **Inject nothing; the run proceeds.** A reasoned `inject_skipped` event is written |
| Exits 0, stdout is bare-empty | **Abort, loud** |
| Exits 0, `expect_prefix` declared, stdout does not start with it | **Abort, loud** — malformed inject content, never fed to the model |
| Anything else | **Inject stdout verbatim** as a turn |

Three things in that table were paid for:

**Bare-empty aborts, `INJECT_IDLE` proceeds.** An earlier draft said "empty
stdout aborts" *and* "emit nothing when there is genuinely nothing" — mutually
exclusive sentences that would have bricked the system at its designed success
state: resolve everything the consumer tracks, and every subsequent run aborts
forever. A tool with nothing to say must *say* so. **Silence is never a
contract value** — a crashed pipe and a genuinely idle state must not share an
observable.

**The sentinel match is byte-exact on the whole stripped stdout** — not a
prefix, not a regex. A near-miss anchor that could not match what a real
producer writes is a failure this project has already shipped once.

**`INJECT_IDLE` is deliberately distinct from `NOTHING_TO_REPORT`.** The latter
is a value the *agent* emits inside turns; the former is a value a *tool* emits
on stdout. Distinct meanings get distinct tokens — the worst bugs in this
system's history were semantic overloads, one identifier meaning two things.

---

## 7. The delivery seam — the engine never touches a transport

**The engine evaluates delivery policy. It never performs delivery.**

Per run, the engine evaluates the automation's `notify:` policy — including the
auto-notify judgment turn and its sentinel, which are mechanism and stay
engine-side — and emits exactly one **delivery-intent event**:

```json
{"type": "delivery_intent", "run_id": "…", "automation": "example-check",
 "session_id": "…", "verdict": "deliver|withhold|demote",
 "gate": "<one of the closed gate enum>",
 "reason": "<required, no default>", "text": "<the full final output>"}
```

alongside `run_started`, `run_completed` (carrying **all** step replies plus
which was selected as `final_reply` and by what rule), `inject_skipped`,
`session_rotated`, `automation_error`, and `turn_completed`.

### Why this boundary exists at all

Three independent gates could each silently zero a run's output, and did. An
automation ran 56 times and produced nothing a human ever saw, and no record
anywhere said why — the absence looked exactly like "nothing to report."

So the rule is now structural: **a run record without a delivery-intent event
is an invalid run**, and the engine enforces that on itself. Every gate firing
is a required, reasoned, queryable record. "N runs, zero intents to deliver" is
one API call, not an investigation.

`automation_error` events are part of this: a dead automation was log-only once
and stayed dead for 27 hours. The consumer is expected to surface them.

### A crashed pass must not read as a quiet one

An intent event says what happened to a run's output. It does not, on its own,
close the sharper version of the same failure: a **notify-capable** automation
that crashes mid-pass reports `notified: false`, which is byte-for-byte what a
healthy run reports when it judges nothing needs the owner. Measured: a run
died on `ContextLengthError` at step 5 of 8 and was operationally identical to
a calm day. The evidence existed — `result.json`'s `failed: true`,
a `failures.log` line, an `automation_error` event — but answering *"is this
automation's most recent run a crash?"* meant walking every run directory or
replaying the outbox from a cursor.

`<data-dir>/failed_passes.json` is the standing one-read answer. At most one
record per automation slug — *its most recent run failed, and nothing has
succeeded since* — written at the same `_persist_run` choke point every run
already passes through. Two edges, both of them: a failed run records,
a successful run clears. A crash flag that never clears is a stuck alarm, which
is its own untrustworthy surface.

Consecutive failures **replace** rather than accumulate, which is what makes
the second consumer of that record correct: the next run of the same automation
carries a plain-language notice on its **first turn** saying the previous run
did not complete, when, which run, and with what error — riding as a preamble
on whichever turn actually runs first, so it can never be skipped because a
system-prompt, requirements, or inject turn came ahead of step 1. The notice
tells the agent to treat that interval as **unchecked** rather than quiet, and
explicitly not to reconstruct what the failed pass would have said: a
fabricated stand-in would be the same defect with better manners.

Scoped to notify-capable automations. A `notify: never` run's silence was never
going to be read as a verdict, so it has no ambiguity to resolve — its failures
stay loud exactly where they already were.

The store's posture is deliberately the *opposite* of `session_pins.json`: that
one refuses to be read as empty, because reading-as-empty there is a silent
mass rotation. Here, raising would take down healthy runs to protect a notice,
so a damaged store is reported loudly on stderr, read as empty, and replaced by
the next write. The cost of that fallback is one missed notice; the cost of the
alternative is every run of every automation failing because a marker file got
truncated.

### Outbox semantics

- **The file** is an append-only, lock-guarded `runs/engine-events.jsonl`. One
  writer: the engine. Appends are line-atomic and performed inside the lock,
  with **fsync before releasing on `delivery_intent`** — an intent that only
  ever existed in the page cache is exactly the failure this seam exists to
  prevent.
- **The cursor is a byte offset**, not a sequence number. Byte offsets survive
  the writer restarting; a per-process counter does not. Events carry no
  consumer-visible monotonic sequence.
- **Torn tail**: a reader never parses past the last newline. A partial final
  line is "not yet written," never an error.
- **Delivery is at-least-once, and a duplicate beats a drop.** The recommended
  consumer order is **push-then-persist**, with dedup at record-mint time so a
  crash-replay is a store-level no-op. The inverse order converts a crash into
  a silent non-delivery *recorded as delivered* — the original failure, rebuilt
  at the new boundary.

**Why an outbox and not a webhook:** a webhook to a consumer that is down
*loses the intent*. The outbox is durable — a consumer offline for an hour
picks up every intent on restart, in order. The cost is one poll interval
against turns that take minutes. Accepted.

**Honest residual:** the engine cannot know whether the consumer's *transport*
succeeded. Intent accounting is the engine's; send accounting is the consumer's.
"Ran, but never reached a human" is answerable by joining two honest records
instead of interrogating a silence.

**Declared, not hidden:** the outbox does not rotate today, it grows unbounded,
and it embeds the full output text of everything the system ever said or
withheld — a permanent shadow copy, and a sensitivity to name plainly before
you point this at anything private. Its size, age, and the consumer's cursor
lag are reported by both `drumbeat doctor` and `GET /api/health`.

---

## 8. The consumer boundary — what the engine is NOT

Each fence rejects a specific temptation this design met and refused.

- **Not a delivery system.** No push, no subscriptions, no quiet hours, no
  notification store, no transport of any kind.
- **Not an item tracker.** No items, no priorities, no urgency scoring, no
  domain objects. `inject:` is the only aperture, and it is domain-blind.
- **Not a policy owner.** Ships zero automations-as-defaults, zero guidance,
  zero prompt text. The `examples/` tree is documentation, not defaults:
  there is **no built-in fallback** if a consumer's files are missing. A
  missing prompt file is a loud failure, not a quiet substitution.
- **Not a platform.** No multi-tenancy, no pack registry, no permission model
  beyond its API key, no trigger grammar beyond `schedule | manual`. The order
  is *connect → prove → generalize*, never *generalize → connect → hope*.
- **Not a fork of the agent runtime.** One OS process per turn, the stock
  library through its documented four-call embedding surface, a fresh session
  once and resume forever, and the library's absence of compaction routed
  around via rotation rather than second-guessed.

### Where the line actually falls

| Concern | Owner |
|---|---|
| When does this run? | **Engine** (scheduler) |
| What does it do? | Consumer (automation markdown) |
| What tools exist? | Consumer (packs) |
| Should this output reach a human? | **Engine evaluates**, emits a verdict + reason |
| How does it reach a human? | Consumer (delivery worker + transport) |
| Which session does a reply resume? | Consumer maps its own identifier → session; **engine executes the turn** |
| What is an "item"? | Consumer, exclusively. The engine has no opinion |

---

## 9. Reply routing — split at the identifier

The load-bearing capability, split so neither side holds the other's state:

```
client → consumer: "reply to <its own notification identifier>: <text>"
       → consumer resolves that identifier → session_id   (its map; unknown → 404,
                                                           "refusing to guess a session")
       → engine POST /api/turns {session_id, text, origin}
       → engine acquires the per-session lock   (locked → 423, honest, not queued;
                                                 unknown session → 404)
       → 202 {turn_id}; consumer polls GET /api/turns/{turn_id}
```

- The **mapping** is the consumer's, minted at delivery time, persisted
  server-side, **never held by a client**.
- The **turn** is the engine's: the same lock that arbitrates scheduled runs
  arbitrates replies. One lock namespace, so "two processes resuming one
  session" cannot re-enter through the new door.
- **A reply targets the session that produced the notification — by explicit
  `session_id`, even if the automation has since rotated.** Rotation retires a
  pin; it never destroys the session behind it, so the reply lands in the
  conversation it answers.
- **Never a module-level "current session" anywhere in the chain.** That
  variable passes every single-conversation test and cross-contaminates every
  concurrent one. The regression gate is two automations, two sessions, two
  notifications, replied to independently, each resuming its own.
- A `423` **must never lose the user's typed text.** The client keeps the draft
  and re-offers send. Losing typed input on an honest busy signal is a worse
  failure than the busy signal.

---

## 10. Fail-loud as contract

The single rule that generated most of the specifics above:

> **Every gate that can reduce output writes a reason. Every absence that could
> be mistaken for "nothing happened" is made into a record instead.**

Concretely, and non-negotiably:

- **No silent fallback.** An unknown session is a 404, never a guess. A missing
  prompt file is an error, never a built-in default. A duplicate tool name
  across packs refuses to start, naming both packs, rather than letting load
  order pick a winner.
- **Required means required, with no default.** A rotation reason, a delivery
  intent's reason — the field has no default value, so it cannot be silently
  omitted.
- **A skip is a record, never an inference.** `INJECT_IDLE` produces an
  `inject_skipped` event naming the tool and its label.
- **Listings can say "this has never fired."** An automation that is enabled
  but structurally unfireable renders as unfireable in the one listing
  everything reads, instead of looking healthy and doing nothing.
- **Timestamps are explicit-UTC ISO-8601; rendering is the client's job.**
- **A partial success is a failure.** A step failure aborts the run rather than
  reporting partial success as success.
- **A record is written atomically or not at all.** `result.json` — the
  canonical run record every consumer reads — lands via temp file plus
  `os.replace`, so a writer killed mid-write leaves an orphaned `.tmp`, never
  a truncated or zero-byte record at the path anyone reads. Measured: two
  zero-byte `result.json` files on the owner's box. A zero-byte record is not
  a loud failure; it is an **absence**, and a consumer counting parseable
  records simply does not see that run.
- **A log's name is part of its contract.** See below.

Every one of these replaced a real incident where a system did something
reasonable-looking and nobody could tell it had gone wrong.

### Which log is the failure log

Two files sat side by side and one of them lied by its name.

| File | Carries | Quiet means |
|---|---|---|
| `failures.log` | **RUN failures.** One greppable line per failed run | nothing is failing |
| `automation_lint.jsonl` | **CONFIG LINT.** An automation *file* that would not parse | nobody has broken an automation file |
| `vocabulary_errors.jsonl` | Config lint for the guidance vocabulary file | same |

The lint log used to be called `automation_errors.jsonl`. Its last write was
08-27 — correctly, because nobody had broken an automation file since 08-27 —
and anything watching a file whose name contained "automation" and "errors"
read a flatline straight through two days that each produced a hundred run
failures. Nothing malfunctioned. The name invited a reading it could not
support, which makes it telemetry that lies.

So the lint log is now named for what it is, and `drumbeat doctor` reports
**which file carries run failures and how fresh it is** — because a monitoring
pipe that has gone quiet looks exactly like a system that has stopped failing.
An unreadable or unparseable failure log is reported as `UNKNOWN`, explicitly
*not* as an absence of failures. A leftover pre-rename `automation_errors.jsonl`
is named as such, so an operator who finds a stale file learns why it stopped
growing instead of trusting its age.

---

## 11. Where things live

**Two roots, not one: policy and state.** `--workspace` is *policy* — files a
human authors, versions, and copies between machines. `--data-dir` is *state* —
files only the engine writes and no human should ever hand-edit or version.
They were the same tree by construction until the flag existed, which made
"the server owns its state" a rule kept by discipline. Separated, it is a
structural property: **a `git clean -fdx` in a policy repo must be incapable of
destroying server state, not merely unlikely to.**

```
<workspace>/                        POLICY -- yours. Put it under git.
  automations/*.md         the automations (consumer-owned)
  guidance/*.md            policy files referenced by requires: (consumer-owned)
  prompts/*.md             engine-loaded prompt text (consumer-owned, no fallback)
  drumpacks.txt            ordered list of drumpack directories
  drumpacks/<name>/        in-workspace drumpacks (drumpack.md + bin/)
  injectors.yaml           turn-context injectors, optional (see docs/INJECTORS.md)
  bin/                     workspace-local executables, on the turn PATH

<data-dir>/                         STATE -- the engine's. Never version it.
  <slug>/<run_id>/         result.json, step-NN.txt, stderr.log
  engine-events.jsonl      the delivery-intent outbox (engine-written)
  session_pins.json        which conversation each automation resumes
  failed_passes.json       notify-capable automations whose latest run crashed
  failures.log             RUN failures -- the failure telemetry
  session_rotations.jsonl
  session_contracts.json
  automation_lint.jsonl    automation FILES that would not parse (config lint)
  vocabulary_errors.jsonl  guidance vocabulary files that would not parse
  api_key
  .scheduler.lock  .scheduler.drain  .session-locks/
```

`--data-dir` **defaults to `<workspace>/runs`** — the pre-split layout, byte
for byte, because behavior preservation is the point of introducing a seam
before anyone moves through it. That default puts state *inside* the policy
tree, which is exactly what the split exists to prevent, so `drumbeat doctor`
reports a containment warning whenever the data dir resolves inside a
git-tracked workspace. **Pass `--data-dir` explicitly the moment your workspace
becomes a git checkout.**

**A data-dir resident's address is a property of the data dir, never of a
process's or a turn's cwd.** This is stated as an invariant because it was
learned twice, the hard way: consumer tools that resolved their store as
`./runs`, and the engine's own log modules that captured `Path.cwd()` at
import, both silently followed the workspace when the two roots split apart —
writing state into the policy tree, where `.gitignore` made it invisible. The
engine exports `DRUMBEAT_DATA_DIR` into every turn's environment for exactly
this reason; anything that keeps state should resolve it from there, not from
where it happened to be started.

**Single writer per file, across the boundary.** This is what makes a shared
directory safe between the engine and its consumer. No engine-written file is
ever written by the consumer, and no consumer-written file is ever written by
the engine.

**`<slug>/<run_id>/` is not only scheduled automations.** A bare-`session_id`
turn accepted at `POST /api/turns` — the notification / Conversation **reply**
path — is persisted here too, under a slug derived from the session id, so a
typed reply is retrievable via the same runs API as an automation run (see
`runner._persist_session_turn`).

**Realtime voice-call transcripts do NOT live in this data-dir.** A voice call
never reaches drumbeat: the gateway mints the realtime session and the device
streams straight to the provider. A voice call's turn-by-turn record is written
by the upstream voice **gateway**, not here — a voice call never invokes
drumbeat's turn path, so drumbeat's `runs/` holds no voice-session records.

---

## 12. Reading order

| You want to | Read |
|---|---|
| Write your first automation | [`AUTOMATIONS.md`](AUTOMATIONS.md) |
| Give the agent tools | [`DRUMPACKS.md`](DRUMPACKS.md) |
| Make an automation actually good | [`TUNING.md`](TUNING.md) |
| Copy a working starting point | [`../examples/`](../examples) |
| Copy the `inject:` pattern | [`../tests/packs/minimal/`](../tests/packs/minimal) |
