---
automation:
  name: Repo Status Digest
  # Ships disabled, like every exemplar. Flip to true once a turn is verified.
  enabled: false
  trigger:
    type: schedule
    expression: daily at 09:00
  notify: auto
  # This conformance exemplar needs no tools; it uses the agent's own shell.
  requires: []
  # Steps are structured frontmatter data (contracts/automation-file.v1.md):
  # an ordered list, each with an `id` (a slug, unique in the file), a
  # non-empty `prompt` (the whole behavior), and an optional `label`.
  steps:
    - id: confirm-fire-time
      label: Confirm the fire time
      prompt: |-
        Report the current wall-clock time and confirm this run fired at or
        after the scheduled time (`daily at 09:00`). Say plainly whether the
        fire time matched.
    - id: gather-digest
      prompt: |-
        For the git repository in the current working directory, gather a short
        status digest using read-only git commands only. Do not commit, push,
        pull, or change any file -- this automation reports, it does not act.
    - id: emit-digest
      prompt: >-
        Emit the complete digest as one message. If a section has nothing in it,
        say so plainly. This combined reply IS the digest, and this is the last
        step.
---

Conformance exemplar for `contracts/automation-file.v1.md`: a valid automation
whose steps are structured frontmatter data and whose body is a human-facing
description, never parsed for execution.
