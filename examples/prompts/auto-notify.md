---
note: >
  This file is yours. It is sent to the agent, verbatim, as the final turn of
  an automation whose automation file sets notify: auto or notify:
  urgent-only -- after all of that automation's own steps have run, in the
  same conversation, with all of that conversation's context (including
  whatever guidance files were loaded) still in view.

  Editing this file changes what gets asked on the very next run. There is no
  built-in copy of this prompt anywhere in the code -- if this file is missing
  or empty, notify: auto automations fail loudly rather than silently using
  some other text instead.

  The one thing you should not remove: the instruction to reply with the exact
  word NOTHING_TO_REPORT (and nothing else) when there is genuinely nothing to
  surface. The runner matches on that exact string to decide whether to emit a
  delivery intent. Everything else below is yours to rewrite.
---
Consider everything that has happened in this conversation so far, including
any guidance documents you loaded and followed as part of the steps above.

Decide whether there is something worth proactively telling the user about,
using the guidance's own rules for what counts as worth surfacing, how long an
item stays worth surfacing, and how often to voice it. Do not invent a
freshness or novelty test of your own, and do not invent a suppression rule of
your own — the loaded guidance decides both. The guidance's judgment governs;
this prompt does not override it.

If the guidance distinguishes tracking an item from voicing it, honor that
distinction: an item you are still tracking does not have to be said again at
full volume on every check. Consult whatever record of what you have already
raised your guidance points you at, so you neither repeat yourself nor
silently drop anything.

If, applying that guidance, there is something worth surfacing: reply with a
concise, ready-to-send notification message for the user.

If, applying that guidance, there is genuinely nothing to surface: reply with
exactly the single word `NOTHING_TO_REPORT` and nothing else — no punctuation,
no explanation, no leading or trailing whitespace.
