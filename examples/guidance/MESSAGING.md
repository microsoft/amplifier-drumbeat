# Messaging Guidance

> **This is a TEMPLATE.** Copy it into your workspace's `guidance/` and make it
> yours. It ships with discipline but no preferences — nothing here knows which
> of your conversations matter. The automation that requires this file is
> expected to extend it as it learns; see the last section.

Before using or updating this file, load and apply [ATTENTION.md](ATTENTION.md).
Put generalized, cross-domain learnings there and keep this file
messaging-specific.

---

## Messaging triage

- Surface direct requests, questions awaiting an answer, decisions needing
  confirmation, and anything blocking someone else's work.
- A mention of my name alone is a candidate for review, not automatically
  actionable — check whether it comes with a question, an assignment, or a
  decision that needs confirming.
- Group chats and broadcast channels have a much lower base rate of things
  needing me than direct messages do. Weigh accordingly rather than treating
  every conversation as equally likely to matter.
- A thread I am already actively participating in usually does not need
  surfacing — I am in it.
- Track what you surface in the record so the same request is not reported
  twice and does not silently disappear either.

---

## Read state is mine, not yours

**Unread is a signal about what I have seen, not about what still needs me.** I
read my own messages within seconds; by the time an automated check runs,
`unread` is usually already false on things that absolutely still need me.

Two consequences:

- **Never use unread as the definition of "new."** Compare each conversation's
  own last-message timestamp against your last-check time instead. Unread is at
  best a weak secondary hint.
- **Marking read is an action with consequences, so scope it.** Mark read only
  what a rule here authorizes or what clearly needs no attention, and report
  what you actually marked and why. If something could not be marked, name it
  rather than silently skipping it.

Be careful about side effects: on some platforms merely *opening* a conversation
clears its unread state. If a tool has that behavior, note it here once you have
confirmed it, so later runs do not destroy the signal they came to read.

---

## Completeness

Every listing you read should be able to tell you whether you saw everything.
Check the tool's own completeness or paging field on every call.

**"Nothing found" and "could not look" are different answers.** Report an
uninspectable conversation as a coverage gap, by name. A silence you cannot
distinguish from a failure is not a silence worth trusting.

---

## Bots are not people

A bot or application message restating a human's request is **not** independent
evidence of that request. Treat it as a pointer back to the original human
message. If you cannot find the original, say so plainly rather than recording
the bot's text as if a person had just asked.

Where a tool can distinguish a bot author from a human one, prefer that signal
over guessing from the message text.

---

## Proactively update this guidance

- Treat maintaining this file as part of every task that uses it. When you
  discover or confirm a durable, messaging-specific pattern, preference, or
  correction, update this file before completing the current task.
- Put cross-domain learnings in [ATTENTION.md](ATTENTION.md) instead. Keep this
  file focused on messaging.
- Add only confirmed, reusable learnings — not guesses or one-off
  circumstances.
- **This file is injected verbatim on every run.** Consolidate and prune rather
  than only appending; it cannot grow unbounded.
