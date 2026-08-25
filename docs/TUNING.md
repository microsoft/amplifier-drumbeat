# Tuning automations

Your automations run. Steps execute, sessions resume, tools get called. The
question this document answers is the one that comes next: **how do you get
from "it runs" to "it is trustworthy, and worth the attention it consumes"?**

The exemplar automations that ship with drumbeat's packs are described as
battle-honed. This is the technique that honed them: a repeatable loop, run
against a live deployment for ten days and roughly 100 commits, every one of
them written as a post-mortem. The worked examples below are that system's
real failures and real numbers. Where the record contradicted itself, the
recomputed number is used and the disagreement is named.

The one-sentence version: **treat your own live traffic as the eval, your
own ledger as the training data, and your own record as subject to the same
honesty rules you impose on the agent.**

## The loop at a glance

The setting that makes it work: **run live from day one**, with safety
rails. Then, repeatedly:

1. **Instrument silence before tuning anything.** Every gate that can zero
   an output must write a reasoned record when it fires.
2. **Review usage, not output.** Daily: what the human acted on, versus
   what was voiced, versus what sat silent.
3. **Trace anomalies to run artifacts, never to plausibility.** Read the
   actual step outputs and classify: wrong policy, bug, or something else.
4. **Fix policy in markdown.** Reach for code only when policy cannot
   express the fix.
5. **Derive rules from your own ledger, and test them counterfactually
   before shipping.**
6. **Pre-state your falsification test before reading the results.**
7. **Read instances individually before acting on aggregates.** A count is
   a hypothesis; the instances are the evidence.
8. **Distrust your own counters, and forbid defaults on judgment fields.**
9. **Make every human disposition teach the system something.** Different
   closing verbs name different failures.
10. **Keep the record honest, including about yourself.** Post-mortem
    commits; wrong diagnoses corrected in the open.
11. **Session state is part of the tune.** Long-lived pinned sessions
    degrade in ways that look like policy failures.

Skim the bold sentences; study the worked examples. Each section below is
one step of the loop.

## The setting: run live from day one

Real sources, real stakes. In ten days of tuning the system this document
draws on, synthetic scenarios produced no tuning findings; every finding
came from live runs reviewed after the fact. Even the review methods that
used simulated readers earned their findings by being pointed at real
artifacts -- real transcripts, real overnight runs, a real ledger -- never
at invented inputs. For a system whose job is judgment over live traffic,
there is nothing else to be right about.

Live-from-day-one is only sane with rails, and the rails are the engine's
own discipline plus two policy lines:

- **Fail loud, never fall back.** A silent fallback under generalized
  tooling does not merely produce a wrong result -- it deletes the finding
  that the tool surface is thin.
- **Nothing outbound to third parties without consent.** Draft, propose,
  queue -- never send. The deployment behind these examples had no code
  path anywhere that transmits a drafted act to another person.
- **Irreversible operations tighten as the human steps back.** After one
  unexplained item loss, delete was gated behind an environment variable
  available to a human at the CLI and refused inside automations. As
  review attention drops, the bar on irreversibility rises.

## 1. Instrument silence before tuning anything

**You cannot tune what you cannot see, and the most expensive failures are
silent ones.** Every gate that can reduce an automation's output to nothing
must write a reasoned record at the moment it fires. Drumbeat's
`delivery_intent` event exists for exactly this: every run records a
verdict -- deliver, withhold, or demote -- plus the specific gate that
decided it, and the withheld text itself. "Has this automation ever
delivered?" must be a question the system can answer about itself.

**Worked example.** A daily-rollup automation ran 57 times and delivered
once -- and the one delivery was the post-fix verification run. Three
independent gates could each zero its output: final-reply selection (the
real report was written in step 3, but only the last turn's reply was
eligible for delivery), the notify policy (`urgent-only`), and an urgency
marker regex anchored so tightly it could not match the markdown an agent
actually writes. None of the three was obliged to record that it had
fired. The failure was invisible for three days because silence is the
designed success state: "nothing to say" and "the delivery path is broken"
produced the same observable -- nothing. The day a withheld-notifications
log existed, the failure was found; it was fixed the same day.

The general form, from the failure catalogue that pass produced: **a gate
with no record is indistinguishable from the system quietly ceasing to
notice.** Before you tune a single policy word, verify that every zeroing
path in your automations writes its reason. Otherwise your first tuning
signal is a lie.

## 2. Review usage, not output

**Daily, compare three sets: what the human actually acted on, what was
voiced to them, and what sat silent in the record.** The gaps between
those three are the tuning signal. Output review -- reading the agent's
reports and nodding -- finds prose problems. Usage review finds product
problems.

**Worked example.** One source came to dominate the ledger -- two-thirds
of everything ever minted -- and the human's few daily dispositions were
spent swatting its items away. The obvious reading, and the first recorded
diagnosis, was "that source is noise; filter it." Tracing before filtering
showed something else entirely: an identity bug. Item identity was minted
from `(source, sender, timestamp)`, and agents were passing wall-clock
time instead of the underlying event's own durable time -- so the same
ongoing need minted a brand-new id on every poll. Of 159 items from that
source, 32 were eventually resolved as duplicates and 37 resolutions
explicitly cite a timestamp problem. The content was real; the identity
was broken. **Filtering would have been exactly the wrong fix** -- it
would have suppressed real needs while leaving the id churn intact.

The lesson is not "don't filter." It is that usage review tells you where
to look, never what is wrong. What is wrong lives in the artifacts.

## 3. Trace anomalies to run artifacts, never to plausibility

**Read the actual step outputs.** Every run leaves artifacts -- the
result, each step's reply, stderr. For every anomaly, force the finding
into one of three buckets, because they have different fixes and
conflating them wastes the tune:

| Bucket | The fix lives in |
|---|---|
| (a) Policy working as written, and the policy is wrong | Guidance and automation markdown |
| (b) A bug | Code, or the tool pack |
| (c) Something else -- degraded session, stale process, broken instrument | The runtime, not the words |

**Worked example, bucket (b) wearing (a)'s clothes.** A review reported 23
open items had never been voiced -- which read as a policy failure of the
re-surfacing rules. Most of them had been voiced. The counter behind the
claim credited an item only if its sender string appeared verbatim as a
substring of the delivered text; senders like a bare notification address
or a synthetic label are strings no agent ever writes in prose, so those
items could never be credited no matter how often they were voiced --
items demonstrably voiced four times read as never-voiced. Worse, the
loop ran backwards: the agent read the broken counter from its own ledger
injection and re-voiced "never notified" items again and again -- **a
repeat amplifier, not a suppressor**, with some items voiced nine times.
Tuning the re-surfacing policy against that counter would have been tuning
against noise.

**Worked example, bucket (a) found precisely.** A run minted four new
items and then reported nothing, its own step output reasoning "already in
the ledger" about an item that had been in the ledger for zero minutes.
That is not a bug -- the policy as written let it happen. **Recording is
not telling.** The fix was one line of guidance: an item's existence in
the ledger is never evidence the human has been told about it. Bucket (a)
fixes are often one sentence; you only find the sentence by reading the
run.

## 4. Fix policy in markdown; reach for code only when policy cannot express it

**The engine's mechanism/policy split is the tuning surface.** Guidance
files and automation steps are where judgment lives, and editing them
requires no deploy, no restart, no release. This is what makes the loop
fast enough to run daily: most fixes are text.

**Worked example.** The timestamp-identity fix from step 2 shipped
entirely as guidance -- zero code changes -- as three cases:

- **Message-shaped sources** (an email, a chat message, a transcript
  action item) use the message's own timestamp -- for a transcript item,
  the meeting start plus the utterance's own offset, never the bare
  meeting start, or every action item in one meeting collides onto one id.
- **Polled conditions** (a session sitting at a prompt, a service still
  degraded) check the open ledger first and mint nothing if an open item
  already covers the need -- and even then prefer a per-entity signal
  (the session's own last-activity time) over the clock.
- **Aggregates** ("23 sessions stuck") get split into one item per
  entity, because an aggregate has no durable time and each entity does.

Duplicate minting dropped immediately. The bug was never in the hashing
code -- only in what value the agent was told to pass, which is policy.

The boundary runs the other way too: when a rule needs a guarantee --
"this field is required and has no default," "this write either lands or
raises" -- that is mechanism, and markdown cannot hold it. Put the
judgment in guidance and the enforcement in the tool.

## 5. Derive rules from your own ledger, and test them counterfactually before shipping

This is the centerpiece, shown at full length because every stage matters.
The problem: **producer/consumer imbalance.** The system minted 97 items
in one day against a human ceiling of roughly 3 dispositions per day. The
consumer is one person and cannot be multiplied -- the only lever is the
producer.

**Measure first.** The day's mints, read from the store: 97 minted, 68
closed by the system the same day (34 of those were duplicate cleanup, not
resolution), and exactly 2 ever reached a human disposition.

**Propose, then let the data reject you.** The first rule proposed was
"decline anything the system can watch for itself." Checked against the
real ledger before shipping, it was **backwards**: a checkable discharge
condition was present on precisely the items that most genuinely needed
the human -- four PR review requests, a permissions consent expiring,
the human's own committed action items. The rule would have kept the
items the system explicitly cannot help with and dropped the ones it can.
It would also have paid the agent to write a reason-code in order to keep
an item, corrupting that field the same way an urgency field had already
degenerated (step 8). The rule was wrong, the record says so, and the
ledger is what said it first.

**Derive what the data supports.** Reading the day's 97 mints
individually: 28 were written with a disjunctive discharge -- "the human
acknowledges X, **or** the condition clears itself" -- and 26 of those 28
closed via the machine branch. Another 28 named no person at all. The
rule that falls out is **no alternative discharge**: before minting, name
the person whose act would discharge this, then check that their act is
the only thing that would. If you can honestly write "or it clears
itself," nobody was blocked, and this was never an item.

**Counterfactual count before shipping.** Applied to that same day's 97:
56 declined, 41 kept -- and, checked by name, every load-bearing item
kept: all the PR reviews, the consent expiry. The counterfactual is the
cheapest possible test of a policy rule, because the data already exists
and nobody is interrupted while you run it.

**Ship as guidance plus a fail-loud tool.** The judgment shipped as
guidance; the mechanism shipped as a decline verb that writes one line to
a log and nothing to the store, with a required "discharged-by" field --
if you cannot fill it in by naming a person, you may not decline. A write
failure raises, because a throttle with no record is indistinguishable
from the agent quietly ceasing to notice.

**Verify against the next day.** Mints fell 97 to 19 (recomputed from the
store by local day). The decline log recorded roughly 600 reasoned
declines in its first day -- observations the system still noticed and
wrote down, but that no longer occupy a ledger row that re-surfaces
forever.

**Keep the honest floor in the rule itself.** The guidance carries its own
residue: the kept set is still an order of magnitude above what one human
disposes of per day, and that residual is not a minting problem. Real
obligations queue faster than one person clears them. **Do not start
declining real needs to make a number look better** -- that sentence was
written into the rule so that nobody, human or agent, "fixes" the gap by
quietly suppressing the genuine work.

## 6. Pre-state your falsification test before reading the results

**Decide what over-firing would look like before the first night's data
exists, and write it down.** Without a pre-stated test you will read any
outcome as success -- mints fell, therefore the rule works. Mints falling
is equally consistent with the rule eating the items it was designed to
protect.

For the decline rule, the protected set was fixed in advance by the
counterfactual: the named, load-bearing items -- human-authored requests,
expiring consents -- that the rule must never touch. The next morning's
check is against that pre-stated list, not against whatever shape the
totals happen to take. A drop in mints from the machine-watched sources
with the human-message sources untouched is the rule working; declines
climbing while human-message mints fall is the rule over-firing, and no
total can tell you which happened. This is the overfitting guard for
policy tuning, and it costs one paragraph written at ship time.

## 7. Read instances individually before acting on aggregates

**A count is a hypothesis; the instances are the evidence.** Twice in ten
days, a number argued one way and reading the items argued the other.

**Worked example one.** A retired near-duplicate merge kept logging what
it would have merged, observation-only. The log accumulated 24 entries in
a day, which read as evidence the retirement was a mistake -- 24 missed
merges. Reading the 24 individually: **5 were dangerous false positives**,
including two distinct PR reviews in different repositories that merely
read alike, and two unrelated work sessions. Restoring the merge would
have buried real, distinct work under one id. The retirement was right;
the doubt was a count doing the work only reading can do.

**Worked example two.** A client code comment stated reply turns took
"5 to 14 minutes, observed." The figure rested on three measurements.
Measured across 88 real replies: median 43.5 seconds, p95 228 seconds --
an order of magnitude off, and it had already propagated into two other
components as a design assumption. A number stated confidently in one
place gets copied before anyone recomputes it.

## 8. Distrust your own counters, and forbid defaults on judgment fields

Two forms of the same disease: instruments that lie in a known direction
still get consumed as instruments.

**Judgment fields degenerate toward their cheapest value.** An optional
urgency field, filled by agents under instruction, converged to "medium"
on every one of 18 open items within days of shipping. The failure was
invisible because the field was never null -- every surface showed a
populated value, and only the distribution revealed that it carried no
information. The fix that held: **required, with no default, grounded in
evidence** -- the value must quote the source ("no rush stated," "due
Friday," "blocks a colleague's onboarding"), enforced at write time below
the CLI so no caller can skip it -- plus the distribution logged every
run, so degeneration shows up in a day rather than a week. A default that
is always chosen is the same class of defect as a guard that always
passes.

**Audit the counters your tuning decisions rest on.** The voiced-count
bug in step 3 nearly drove a re-surfacing "fix" for a problem that did
not exist. Before a counter justifies a policy change, verify it against
a handful of raw artifacts: pick items the counter describes, read their
actual runs, confirm the counter and the artifacts agree. The deployment
behind this document found its counter wrong at that exact check -- a
delivered run named four items by id, and all four still read as never
notified.

## 9. Make every human disposition teach the system something

**Distinguish the closing verbs, because they name different failures.**
One undifferentiated "close" throws away the labeled training data your
next guidance edit needs. Three verbs, three gaps:

| The human says | It means | The failure it labels |
|---|---|---|
| "done" (by hand) | The world changed and the system did not see it | Closure detection -- the system should have observed the discharge itself |
| "not mine" | Right item, wrong owner | Ownership assignment |
| "dismiss" | Right owner, read it, declining to act | Surfacing judgment -- it was correct that the thing existed and wrong that it needed the human |

A done tapped by hand is a discharge-detection bug to trace. A cluster of
not-mine on one source means the stake-assignment guidance for that
source needs work. A run of dismissals means the minting bar is too low
-- or, per step 2, means something upstream is manufacturing duplicates.
Route each verb's accumulation to its own fix, and the human's smallest
actions become the highest-quality signal in the loop.

## 10. Keep the record honest, including about yourself

**Write your tuning changes as post-mortems: symptom, root cause, what
made it invisible, and -- when a diagnosis was wrong -- the correction,
kept visible.** In the deployment behind this document, at least sixteen
commits open by withdrawing or correcting a prior claim: "I diagnosed it
wrong," "the finding was wrong -- the counter is broken," "my candidate
rule was wrong and the data said so." Those corrections are not
embarrassments in the record; they are the record's value. Two numbers
stated confidently in commit messages were later found wrong by
recomputation -- a downtime figure and a run count -- and were corrected
in a follow-up rather than quietly amended. The pattern worth internalizing:
a number stated in a title propagates into design documents before anyone
recomputes it, so recompute before you cite, and correct in the open when
you were wrong.

Two recurring shapes of wrong diagnosis, worth watching for in your own
tuning: **the plausible neighbour** (the wrong cause sits adjacent to the
right one and explains all the available evidence) and **counting instead
of reading** (step 7).

Periodically, run a failure-catalogue pass: group the accumulated
post-mortems into classes, and for each class ask one question -- *would
the current design make this failure impossible, or at minimum loud?*
"Loud" is the weaker bar and is usually the right one. Nearly every
failure in the source record was recoverable in minutes once seen; what
made them expensive was that nobody saw them. The catalogue becomes the
design input for the next round of structural work.

**The system's own instruments obey the same rule.** Never falsify the
record; a run without a delivery-or-withhold record is invalid by
construction. That invariant is enforced by the engine precisely because
the tuning loop is worthless the moment its inputs can be quietly wrong.

## 11. Session state is part of the tune

**Long-lived pinned sessions degrade in ways that look like policy
failures.** Before you rewrite guidance, check the session.

**Worked example.** An automation answered a step that asks no question
with a bare not-applicable sentinel, fifteen consecutive runs, while
reasoning about a schedule that had been edited out of its automation
file -- the pinned session still carried every old instruction as
conversation history and kept obeying it. That reads exactly like a
policy failure. It is contract drift, and no guidance edit fixes it.

What the tuning practice looks like:

- **Watch tokens, not megabytes.** Measured against the only two prompts
  whose true token counts the provider ever reported: a 10.4 MB
  transcript produced a 219,685-token prompt while a 33.0 MB transcript
  produced 201,361 -- the smaller file made the larger prompt, with
  bytes-per-token differing 3.4x. Any megabyte threshold is a coin flip.
- **Rotate on real signals.** Two are worth acting on: a provider ceiling
  hit (in the measured deployment, prompts landing in the window between
  the provider's refusal limit and the compaction trigger could never
  recover -- zero recoveries ever observed, so the first hit is a
  zero-false-positive signal), and contract drift -- the automation's
  steps no longer match what the session was pinned under, detectable by
  fingerprinting the steps and comparing on resume.
- **Rely on re-injection, not handoff prose.** The engine re-injects
  durable state -- guidance, the open-items ledger -- into every run,
  fresh or resumed. Measured across five rotations: the item ids named
  before the boundary carried across after it (two automations at 100%,
  the worst at 26 of 31), with zero "I don't have that context" phrases,
  and delivery improved after three of the five. A handoff note would be
  a worse, non-deterministic duplicate of the injection -- written by a
  degraded agent at the exact moment it is least trustworthy. Design the
  durability line first, and rotation becomes cheap.

## What this is not

**Not synthetic benchmark suites.** For a system whose job is judgment
over live traffic, the live traffic is the eval. Ten days of tuning
produced no findings from invented scenarios and every finding from real
runs reviewed after the fact.

**Not grading the agent's prose.** Grade the records it writes and the
dispositions the human takes. A beautifully reasoned report that never
reached anyone, minted duplicates, or closed nothing is a failing run
with good handwriting.

**Not a one-time pass.** The loop reruns whenever a new source, pack, or
automation joins, because every producer changes the economics of the
consumer's attention. The mint-throttle arithmetic in step 5 is not a
solved problem you inherit -- it is a balance you re-measure every time
the producer side grows.

## Named profiles (fast turns, local models, richer status)

Some turns want a different provider or model than the workspace baseline: a
typed, back-and-forth turn is glanceable and latency-sensitive and should run on
a fast model; a private turn might run on a local box. A turn selects one with a
**profile** — an open-vocabulary named overlay the owner defines, chosen per
interactive/API turn.

### Model: give a turn its own provider/model with a profile

Provider/model choice is **policy**, and it lives in the workspace's layered
config file the owner reads and edits: `agent-config.yaml` at the workspace root
(beside `drumpacks.txt`). Named profiles live under its `profiles:` block. The file
is optional — with no file (or no matching profile), a turn resolves against the
`default:` baseline, exactly as before. See `examples/agent-config.yaml` for a
copy-ready template.

```yaml
# agent-config.yaml
profiles:
  quick:                                    # the name is YOURS — open vocabulary
    provider:
      config:
        default_model: claude-3-5-haiku-latest   # the provider's fast model
```

A turn selects a profile by naming it on the request
(`{"profile": "quick", ...}` on `POST /api/turns`). The named profile is folded
in as **layer 3** of the shared config merge (`$AMPLIFIER_AGENT_CONFIG` base →
`default:` → **profile** → automation `agent_config:`).
A turn that names **no** profile uses the `default:` layer. An **unknown**
profile name is refused loud, listing the profiles you defined — never a silent
fallback to the wrong provider/model.

drumbeat does **not** resolve models itself and keeps no model registry. The
`default_model` you name is forwarded verbatim into amplifier-agent's own
host-config field `provider.config.default_model` (handed down via `--config`) —
the same mechanism `amplifier-agent run` already uses to pick a per-provider
model. drumbeat picks *which* config per turn; amplifier-agent still does the
model resolution. A malformed `agent-config.yaml`, an unknown top-level key in a
profile, or a credential inside one is a loud refusal that names the file and the
profile — never a silent wrong-model turn.

#### Add a fast profile right now

The shipped template already defines a `quick` profile on a fast model. To apply
it to a running workspace, drop the file in and the next turn that names
`profile: quick` picks it up (the file is re-read every turn — no restart):

```bash
cp examples/agent-config.yaml <your-workspace>/agent-config.yaml
# edit the profile's `default_model:` to your provider's fast model id
```

`claude-3-5-haiku-latest` is Anthropic's fast model and matches the default
routing matrix. For OpenAI use a `*-mini`; for xAI a `*-fast`. Nothing is
hardcoded in drumbeat — the id you write in the file is the id that runs.

### Pointing a profile at a local model

The **same** `provider.config` path reaches a local, OpenAI-compatible server
(llama.cpp, Ollama, LM Studio, vLLM…) on your LAN — no new mechanism. A profile
carries a full provider block:

- `provider.module` — the provider **short-name** amplifier-agent mounts. For an
  OpenAI-compatible endpoint that is `openai` (catalog names: `anthropic`,
  `openai`, `azure-openai`, `ollama`).
- `provider.config` — extra provider knobs folded verbatim into amplifier-agent's
  `provider.config`. This is where `default_model`, `base_url` (and
  `use_streaming`, `max_tokens`, `timeout`, …) live.

```yaml
# agent-config.yaml — a `local` profile pointing at a box at 192.168.1.7:8081
profiles:
  local:
    provider:
      module: openai
      config:
        default_model: qwen3.6-35b-a3b
        base_url: http://192.168.1.7:8081/v1
        use_streaming: false        # see "quirks" below — required for this box
        max_tokens: 1024
```

Define as many profiles as you like; each is independent by design, so which
provider/model a turn runs on stays legible at a glance.

**Credentials are an environment concern, never this file.** amplifier-agent
re-asserts `api_key` / `host` / `endpoint` from the engine environment *after*
overlaying `provider.config`, so a credential written here is silently ignored —
and in a file you might commit, it would leak a secret. drumbeat therefore
**refuses** those keys inside `config` with a pointer to the env var. For the
local box, the `openai` provider still requires *some* key to be present even
though the box ignores it:

```bash
export OPENAI_API_KEY=local     # any non-empty dummy
```

#### Why `source:` and `module: provider-chat-completions` do NOT belong here

A full amplifier provider block (the `- module: provider-chat-completions` /
`source: git+…` / `config:` shape) is a **bundle** construct. amplifier-agent's
`run --config` host config is not a bundle: it reads only
`provider.module` (a catalog short-name) and `provider.config`, and it does
**not** read `provider.source`. So the way to reach an OpenAI-compatible local
box through this path is `provider: openai` + `config.base_url`, not a
`provider-chat-completions` module reference. (A genuinely non-catalog provider
module would have to be declared by the bundle amplifier-agent runs, which is
outside drumbeat's reach.)

#### Local-model quirks (measured, not guessed)

Verified against a real llama.cpp box serving `qwen3.6-35b-a3b`
(`EVIDENCE/local-run/`). Local models fail differently from hosted ones; what we
observed:

- **Streaming crashes this server's responses.** With `use_streaming: true` (the
  default), the openai provider dies with
  `LLMError: 'NoneType' object has no attribute 'append'` while parsing the
  stream — reproduced twice (default and explicit `true`). `use_streaming: false`
  returned a clean `"reply": "PONG"`. **Set `use_streaming: false` for this box.**
- **It's a reasoning model: budget for the thinking.** The server puts chain-of-
  thought in `reasoning_content` and leaves `content` empty until it finishes. A
  low `max_tokens` spends the whole budget thinking and returns an empty reply
  (`finish_reason: length`). Give generous `max_tokens`, or add Qwen's `/no_think`
  to the turn to skip reasoning entirely.
- **First token is slow; cold start slower still.** The first turn also cold-
  prepares the bundle (fetches the `openai` provider). A warm non-streaming turn
  answered in ~0.9–2 s; budget more for the first one. The turn ceiling
  (`ceiling_seconds`, default 20 min) already covers a slow local box, but a
  latency-sensitive turn on a slow local model is a UX tradeoff to make
  with eyes open.

### Status: stream the activity, don't show a bare "working"

Every chat/reply turn already surfaces live, **truthful** progress. amplifier-agent
emits an NDJSON event stream (`thinking/delta`, `tool/started`, `tool/completed`,
…); drumbeat translates it into a safe present-tense activity line and mirrors it
onto the turn record's `progress` field, which a caller reads by polling
`GET /api/turns/{turn_id}`:

```json
"progress": { "step": 12, "activity": "Checking your calendar…", "tool": "bash" }
```

- `activity` is one of `Thinking…`, a translated tool phrase (`Checking
  email…`, `Reading a file…`), or `Working…` before the first event. It is
  **never** a raw command line, session id, or tool argument — translation is a
  fixed table (`runner._translate_tool_activity`), so a longer turn reads as
  "checking calendar…, reading email…" rather than an opaque spinner.
- `tool` is the bare tool name while a tool runs, and `null` otherwise — so the
  UI never shows a tool that is not running.
- `step` bumps on every event (even quiet ones), so the caller can tell the turn
  is alive between activity changes.

The truthfulness guarantee is structural: an event type drumbeat does not
recognize is dropped, never guessed at.

### App-side follow-up (not in this lane)

This lane ships the drumbeat side only. To light it up end to end, the app must:

1. **Send `profile`** on the turns it wants routed: `POST /api/turns` with
   `{"profile": "quick", ...}`. Until it does, every turn uses the `default:`
   layer (safe, unchanged).
2. **Render `progress`** from the poll response — show `activity` (and a
   spinner/liveness cue keyed off `step`) instead of a static "working".
3. Ship an `agent-config.yaml` in the deployed workspace defining the profiles
   the app names (e.g. a `quick` profile on the provider's fast model id).
