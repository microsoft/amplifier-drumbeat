# Examples

Working starting points. **Copy these into your workspace and edit them** —
they are documentation, not defaults. The engine ships no automations and no
guidance; nothing in this directory is loaded at runtime.

```
examples/
  automations/     seven automations, battle-honed then generalized
  guidance/        the policy files those automations reference
  prompts/         the two prompt files the engine sends turns from
  consumers/       a fifty-line example of the half the engine does NOT do
  drills/          gate drills — not starting points; see drills/README.md
```

**Most of you will run 2 of these 7 this week.** The set is here to show the
shape of the judgment, not to be adopted wholesale. Pick the one that matches a
check you already do by hand.

Copy an automation together with everything it needs — the prompts, then its
guidance:

```bash
cp examples/prompts/auto-notify.md          <workspace>/prompts/
cp examples/prompts/system.md               <workspace>/prompts/
cp examples/automations/messaging-check.md  <workspace>/automations/
cp examples/guidance/ATTENTION.md           <workspace>/guidance/
cp examples/guidance/MESSAGING.md           <workspace>/guidance/
```

The prompt files are copied **once per workspace**, not once per automation.
Skip them and every `notify: auto` automation here — six of the seven — aborts
by name on its first run: the engine ships no built-in prompt text and will not
invent any.

---

## What you must change before these run

**1. Tool names are placeholders.** Every `requires:` entry naming an
executable — `chat-cli`, `mail-cli`, `calendar-cli`, `transcript-cli`,
`worker-cli`, `items-cli` — is a stand-in for whatever your own packs provide.
The engine ships no tools. Substitute your real tool names, or the pre-run gate
will abort the run naming the missing requirement (which is the correct
behavior, and is how you will discover you skipped this step).

**2. Guidance files are templates.** The files in `guidance/` work blandly out
of the box: they encode *discipline* — what earns an interruption, how to
timestamp a record, when to stay quiet — but no preferences about you, your
work, or the people you work with. That part is yours to write. Several of
these automations have a final step that maintains its own guidance file, so
they will start filling themselves in once they run.

**3. `IDENTITY.md` is a skeleton with placeholders.** It will actively mislead
attribution until you replace the placeholder names.

**4. Do not add a `session:` key.** These files carry none, and the parser
**refuses** `session:` / `session_workspace:` in frontmatter rather than
ignoring them. Session pins are engine state now: the engine records them in
`<data-dir>/session_pins.json` after the first run and never writes to your
automation file. See [`../docs/AUTOMATIONS.md` §2](../docs/AUTOMATIONS.md).

**5. Start with `enabled: false`.** Every automation here ships disabled. Run
one by hand, read its run artifact, then enable it. See
[`../docs/TUNING.md`](../docs/TUNING.md).

---

## The set

| Automation | Notify | What it demonstrates |
|---|---|---|
| `messaging-check.md` | `auto` | The core loop: read policy, report what's new, take scoped cleanup actions, maintain its own guidance |
| `email-check.md` | `auto` | Authorized-to-act: applying archive rules for real, and reporting what it *did*, not what it *found* |
| `meetings-check.md` | `auto` | Expensive sources: a digest tool instead of raw content, and attribution that never upgrades an unknown speaker to a name |
| `channels-check.md` | `urgent-only` | Push demotion: full work, recorded findings, delivery only on an explicit `URGENT:` marker |
| `agent-sessions-check.md` | `auto` | Polled state with no message: timestamp-as-identity, and the decline rule that keeps machine-watching out of a human's queue |
| `daily-rollup.md` | `always` | A periodic report whose whole point is to arrive unconditionally |
| `session-growth-check.md` | `auto` | Engine-domain, no pack required: watches the engine's own session fleet |

`session-growth-check.md` is the one you can run today with **no packs at all** —
it watches the engine's own session fleet. Start there. It still needs
`prompts/auto-notify.md` copied in (it is `notify: auto`) and it lists
`guidance/ATTENTION.md` in `requires:`; drop that line if you have not copied a
guidance file yet.

---

## Where these came from

These are generalized from automations that ran unattended against real
accounts for months. The judgment in them is paid for: every "say so
explicitly rather than approximating," every "this is a read-only run," every
"check the completeness field" replaced a specific run that went quietly wrong.

What has been removed is the personal part — real names, real policy, real
tool surfaces, real session pins. What is left is the shape, and the shape is
the transferable part.
