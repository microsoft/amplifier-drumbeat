---
automation:
  name: Inject Drill Empty
  enabled: false
  trigger:
    type: manual
  notify: never
  inject:
    - argv: ["true"]
      label: "empty-stdout drill"
  steps:
    - id: step-1
      prompt: |-
        This step must never execute -- the empty-stdout inject tool above must abort the run before any turn runs. If you are reading this, the forced-empty drill FAILED.
---

Mechanism drill: an inject tool that exits 0 with empty stdout must abort the run.
