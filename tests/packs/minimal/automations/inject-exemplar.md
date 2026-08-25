---
automation:
  name: Inject Exemplar
  enabled: false
  trigger:
    type: manual
  notify: never
  requires:
    - minimal-state
  inject:
    - argv: ["minimal-state"]
      label: "minimal state"
---

1. Account for the injected state above, then reply DONE.
