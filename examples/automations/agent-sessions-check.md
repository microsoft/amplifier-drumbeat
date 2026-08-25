---
automation:
  name: Agent Sessions Check
  enabled: false
  trigger:
    type: schedule
    expression: every 25 minutes
  notify: auto
  # PLACEHOLDER tool names -- substitute whatever your packs provide.
  # `worker-cli` stands in for anything that lists long-running agent or
  # terminal sessions and reports each one's own last-activity time.
  requires:
    - worker-cli
    - items-cli
    - guidance/ATTENTION.md
  inject:
    - argv: ["items-cli", "inject-turn"]
      label: "open items"
---

1. Load and follow guidance/ATTENTION.md.

2. Check which of my long-running agent or terminal sessions need my attention,
   and why. Each session reports its own last-activity time as an absolute
   timestamp — that is a fact about that session, not a relative age you compute
   and not the current time.

   Use your judgement about what actually warrants interrupting me versus
   routine background activity a session can keep working through unattended.

3. **For each session that looks like it needs attention, apply the decline rule
   in guidance/ATTENTION.md before recording anything.** If the only thing that
   would discharge it is the session moving on by itself, nobody is blocked on
   me and it is not an item — decline it and move on. Only a session genuinely
   waiting on a decision that only I can make earns a record.

   This rule is the difference between a useful queue and an unusable one.
   Measured on real data: a majority of one day's records were machine-watching
   wearing an item's clothes — conditions written as *"I acknowledge it, **or**
   the session moves past the prompt"*, almost all of which closed via the
   second branch. **The escape branch IS the finding.** If you can honestly
   write "or it clears itself," stop and decline.

   When a session does earn a record, timestamp it with **that session's own
   last-activity time**, converted to ISO 8601 — never the current wall-clock
   time. This is what makes the record's identity stable: while a session sits
   blocked on the same thing, its last-activity time does not change, so
   re-running on a later check updates the SAME record instead of minting a new
   one. Using "now" mints a brand-new record for a need that never went
   anywhere, and the queue fills with near-identical restatements of one thing.
   See guidance/ATTENTION.md, "Timestamp is identity."

   If a session later unblocks and then blocks again on something new, its
   last-activity time will have moved — which correctly produces a NEW record,
   not a reopening of the old one.

4. Before reporting, check your durable record for anything still open whose
   session no longer needs my attention — it finished, the blocking question was
   answered, or it is no longer listed — and resolve it with a real reason.

   Then give me a summary naming each currently-relevant session and the reason
   it does or does not need me. If this run declined anything, end with a
   one-line count and shape — e.g. `Not minted: 14 sessions clearing themselves,
   1 job re-running.` A count and a category, not a list of all fourteen. Omit
   the line entirely when nothing was declined.

5. This is a read-only run with respect to the sessions themselves: do NOT send
   input into any session, do NOT take any action inside one, do NOT edit any
   files. The record-keeping in steps 3 and 4 is the one explicit exception —
   it is internal bookkeeping, not an action taken against a session.

   If some part of this cannot be carried out with the tools you have, say so
   explicitly rather than approximating.
