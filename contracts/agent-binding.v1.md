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

drumbeat registers exactly **one caller tool**, `run_command` (§10) — a shell tool whose
result is bounded before it enters the conversation. Everything else the model can reach
is the agent's built-in tool surface plus the workspace's pack-augmented `PATH`
(`docs/ARCHITECTURE.md` §4), so the drumbeat-side tool vocabulary is one name long.

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
never a fabricated `0`. Each entry also names the provider and model that ACTUALLY
served the turn, and the run record carries both per step (`provider`, `model`) —
not the ones drumbeat asked for. A model ceiling can be refined down per turn, so
"what was configured" and "what answered" are different facts and the record owes
the second one.

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

### 10. A tool result is bounded before it enters the conversation

A tool result is not a transient display artifact: it is appended to the session
transcript and **re-sent, in full, on every subsequent turn**. One oversized result
therefore poisons the session permanently. Measured on the originating deployment
(2026-09-07): a single m365 tool result of **46,464,072 bytes** entered `teams-check`'s
pinned session, after which OpenAI refused every later request
(`invalid_request_error` / `string_above_max_length: input[37].output > 10,485,760`).
The identical run failed again 4 hours later on the same pinned session. Manual
`drumbeat rotate-session` was the only remedy.

The library does not bound this for us. Measured on `amplifier-agent 1.0.0a1`, the
built-in `bash` tool **deliberately disables truncation** — its `CapturedBash`
subclass overrides `_truncate_output` to return the output unchanged
(`packages/engine/src/amplifier_agent_engine/_engine/builtin_tools.py`) — and the
library never compacts (§9).

So drumbeat bounds every result it owns:

- **Ceiling.** `$DRUMBEAT_TOOL_RESULT_CEILING_BYTES`, default **262144** (256 KiB),
  overridable per automation by `agent_config.tool_result_ceiling_bytes`
  (`contracts/automation-file.v1.md`). A non-positive value is refused at load; `0`
  is not "unlimited" and there is no unlimited.
- **Truncation is by BYTES, on a UTF-8 character boundary.** The first `N` bytes are
  kept; a partial multi-byte sequence at the cut is dropped rather than emitted
  broken.
- **The note is mandatory and has exactly this shape**, appended after the kept bytes:

  ```
  \n[drumbeat: output truncated -- kept <kept> of <total> bytes; full output: <path>]
  ```

  A truncated result that did not say so would be a lie the model then reasons from.
- **The full output lands in the run directory**, at
  `<run dir>/tool-output/<call-id>.txt`, written before the truncated result is
  returned. The `call_id` is the library's own correlation id from `ToolContext`, so
  the transcript's note and the file on disk name the same call. If the write fails,
  the note says so (`full output: unavailable -- <reason>`) and the result is still
  truncated: an unwritable run directory must never be the reason a 46 MB result
  reaches the provider.
- **Under the ceiling, nothing changes.** A result at or below the ceiling is returned
  byte-identical, with no note and no file written.

**Which results this covers, measured — not assumed.** The ceiling reaches exactly the
tool results drumbeat's own process produces, and `v1` gives it no way to reach the
others:

| source | executor | bounded by this contract |
| --- | --- | --- |
| `caller` (`run_command`) | drumbeat's own handler | **yes** |
| `built-in` (`bash`, `read_file`, …) | the library, in-process | **no — see below** |
| `mcp` | a third process the library connects | **no — see below** |

A caller tool named `bash` **cannot** shadow or replace the built-in. Measured
directly against `1.0.0a1`:

```
AgentError(code='invalid_input',
           message='Duplicate tool name: bash.',
           remedy='Give caller and MCP tools names distinct from the built-in tools.')
```

`prepare_tools` registers caller tools first and then the built-ins into the same flat
registry, and `ToolRegistry.add` refuses a duplicate name at construction — so the
`bash` name is reserved, and the same is true of `read_file`. Nor can the built-in be
disabled: `AgentOptions` is a closed list (`docs/concepts/agents.md`: "That list is
closed") with no tool-filtering field, `docs/configuration.md` closes the host-config
file to five keys, and the engine's `allowed_tools` filter is delegation-internal and
not caller-settable. An approvals handler cannot stand in for one either — a `deny`
is **terminal** (`approval_denied`, `docs/concepts/approvals.md`), so denying a
built-in `bash` call kills the turn rather than steering it.

drumbeat therefore does the two things it actually can, and claims nothing more:

1. registers `run_command` — same contract as the built-in `bash` (one `command`
   string, optional `timeout` 1–120 s defaulting to 30, run under the turn's own cwd
   and pack-augmented environment) with the ceiling applied to its result;
2. states the preference in `AgentOptions.instructions`, verbatim: shell work goes
   through `run_command`, because `bash` results are unbounded.

(2) is **advisory and known to be advisory**. A model that calls the built-in `bash`
anyway, or an MCP server that returns 46 MB, is still unbounded — which is precisely
why §11 exists and is not optional. The upstream ask (a caller-supplied ceiling, or
replaceable built-ins) is recorded under "Known gap".

Measured once, on a real provider in a clean container, with a prompt naming no tool
("Run this shell command and tell me how many characters it printed"): the model chose
`run_command` unprompted, the 20 MB result was bounded, the run succeeded at 5,309
prompt tokens, and it still answered correctly — reading the count off the truncation
note's own byte figures, which is why the note carries them. **One observation is not
a guarantee**, and nothing here is entitled to claim one; it is evidence the steering
is worth having, not evidence it can be relied on.

### 11. A provider input-size refusal rotates the pinned session

§9's pre-emptive token gate reads `steps[].tokens_in` from the previous run. A run the
provider **refuses outright records no usage at all**, so the gate never sees the
growth — measured: `teams-check` failed on the same pinned session twice, four hours
apart, and the gate did not fire either time. A refusal is therefore its own rotation
trigger, on the same single path §9 and Trigger 1 use.

A run's captured stderr matches an input-size refusal when it carries any of:

```
context_length_exceeded          string_above_max_length          request_too_large
prompt is too long: <N> tokens > <M> maximum
```

The first three are provider error codes matched on a strict word boundary; the fourth
is the OpenAI/Anthropic prose form already used by Trigger 1. On a match, and only when
the run failed:

- `run_completed.error_details` names the real cause —
  `{"provider_code": "<the matched code>", "source": "stderr"}`, plus
  `prompt_tokens`/`limit_tokens` when the prose form supplied them. This closes the
  measured gap where the library surfaced only the lossy `provider_failed` while the
  structured code sat in the worker's `stderr.log`. `error_details` is `null` on every
  run that matched nothing — honestly absent, never `{}`.
- The pinned session is rotated through `_auto_rotate`, so the rotation lands in
  `runs/session_rotations.jsonl` **and** as a `session_rotated` engine event whose
  `reason` names the provider code.

The record is written **before** the rotation, so the evidence outlives it.

## Known gap (recorded, not worked around)

**A built-in or MCP tool result cannot be bounded.** §10 records the measurement: the
built-in tool names are reserved (a caller `bash` is refused at construction), there is
no documented way to disable or filter a built-in, and MCP results are read by the
library from a third process. drumbeat can bound only its own caller tools. The
upstream ask is a caller-settable per-result ceiling — or replaceable built-ins — and
until one exists, §11 is the containment: an unbounded result still poisons a session,
but the session now self-heals on the next run instead of failing forever.

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
- `tests/test_tool_ceiling.py` — §10's mechanism: the ceiling applied, the note's exact
  shape, the full-output file, byte-boundary safety, an under-ceiling result returned
  untouched, an unwritable run dir still truncating, and the per-automation override.
- `tests/test_input_size_refusal.py` — §11: each provider code and the prose form
  detected, a non-refusal failure left alone, `error_details` on the run record and the
  `run_completed` event, and the `session_rotated` event naming the code.
- `tests/test_v1_tool_shadowing.py` — the §10 measurement, kept green rather than
  remembered: a caller tool named `bash` is refused at construction by the REAL library
  with `Duplicate tool name`, and a distinctly-named one is accepted.
- `tests/test_soft_launch_gates.py` — the preflight imports `amplifier_agent` for real.
- The clean-container proof: a stock container with no Amplifier anywhere installs
  drumbeat, runs one automation end to end on a real provider, and destroys.
- §10/§11's clean-container proof, on a real provider (evidence:
  `.amplifier/evaluation/drumbeat-m0j/20260907T155544Z/` in the consumer workspace).
  **Positive** (ceiling on, a step emitting 20,000,022 bytes): run succeeded, the
  conversation carried 286,720 bytes of agent storage, the last turn sent
  **5,359 prompt tokens**, the full output sat at
  `<run dir>/tool-output/call_….txt`, and the model read the truncation note back
  verbatim. **Negative** (same automation, ceiling raised above the payload — the
  pre-change engine): the provider refused with `string_above_max_length`, `error`
  carried only the library's misleading "check provider credentials" sentence,
  `error_details` named the real code, and the pin was rotated exactly once.

## Changelog

- **2026-09-07** — Sections **10** and **11** added, and §2 amended: drumbeat now
  registers exactly one caller tool. Evidence that promoted this out of "backlogged":
  a production incident on the `v1` line. A single 46,464,072-byte m365 tool result
  entered `teams-check`'s pinned session; every later turn re-sent it, OpenAI refused
  with `string_above_max_length`, and the identical run failed again on the same pinned
  session four hours later. Nothing in the engine caught it: the library surfaced only
  the lossy `provider_failed` (the structured code was in the worker's `stderr.log`),
  and §9's pre-emptive gate could not fire because a refused turn records no usage to
  measure. Manual `rotate-session` was the only remedy.
  Two changes, and one measured refusal recorded rather than worked around:
  - **§10, the ceiling.** Every tool result drumbeat's own process produces is bounded
    to `$DRUMBEAT_TOOL_RESULT_CEILING_BYTES` (default 262144, per-automation override
    `agent_config.tool_result_ceiling_bytes`) before it enters the conversation, with a
    mandatory truncation note and the full output written to
    `<run dir>/tool-output/<call-id>.txt`.
  - **§10's boundary, measured.** A caller tool named `bash` is REFUSED at construction
    by `1.0.0a1` (`Duplicate tool name: bash.`), there is no documented way to disable
    a built-in, and an approvals `deny` is terminal — so built-in and MCP results stay
    unbounded. Recorded under "Known gap" with the upstream ask, and covered by a test
    against the real library so the claim cannot rot. drumbeat registers `run_command`
    and states the preference in `AgentOptions.instructions`, which is advisory and
    says so.
  - **§11, rotate on refusal.** A provider input-size refusal in a failed run's stderr
    (`context_length_exceeded`, `string_above_max_length`, `request_too_large`, or the
    `prompt is too long: N tokens > M maximum` prose form) now names the provider code
    in `run_completed.error_details` and rotates the pinned session through the same
    single path every other trigger uses. §9's gate is unchanged; this is the backstop
    for the case the gate structurally cannot see.
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
