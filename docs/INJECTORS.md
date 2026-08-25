# Turn-context injectors

An **injector** is an owner-declared command whose stdout drumbeat prepends to a
turn as a labeled preamble block. It is how you fold standing context the turn's
own text does not carry — working state, a recall of durable facts, a summary of
what is open right now — in front of the brain, without drumbeat ever knowing
what that context is.

This is pure mechanism. drumbeat names no service, embeds no vendor, and
hardcodes no command. You declare injectors in a workspace file; drumbeat runs
them and injects their output. It is the turn-level sibling of an automation's
[`inject:`](ARCHITECTURE.md#6-inject--the-only-aperture-for-consumer-state)
aperture, and honors the same hybrid-sentinel contract.

## The file

Injectors live in `injectors.yaml` at your workspace root (beside `drumpacks.txt`).
The file is **optional**: with no file, no injectors run and every turn is
unchanged. Copy [`examples/injectors.yaml`](../examples/injectors.yaml) to start.

```yaml
injectors:
  - label: "Working context"
    argv: ["context-tool", "--summarize"]
    apply_to: ["typing", "chat"]

  - label: "Open items"
    argv: ["items-cli", "list", "--for-turn"]
    apply_to: ["chat"]
```

Each entry has exactly three fields, all required:

| Field | Meaning |
|---|---|
| `argv` | A non-empty list of non-empty strings — the command and its arguments, run directly (no shell). |
| `label` | A non-empty string. Heads the injected block, and names the injector in any refusal. |
| `apply_to` | A non-empty list of **profile names** this injector runs for — the names you defined in `agent-config.yaml` `profiles:`, matched against a turn's requested `profile`. A turn whose profile is not listed (or that names no profile) never runs it. Open vocabulary — there is no fixed set of interaction modes. |

Unknown keys or a missing field are a **loud refusal** — the engine names the
file and the problem rather than run a silently misconfigured turn. The file is
re-read every turn (no cache), so an edit takes effect on the next turn.

## When and where they run

For each turn, every injector whose `apply_to` includes the turn's profile is
run. Its rendered block is prepended to the turn text **in file order** (top to
bottom); the now-context line and the person's own message stay last, most
salient. Blocks are fenced with their label:

```
--- Working context ---
<the command's stdout>

--- Open items ---
<the command's stdout>

<now-context and the person's message>
```

Injectors ride the turn that answers the person — a reply, or a chat message.
They do **not** ride a brand-new chat session's one-time identity/requirements
setup turns: those run once, ever, and are not seeded from the person's message.

An injector runs with the **turn environment**: the same constructed PATH the
agent's own tool calls get (workspace `bin/` and pack bins prepended), and
`DRUMBEAT_DATA_DIR` pointing at the data dir. So an injector command resolves
exactly like a pack tool, and finds engine state without depending on the
current directory.

## The contract (fail loud)

An injector's result is classified in a **fixed order**:

| Result | Behavior |
|---|---|
| The binary cannot be spawned | **Refuse the turn**, loud |
| Times out (60s) | **Refuse the turn**, loud |
| Exits non-zero | **Refuse the turn**, loud (stderr tail named) |
| Exits 0, stdout (whole, stripped) is byte-exactly `INJECT_IDLE` | **Inject nothing; the turn proceeds** |
| Exits 0, stdout is bare-empty | **Refuse the turn**, loud |
| Anything else | **Inject stdout** as this turn's labeled block |

A configured injector that fails never runs the turn with a silently smaller
context — the turn is recorded failed with the reason. An injector with nothing
to add **this** turn must print `INJECT_IDLE` (byte-exact, the whole of stdout),
not exit empty: **silence is never a contract value** — a crashed pipe and a
genuinely idle source must not share an observable. `INJECT_IDLE` is the same
sentinel an automation `inject:` tool uses, so a single tool can honor both.

## Writing an injector command

The rules are contract, not style — the same ones an `inject:` tool follows:

- **Errors go to stderr only.** Never print an error to stdout; stdout is the
  block. On a failed read, exit non-zero.
- **Print `INJECT_IDLE` when you have nothing to say** — the exact string, as
  the whole of stdout — rather than exiting 0 with empty output.
- **Find state via `DRUMBEAT_DATA_DIR`, not the current directory.** The turn
  env carries it; a cwd-relative default reads false-empty the moment the
  workspace and data dir diverge.
