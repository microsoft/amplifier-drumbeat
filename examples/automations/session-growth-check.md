---
automation:
  name: Session Growth Check
  enabled: false
  trigger:
    type: schedule
    expression: daily at 12:50
  notify: auto
  # This automation requires NO PACK -- it watches the engine's own session
  # fleet by reading files the engine already writes. It is the one example
  # here you can run today with nothing else installed.
  #
  # Two things it DOES need: `prompts/auto-notify.md` copied into your
  # workspace (it is `notify: auto`, and the engine ships no built-in prompt
  # text), and the requirement below. `guidance/ATTENTION.md` is optional --
  # drop the line if you have not copied one in yet.
  requires:
    - guidance/ATTENTION.md
  steps:
    - id: confirm-fire-time
      prompt: |-
        Report the current wall-clock time and confirm which schedule expression
        triggered this run. This automation exists partly to prove the
        `daily at HH:MM` form fires correctly, so say plainly whether the fire time
        matched what the automation declares.
    - id: gather-measurements
      prompt: |-
        Gather the measurements. **The files are the measurement; your job is the
        judgment.** Everything below ships with the engine — no pack, no extra
        tool:

        - `drumbeat sessions --workspace .` — which conversation each automation
          resumes, and the orphan-pin count.
        - `<data-dir>/failures.log` — one line per failed run, newest last. This is
          where you count *consecutive* failures per automation.
        - `<data-dir>/session_rotations.jsonl` — every deliberate or automatic
          session rotation, with its reason.
        - `<data-dir>/engine-events.jsonl` — the outbox. `automation_error` events
          are the failures as the consumer saw them.

        Read `guidance/ATTENTION.md` for how much of this is worth voicing.

        If your workspace supplies its own richer session-health tool, use that
        instead and say which one you used — but do not assume one exists. Name the
        files you actually read.

        **Transcript size is not a risk
        signal and must never be reported as one.** This was measured against the
        only two sessions whose true token counts the provider ever reported: one at
        10.4 MB on disk produced a 219,685-token prompt, while another at 33.0 MB
        produced 201,361. The *smaller* file made the *larger* prompt. Compaction has
        already discarded an unknown prefix, and the file carries megabytes of
        signature data that is never sent to the provider. "Session X is at 42 MB and
        at risk" is a false alarm, and past runs of this automation made exactly that
        mistake. A large transcript is a housekeeping aside at most.
    - id: report-needs
      prompt: |-
        Report what actually needs me, in descending order of what matters:

        - **A dead session** — the provider refused the prompt, which the failure
          text says outright. This never recovers on its own; the next run of that
          automation auto-rotates it. Say which automation, how many consecutive
          runs it has already lost, and roughly how long it has been down. A dead
          automation has been silently doing nothing since its first failure —
          **the outage is the story, not the session.**
        - **A drifted session** — the automation's steps were rewritten and the
          session was still carrying the old contract. You will see this as a
          rotation whose reason names contract drift. Auto-rotates on the next run.
          Worth one line so I know a rewrite landed.
        - **Consecutive failures with no such explanation** — something is broken
          that these mechanisms do not cover. That is the most interesting thing you
          can find, because it means the failure is new. Say so plainly, and say
          that you do not know the cause.
        - **Any rotation since your last run** — I should learn from you that a
          session was replaced, not discover it later.

        Say which files you read and over what window. If a file does not exist
        yet, say that rather than treating its absence as "all clear" — an empty
        `failures.log` and a missing one are different facts.
    - id: judge-notify
      prompt: |-
        Judge whether any of this is worth interrupting me. Nothing dead, nothing
        drifted, no unexplained failures, no new rotations means this run should say
        so in one line and go no further — **that is the correct and expected outcome
        most days.** This automation is `notify: auto`, so ending with the
        `NOTHING_TO_REPORT` sentinel keeps it silent, and a quiet run is a success,
        not a failure.

        If something *is* wrong, say it plainly in the report itself. Do not reach
        for an `URGENT:` marker to make it land — `notify: auto` delivers whatever
        you judge worth saying. A dead automation is worth saying.
    - id: no-act-guard
      prompt: |-
        Do not rotate anything yourself, and do not delete any transcript. Dead and
        drifted sessions auto-rotate on their own next run; manual rotation exists
        for the cases I decide by hand. **This pass reports; it does not act.**
---

Watches the engine's own session fleet by reading the files the engine already writes -- the one example you can run today with no pack installed. The steps live in the frontmatter.
