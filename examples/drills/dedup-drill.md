---
automation:
  name: Dedup Drill
  enabled: false
  trigger:
    type: manual
  notify: always
  requires:
    - guidance/IDENTITY.md
  steps:
    - id: step-1
      prompt: |-
        This is the worker-side duplicate-suppression drill (decomposition step 2 gate battery). Reply with exactly this sentence and nothing else: "DEDUP DRILL: 3 items pending review (2h 15m elapsed)." Do not use any tools.
---

Mechanism drill: worker-side duplicate suppression.
