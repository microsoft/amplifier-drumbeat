---
automation:
  name: Inject Drill Idle
  enabled: false
  trigger:
    type: manual
  notify: never
  inject:
    - argv: ["ledger-items", "--runs-dir", "/tmp/inject-drill-idle-store", "inject-turn"]
      label: "idle drill store"
  steps:
    - id: step-1
      prompt: |-
        This is the forced-idle inject drill (decomposition step 2 gate battery). Reply with exactly: IDLE DRILL COMPLETE. Do not use any tools.
---

Mechanism drill: the INJECT_IDLE sentinel skips injection and the run proceeds.
