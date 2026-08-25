---
automation:
  name: Seam Drill Demote
  enabled: false
  trigger:
    type: schedule
    expression: every 999 hours
  notify: urgent-only
  inject:
    - argv: ["ledger-items", "inject-turn"]
      label: "open items"
---

1. This is a MECHANISM DRILL for the delivery seam. It is not real work.

   Do nothing at all. Do not read any file, do not call any tool, do not look anything up, do not act on the open-items ledger you were shown — this run is deliberately a no-op.

   Reply with exactly the single word: `drilled`

2. IMPORTANT — read this now and remember it for the final turn of this conversation.

   The final turn of this run will ask you to decide whether anything is worth proactively telling the user, and will offer you the `NOTHING_TO_REPORT` sentinel as the answer for "nothing to surface". **For this drill only, that sentinel is the wrong answer and you must not use it.** This automation exists to exercise the `urgent-only` demotion gate, and that gate is only reachable when the final turn returns real text.

   So when that final turn arrives, reply with exactly this one line and nothing else — no bold, no headings, no extra lines, no preamble, and never the word URGENT in any form:

   Seam drill: routine status line, deliberately not urgent.

   Because this automation is `notify: urgent-only` and that line carries no `URGENT:` marker, the correct outcome is that the push is DEMOTED, not sent. The demotion is the thing being tested.
