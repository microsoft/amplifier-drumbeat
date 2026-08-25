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
---

1. This is the forced-idle inject drill (decomposition step 2 gate battery). Reply with exactly: IDLE DRILL COMPLETE. Do not use any tools.
