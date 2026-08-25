---
automation:
  name: B4 Namespace Drill
  enabled: false
  trigger:
    type: manual
  notify: auto
  steps:
    - id: step-1
      prompt: |-
        This is a MECHANISM DRILL for the namespace batch, fired manually on purpose. It is not real work, it is not about anything in your ledger, and nothing here needs judgment.

        Do exactly three things and nothing else. Do not read guidance files. Do not check mail, Teams, or sessions. Do not touch the item ledger.

        (a) Copy, VERBATIM, the very first line of this turn's text — the line that starts with a bracketed label and states the current date/time. Do not paraphrase it, do not reformat it, do not correct it.

        (b) Run exactly this one bash command and paste its output verbatim:

            env | grep -E '^DRUMBEAT_TURN_SESSION_ID=' | sort

        (c) Reply with those two things under the headings `PREFIX LINE:` and `ENV:` and nothing else — no commentary, no bold, no summary.
    - id: step-2
      prompt: |-
        Reply with exactly this one line and nothing else — no bold, no headings, no preamble:

        B4 NAMESPACE DRILL complete. Ignore this.
---

Mechanism drill: turn prefix + dual-emitted session-id env vars.
