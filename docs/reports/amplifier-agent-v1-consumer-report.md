> Published copy of the consumer report handed to the amplifier-agent developers on 2026-09-07.
> Companion upstream PR: microsoft/amplifier-agent #167 (tool-result ceiling proposal); see also #161.

# amplifier-agent `v1` — consumer report from drumbeat/Cortex

**Branch under test:** `v1` @ `412cc17` (2026-09-06). **Consumer:** drumbeat, the automation engine that runs
Cortex (~290 automation turns/day, 16 automations, amplifier-agent embedded as a library since day one).
**Method:** read-only API map of the tree; hands-on run in an isolated venv with a PATH that has no
`amplifier` on it; a fresh-container proof (Digital Twin, never had Amplifier installed); drumbeat's own suite
against v1. Every claim below has a verbatim command + output in the raw findings log (available on request; 1,118 lines of verbatim commands/output) (1,118 lines) or
the clean-container proof (scripts + logs, available on request) (scripts, .out files, install.log, TIMINGS.md). Model used: `gpt-5.6-luna`; never `sol`.

## 1 · Headline

**The lib-first promise holds.** On a clean machine: `uv pip install "amplifier-agent @ git+…@v1#subdirectory=packages/python"`
took **5.9 s** (100 packages; engine 1.0.0a1 pulled as an in-process dependency, not a subprocess or
service). `command -v amplifier` → not found before and after. The README quickstart ran **verbatim**
(state=success, "OK", 2,457 in / 5 out, $0.000056). Bash tool, skills (`AgentOptions.skills=[dir]` →
`load_skill` appears, `$ARGUMENTS` interpolated), durable sessions + resume-by-id in a new process, clean
no-credential error (`engine_unavailable` with the exact remedy) — all pass. The library created only
`~/.amplifier-agent/workspaces/default/{locks/,sessions.sqlite3}`. No `~/.amplifier`, no keys.env, no bundle
cache, no `uv pip install` at runtime. The only `~/.amplifier` reads left are for `github-copilot` /
`openai-chatgpt` OAuth caches (`engine/_engine/provider_connections.py:52,56`).

**But no existing consumer can move to it as-is.** `v1` deletes the whole `amplifier_agent_lib` /
`amplifier_agent_cli` / `amplifier_agent_home` namespace with no shim, no CHANGELOG, no migration doc.
Every one of drumbeat's six imports is `ModuleNotFoundError`. drumbeat's suite still reports 710/711 green
against v1 because it mocks the boundary — the one honest failure is its engine-library preflight. That is a
warning about consumer test suites as much as about v1.

## 2 · What we need from v1 that is missing (blocking for drumbeat)

| # | Need | Today on `main` | On `v1` | Ask |
|---|---|---|---|---|
| M1 | **Reasoning effort as a first-class knob** | `provider.config.reasoning_effort` | Only via config **file** `extra_request_params.openai.reasoning.effort` (verified on the wire, T1b). No `AgentOptions` field, no per-turn override. | `AgentOptions.reasoning_effort` / `TurnInput.reasoning_effort`, validated `minimal|low|medium|high|xhigh`. We select effort per automation from eval results; a process-global file cannot express that. |
| M2 | **Credential from code** | host config | Env var only. `AgentOptions` has no field (bare `TypeError`); the config file refuses `api_key` with remedy `"Use provider."` (D3). | `AgentOptions.credentials={provider: key}` or a `credential_source` callable. Hosts pulling secrets from a vault must not mutate `os.environ` in a multi-tenant process. |
| M3 | **Provider error cause** | provider error text | `provider_failed` for bad key, bad model, rate limit alike; the real `invalid_api_key` is only in an INFO log from an internal module (D2). | Put `provider_code`, `provider_message`, `http_status` in `AgentError.details`; make `remedy` specific. `docs/concepts/errors.md` promises "always actionable". |
| M4 | **Context-pressure signal** | transcript.jsonl bytes drive our rotation | No compaction by design (`adapters.py:91`), `tokens_in` grew 2,446 → 11,242 → 20,046 over three 40 KB turns, no event/field says "you are at N% of the window", no doc says compaction is the host's job (D7). | Either a `context_pressure` field on `usage` events, or a documented `Session.history` token estimate + a one-paragraph doc: "the host rotates; here is how to know when". |
| M5 | **Raw request/response capture** | `debug.rawLlmPayloads` | Gone. `turn.events()` gives deltas, not wire payloads. | A debug option that writes the exact provider request/response (redacted keys) per turn to a path the host names. We used this last week to find a JSON-mode bug; without it the only forensic path is monkey-patching the provider module. |
| M6 | **Migration path** | — | Nothing on the branch mentions `amplifier_agent_lib` (D1). | `docs/migration.md` + a stub `amplifier_agent_lib/__init__.py` raising `ImportError("renamed to amplifier_agent in v1; see docs/migration.md")`. Also fix D9: the documented install line conflicts with an existing unpinned `amplifier-agent @ git+…` requirement — say so, and show the exact replacement line. |
| M7 | **Tool-result size ceiling + input-size refusal handling** | host truncated | A 46 MB tool result entered a v1 session; every later turn re-sent it; OpenAI refused (`string_above_max_length`); v1 reported only `provider_failed`. No compaction, no ceiling, no signal — the session was dead until the host rotated it by hand. | Cap tool results before they enter history (truncate + note), and surface input-size refusals as a distinct error code so hosts can rotate. Observed in production 2026-09-07. |

### M7 addendum — production data (2026-09-07)
Cortex ran 11.5 h on v1: **225.7 M prompt tokens across 209 runs**, 98 host-side size rotations (per-run prompts
0.34–1.15 M tokens; one pinned chat session reached 1.81 M and failed every 15 min). The same automations on the
previous library ran on ~$3–5/day. Cause: the built-in `bash` tool returns unbounded output and nothing compacts.
We tried to fix it host-side and hit a wall: **v1 refuses a caller-supplied tool named `bash` ("Duplicate tool
name")**, and there is no way to disable or wrap a built-in, so a host ceiling can only cover its own tools —
the model may still call the built-in and blow the context. Concrete asks, either of which unblocks us:
(a) `AgentOptions` accepts a per-tool-result byte ceiling (truncate + note) applied to built-ins and MCP tools;
(b) a caller tool may replace a built-in of the same name, or built-ins can be disabled by name.
Until one lands we rolled Cortex back to 0.17 (evidence: microsoft/amplifier-drumbeat PR #13 and its DTU proof run 20260907T155544Z; proposal: microsoft/amplifier-agent PR #167).

## 3 · Compat map for drumbeat's actual usage (from the tree, cited)

| drumbeat (file:line) | v1 | verdict |
|---|---|---|
| `amplifier_agent_lib.engine.Engine` (agent_worker.py:216) | `create_agent(AgentOptions)` → `Agent`; `Session.run(TurnInput)` | changed shape |
| `_runtime.make_turn_handler` (:214) | — | missing (internal) |
| `bundle.cache.load_and_prepare_cached` (:215, :204) | — | missing; bundles "not configurable" (`docs/configuration.md`) — skills + tools + MCP replace them |
| `protocol_points.defaults_http.HttpAutoApprovalSystem` (:217) | `AgentOptions(approvals="allow" \| ApprovalHandler)` | changed shape |
| `protocol.PROTOCOL_VERSION` / capabilities (:213, :269–276) | `contract_versions` | changed; no negotiation |
| `amplifier_agent_cli.provider_sources` enumerate/inject (:218, :236–244) | — | missing; provider is an id string + env credential; "credentials do not select a provider" |
| `amplifier_agent_home` / `$AMPLIFIER_AGENT_HOME` (paths.py:22) | `AgentOptions.storage` / `AMPLIFIER_AGENT_STORAGE` | renamed |
| per-session dir + `transcript.jsonl` (runner.py:525–534, session_health.py:553–561) | one SQLite row per session, layout declared private | missing — see M4 |
| `rmtree(session dir)` for a fresh turn (:229–232) | `agent.delete_session(id)` | equivalent, now an API |
| NDJSON display events (:64–115, 261) | `turn.events()` async iterator; `inputTokens`→`tokens_in`, cost str → `Decimal` map | changed shape |
| `result["tokensIn"/"tokensOut"/"costUsd"]` (:285–291) | `TurnResult.usage.entries[].tokens_in / .cost["USD"]` per model | changed shape |
| `provider.config.default_model` (agent_config.py:371) | `AgentOptions.model` (a ceiling; refinable down per session/turn) | equivalent |
| `provider.module` path | provider **id** | changed |
| `mcp` / `skills` in host config (agent_config.py:106) | `AgentOptions.mcp_servers` / `.skills` | code, not file |

Also relevant: v1 refuses a second turn on a busy session (`busy`) and a durable id with a live handle
(`session_in_use`); drumbeat's one-process-per-turn model needs testing against that. Session ids must match
`[a-z0-9][a-z0-9-]{7,63}`.

## 4 · Smaller defects (all reproduced; details in FINDINGS-raw.md §Defects)

- **D4** an unadvertised `amplifier-core` console script lands in the consumer's `bin/` — README says "no command".
- **D5** `amplifier_module_provider_openai` logs ~8 INFO records per turn (roles, tool counts, request shape); no documented logger name to silence.
- **D6** that log says `effort=None` on a request whose body carries `'reasoning': {'effort': 'low'}` — the log contradicts the wire.
- **D8** every turn emits three `usage` events: one all-`None`, then two byte-identical populated ones.
- **DTU step 6** skills run: the model probed `read_file` on a missing path → default `tool_error_policy="stop"` ended the turn as `failure`. Documented behavior, but the default turns a harmless probe into a failed turn; consider `continue` as default or a doc callout in the skills guide.

## 5 · What we will do on our side

- drumbeat gets an `amplifier_agent` (v1) worker behind the same `agent_config` contract: provider id + model +
  effort (via the file knob until M1 lands), skills from the bundle's `skills/`, approvals `allow`, sessions
  by id with `delete_session` for fresh turns, usage from `TurnResult.usage`. Rotation moves from transcript
  bytes to a token estimate from `Session.history` until M4 lands. Raw-payload capture (M5) is the one thing
  we cannot replace host-side.
- We will not switch Cortex production until M1 (effort per automation) and M4 (a rotation signal) exist in
  some form; both are load-bearing for cost and for the ContextLengthError class we just eliminated.

## 6 · Evidence index

- the raw findings log (available on request; 1,118 lines of verbatim commands/output) — T1–T7 with commands, exit codes, verbatim excerpts, timings; 9 defects with repro.
- the clean-container proof (scripts + logs, available on request) — clean-container proof: `aa-v1-clean-host.yaml`, `proof.sh`, `install.log` (5.9 s),
  `quickstart|tools|skills|negative.{py,out}`, `env-before/after.txt`, `TIMINGS.md`. DTU destroyed after capture.
- Compat map source: `git -C amplifier-agent show origin/v1:<path>` over `packages/python/src/amplifier_agent/{__init__,_records,_ports}.py`, `docs/{install,configuration,providers,concepts/*}.md`, `contracts/*.v1.md`, `conformance/`.
