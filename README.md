# drumbeat

**An automation engine for long-running agent sessions.** You write an
automation as a markdown file — a schedule, a notify policy, and an ordered
list of natural-language steps. drumbeat runs those steps as sequential turns
in a pinned agent session, on time, unattended, with the tools you bring; then
it emits a reasoned, durable record of what it decided, including *why* it
stayed quiet. It never delivers anything itself: it hands your service a
delivery intent and gets out of the way.

The design goal is not "agents that do things." It is **agents that run
unattended for months without silently going wrong** — so most of what is in
here exists to convert a silence you cannot trust into a record you can.

---

## Prerequisites

Two things. The engine installs as a single command that co-installs the agent
it runs; you supply the provider key it runs against.

**1. `uv` and `git`.** drumbeat installs from a git URL with
[uv](https://docs.astral.sh/uv/), which also provisions the Python it needs
(3.13). Install `uv` first if you don't have it; `git` must be present because
the install is a git reference. `amplifier-agent` — the library every turn
imports — is a declared dependency, so the single install below brings it too.
It is **not on PyPI**; there is nothing separate to install, no agent CLI, and
nothing to put on your PATH.

**2. An LLM provider key, exported in the environment the engine starts in.**

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or your provider's equivalent
```

A provider whose credential is missing is a **loud** failure, not a quiet one:
the agent library refuses to start the turn with its own typed error, and the
run is recorded `failed: true` carrying that message and its remedy verbatim.
A turn that somehow replies with a provider-unavailability statement instead
fails the run too. Neither ever reads as a successful run that did nothing.

Prove the key end to end the way the engine will, once, rather than discovering
it in a run artifact at 03:40: start the engine, trigger one run of a trivial
automation (Quickstart steps 3–5 below), and read its `result.json`.

Running the engine under systemd? The key must be in the *unit's* environment
(an `EnvironmentFile=`), not just your shell's.

---

## Quickstart

**1. Install the engine.** One command. It installs `drumbeat` and co-installs
the `amplifier-agent` engine library it runs on into the same tool environment.

```bash
uv tool install git+https://github.com/microsoft/amplifier-drumbeat
drumbeat --help
```

That is the whole install. `amplifier-agent` rides along as a declared
dependency, so the library lands in the same tool venv and every turn imports it
there automatically — there is no second install and nothing to put on your
PATH. `uv tool upgrade drumbeat` takes drumbeat and its pinned agent library in
one step.

If you ever need to install the library yourself (a dev checkout, or a venv that
has not installed its dependencies), the full git URL is the mechanism — it is
not on PyPI and the distribution lives in a subdirectory, so a bare
`uv tool install amplifier-agent` will not resolve:

```bash
uv tool install "amplifier-agent @ git+https://github.com/microsoft/amplifier-agent@v1#subdirectory=packages/python"
```

A running engine keeps executing the code it was started from until you restart
it: reinstall or upgrade under a live engine and `drumbeat doctor` reports
exactly that, as `STALE` — the difference between "I upgraded that" and "I
upgraded that and restarted."

**2. Make a workspace.** One command scaffolds the whole layout — the four
directories, both prompt files, a drumpack list, a placeholder agent config, and
one working example automation.

```bash
drumbeat init ~/myspace
```

Run it twice and it refuses rather than overwrite, naming exactly what already
exists; `--force` overwrites the scaffold files and nothing else. Everything it
writes is yours to edit.

`prompts/` is not optional decoration. `auto-notify.md` is the exact text sent
to the agent to ask "is any of this worth telling the user about?" — the engine
ships **no built-in copy**, so a `notify: auto` automation aborts, loudly and by
name, if you empty or delete it. `init` writes it for you; it is a plain file.

**3. Write one automation.** `init` already dropped one example under
`automations/` (shipped disabled, like every exemplar). For your very first run,
add a trivial always-on one — `~/myspace/automations/hello.md`:

```markdown
---
automation:
  name: Hello
  enabled: true
  trigger:
    type: schedule
    expression: every 10 minutes
  notify: always
---

1. Report the current time and confirm which schedule triggered this run.
```

That runs today, with no tools at all.

**4. Start the engine.**

```bash
drumbeat serve --workspace ~/myspace --port 9100
```

It refuses to start, naming the fix, if the `amplifier-agent` engine library
cannot be imported — so an engine that cannot execute a single turn never
reaches the point of reporting itself healthy.

**5. Run it now, before trusting the schedule.** In a second terminal:

```bash
KEY=$(drumbeat api-key --workspace ~/myspace --show)
curl -s -X POST -H "X-API-Key: $KEY" localhost:9100/api/automations/hello/run
curl -s -H "X-API-Key: $KEY" localhost:9100/api/automations/hello/runs
```

Every mutating request needs that key, including from loopback. It is minted
on first start and lives in your data dir; `--show` prints it.

Then read the run artifact under `~/myspace/runs/hello/<run_id>/`. Every step's
output is there.

**6. Check it.**

```bash
drumbeat doctor --workspace ~/myspace
```

**What healthy looks like:** `status: FRESH` (the running process matches the
code on disk), `agent turns in flight: 0` between runs, `agent command:` naming
the agent library's version and the interpreter that imports it,
`draining: no`, and `orphan pins: 0`. `FRESH` is the one to learn
— it goes `STALE` the moment you edit engine code under a running process,
which is the difference between "I fixed that" and "I fixed that and restarted."

**Two notes you will see on a fresh quickstart workspace, both expected.**
`workspace git: not a git checkout` — the engine is telling you your policy has
no archive, which is true and fine for an evaluation. And a `CONTAINMENT
WARNING` that your data dir sits inside your workspace: with the layout above
it does, deliberately, because one directory is simpler to start with. It
matters the day you `git init` that workspace, since a `git clean -fdx` would
then take engine state with it. Splitting them is a `--data-dir` flag away
whenever you want it — see [`docs/PLATFORMS.md`](docs/PLATFORMS.md). Neither
note means anything is broken.

---

## Give it a tool

The engine ships **zero tools** — you bring your own as a *drumpack*: a directory
with a `drumpack.md` card and a `bin/` of executables.

```
myspace/drumpacks/hello/
  drumpack.md          # frontmatter + the card, injected verbatim into runs
  bin/hello-state      # any executable; must self-document via --help
```

List it in `myspace/drumpacks.txt`, then name the tool in your automation's
`requires:`. The engine puts the drumpack's `bin/` on the turn PATH and injects
its card into every automation that requires one of its tools. **The card is
structurally inseparable from the binary** — a tool on PATH with no
documentation attached is the one thing the format prevents.

Copy [`tests/packs/minimal/`](tests/packs/minimal) — it is a complete working
drumpack in about fifteen lines, and it carries the `inject:` and `activity:`
exemplars.

---

## How do I know it's alive?

Four checks, in the order you should reach for them. Nothing here needs the
API key except the last.

```bash
# 1. Is the process healthy, and is it running the code on disk?
drumbeat doctor --workspace ~/myspace

# 2. Has anything happened? Every decision the engine has ever made:
ls ~/myspace/runs/                      # one directory per automation
tail ~/myspace/runs/engine-events.jsonl # the outbox — the durable record
cat  ~/myspace/runs/failures.log        # every failed run, one line each

# 3. Is the HTTP face up?
curl -s localhost:9100/api/health

# 4. What is scheduled, and when does it fire next?
curl -s -H "X-API-Key: $KEY" localhost:9100/api/automations
```

**The read door.** `engine-events.jsonl` is the whole product. Leave this
running in a terminal while you wait for a scheduled run:

```bash
tail -f ~/myspace/runs/engine-events.jsonl | jq -c 'select(.type=="delivery_intent")
  | {automation, verdict, gate, reason}'
```

The payoff is not the hello automation's output — you could have gotten that
from a cron job. **The payoff is the first line where `verdict` is not
`deliver`: the run that stayed quiet, and wrote down why it stayed quiet.**
That reasoned record is what the engine exists to produce. A run that produces
no delivery intent at all is an *invalid run*, and the engine enforces that
against itself.

[`examples/consumers/tail_intents.py`](examples/consumers/tail_intents.py) is
the same idea in fifty lines of stdlib, turning `deliver` intents into desktop
notifications. It is an example, not a product: copy it and own it.

---

## Documentation

Read in this order.

| Doc | What it covers |
|---|---|
| [`examples/`](examples) | Seven automations and their guidance templates, generalized from ones that ran unattended for months. **Start here** — copy one and edit it |
| [`docs/AUTOMATIONS.md`](docs/AUTOMATIONS.md) | The automation format in full: frontmatter, triggers, notify policy, `requires:`, `inject:`, and how to write steps that hold up |
| [`docs/TUNING.md`](docs/TUNING.md) | The loop for making an automation actually good — instrument silence, review usage not output, derive rules from your own data. **The page that makes people believe this works**; the format is easy and the tuning is the whole job |
| [`docs/DRUMPACKS.md`](docs/DRUMPACKS.md) | The drumpack contract: load rules, the optional `activity:` narration map, the `INJECT_IDLE` sentinel, PATH guarantees, completeness conventions |
| [`docs/PLATFORMS.md`](docs/PLATFORMS.md) | Running it for real: WSL, macOS, one-serve-per-workspace, and how to stop it without corrupting a session |

Reference, once you want to know why: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
— what the engine is, the turn model, session lifecycle, the delivery seam, and
the consumer boundary.

---

## Skills

`skills/` is a first-class surface: an AI agent can learn to operate the engine,
author automations, and build drumpacks from this repo alone. Each skill is a
self-contained [Agent Skill](https://agentskills.io) — it carries the how-to
layer inline and links the canonical docs above for depth.

| Skill | What it teaches |
|---|---|
| [`drumbeat-operations`](skills/drumbeat-operations) | Install, run, supervise (`service`), health-check (`doctor`), rotate pinned sessions, `drain`/`sweep`, and troubleshoot |
| [`drumbeat-automation-authoring`](skills/drumbeat-automation-authoring) | The automation-file contract as a how-to: closed frontmatter, structured `steps:`, schedules, notify sentinels, `requires:`/`inject:`, `agent_config:` (incl. `model_class:`/`reasoning_effort:`), the guidance loop |
| [`drumbeat-drumpack-authoring`](skills/drumbeat-drumpack-authoring) | The `drumpack.md` card, `bin/` conventions, `activity:` labels, `inject:` tool rules, and wiring via `drumpacks.txt` |

Add them to your own Amplifier session in one command -- this composes
[`behaviors/drumbeat.yaml`](behaviors/drumbeat.yaml) onto your bundle and
registers the three skills, nothing else:

```bash
amplifier bundle add git+https://github.com/microsoft/amplifier-drumbeat@main#subdirectory=behaviors/drumbeat.yaml --app
```

**Requires an Amplifier installed or upgraded on or after 2026-09-03**
(`uv tool upgrade amplifier`), i.e. `amplifier-foundation` at or past commit
`5ebf1da`. Older loaders editable-install a bundle repository's root Python
package into the Amplifier environment; this repository's package is the
engine itself (Python >= 3.13), so on an older Amplifier the line above breaks
every session on the machine until `amplifier bundle remove drumbeat`. The
loader fix is [microsoft/amplifier-foundation#352](https://github.com/microsoft/amplifier-foundation/pull/352).

On an older Amplifier -- or if you would rather not compose a bundle from an
application repo at all -- point `tool-skills` at this repo's `skills/`
subdirectory instead. This path works on every Amplifier and was proven in the
same clean environments:

```yaml
tools:
  - module: tool-skills
    source: git+https://github.com/microsoft/amplifier-bundle-skills@main#subdirectory=modules/tool-skills
    config:
      skills:
        - "git+https://github.com/microsoft/amplifier-drumbeat@main#subdirectory=skills"
```

Either way, consumers get **only** `skills/` -- each skill stands alone. The
repo also carries a root [`bundle.md`](bundle.md) for running a standalone
foundation session with the same skills loaded. Validate any edits with the
reference linter -- `npx skills-ref validate skills/drumbeat-operations`.

---

## What it does not do

Fences, each rejecting a specific temptation:

- **Not a delivery system.** No push, no transport, no quiet hours, no
  notification store. It emits an intent with a required reason; your service
  delivers.
- **Not an item tracker.** No items, no priorities, no domain objects.
  `inject:` is the only aperture for your state, and it is domain-blind.
- **Not a policy owner.** Zero default automations, zero default guidance, zero
  default prompt text. `examples/` is documentation — there is no built-in
  fallback if your files are missing.
- **Not a platform.** No multi-tenancy, no pack registry, no permission model
  beyond an API key, no trigger grammar beyond `schedule | manual`.

---

## The parts that were expensive to learn

Skim these before assuming a simpler design would do:

- **Sessions are pinned, not per-run**, which is what makes "since your last
  check" mean anything — and it forces a rotation story. The health triggers
  are zero-judgment: the provider refusing the prompt, the automation's steps
  having been rewritten under a session still obeying the old ones, and the
  session's prompt crossing a token gate before the turn rather than after the
  crash.
- **The gate counts what the provider counts.** Rotation fires pre-emptively
  when a session's most recent run recorded more than 150,000 prompt tokens —
  the agent library's own reported count, in the unit the provider refuses on. Stored
  size would not do: measured, 10.4 MB of stored conversation produced a
  219,685-token prompt while 33.0 MB produced 201,361 — the *smaller* store
  made the *larger* prompt. The two prompts the provider ever reported were
  refused at 219,685 and 201,361 tokens, so the gate sits about a quarter below
  the smallest observed refusal: it keeps sessions in a region where no crash
  was ever observed, and does not pretend to predict one.
- **Silence is never a contract value.** A tool with nothing to say prints
  `INJECT_IDLE`; bare-empty stdout aborts the run loudly. A crashed pipe and a
  genuinely idle state must not share an observable.
- **Every gate that can reduce output writes a reason.** A run with no delivery
  intent is an invalid run, and the engine enforces that against itself.

---

## Status and provenance

Working and in daily unattended use, but young: the API surface is small on
purpose and will grow only when a real consumer needs it. There is no packaged
release; you install from a checkout, and the setup is deliberately manual —
you will write your own systemd unit, and the exemplars ship disabled.

The `examples/` tree is generalized from automations that ran against real
accounts for months — the judgment in them is paid for; the specifics of any
one deployment have been removed.

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
