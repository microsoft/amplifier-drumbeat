---
name: drumbeat-operations
description: >-
  Install, run, supervise, and troubleshoot the drumbeat automation engine and
  its `drumbeat` CLI. Covers the single `uv tool install`, workspace `init`, the
  `doctor` staleness check, running under a supervisor (`service install|status|
  uninstall` — systemd --user on Linux, launchd on macOS), pinned-session
  inspection and rotation (`sessions`, `rotate-session`), safe restart via
  `drain`, the invalid-run `sweep`, the engine API key, and fixes for wedged or
  orphaned session pins, PATH gaps, and a missing provider key. Use when
  installing, starting, supervising, health-checking, restarting, or debugging a
  drumbeat engine — or whenever a `drumbeat <command>` is involved.
metadata:
  project: drumbeat
  canonical_docs: https://github.com/microsoft/amplifier-drumbeat
compatibility: Requires uv and git; Python 3.13 is provisioned by uv. An LLM provider key in the engine's environment.
---

# Operating the drumbeat engine

drumbeat is an automation engine for long-running agent sessions. It runs
markdown automations on a schedule, in pinned agent sessions, and emits a
durable delivery-intent record. This skill is the how-to for standing one up
and keeping it healthy. For the deeper "why", link out to the canonical docs
(cited inline) — do not assume this repo's files are on disk, they usually are
not when this skill is consumed on its own.

**One rule underlies everything here:** a run that looks successful but did
nothing is the enemy. Every command below fails loud with a remedy rather than
degrading silently. When something is off, read the actual message — it names
the fix.

## The command surface

`drumbeat` is one binary with subcommands. **`--workspace <dir>` is required
with no default on every command except `init`** (which *creates* a workspace).
Every `--workspace` command also accepts `--data-dir` (default
`<workspace>/runs`); if you split state from policy, pass it *consistently* to
every command or they read different directories.

| Command | Purpose |
|---|---|
| `drumbeat init <dir> [--force]` | Scaffold a workspace (dirs + default files) |
| `drumbeat serve --workspace <dir> [--port N] [--host 127.0.0.1]` | Run the engine (scheduler + loopback HTTP API), foreground |
| `drumbeat doctor --workspace <dir>` | Is the running engine executing the code on disk? Health snapshot |
| `drumbeat sessions --workspace <dir>` | Which conversation each automation resumes; orphan pins |
| `drumbeat session-health --workspace <dir>` | Per-automation consecutive failures, ceiling/drift detail |
| `drumbeat rotate-session <slug> --workspace <dir> --reason R` | Abandon one automation's pinned conversation |
| `drumbeat drain --workspace <dir> --reason R [--wait\|--status\|--clear] [--pid N]` | Stop starting runs; verify it is safe to kill |
| `drumbeat sweep --workspace <dir> [--since ISO8601Z]` | Find invalid runs (runs with no delivery intent) |
| `drumbeat api-key --workspace <dir> [--show]` | This instance's engine API key |
| `drumbeat service install\|status\|uninstall …` | Run the engine under systemd --user / launchd |

## 1 · Install (one command)

The engine installs from a git URL with [uv](https://docs.astral.sh/uv/), which
also provisions Python 3.13. `amplifier-agent` — which ships the engine library
every turn imports — is a dependency, so this single install co-installs it into
the same tool venv. It is **not on PyPI**; there is nothing separate to install
and nothing to put on PATH.

```bash
uv tool install git+https://github.com/microsoft/amplifier-drumbeat
drumbeat --help
```

`uv tool upgrade drumbeat` takes drumbeat *and* the latest agent `main` in one
step (the agent dependency is deliberately unpinned).

A running engine keeps executing the code it started from until restarted:
reinstall or upgrade under a live engine and `drumbeat doctor` reports `STALE`.

## 2 · Provide a provider key (verify it by hand once)

Export the key in the environment the engine starts in:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or your provider's equivalent
```

**The failure this prevents:** with no key, a turn still **exits 0** and returns
the reply `Error: No providers available` — a successful-looking run that did
nothing. Verify once, the same way the engine will:

```bash
uvx --from git+https://github.com/microsoft/amplifier-agent \
  amplifier-agent run --fresh --session-id keycheck --output json -y --cwd . "say ok" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["reply"])'
```

A real greeting means you are done. `Error: No providers available` means every
automation will produce that string. **Under a supervisor the key must be in the
*unit's* environment** (an `EnvironmentFile=`), not just your shell's.

## 3 · Scaffold a workspace and run it

```bash
drumbeat init ~/myspace                       # dirs + default files + one example
drumbeat serve --workspace ~/myspace --port 9100
```

`init` refuses rather than overwrite (naming what exists); `--force` overwrites
only the scaffold files. `serve` refuses to start, naming the fix, if the
`amplifier-agent` engine library cannot be imported — an engine that cannot run
a turn never reports itself healthy. Give each engine its own port; there is one engine per
workspace.

`prompts/auto-notify.md` is not decoration: it is the exact text a `notify:
auto` automation sends to ask "is any of this worth telling the user about?"
The engine ships **no built-in copy** — empty or delete it and such an
automation aborts loudly by name. `init` writes it for you.

Trigger a run and read the artifact without waiting on the schedule:

```bash
KEY=$(drumbeat api-key --workspace ~/myspace --show)
curl -s -X POST -H "X-API-Key: $KEY" localhost:9100/api/automations/<slug>/run
curl -s        -H "X-API-Key: $KEY" localhost:9100/api/automations/<slug>/runs
```

Every mutating request needs that key, including from loopback.

## 4 · doctor — the health check to learn first

```bash
drumbeat doctor --workspace ~/myspace
```

What healthy looks like: `status: FRESH` (running process matches disk),
`agent turns in flight: 0` between runs, `agent command:` naming the engine
library and the interpreter that imports it, `bundle prewarm: OK`,
`draining: no`, `orphan pins: 0`. Two notes are expected on a fresh workspace
and mean nothing is broken: `workspace git: not a git checkout` (your policy has
no archive yet) and a `CONTAINMENT WARNING` that the data dir sits inside the
workspace (fine until you `git init` it — split with `--data-dir`).

`FRESH` → `STALE` the moment you edit engine code under a running process. That
is the difference between "I fixed that" and "I fixed that **and restarted**."

## 5 · Run it as a supervised service

`serve` is the foreground engine; leaving it running is a separate job for the
platform supervisor. These verbs generate the correct unit/plist, install it,
start it, and **verify a real turn** before reporting success — an install that
enabled a unit which never bound its port would otherwise look like it worked.

```bash
drumbeat service install --workspace ~/myspace --port 9100   # systemd --user (Linux) / launchd (macOS)
drumbeat service status                                       # unit state + probe /api/health
drumbeat service uninstall                                    # stop, disable, remove, verify gone
```

`--skip-turn-verify` exists but is not recommended: `/api/health` can pass on a
unit whose every scheduled run fails (missing provider key, or a tool not on the
unit's PATH). Let the install prove a real turn. Remember the provider key must
live in the unit's `EnvironmentFile=`.

## 6 · Restart safely (never `pkill -f`)

When `doctor` says `STALE`, the running process is on old code. Restart with the
drain procedure so an in-flight turn is never severed:

```bash
drumbeat drain --workspace ~/myspace --reason "picking up engine edits" --wait
# … wait for DRAINED, then kill by the EXPLICIT pid doctor/drain reported, then:
drumbeat serve --workspace ~/myspace --port 9100    # or: drumbeat service … / systemctl --user restart
drumbeat drain --workspace ~/myspace --clear        # resume scheduling
```

`--reason` is required to set a drain (a drained scheduler that cannot say why
is indistinguishable from a broken one). `drumbeat drain --status [--pid N]`
reports without changing anything. **Never `pkill -f` / `killall`** — signal
only the pid you identified. (Under a supervisor, prefer `systemctl --user
restart` / `launchctl kickstart -k` over a manual kill.)

## 7 · Session pins: inspect, rotate, unstick

Each automation resumes **one pinned conversation** across runs — that is what
makes "since your last check" mean anything. Pins live in
`<data-dir>/session_pins.json`, written by the engine; **nothing writes to your
automation file, ever.**

```bash
drumbeat sessions --workspace ~/myspace                                 # what each slug resumes + orphans
drumbeat rotate-session <slug> --workspace ~/myspace --reason "context bloat"
```

`rotate-session` takes a **required reason**, clears the pin, writes a durable
line to `<data-dir>/session_rotations.jsonl`, and the next run starts fresh. It
never deletes a transcript.

**Orphan pins** are the named cost of keying the store by slug. Renaming an
automation (`git mv teams-check.md teams-check-v2.md`) starts the new slug cold
and strands the old pin as an orphan (reported by `doctor` and `sessions`). To
retire the stranded entry, `rotate-session` the *old* slug. The engine also
rotates a pin **automatically**, with a logged reason, on three zero-judgment
signals: a provider context-ceiling hit, a rewrite of the automation's step
**prompts**, and a change of **provider module** in `agent_config:` (changing
only the model does not rotate).

If `rotate-session` prints `no pinned session to clear` (and exits non-zero) but
you expected one, the usual cause is a `--data-dir` that does not match the
running engine's. Confirm with `drumbeat sessions` using the same `--data-dir`.
An `AMPLIFIER_AGENT_WORKSPACE` export in your shell also re-buckets session ids
under a different slug — `doctor` flags it as `ambient workspace override SET`;
unset it before pin surgery.

## 8 · sweep — catch the runs that stayed silent

Every run must emit a delivery intent; a run with none is an **invalid run**,
and the engine enforces that against itself. Sweep for them:

```bash
drumbeat sweep --workspace ~/myspace                         # all recorded runs
drumbeat sweep --workspace ~/myspace --since 2026-08-09T16:00:00Z
```

Non-zero exit means findings exist. `--since` must be ISO-8601 UTC.

## 9 · Is it alive? Reads that need no key

```bash
drumbeat doctor --workspace ~/myspace                    # process healthy + running disk code?
ls   ~/myspace/runs/                                      # one directory per automation
tail ~/myspace/runs/engine-events.jsonl                   # the durable outbox — the whole product
cat  ~/myspace/runs/failures.log                          # every failed run, one line each
curl -s localhost:9100/api/health                         # HTTP face up?
```

The payoff line is the first `delivery_intent` whose `verdict` is not
`deliver`: the run that stayed quiet **and wrote down why**.

## Troubleshooting quick map

| Symptom | Cause → fix |
|---|---|
| Every automation replies `Error: No providers available` | No provider key in the engine's environment. Export it (or set the unit's `EnvironmentFile=`); verify per §2 |
| `serve` refuses to start naming `amplifier-agent` | The engine library does not import. Reinstall per §1; `doctor` shows `agent command: MISSING` with the install hint |
| `doctor` says `STALE` | Process on old code. Restart with the drain procedure (§6) — never `pkill -f` |
| `doctor`/`sessions` show `orphan pins: N` | An automation was renamed. `rotate-session` the old slug to retire it (§7) |
| A tool the automation `requires:` is "not found" at run time | It is not on the constructed turn PATH. See PATH rules in [DRUMPACKS.md](https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/DRUMPACKS.md); read the live base from `GET /api/capabilities` (`packs.path_base_pinned`) |
| `rotate-session` clears nothing but a pin exists | `--data-dir` mismatch, or `AMPLIFIER_AGENT_WORKSPACE` set. Reconcile per §7 |
| A run "succeeded" but delivered nothing expected | Check `notify:` policy and its sentinels, then `drumbeat sweep` for invalid runs (§8) |

## Canonical docs (link, don't duplicate)

- Running for real (WSL, macOS, one-serve-per-workspace, clean shutdown): [docs/PLATFORMS.md](https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/PLATFORMS.md)
- The engine, turn model, session lifecycle, delivery seam: [docs/ARCHITECTURE.md](https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/ARCHITECTURE.md)
- Making an automation actually good over time: [docs/TUNING.md](https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/TUNING.md)
- Authoring automations / drumpacks: the sibling `drumbeat-automation-authoring` and `drumbeat-drumpack-authoring` skills
