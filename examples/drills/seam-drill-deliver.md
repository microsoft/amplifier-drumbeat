---
automation:
  name: Seam Drill Deliver
  enabled: false
  trigger:
    type: schedule
    expression: every 5 minutes
  notify: always
  inject:
    - argv: ["ledger-items", "inject-turn"]
      label: "open items"
---

1. This is a MECHANISM DRILL for the delivery seam, fired by the scheduler on purpose. It is not real work and it is not about anything in your ledger.

   Do nothing at all. Do not read any file, do not call any tool, do not look anything up, do not act on the open-items ledger you were shown.

   Reply with exactly this one line and nothing else — no bold, no headings, no preamble:

   Seam drill 5a: this intent was written while the delivery worker was stopped. Ignore this.
