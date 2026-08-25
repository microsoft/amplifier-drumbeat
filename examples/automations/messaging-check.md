---
automation:
  name: Messaging Check
  # Ships disabled. Run it by hand, read the run artifact, then enable it.
  enabled: false
  trigger:
    type: schedule
    expression: every 30 minutes
  notify: auto
  # Tool names come from whatever packs you install -- `chat-cli` and
  # `items-cli` are PLACEHOLDERS. Substitute your own, or the pre-run gate
  # aborts the run naming the missing requirement.
  requires:
    - chat-cli
    - items-cli
    - guidance/ATTENTION.md
    - guidance/MESSAGING.md
  # Optional: puts your durable open-work record in front of every run.
  # Delete this block if you have no such tool yet.
  inject:
    - argv: ["items-cli", "inject-turn"]
      label: "open items"
  steps:
    - id: load-guidance
      label: Load guidance
      prompt: |-
        Load and follow guidance/MESSAGING.md and guidance/ATTENTION.md as written.
    - id: review-activity
      label: Review activity since last check
      prompt: |-
        Review messaging activity **since your last check in this conversation** — not
        just what still shows as unread. Unread state tells you what I have *seen*,
        not what still needs me: I read my own messages within seconds, so `unread`
        is usually already false by the time you look. Compare each conversation's
        own last-message timestamp against your last-check time rather than relying
        on a read flag.

        Report what needs my attention from that window. Use your judgement about
        whether to re-surface something you have already told me about — guidance/ATTENTION.md
        governs that. Check the command's own completeness field: if any conversation
        could not be inspected, report it explicitly as a coverage gap. Never say a
        source was quiet when it could not actually be checked.
    - id: mark-read
      label: Mark read what can be marked
      prompt: |-
        Determine which messages can be marked read, based on guidance I have
        previously given or because they clearly do not need my attention. Act on
        that determination for real, and report what you actually did — for each
        message, what it was and which rule matched. If a message could not be
        marked (tool error, ambiguous match, protected content, any other reason),
        say so and name it rather than silently skipping it or approximating success.
    - id: update-guidance
      label: Update guidance
      prompt: |-
        Update guidance/MESSAGING.md with anything durable you learned this run —
        a confirmed preference, a correction, a pattern worth reusing. This file
        cannot grow unbounded, so use judgement about when to revise, consolidate,
        or prune rather than only appending. Put cross-domain learnings in
        guidance/ATTENTION.md instead.

        If some part of this run could not be carried out with the tools you have,
        say so explicitly rather than approximating.
---

The canonical worked example: copy this file, edit it, and enable it. It reviews messaging activity since its last check, marks read what your guidance authorizes, and maintains its own guidance file. Every step lives in the frontmatter `steps:` list -- each with an `id` (identity in run records), a `prompt` (the whole behavior), and an optional `label`.
