# Email Guidance

> **This is a TEMPLATE.** Copy it into your workspace's `guidance/` and make it
> yours. The auto-archive section below ships **empty on purpose** — an archive
> rule encodes a real judgment about real mail, and a wrong one silently
> disappears something you needed. Add rules only as you confirm them.

Before using or updating this file, load and apply [ATTENTION.md](ATTENTION.md).
Put generalized, cross-domain learnings there and keep this file email-specific.

---

## Email triage

- Surface direct requests, questions awaiting an answer, approvals, access and
  security items, and anything with a stated deadline.
- Prefer explicit asks and clear consequences over general topical relevance.
- **Distinguish "addressed to me" from "copied on."** Being on a thread is not
  the same as being asked something. Weigh a direct address far more heavily
  than an appearance in a recipient list.
- Automated mail — build results, dashboards, digests, marketing, routine
  notifications — is noise **unless** it names an action I must take or reports
  a failure that is current and outstanding. A resolved incident notification is
  not an attention need.
- A long thread usually needs one summary of where it landed and what is owed,
  not a message-by-message account.
- Track what you surface in the record so the same request is not reported twice
  and does not silently disappear either.

---

## Completeness

Check the tool's own completeness or paging field on every listing. If a folder
or a page could not be read, **report it as a coverage gap by name** rather than
treating it as empty. "Nothing found" and "could not look" are different
answers.

---

## Auto-archive

> **Empty by design. Add rules here only when you have confirmed them.**

This section is where you authorize the automation to act on mail rather than
merely report it. Anything listed here will be archived or moved **for real**,
unattended, without further confirmation. That is exactly why the rules must be
written carefully.

Every rule you add must state:

1. **A stable discriminator.** Something that will still identify this class of
   mail next month — a sender address, a list identifier, a distinctive header
   or subject pattern. Not a phrase that happens to appear today.
2. **The verification step.** What the automation must inspect *before* acting,
   to confirm this specific message really matches. A rule that matches on a
   subject line alone will eventually match something it should not.
3. **The destination.** Archive, or a specific named folder.
4. **The exceptions.** In particular, anything current or outstanding. The
   canonical shape of a good rule is: *"treat this class as noise **unless**
   there is a current, outstanding one"* — a resolved notification is noise, but
   the live one it resolves is not.

Standing constraints that apply to every rule here:

- **Never touch a message I sent myself.**
- **Never archive something carrying an unanswered question addressed to me**,
  regardless of what else matches.
- Verify per-message before acting. A rule matching a class does not mean every
  member of that class matches today.
- Keep concurrent modifications modest and report every action taken, with the
  message, the rule that matched, and the tool's own result.
- **If a candidate could not be archived or moved for any reason, say so and
  name the message.** Never silently skip, and never approximate success.

Report what you **did**, not what you found. "I identified 12 candidates" and
"I archived 12 messages" are different claims, and only one of them can be
verified later.

---

## Proactively update this guidance

- Treat maintaining this file as part of every task that uses it. When you
  discover or confirm a durable, email-specific pattern — a new stable
  discriminator, a rule that fired wrongly, a correction — update this file
  before completing the current task.
- Put cross-domain learnings in [ATTENTION.md](ATTENTION.md) instead.
- Add only confirmed, reusable learnings — not guesses or one-off
  circumstances.
- **This file is injected verbatim on every run.** Consolidate and prune rather
  than only appending; it cannot grow unbounded.
