---
automation:
  name: Seam Drill Withhold
  enabled: false
  trigger:
    type: schedule
    expression: every 999 hours
  notify: auto
  inject:
    - argv: ["ledger-items", "inject-turn"]
      label: "open items"
  steps:
    - id: step-1
      prompt: |-
        This is a MECHANISM DRILL for the delivery seam. It is not real work and it must not interrupt the user.

        Do nothing at all. Do not read any file, do not call any tool, do not look anything up, do not act on the open-items ledger you were shown — this run is deliberately a no-op.

        Reply with exactly the single word: `drilled`
    - id: step-2
      prompt: |-
        This run found nothing worth the user's attention, by construction. When you are asked whether anything here is worth telling him, the correct and only answer is the sentinel `NOTHING_TO_REPORT`, verbatim and alone. Nothing in this drill is real, so surfacing any of it would be a false interruption.
---

Mechanism drill: the NOTHING_TO_REPORT sentinel withholds the push.
