# Changelog

## Unreleased

### Added

- **A tool result is bounded before it enters the conversation, and a provider
  input-size refusal now rotates the pinned session.** Both halves of the
  2026-09-07 incident, contract-first in
  `contracts/agent-binding.v1.md` sections 10 and 11.

  **What happened.** A single 46,464,072-byte tool result entered
  `teams-check`'s pinned session. A tool result is transcript, not display, so
  every later turn re-sent it in full and OpenAI refused the request
  (`invalid_request_error` / `string_above_max_length: input[37].output >
  10,485,760`). Three things then failed at once: the run reported only the
  library's lossy `provider_failed` while the real code sat unread in the
  worker's `stderr.log`; the pin was never rotated, because
  `detect_ceiling_hit` matched one provider's *prose* form and this arrived as
  a structured *code*; and the pre-emptive token gate could not help, because a
  refused turn records no usage for it to measure. The identical run failed
  again four hours later on the same session. Manual `rotate-session` was the
  only remedy.

  **The ceiling.** Every tool result drumbeat's own process produces is now
  truncated at `$DRUMBEAT_TOOL_RESULT_CEILING_BYTES` (default 262144, per
  automation via the new `agent_config.tool_result_ceiling_bytes` key), with a
  mandatory note naming what was dropped and the file holding the whole thing
  (`<run dir>/tool-output/<call-id>.txt`). Under the ceiling nothing changes.
  An unwritable run directory loses the overflow but still truncates: never
  fail open.

  **What the ceiling does NOT reach, measured rather than assumed.** The agent
  library gives a caller no way to bound a *built-in* or *MCP* tool result. A
  caller tool named `bash` is refused at construction by `1.0.0a1`
  (`AgentError(code='invalid_input', message='Duplicate tool name: bash.')`),
  `AgentOptions` is a closed list with no tool-filtering field, and an
  approvals `deny` is terminal so it cannot steer either. drumbeat therefore
  registers one caller tool, `run_command` — same arguments as the built-in
  `bash`, result bounded — and states the preference in
  `AgentOptions.instructions`. That steering is **advisory and says so**; the
  gap and its upstream ask are recorded in the contract, and
  `tests/test_v1_tool_shadowing.py` drives the real library so the claim cannot
  rot.

  **The backstop.** Because the advisory half can be ignored, a provider
  input-size refusal is now its own rotation trigger. A failed run whose stderr
  carries `context_length_exceeded`, `string_above_max_length`,
  `request_too_large`, or the `prompt is too long: N tokens > M maximum` prose
  form records `run_completed.error_details` naming the provider code, and
  rotates the pin through the same single path every other trigger uses — so a
  poisoned session self-heals on the next run instead of failing forever. The
  §9 token gate is unchanged; this covers the case the gate structurally cannot
  see.

  **Also new on the run record:** `error_details` — the machine-readable cause
  beside `error`'s human sentence. `null`, never `{}`, when nothing was
  established.

### Changed

- **The engine runs on `amplifier-agent` `v1` (`amplifier_agent`) — a clean cut,
  no shim.** This is the migration note; the rest of the repository is written
  as though it has always been this way, which is why this file is the only
  place the move appears.

  **What was replaced.** The previous embedding surface — the
  `amplifier_agent_lib` / `amplifier_agent_cli` / `amplifier_agent_home`
  namespace, bundle preparation and caching, provider enumeration and
  injection, the turn-handler/`Engine`/`boot`/`submit_turn` assembly, and the
  `$AMPLIFIER_AGENT_HOME` session tree with its per-session
  `transcript.jsonl` — is gone. `v1` ships exactly one public module,
  `amplifier_agent`, and every one of drumbeat's six previous imports is a
  `ModuleNotFoundError` against it, so there was no partial move available:
  either the whole surface moves or nothing runs. The seam is now frozen in
  `contracts/agent-binding.v1.md`.

  **Evidence that made the call.** A hands-on evaluation of the library on a
  clean host and in a fresh container (`evidence/aa-v1-eval/` in the consumer
  workspace): install in 5.9 s, no CLI on `PATH` before or after, no
  `~/.amplifier`, no bundle cache, no `uv pip install` at run time, real
  completions with fully populated usage and `Decimal` cost, durable sessions
  resumed in a different process, and a clean typed error when no credential is
  configured.

  **What operators need to know.**
  - The dependency is `amplifier-agent @
    git+https://github.com/microsoft/amplifier-agent@v1#subdirectory=packages/python`.
    A bare `amplifier-agent` requirement does not resolve: the distribution
    lives in a subdirectory, and the repository's default branch builds a
    structurally different package from the same tree.
  - `agent_config:` narrows to `provider | mcp | skills`. `debug:` and
    `providers:` are refused by name, as are unknown keys under `provider` and
    `provider.config` (closed to `default_model | model_class |
    reasoning_effort`). A config carrying any of them fails its automation at
    LOAD, through the ordinary lint path, rather than at run time.
  - `provider.module` carries a provider **id** (`openai`, `anthropic`, …).
  - `$DRUMBEAT_SESSION_ROTATE_BYTES` is replaced by
    `$DRUMBEAT_SESSION_ROTATE_TOKENS` (default 150,000). The pre-emptive
    rotation gate measures prompt tokens, which is the unit a provider actually
    refuses on.
  - Sessions live at `<data-dir>/agent-storage/<session-id>/`, inside the
    workspace. Nothing is read from `~/.amplifier-agent`. The first run of each
    pinned session after this change rotates once, because the recorded
    provider identity changes — which is the correct verdict: the provider
    stack underneath those conversations changed wholesale.

  **What v1 cannot do yet, recorded rather than worked around.** Raw provider
  request/response capture (the retired `debug.rawLlmPayloads`) has no
  equivalent: the library exposes turn events, not wire payloads. There is no
  host-side substitute short of monkey-patching a provider module, which this
  engine will not do. The upstream ask is filed with the library's authors
  (`evidence/aa-v1-eval/REPORT-for-amplifier-agent-devs.md` §2, M5), along with
  asks for reasoning effort as a first-class option (M1) and a
  context-pressure signal (M4).

### Fixed

- **A turn with no working brain was recorded as a success.** Two rules now fail
  such a run, both of them drumbeat's own:
  1. a non-empty `module_failures` (session init dropped a provider/tool/hook and
     the turn ran with a reduced module set) sets `failed: true`, and the error
     names the modules. Measured: 96 of 96 runs in one morning carried a
     module-load failure on stderr while every one of them recorded
     `"failed": false, "error": null`. Visibility that nothing acts on is not
     visibility;
  2. a reply that is itself a statement of provider unavailability fails the run.
     Measured: 34 runs between 20:30Z and 23:44Z recorded `failed: false` while
     their reply was the engine reporting it had no provider. The match is
     anchored, never a substring search, so an automation that legitimately
     quotes the phrase while reporting on its own fleet is not failed.

### Added

- **`model_class:` / `reasoning_effort:` / a model deny-list — pick a model
  TIER, not a model id.** `agent_config`'s `provider.config` gained two
  closed-vocabulary keys. `model_class: fast | standard` is resolved at
  materialization into a concrete `default_model` per `provider.module`
  (anthropic: `claude-haiku-4-5-20251001` / `claude-sonnet-4-6`; openai:
  `gpt-5.6-luna` / `gpt-5.6-terra`) and never reaches the engine as a key —
  model ids rotate, tiers don't, so a fleet-wide model change stops being a
  fleet-wide edit. An explicit `default_model` still wins, the resolved config
  records WHICH decided (`model_source`: `"model_class:fast"` vs
  `"default_model"`), and a shadowed `model_class` produces a warning rather
  than a silent drop. `reasoning_effort: minimal | low | medium | high | xhigh`
  is validated at load so a typo is an authoring-time refusal instead of a
  mid-turn provider error, and is materialized into the host config the turn's
  worker reads (see the `v1` entry above for the exact shape).
  A model deny-list (default `["gpt-5.6-sol"]`) is checked at LOAD against the
  RESOLVED model: a denied automation is refused through the ordinary
  config-lint path (named by `drumbeat doctor` and every scheduler tick) and
  therefore never runs at all. Both tables are overridable per workspace via a
  new registered top-level key in `agent-config.yaml` — `models:` with the
  closed sub-vocabulary `classes | deny` (`classes:` merges per provider module
  and tier; `deny:` replaces wholesale). Contract
  (`contracts/automation-file.v1.md`), docs, example, and skill amended in the
  same change; proven by a RED→GREEN suite
  (`tests/test_model_class_policy.py`, 20 tests).
- **`priority:` — a dispatch tier for automations that come due together.**
  Optional top-level frontmatter key with the closed vocabulary
  `high | normal` (absent = `normal`). Among DUE automations, `high` is
  dispatched first; the sort is stable, so within a tier the prior order is
  untouched and a fleet that never declares the key dispatches in
  byte-identical order to before. Promoted out of "backlogged" by measurement:
  the reference fleet demands ~412 runs/day and completes ~127 (31%), with
  30-minute automations attaining 52–72% of their declared cadence — a schedule
  expression had become a bid in an auction nobody clears, and the auction had
  no priority, so the one notify-capable path to the owner starved on equal
  terms with bulk checks. **Ordering only: it changes who waits, not how much
  gets done.** No concurrency added, no running turn preempted, nothing
  dropped; the capacity fix remains a separate architecture decision. Owner
  precedence is unchanged and still absolute — the owner-priority latch defers
  a due automation of ANY tier whose session the owner is using. Unknown values
  are refused loudly naming the vocabulary.
  `contracts/automation-file.v1.md` amended in the same change (2026-09-02
  changelog entry), with `docs/AUTOMATIONS.md` §2/§2.3, `docs/ARCHITECTURE.md`
  §4, and the authoring skill's key registry.

- Required guidance reaches the agent by **reference**.
  `format_requirements_turn` gained a `mode` parameter and a new automation
  field `guidance_delivery` (`reference`, the default, or `inline`). In
  reference mode the requirements turn carries the workspace-relative guidance
  PATHS plus a mandatory "read these first" preamble; the agent loads the
  bodies with its own file tools, so the turn text stays a few hundred bytes no
  matter how large the guidance grows — and a resumed session reads the CURRENT
  file rather than a body snapshotted into its transcript. Verified against the
  real installed `amplifier-agent`, which does NOT auto-load FILE @-mentions in
  turn text; the reference form drives the agent's read tools instead.
  `check_requirements` still reads every referenced file up front, so a
  missing/empty guidance file is still a loud pre-run failure. An automation
  that never sets `guidance_delivery` is a reference-form automation;
  `inline` stays selectable for one that genuinely wants its guidance literally
  in the transcript.

- `list_automations`/`get_automation_detail` now serve `last_run` (the most
  recent run ATTEMPT -- `{run_id, started_at, finished_at, failed, error}`,
  or `None` if never run), `consecutive_failures`, and `session_status`
  (`"healthy"` / `"degraded"` / `"dead"` / `"unknown"`). Previously a failing
  automation's reported last run silently fell back to its last SUCCESS
  (nothing served the latest attempt at all), and the consecutive-failure
  counter `session_health.health_for` already computed had zero callers
  anywhere in the codebase.
- `drumbeat session-health --workspace <dir>` -- new CLI verb printing every
  automation's pinned session, consecutive-failure count, and health detail.
  This is the first caller of `session_health.health_for`.

### Fixed

- A run that died from an UNEXPECTED exception (one that escaped `runner.run`'s
  own fail-loud aborts) left the automation's surfaced `last_run` pointing at
  its previous SUCCESS -- a failing automation reading as healthy, its real
  failure time recorded nowhere the app looks. The `last_run` read path
  (`management_api._iter_run_records`) consults each run's `result.json` ONLY.
  Every failure path *inside* `runner.run` already writes one, but an escaped
  exception wrote none: the scheduler recorded it only in memory, and the
  management API's "run now" wrote a `status.json` that `_iter_run_records`
  ignores. `runner.run` now wraps its body and, on any escaped exception,
  persists a canonical failed `result.json` (at the one place `run_id` /
  `started_at` are known) and THEN re-raises -- fail loud is preserved, but the
  failure's timestamp is no longer lost, so both the scheduler and manual "run
  now" surfaces report the FAILURE as the latest run instead of a stale prior
  success. Evidence: `EVIDENCE/before-after.txt`
  (`EVIDENCE/repro_before.py` / `repro_after.py`).
- Every manual "Run Now" reported `tracking failed -- required field
  'automation_name' is absent`, for every automation, whether or not the run
  itself succeeded. `GET /api/runs/<slug>/<run_id>` -- the endpoint a client
  polls after the 202 -- served the raw on-disk bookkeeping document. Neither
  `status.json` (in flight) nor `result.json` (finished) carries
  `automation_name`, and `status.json` carries neither `failed` nor `notified`
  either; all three are required by the client's run-record decoder, which is
  the SAME decoder it uses for the run-history list. Only the list assembled
  that shape, which is why "Last run" rendered while every manual run read as
  untrackable. Both endpoints now build the record through one shared
  contract, so a run decodes in every state it can be in. Note a fix limited
  to `automation_name` would only have moved the error to `'failed' is
  absent`. Evidence: `EVIDENCE/` (`04a`/`04b` payload before/after).
- `GET /api/runs/<slug>/<run_id>` served the automation's DISPLAY NAME under
  `automation`, where the list endpoint has always served the SLUG -- one
  field meaning two different things depending on which endpoint answered. It
  is now the slug on both; the display name is `automation_name` on both.
- A run's `started_at` crept forward: every status write re-stamped it with
  the current time, so elapsed time computed from it drifted toward zero and a
  failure record always claimed the run started the instant it died. It is now
  minted once, when the run starts.
- A manual run whose background thread died before `runner.run` could write
  `result.json` had no `finished_at`, so it was indistinguishable from a run
  still in flight -- it displayed as "running..." until the client's own poll
  ceiling expired minutes later, and its real error was never shown. The
  failure record now records when the run ended.
- `/api/capabilities` reported tools as unresolvable in any workspace whose
  `automations/` is a symlink to policy kept elsewhere. The workspace was derived
  by resolving `automations_dir` before taking `.parent`, which followed the
  symlink out of the workspace and built the reported turn PATH against the
  policy repo's `bin/` instead of the workspace's own. Four installed,
  running tools were shown in the app as "not installed on this box" while
  their scheduled runs passed. `pack_list`, `path_prepended` and `turn_path` were
  wrong in the same direction. Runner behaviour was never affected.
  Evidence: `EVIDENCE/resolver-symlink/`.
