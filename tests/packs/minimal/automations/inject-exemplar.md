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
  steps:
    - id: account-and-reply
      prompt: |-
        Account for the injected state above, then reply DONE.
---

Minimal fixture automation exercising the inject sentinel contract. The step lives in the frontmatter.
