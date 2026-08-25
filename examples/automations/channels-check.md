---
automation:
  name: Channels Check
  enabled: false
  trigger:
    type: schedule
    expression: every 90 minutes
  # `urgent-only`: identical work and identical judgment to `auto`, but the
  # reply is delivered ONLY if it opens with an `URGENT: <reason>` marker.
  # This is the right policy for a broad sweep whose findings belong in a
  # record rather than in an interruption. See docs/AUTOMATIONS.md section 4.
  notify: urgent-only
  # PLACEHOLDER tool names -- substitute whatever your packs provide.
  requires:
    - chat-cli
    - items-cli
    - guidance/ATTENTION.md
    - guidance/MESSAGING.md
  inject:
    - argv: ["items-cli", "inject-turn"]
      label: "open items"
  steps:
    - id: load-guidance
      prompt: |-
        Load and follow guidance/MESSAGING.md and guidance/ATTENTION.md.
    - id: sweep-channels
      prompt: |-
        Sweep every broadcast channel you have access to for activity since your last
        check in this conversation — or the last two hours if this is the first run.
        Review both the root messages and their replies.
    - id: report-attention
      prompt: |-
        Report what needs my attention from that window, using your judgement per the
        guidance. Check the command's own completeness field: if any channel could
        not be inspected, report it explicitly as a coverage gap. **Never say a
        channel was quiet when it could not actually be checked** — "nothing found"
        and "could not look" are different answers, and only one of them is
        trustworthy.
    - id: read-only-guard
      prompt: |-
        This is a read-only run: do NOT mark anything read, do NOT send anything, do
        NOT edit any files. Recording items in your durable record is the one
        exception — that is internal bookkeeping, not an action against a channel.
    - id: notify-policy
      prompt: |-
        This automation is `notify: urgent-only`. **Your report is always saved to
        this run's own record whether or not it reaches my phone**, so recording an
        item and reporting a coverage gap are never optional — do both regardless of
        urgency. Never let "this one doesn't push" become "this one doesn't bother."

        A notification is delivered ONLY if your final reply's first line is
        `URGENT: <one-line reason this specifically needs me right now>`. Reserve
        that for something a broad channel sweep rarely produces. **Most sweeps
        should not carry it** — if you are unsure whether something clears the bar,
        it does not. The finding lives in the record either way, and I can find it
        when I look.

        If some part of this cannot be carried out with the tools you have, say so
        explicitly rather than approximating.
---

A broad, low-noise sweep of your broadcast channels. Its findings live in your durable record; it interrupts you only when a reply is marked urgent. The steps live in the frontmatter.
