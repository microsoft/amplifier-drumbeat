---
automation:
  name: Email Check
  enabled: false
  trigger:
    type: schedule
    expression: every 45 minutes
  notify: auto
  # PLACEHOLDER tool names -- substitute whatever your packs provide.
  requires:
    - mail-cli
    - items-cli
    - guidance/ATTENTION.md
    - guidance/EMAIL.md
  inject:
    - argv: ["items-cli", "inject-turn"]
      label: "open items"
---

1. Load and follow guidance/EMAIL.md and guidance/ATTENTION.md as written — no
   override. This automation **is authorized to act**, not merely to report:
   where guidance/EMAIL.md's auto-archive section says to move a message, do it
   for real. Marking read, drafting, and sending remain out of scope: do NOT
   mark anything read, do NOT draft, do NOT send.

2. Check the inbox and any other folders your guidance names for mail that is
   new or newly relevant since your last check in this conversation. Tell me
   what needs my attention, using the triage rules in guidance/EMAIL.md.

   Check the command's own completeness field on every listing. If a folder or
   page could not be read, report it as a coverage gap rather than treating it
   as empty.

3. Apply guidance/EMAIL.md's auto-archive rules **for real**. For each message
   that clearly matches: verify it per the guidance (inspect the message or
   thread, confirm the stable discriminator, never touch a message I sent
   myself), then archive or move it.

   Report what you actually **did** this run — for every message archived or
   moved, its subject, its sender, the rule that matched, and the tool's own
   result. Not what you found, and not what you would have done. If a candidate
   could not be archived or moved for any reason, say so explicitly and name the
   message rather than silently skipping it or approximating success.

4. Update guidance/EMAIL.md with anything durable this run taught you — a new
   stable discriminator for a recurring class of mail, a rule that fired wrongly,
   a correction I gave you. Prune or consolidate rather than only appending; this
   file is injected verbatim on every run and cannot grow unbounded. Put
   cross-domain learnings in guidance/ATTENTION.md instead.

   If some part of this cannot be carried out with the tools you have, say so
   explicitly rather than approximating.
