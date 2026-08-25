---
name: drumbeat-drumpack-authoring
description: >-
  Author drumbeat drumpacks — the tool-bundle contract that brings your own tools
  to the engine. Covers the drumpack directory layout, the `drumpack.md` card and
  its required frontmatter (`pack_format`, `name`, `description`, `tools`), the
  loader's fail-loud rules, `bin/` executable conventions and `--help`
  self-documentation, the optional `activity:` progress-narration map, the
  `inject:` tool sentinel rules (INJECT_IDLE, errors-to-stderr, exit codes), the
  pinned turn-PATH guarantee, the completeness conventions, what belongs in a
  card, and wiring a drumpack into a workspace via `drumpacks.txt` — with one
  complete worked example. Use when building, packaging, or debugging a drumbeat
  drumpack or its `bin/` tools.
metadata:
  project: drumbeat
  canonical_doc: https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/DRUMPACKS.md
---

# Authoring a drumbeat drumpack

The engine ships **zero tools**. You bring your own as a *drumpack*: a directory
(normally a git repo) with a `drumpack.md` card and a `bin/` of executables. The
whole contract is **PATH + card + fail-loud load rules** — there is no registry,
no install step, no manifest beyond the card's frontmatter, no version solver.
This skill is the how-to; the enforced contract is
[`docs/DRUMPACKS.md`](https://github.com/microsoft/amplifier-drumbeat/blob/main/docs/DRUMPACKS.md).

Everything under **Rules** below is enforced by the loader and refuses at startup
when violated. Everything under **Conventions** is on you — the engine cannot
enforce it without becoming the plugin protocol drumbeat deliberately refused.

## Directory layout

```
drumpack.md      frontmatter + the card (injected verbatim into runs)
bin/             executables; each MUST self-document via --help
guidance/        optional: exemplar policy files for this source
automations/     optional: exemplar automations requiring this drumpack's tools
```

## 1 · The `drumpack.md` card

The frontmatter is the manifest; the body is the card. The card is injected
**verbatim** into every automation whose `requires:` names one of your tools —
so write it for a reader who has never seen the tool and cannot ask you a
question.

```yaml
---
pack_format: 1                     # required, no default; unknown versions refuse
name: minimal                      # required; the drumpack's name
description: >                     # required
  One-line-plus statement of what this drumpack is and what source it fronts.
tools:                             # required; the EXHAUSTIVE list of bin/ entries
  - minimal-state
activity:                          # optional; subcommand -> narration label
  state: "Checking minimal state…"
---

# minimal (the card body — non-empty is required)

One tool, `minimal-state`: prints a one-line state snapshot on stdout, or the
byte-exact `INJECT_IDLE` sentinel when there is nothing to inject. Exit non-zero
on any failed read; errors go to stderr, never stdout.
```

## 2 · Rules the loader enforces (violation = refuse to start)

1. **`drumpack.md` must exist**, with YAML frontmatter.
2. **`pack_format` is required, with no default.** Unknown versions refuse loudly.
3. **`name`, `description`, and `tools` are required.** `tools` is the
   **exhaustive** list of `bin/` entries.
4. **Every declared tool must exist in `bin/`, be a file, and have the exec bit
   set.** A card naming a tool the agent cannot run is theater.
5. **Every executable in `bin/` must be declared.** An undeclared binary on PATH
   is an undocumented weapon. The card must lie in *neither* direction.
6. **The card body must be non-empty.** A tool on PATH with no knowledge attached
   is the one thing the card exists to prevent.
7. **Duplicate tool names across drumpacks refuse to start, naming both.** There
   is no precedence rule.
8. **Duplicate drumpack names refuse to start, naming both directories.**

Rule 7 cannot know what the base PATH already holds — **not shadowing an existing
system binary is your job** (see the name-prefix convention).

## 3 · `bin/` executables

Each tool is any executable, and **must self-document via `--help`**. In
practice every shipped tool is a `#!/usr/bin/env bash` shim. Set the exec bit
(`chmod +x bin/your-tool`) — an undeclared or non-executable file fails the load
rules above.

**What may a tool assume is on PATH? The pinned base list, and nothing more.**
The turn PATH is constructed as:

```
<drumpack 1 bin>  <drumpack 2 bin>  …  <workspace bin>  <PINNED BASE>
```

The pinned base is captured **once at engine startup**, identical for every turn
regardless of how the service was launched, and echoed in
`GET /api/capabilities` under `packs.path_base_pinned`. In practice it gives you
`bash`, `python3`, `git`, `jq`, `curl` and the usual system binaries. **Read the
live list from `/api/capabilities` rather than assuming.** A tool needing
anything beyond the base **declares it in its card and fails loud without it**
(exit non-zero naming it) rather than degrading to a different answer.

## 4 · The optional `activity:` narration map

While a tool runs, a chat/reply turn shows a human phrase instead of an opaque
spinner. That phrasing is **mechanism, not policy** — the engine hardcodes no
vocabulary. Each drumpack declares phrasing for its OWN tools, **keyed by
subcommand** (the first argument after the tool name):

```yaml
activity:
  calendar: "Checking your calendar…"
  send-mail: "Sending an email…"
```

When the agent runs `your-cli calendar --json`, the engine looks up `calendar`
in the drumpack that declares `your-cli` and narrates its label. **Absent is
fine** — an unmapped tool/subcommand narrates with a generic "Running a
command…". **The label is the only thing shown** — no part of the raw command
line (which may carry a session id or path) ever reaches the UI. Present-but-
malformed (an empty key or value) is a loud refusal, same as every other field.

## 5 · `inject:` tools — the rules are contract, not style

An `inject:` tool runs before step 1 of every run and its **stdout becomes a
turn**. The engine classifies the result in a fixed order — **timeout → exit
code → stdout**:

| Your tool does | The engine does |
|---|---|
| Times out | **Aborts the run**, voiced |
| Exits non-zero | **Aborts the run**, voiced |
| Exits 0, stdout is byte-exactly `INJECT_IDLE` | **Injects nothing; run proceeds** (a reasoned `inject_skipped` event is recorded) |
| Exits 0, stdout is bare-empty | **Aborts, loud** |
| Anything else | **Injects your stdout verbatim** as a turn |

Therefore, as contract:

- **Exit non-zero on any failed read.** A half-read state file injected as a turn
  is a silent fallback wearing your tool's name.
- **Errors go to stderr, never stdout.** stdout is the injection channel; a stack
  trace on stdout becomes a turn the agent tries to act on.
- **When you have nothing to say, print `INJECT_IDLE`** — exactly that, alone,
  and exit 0. The match is **byte-exact on the whole stripped stdout**, not a
  prefix or regex.
- `INJECT_IDLE` (tool, on stdout) is deliberately distinct from
  `NOTHING_TO_REPORT` (an agent value inside turns). Distinct meanings, distinct
  tokens.

A complete inject tool implementing all four behaviors in a dozen lines of bash:

```bash
#!/usr/bin/env bash
# Contract: stdout is the injection channel; INJECT_IDLE == "nothing to inject";
# exit non-zero on any failed read; errors to stderr, never stdout.
set -euo pipefail
if [ "${STATE_FAIL:-0}" = "1" ]; then
  echo "your-cli: read failure" >&2      # error to stderr
  exit 3                                  # non-zero -> engine aborts, voiced
fi
if [ "${STATE_IDLE:-0}" = "1" ]; then
  echo "INJECT_IDLE"                      # nothing to say, byte-exact sentinel
  exit 0
fi
echo "your-cli: 1 open item (payload injected as a turn)"
```

The automation side declares it (`argv` is the tool + its args):

```yaml
inject:
  - argv: ["your-cli", "state"]
    label: "current state"
```

## 6 · Conventions (yours to keep; the engine cannot enforce them)

**Name prefixes.** Repos: `drumbeat-pack-<source>` (e.g. `drumbeat-pack-mail`) —
provenance is legible from the name. Tools: prefix distinctly enough that
cross-drumpack collisions stay rare *and* you do not shadow a base-PATH binary.
A real lesson: name the tool `<source>-cli`, **not** the bare `<source>`, because
a binary of the bare name already existed on the host — rule 7 would not have
caught that; only you can.

**Completeness contracts.** Every list-returning tool should answer *"is this
everything?"* in the response itself — a paged client returns a `_paging` block
(`pages_fetched`, `complete`, `capped`, `next_link`); a windowed reader returns
`_completeness`; use a tri-state (`"yes"`/`"no"`/`"uncertain"`) where a bare
boolean would lie. A tool that cannot tell its caller whether they saw the whole
world lets a partial view pass for a complete one.

**What belongs in the card:** the verbs at a glance; invocation shapes for
anything non-obvious; **quirks `--help` cannot express** (a filter that silently
uses a different field, a default window applied when you pass nothing);
**negative space** — what the tool deliberately will *not* do, and why (this
stops an agent inventing a workaround for a boundary you drew on purpose); the
completeness convention your tools implement; and anything beyond the base PATH
they assume. **Do not put consumer policy in the card** — "record what you
surface in the ledger" is the consumer's guidance, not your tool's docs.

**Credentials are drumpack-private.** Login, refresh, and the token store belong
to your drumpack. The engine never sees, stores, or proxies a credential.

## 7 · Wire it into a workspace (`drumpacks.txt`)

The consumer lists drumpack directories in `drumpacks.txt` at the workspace
root — one path per line, relative to the workspace or absolute, `#` comments and
blank lines ignored:

```
drumpacks/ledger          # a private, in-repo drumpack
../drumbeat-pack-mail      # a sibling checkout
```

Order is for deterministic **reporting** only; it never decides tool resolution
(a duplicate tool name refuses to load rather than letting position pick a
winner). Then name the tool in an automation's `requires:`, and the engine puts
the drumpack's `bin/` on the turn PATH and injects its card into every automation
that requires one of its tools. **The card is structurally inseparable from the
binary** — a tool on PATH with no documentation attached is exactly what the
format prevents.

## 8 · Exemplar guidance and automations

`guidance/` and `automations/` in a drumpack are **exemplars, and they are
documentation** — the consumer copies them into its own workspace; ownership
transfers at copy. Two obligations follow: (1) an exemplar automation should run
**as-is** after a copy — if it `requires:` a guidance file or `inject:` tool your
drumpack does not ship, a copier gets an automation that fails its own gate on
first use; (2) if your exemplars encode a real person's real policy, **scrub
before publish** and say so in your README where a skimmer will see it.

## Complete worked example (a one-tool drumpack)

```
drumbeat-pack-example/
  drumpack.md
  bin/example-state        # chmod +x; #!/usr/bin/env bash; --help; INJECT_IDLE rules
```

```yaml
# drumpack.md frontmatter
---
pack_format: 1
name: example
description: A minimal one-tool drumpack that injects a state snapshot each run.
tools:
  - example-state
activity:
  state: "Checking example state…"
---

# example

`example-state`: prints a one-line state snapshot on stdout, or the byte-exact
`INJECT_IDLE` sentinel when there is nothing to inject. Exits non-zero on a
failed read; errors go to stderr. Assumes only the pinned base PATH. Read-only:
it will not mutate any source.
```

Then, in `drumpacks.txt`: `../drumbeat-pack-example`, and in an automation:
`requires: [example-state]` (or an `inject:` entry with
`argv: ["example-state"]`).
