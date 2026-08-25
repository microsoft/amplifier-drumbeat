---
automation:
  name: Daily Rollup
  enabled: false
  trigger:
    type: schedule
    # `daily at HH:MM` is a 24-hour clock in the server's local timezone,
    # recomputed on every evaluation -- a daylight-saving shift does not
    # silently move this.
    expression: daily at 07:00
  # `always`: the whole point of a periodic report is that it arrives.
  # "Urgent" and "worth surfacing" are the wrong test for a status report,
  # so this reply is delivered exactly as written, every time.
  notify: always
  # PLACEHOLDER tool name -- substitute whatever your packs provide.
  requires:
    - items-cli
    - guidance/ATTENTION.md
  inject:
    - argv: ["items-cli", "inject-turn"]
      label: "open items"
---

1. Report the current wall-clock time and confirm whether this run fired at or
   after the time this automation declares (`daily at 07:00`). Say plainly
   whether the fire time matched. A scheduled report that quietly drifts is
   worth catching on its own.

2. Gather and state the rollup, covering the last 24 hours, as your reply to
   THIS step:

   - **Resolved** — everything closed in the window, cross-checked against each
     record's own resolution time so the window is real. Everything *any*
     automation resolved, not just this one.
   - **Still open, and why** — grouped by whose act would discharge it, and by
     whether it carries a checkable condition or only a reason it cannot be
     checked.
   - **Blocked** — anything structurally unreachable or otherwise stuck, and
     why, drawn from the open list above.

3. End this step's reply with the **complete rollup**: restate the sections from
   the previous step unchanged, plus anything this step adds. This combined
   reply IS the rollup, and this is the LAST step — there is no separate
   reporting step and no notify-worthiness judgment after it. Do not add
   editorial framing on top of it.

   If a section has nothing in it, say so plainly and say what you checked.
   Never skip a section silently: an omitted section and an empty section look
   identical to me, and only one of them means "nothing happened."

   **This is reporting only.** Do not resolve, correct, record, or delete
   anything as part of producing this reply, and do not send any message. This
   automation summarizes what the other checks already did; it does not do their
   job. If something in the rollup looks wrong — a record that should have
   closed and did not — name it in the report. The next reconciliation pass is
   where it gets acted on.
