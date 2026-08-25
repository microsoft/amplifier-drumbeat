# Identity Guidance

> **This is a SKELETON with placeholders.** Every name below is a stand-in.
> **Replace them before enabling any automation that requires this file** — as
> shipped it will actively mislead attribution, which is worse than having no
> identity file at all.

Before using or updating this file, load and apply [ATTENTION.md](ATTENTION.md).
Put generalized, cross-domain learnings there and keep this file
identity/attribution-specific.

---

## What this file is for

A meeting transcript frequently **cannot say who spoke**. When someone joins
from a shared or room device rather than a signed-in account, they are rendered
as an anonymous placeholder (`@1`, `@2`, `@3`, …) instead of a name — and **the
same placeholder means a different real person in a different meeting.**

This file is the accumulated evidence for recognizing me and the people I work
with despite that gap.

**It is evidence to be extended, not a licence to guess.** A pattern recorded
here can raise confidence that an anonymous turn *might* be a specific person.
It never manufactures certainty on its own, and it never overrides a transcript
tool's own attribution rules. **If a turn is anonymous and nothing here
corroborates it with a named speaker tag, the correct answer is still
"unattributed"** — never a name pulled from this file alone.

---

## How I appear in transcripts

> Replace this block with your own details.

- My display name as it appears in transcripts: **`<YOUR DISPLAY NAME>`**.
- My work address: `<you@example.com>`.
- When I join from my own signed-in device, my turns are tagged with my name and
  I appear in the named-speaker list like anyone else.
- When I join from a shared room device, I am rendered as an anonymous
  participant placeholder, exactly like anyone else physically in that room.
  **Nothing in the transcript distinguishes me from another anonymous attendee
  in the same room** — do not assume an anonymous turn is me just because I was
  known to be there.
- Known display-name variants: *(add them as you confirm them — a middle
  initial, a maiden name, a differently-cased account)*.

---

## Colleagues seen so far

> Replace these placeholders entirely. The format is: name, how they appear,
> what they own, and **when it was confirmed** — a role recorded without a
> confirmation date rots silently.

- **`<Colleague A>`** — appears by name in transcripts. Owns `<area>`. Confirmed
  `<date>`.
- **`<Colleague B>`** — appears by name in transcripts. Leads `<workstream>`.
  Confirmed `<date>`.

Two conventions worth keeping:

- **When two people have similar names, record the distinction explicitly**, in
  the words of whoever corrected you. Conflating two people is a mistake that
  propagates into every later record.
- **When a last name has never actually been confirmed, say so and do not guess
  it.** Write `<first name> — last name not yet confirmed in any transcript; do
  not guess it`.

---

## Search scope is not evidence of absence

A narrow lookback window is **not** evidence that a person or a thread does not
exist. If a search over the last N days finds nothing, the honest conclusion is
"the window was insufficient", not "it isn't there". Widen the search or say the
window was too narrow — never conclude absence from a bounded query.

This is recorded because it actually happened: a scoped lookback led to a
confident, wrong conclusion that a specific person had no recent activity.

---

## Speech and context patterns that raise confidence (never certainty)

> Replace with your own.

- Topics I am personally addressed about or own: `<topic>`, `<topic>`,
  `<project>`.
- Someone addressing me by name in a turn is a signal about who they are talking
  **to**, not evidence about who spoke a nearby anonymous turn. The reply is
  still whatever speaker tag the transcript actually assigned it — or
  unattributed, if that tag is anonymous.

---

## Proactively update this guidance

- Treat maintaining this file as part of every task that uses it. When a
  transcript's own **named-speaker tag** confirms an identity in a new way — a
  new display-name variant, a new device or join pattern, a colleague's role —
  add it here.
- **Add only confirmed identity evidence backed by an actual named-speaker
  tag.** Never a guess, and never anything inferred from an anonymous turn.
- Put cross-domain learnings in [ATTENTION.md](ATTENTION.md) instead. Keep this
  file focused on identity and attribution.
- **This file is injected verbatim on every run.** Consolidate and prune rather
  than only appending; it cannot grow unbounded.
