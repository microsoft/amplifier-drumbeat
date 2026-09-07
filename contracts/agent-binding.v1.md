# Agent Binding Contract — v1 (DRAFT — held un-frozen while the amplifier-agent `v1` line is itself pre-release; implementation passes and the freeze bar is met in-repo)

## Who builds against this

- The drumbeat engine itself — `src/drumbeat/agent_worker.py` (the ONLY module that
  imports the agent library), `src/drumbeat/agent_config.py` (the only module that
  materializes what the library reads), `src/drumbeat/runner.py` (the only module that
  spawns a worker).
- Automation authors, indirectly: the `agent_config:` block in
  `contracts/automation-file.v1.md` is projected onto this seam and nowhere else.
- Anyone porting drumbeat onto a different agent library: this file is the complete
  list of what drumbeat needs from one.

## Purpose

drumbeat embeds `amplifier-agent` as a **library**. There is no agent CLI, no argv
contract, no stream parsing, and no subprocess other than drumbeat's own per-turn
worker. This contract freezes the seam between the two: what drumbeat hands the
library, what it reads back, and which of those are closed vocabularies.

Everything here is measured against `amplifier-agent` `1.0.0a1`
(`contract_versions == ("agent-interface/1", "turn-events/1", "language-binding/1",
"host-config/1")`).

## Core (the frozen part)

### 1. One import surface, one importer

`amplifier_agent` is the **only** module of the agent library drumbeat imports, and
`drumbeat.agent_worker` is the **only** drumbeat module that imports it. Every other
module — including the long-lived `serve`/scheduler process — stays free of it, so the
library's process-global state can never outlive a turn.

Conformance: `python -c "import drumbeat.agent_worker"` in an environment with no
agent CLI on `PATH` succeeds, and a repository-wide grep for any of the retired
module names (the `_lib`, `_cli` and `_home` namespaces the previous library
shipped) returns zero lines outside `CHANGELOG.md`, which is the only place the
move is narrated.

### 2. One agent, one session, one turn, per OS process

A turn is one `python -m drumbeat.agent_worker` process. Inside it, exactly one
`create_agent(AgentOptions(...))`, exactly one session handle, exactly one turn:

```
create_agent(AgentOptions(
    provider   = <provider id>,          # §3
    model      = <resolved default_model>,
    skills     = [<skills dirs>],        # §5
    mcp_servers= [McpServer(...)],       # §5
    storage    = <session storage root>, # §6
    approvals  = "allow",                # §7
    tool_error_policy = "continue",      # §7
))
```

Then `create_session(SessionOptions(session_id=…, persistence="durable"))` for a fresh
turn, or `resume_session(session_id)` for a continuing one, and
`session.start_turn(TurnInput(content=[TextPart(prompt)]))`.

drumbeat registers **no caller tools**. The agent's built-in tool surface plus the
workspace's pack-augmented `PATH` (`docs/ARCHITECTURE.md` §4) is the whole capability
surface, so there is no drumbeat-side tool vocabulary to keep in sync.

### 3. `provider.module` is a provider **id**

The `agent_config:` key `provider.module` carries a provider **id** —
`anthropic | openai | azure-openai | gemini | ollama | vllm | github-copilot | …` — and
is passed as `AgentOptions.provider` unchanged. It is not a dotted module path, not a
filesystem path, and drumbeat never enumerates, injects, or mounts a provider: the
library ships every provider in-process.

`provider` is closed to `module | config`, and `provider.config` to
`default_model | model_class | reasoning_effort` — exactly what §4 projects onto the
library. A fourth key there would validate, be materialized, and be read by nothing,
so it is refused at load naming the vocabulary.

Credentials **and endpoints** are environment-only, never config. drumbeat refuses a
credential-bearing key at any depth of any config layer
(`agent_config._CREDENTIAL_KEYS`) and the library has no field to accept one; an
endpoint (`base_url`) is refused by the closed vocabulary above, because an author
who writes one has every reason to believe the turn is pointed somewhere it is not.

A provider whose credential is absent fails at `create_agent` with the library's own
`AgentError(code="engine_unavailable")`. That is a **run failure** (§8), reported with
the library's `message` and `remedy` **verbatim** — never re-worded, never downgraded
to a successful run carrying an apologetic reply.

### 4. Reasoning effort is materialized into a per-turn config file

`AgentOptions` has no reasoning field, and the library reads `extra_request_params`
from a **config file only** (`docs/configuration.md`: "`extra_request_params` has no
environment form. It is settings-only.").

So drumbeat materializes one JSON file per turn and points the worker subprocess at it
with `AMPLIFIER_AGENT_CONFIG`:

```json
{
  "provider": "openai",
  "model": "gpt-5.6-luna",
  "extra_request_params": { "openai": { "reasoning": { "effort": "low" } } }
}
```

- The file's top-level vocabulary is CLOSED to the library's five documented keys
  (`provider`, `model`, `storage`, `workspace`, `extra_request_params`). drumbeat
  writes at most `provider`, `model`, and `extra_request_params`; `storage` is passed
  in code so a relative path can never be re-anchored by the child's working
  directory, and `workspace` is never written (§6).
- `reasoning.effort` is written **only** when the automation declares
  `provider.config.reasoning_effort`, and only under the resolved provider id — the
  library scopes `extra_request_params` per provider, so writing it under the wrong id
  would validate and do nothing.
- Effort with no provider id is refused at load, naming the remedy. A process-global
  file cannot express per-automation effort; the per-turn env var is what preserves it.
- The variable is set **on the child only**. An operator's own
  `$AMPLIFIER_AGENT_CONFIG` is still layer 1 of drumbeat's own merge
  (`agent_config`), so setting it changes turns rather than being silently overridden.

### 5. Skills and MCP servers are code, not file

`agent_config`'s `skills:` and `mcp:` blocks are projected onto `AgentOptions.skills`
and `AgentOptions.mcp_servers`. They never appear in the materialized file — the
library's host-config vocabulary has no key for either, so a file entry would be
refused by name.

- `skills:` is a list of directory paths (each containing skill subdirectories).
  Relative paths resolve against the turn's `cwd`.
- `mcp:` is a mapping of server name → `{transport, command?, args?, env?, url?,
  headers?}`, projected to `McpServer`. An unknown key inside a server entry is
  refused at load, naming it.

### 6. Sessions: one storage root per pinned session

- **Identity.** A v1 session id must match `[a-z0-9][a-z0-9-]{7,63}`. drumbeat's own
  session ids do not (they carry uppercase and `T`/`Z` stamps), so every id crossing
  this seam is translated by `paths.agent_session_id()`: an id already matching the
  pattern is used **verbatim**; any other is lowercased, non-conforming characters
  become `-`, and a `-<sha256[:8] of the original>` suffix is appended so two distinct
  drumbeat ids can never collapse onto one agent id. The translation is pure and
  deterministic — the same drumbeat id always yields the same agent id.
- **Storage.** Each pinned session gets its own root,
  `<runs_dir>/agent-storage/<agent-session-id>/`, passed as `AgentOptions.storage`.
  drumbeat owns that path; it never reads inside it (the library declares the layout
  non-contractual).
- **`workspace` is never set.** The library's `workspace` key must match
  `[a-z0-9][a-z0-9-]{0,63}`, which drumbeat's cwd-derived slug does not; separation is
  achieved by the per-session storage root instead.
- **Existence.** The probe (`runner._probe_session`) stats that root and nothing
  inside it: present ⇒ `EXISTS`, absent ⇒ `MISSING`, `OSError` ⇒ `UNKNOWN`. Because the
  root lives inside the workspace, a renamed or moved project carries its sessions with
  it and orphans nothing.
- **A fresh turn** removes the session's storage root before the turn, so a fresh turn
  can never resume a stale conversation.
- **Concurrency.** The library refuses a second turn on a live session (`busy`) and a
  durable id that already has a live handle (`session_in_use`). drumbeat's own
  per-session advisory lock (`runner._session_lock`) is what keeps two OS processes off
  one session; either library refusal reaching a turn is reported as an ordinary run
  failure naming the code.

### 7. Approvals and tool errors

- `approvals="allow"`. drumbeat runs unattended; there is no human to ask, and the
  library fails a turn loudly (`approval_unavailable`) rather than silently proceeding
  when no policy is set.
- `tool_error_policy="continue"`. Measured on the `v1` evaluation (evidence:
  `evidence/aa-v1-eval/dtu-proof/`, step 6): under the library default `"stop"`, a model
  probing `read_file` on a path that does not exist ended an otherwise-healthy turn as
  `failure`. For an unattended engine that is a false failure with no defect behind it.
  A tool error still reaches the run as a `tool_result` event with `outcome=failed`.

### 8. What a turn reports back, and when it is a failure

The worker consumes `turn.events()` and forwards every event to the parent as one
NDJSON line, then emits exactly one terminal envelope. Field names on the wire follow
the library: `tokens_in`, `tokens_out`, `cache_read_tokens`, `cache_write_tokens`, and
`cost` as a `Decimal` map keyed by ISO 4217 — recorded as `cost_usd`, a decimal
**string**, so monetary precision survives persistence.

Usage is read from the terminal event's `usage` snapshot, summed across
`Usage.entries`. A counter the library did not report stays `None` — honestly absent,
never a fabricated `0`.

A turn is a **failure** when any of these hold:

1. the terminal state is not `success`;
2. `create_agent` or session setup raised `AgentError` (including
   `engine_unavailable`);
3. the worker produced no terminal envelope;
4. the turn's own stderr carried a session-init module-load failure
   (`module_failures` non-empty);
5. the reply is a provider-unavailability sentinel.

(4) and (5) are drumbeat's, not the library's, and they are **new in this contract**.
Both describe a turn that exits zero while having no working brain. Measured on the
originating deployment: 34 runs between 20:30Z and 23:44Z recorded
`"failed": false, "error": null` while their reply was the engine reporting it had no
provider. A run that did nothing must never read as a run that succeeded
(`docs/VISION.md` §4).

`module_failures` is still recorded verbatim on the run record for visibility; what
changed is that it now also fails the run.

### 9. Context pressure is measured in tokens

The library does not compact and exposes no context-pressure signal
(evidence: `evidence/aa-v1-eval/FINDINGS-raw.md` T6/D7). drumbeat's pre-emptive session
rotation therefore measures **prompt tokens** — the unit the provider actually refuses
on — taken from the most recent run record for that session (`steps[].tokens_in`, which
is the library's own reported count). The gate is
`$DRUMBEAT_SESSION_ROTATE_TOKENS`, default `150000`.

The rotation *contract* is unchanged (`docs/ARCHITECTURE.md` §5, Trigger 3): a pinned
session over the gate is rotated before the run's first turn, through the same single
path every other trigger uses, and always lands in `runs/session_rotations.jsonl`. Only
the measurement changed, and it changed to a **stronger** one: the byte-era proxy varied
3.4× in bytes-per-token between two measured sessions, while this is the count the
provider itself returned.

## Known gap (recorded, not worked around)

**Raw request/response capture has no v1 equivalent.** The library exposes turn events,
not wire payloads. There is no drumbeat-side substitute short of monkey-patching a
provider module, which this engine will not do. The upstream ask is recorded in
`evidence/aa-v1-eval/REPORT-for-amplifier-agent-devs.md` §2 (M5). Until it lands, a
provider-level forensic question cannot be answered from a run's artifacts.

## Conformance

- `tests/test_agent_worker.py` — the worker against a fake `amplifier_agent`: options
  assembly, fresh-vs-resume, event forwarding, usage extraction, `AgentError` →
  failure with the remedy verbatim.
- `tests/test_agent_config.py` / `tests/test_agent_config_run.py` — the materialized
  file's exact shape, including `extra_request_params.<provider>.reasoning.effort`.
- `tests/test_session_pins.py` — `agent_session_id()` translation, verbatim
  pass-through, collision resistance.
- `tests/test_preemptive_size_rotation.py` — the token gate.
- `tests/test_soft_launch_gates.py` — the preflight imports `amplifier_agent` for real.
- The clean-container proof: a stock container with no Amplifier anywhere installs
  drumbeat, runs one automation end to end on a real provider, and destroys.

## Changelog

- **2026-09-06** — v1 drafted. drumbeat adopts `amplifier-agent` `v1`
  (`amplifier_agent`, dist `amplifier-agent 1.0.0a1`) as its only agent library. Clean
  cut, no shim, no dual-read: the previous embedding surface is deleted rather than
  deprecated, and the migration is narrated in `CHANGELOG.md` alone. Evidence that
  promoted this out of "backlogged": a hands-on library evaluation on a clean host and
  in a fresh container (`evidence/aa-v1-eval/`) — the lib-first promise holds
  (install 5.9 s, no CLI, no `~/.amplifier`, real completions, durable resume across
  processes, clean typed no-credential error), and every one of drumbeat's previous six
  imports is a `ModuleNotFoundError` under `v1`, so there was no partial move to make.
  Sections 3, 4, 6, 8 and 9 record the four seams that genuinely changed shape:
  provider id, effort via a materialized file, per-session storage roots with translated
  ids, and a token-measured rotation gate. Section 8 also closes a defect this port
  found: a turn with no working brain used to be recorded as a success.
