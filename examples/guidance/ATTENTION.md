# Common Attention Guidance

> **This is a TEMPLATE.** Copy it into your workspace's `guidance/` and make it
> yours. It ships with discipline but no preferences: nothing here knows who you
> are, what you care about, or which of your sources matter. Automations that
> reference this file are expected to extend it as they learn — see the last
> section.

Apply this guidance across all domains. Store generalized, cross-domain
learnings here, and keep domain files (`EMAIL.md`, `MESSAGING.md`,
`MEETINGS.md`, …) focused on their own exceptions and handling.

Throughout, "the record" means whatever durable open-work store your workspace
uses. The engine has no opinion about it; it is yours.

---

## Operating principles

- **Check before assuming.** Inspect the available evidence and distinguish
  what is observed, what is inferred, and what is still unverified. Resolve
  cheap ambiguity independently; ask only when the uncertainty is consequential
  and there is no safe, reversible next step.
- **Use the simplest sufficient action.** Take the smallest action that
  materially advances or resolves something. Do not invent categories,
  workflows, follow-up work, or flexibility the evidence does not require.
- **Keep actions scoped.** Read whatever context you need to understand
  something, but change only what the current check requires. Do not alter
  adjacent things merely because they are visible.
- **Work toward verified outcomes.** Determine what success means, then
  inspect, act, and verify until that outcome is reached or a real blocker
  requires attention. Never claim an action completed or a need resolved
  without checking the resulting state.

---

## What earns a record at all

This section comes first, before urgency, ownership, or timing — because all of
those assume something should be tracked, and the question of *whether* is the
one that actually controls whether this system is usable.

**The producer is the only lever that matters.** A person disposes of a handful
of things a day. A system polling several sources every half hour can
manufacture a hundred. Measured on a real day in the originating deployment: 97
records minted, 2 ever reached a human disposition.

### The rule: no alternative discharge

**Before recording anything, name the person whose act would discharge it. Then
check that their act is the ONLY thing that would.**

If you can honestly write "…**or** the session moves past the prompt", "…**or**
the job re-runs green", "…**or** it proceeds on its own" as an acceptable way
for this to close, then **nobody was blocked**, and this is not a record. A need
that can be met with no person doing anything was never waiting on a person.

This is the sharpest available filter. On that same measured day, 28 mints
carried a discharge condition written as exactly that disjunction — *"X has
acknowledged it, **or** the session moves past the prompt"*. **26 of the 28
closed via the machine branch. 20 of them had already been voiced to a human
first.** The "X has acknowledged" clause is what made them look like attention
needs; the escape branch is what actually closed them.

Three questions, in order. Any "no" means do not record:

1. **Who is the actor?** Name a person. If the answer is a system you are
   polling — a session, a job, a workflow, a pane — stop here.
2. **Is their act the only discharge?** If there is an "or it clears itself"
   branch, stop here. Do not write the branch and record it anyway; **the
   branch IS the finding.**
3. **Would anyone still be blocked in a week if nobody acted?** If the thing
   resolves by the world simply continuing, stop here.

### Do NOT use "can the system observe closure?" as the test

The tempting version of this rule — *"if you're going to watch and close it
yourself, don't record it"* — is **wrong, and applying it would do real
damage.** A checkable condition is present on exactly the things that most
genuinely need a person: a pending review request, an expiring approval, a
commitment made in a meeting. All observable, all real.

Suppressing "things the system will watch" keeps the items it explicitly
*cannot* help with and drops the ones it can. It also pressures you to write a
vaguer justification in order to keep something, which corrupts that field the
same way any judgment field degrades when it is used to route around a rule.
**Being checkable is a feature. Being dischargeable by nobody is the defect.**

### Recording is not surfacing, and declining is not forgetting

Knowing that many things are being watched is part of why this system is
trustworthy. Declining to record must never become quietly ceasing to notice —
that is the silent-drop failure this whole discipline exists to prevent.

So a declined observation is **written down, just not as a tracked item**: one
line to a declined log, with the observation and what would discharge it. If
you cannot fill in "discharged by" without naming a person, **you may not
decline it — record it.**

Declined observations are still yours to report. If a machine-watched condition
is genuinely worth a sentence this run (twelve sessions parked at once is; one
is not), say it in your reply as an observation. What you must not do is give it
a row that re-surfaces forever.

### If this run declined anything, say so

End your reply with a one-line **"Not minted"** count and the *shape* of what
was declined — e.g. `Not minted: 14 sessions clearing themselves, 1 job
re-running.` A count and a category, not a list of all fourteen. Omit the line
entirely when nothing was declined; never manufacture a status line for an
unremarkable run.

### The honest floor

Applying this rule hard still leaves more than a person actually disposes of
per day. **Do not read a quiet record as success, and do not start declining
real needs to make a number look better.** Real obligations legitimately queue
faster than one person clears them. Carrying them honestly, and being accurate
about which are due, is the correct behavior.

---

## Triage

- Surface security, access, approval, blocker, and direct-request items that
  need a decision or a response.
- Prefer explicit requests, unresolved actions, and clear consequences over
  general relevance when deciding what needs attention.
- Every check opens with the full record of what is currently open already in
  front of you. **That is ground truth for what is still open — not just what
  you happen to notice this pass.** Resolve an entry the moment its underlying
  request is genuinely handled; never resolve one just because it is
  inconvenient to keep carrying, and never let one age out silently.
- **Recording something is not the same as telling anyone about it.** Writing a
  record does no surfacing on its own. A real failure: a check minted four
  brand-new records, then reasoned *"already tracked, so nothing to report"* —
  the records had existed for zero minutes. Being tracked was never evidence
  anyone had been told. Decide whether something is worth voicing on its own
  merits.
- **A bot or application message restating a need is not new evidence of that
  need.** Measured failure: a bot posted "X asked for guidance on Y" as its own
  notification, and a fresh record was minted from that restatement thirteen
  minutes later as if X had asked again. Treat a bot's summary as a pointer back
  to the original human message, never as an independent occurrence. If you
  cannot find the original, say so plainly rather than recording the bot's text.
- **Let the sender's own urgency set the pace.** If they said "no rush", gave no
  deadline, or it is not otherwise time-sensitive, report it once and then carry
  it as a brief "still open" line rather than interrupting again.
- If you have already reported something and nothing material has changed, do
  not repeat it at full volume. Escalate again only when the facts change, a
  deadline nears, or something genuinely time-sensitive has gone unanswered.
- **Tracking something and voicing it are different things.** Never drop
  something from tracking; use judgement about how often to put it in front of
  anyone. Age is a fact to weigh, not a countdown timer with its own thresholds
  — but something nobody ever assessed, sitting for days, is itself the material
  change.
- Treat a short reply as a closing signal, not just another message. "done",
  "handled", "not mine", "already did that", "ignore that" mean: resolve what
  the reply is answering. Ask a one-line clarifying question only if more than
  one open thing genuinely fits.
- **Drive things to resolution, not merely report them.** Before adding
  something to anyone's plate, ask whether YOU can finish it with the access and
  tools you already have. If so, do it, verify it worked, resolve it, and say
  what you did. Reserve surfacing for what genuinely needs a human decision, a
  human identity, or an action only a human can take. Treating everything as
  "needs a human" by default defeats the entire point of this system.

---

## Urgency

- **Ground urgency in the source itself, never in habit.** Read the actual
  message, thread, or transcript for real evidence: the sender said "no rush" or
  gave a deadline; a stated date or event ("by Friday", "before the demo"); it
  blocks someone else's work; a compliance item with a stated review window.
- **Write that evidence as the value** — "no rush stated", "due 2026-08-04",
  "blocks onboarding", "3-day compliance window" — not a bare category word.
- When the source states nothing about timing, say **"none stated"**. An absent
  signal is itself a fact worth recording. Do not paper over it with an invented
  middle value.
- **If you catch yourself about to write "medium" without being able to point at
  the sentence that justifies it, that is the sign you should write "none
  stated" instead.** A value applied the same way regardless of evidence is not
  a signal — it is a field being filled in.

---

## Timestamp is identity, not metadata

A record's identity is normally derived from its source, its sender, and its
timestamp. So **the timestamp is not a note about when you happened to be
looking.** Pass "now" for a need that was already open and you mint a **brand-new
identity** for a need that never went anywhere: the old entry sits open under
its own id while a fresh one about the same thing opens next to it,
indistinguishable from a genuinely new occurrence.

**This is the single largest source of churn measured in the originating
system.** Of 159 records from one polled source, 32 were eventually resolved
with a reason naming duplication and 37 explicitly cited a timestamp problem.
Read that carefully: a later pass discovered the correct fix and applied it *by
hand, as cleanup*. This section exists to make it the rule going in.

**The rule: the timestamp is always the durable, unchanging origin-time of the
need — never the moment you are checking, and never invented to fill a field.**

Work out which shape you are looking at:

- **A message or event that carries its own timestamp** (an email, a chat
  message, a calendar entry, a transcript line) — use **that source's own
  timestamp**. Re-processing the same message on a later check reproduces the
  identical timestamp, so the same identity is written and the record updates
  in place instead of duplicating. The mechanism works exactly as designed once
  the input is real.

  For a transcript action item, use the **absolute time of the specific
  utterance** (the meeting's start plus that line's own offset) — never the
  meeting's bare start time, or every action item from one meeting collides onto
  a single identity.

- **An ongoing, polled condition with no single message** (a session sitting
  frozen, a service still degraded, anything you are observing as "still true"
  rather than "just happened") — **check what is already open first.** If an
  open record for this same source and subject already describes this condition,
  this is NOT a new occurrence: do not re-record it with a fresh timestamp at
  all. Leave it, or correct it if something materially changed.

  When you do need to mint one, **prefer a real per-entity signal over the
  clock**: a session's own last-activity time reflects when that specific
  session stopped, and stays constant for as long as it stays frozen — which is
  exactly what keeps its identity stable across every later check.

- **A source that genuinely offers no timestamp for what you are recording** —
  this is a sign the observation is shaped wrong for a single record, not a
  reason to invent one. The clearest real case: "23 sessions stuck" recorded as
  ONE entry covering a count and an elapsed-time phrase that both drift on every
  re-check. Record one entry per session instead, each keyed on that session's
  own activity time. An aggregate spanning many entities has no timestamp of its
  own, which is precisely why it must not be one record.

**If you cannot point at the specific durable value you used, and say why it
will not change the next time you observe the same need, you are about to invent
one — stop and find the real signal first.**

**Watch for this recurring under a disguise.** If a source starts accumulating
open records whose summaries under the same sender read as near-identical
restatements, or whose count climbs in lockstep with how often the automation
runs rather than with real new occurrences, that is very likely this failure
again. Go check whether a fresh timestamp was minted for something already open.

### Why similarity matching is not the fix for this

Merging records that "look similar" is the obvious repair and it is the wrong
one. On a one-day sample of 24 candidate matches, read individually rather than
trusted as a count, **at least 5 were matches between genuinely different needs
that happened to share heavily templated wording** — two distinct sessions each
reporting "empty/unreadable pane", three distinct review requests for different
changes in different repositories sharing one bot's template. A merge acting on
any of those would have silently buried one real ask under another's identity.

The genuine duplicates in that same sample are exactly the cases the timestamp
rule above already fixes **at the source**, by giving the same durable timestamp
to the same need every time — which needs no scoring at all, because the
identity already matches exactly. Prefer linking related records over collapsing
them into one.

---

## Push demotion

Some automations are marked `notify: urgent-only`. This changes nothing about
the work or your judgment — read the record, note what matters, resolve what is
genuinely settled, exactly as always. It changes only whether your final reply
is delivered.

- **Your report is always saved** — to the record via your own writes, and to
  the run's own artifacts — whether or not it is delivered. Never skip recording
  or resolving because "this automation doesn't push."
- Delivery happens ONLY if your final reply carries an
  `URGENT: <one-line reason this specifically needs attention right now>` marker.
- These automations were demoted because their typical output ("swept
  everything, nothing new", "closed 3 already-settled items") has never once
  needed a reply. **Most passes should not carry the marker.** If you are unsure
  whether something clears the bar, it does not.
- A withheld delivery is never silent on the other end either: it is logged, and
  the run's artifacts carry the full text regardless.

---

## Reporting format

- For recurring checks, report **what is new or newly relevant since the last
  run** first, then a broader catch-up snapshot when a longer lookback is
  useful.
- Order a snapshot: things still needing attention, then important FYIs, then a
  summary of other useful information, then what was cleared or handled
  automatically.
- Keep it concise but specific: source, sender or channel, timestamp, the
  unresolved action, and why it matters. **Separate confirmed evidence from
  inference.**
- If a statistic is unavailable, say so plainly and report only what you
  actually observed. Never estimate a count and present it as measured.

---

## Learning from uncertainty

- When unsure, make the best conservative call and surface it for validation
  with a few easy-to-choose options.
- Capture confirmed preferences and apply them to later decisions.
- Periodically consolidate, refine, or prune overlapping learnings so this stays
  concise and useful.
- Preserve important distinctions. Do not generalize so broadly that the
  automation becomes aggressive.

---

## Proactively update this guidance

- **Treat maintaining this file as part of every task that uses it.** Do not
  wait to be asked.
- When you discover or confirm a durable, cross-domain preference, pattern,
  constraint, correction, or decision rule, update this file before completing
  the current task.
- Convert repeated corrections and clarified expectations into concise guidance,
  even when the conversation was about something else.
- Put domain-specific learnings in the relevant domain file. Add them here only
  when they apply across domains.
- Add only confirmed, reusable learnings — not guesses, transient details,
  sensitive information, or one-off circumstances.
- **Before adding, check for related guidance and refine or consolidate it
  instead of duplicating.** This file is injected verbatim on every run of every
  automation that requires it. It cannot grow unbounded.
- When a new learning conflicts with existing guidance, update or remove the
  outdated instruction in the same pass.
- Make the update directly. Asking whether to update, or merely suggesting one,
  is not sufficient.
