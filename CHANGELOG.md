# Changelog

## Unreleased

### Fixed

- **`recency-check` could never run: a new per-automation `prompt_caching`
  toggle routes around an upstream provider bug.** Since the step-grammar
  refactor, every `recency-check` run exited 1 with the upstream Anthropic
  provider rejecting the request —
  `messages.N.content.0.thinking.cache_control: Extra inputs are not permitted`.
  Root cause is UPSTREAM in `amplifier-agent` (not this repo): its provider
  stamps a `cache_control` breakpoint onto a `thinking` content block, which
  Anthropic forbids. It only bites an automation whose FINAL assistant turn is
  thinking-only (a thinking block with no sibling text block) —
  `recency-check` is the only one, because it writes solely to the recency
  store and emits no final text reply. Every other automation ends with a
  written summary and never trips it. The `Automation` dataclass gains an
  optional `prompt_caching: bool` frontmatter field (default `true` — the whole
  fleet is unchanged); when `false`, `runner.run`/`_run_body` materialize a
  minimal host config carrying `provider.config.enable_prompt_caching: false`
  and thread it as `--config` into every turn (`_automation_host_config_path`).
  This makes real `recency-check` runs succeed without changing anything the
  pass observes or records. The durable fix belongs upstream (guard `thinking`
  blocks in `amplifier_module_provider_anthropic._stamps_empty_text_block` /
  `_stamp_last_block`); remove the frontmatter opt-out once it lands. Proven by
  an offline reproduction against the installed provider (bug present with
  caching on, gone with `enable_prompt_caching=false`) and a RED→GREEN suite
  (`tests/test_recency_prompt_caching.py`).

### Added

- Required guidance now reaches the agent by **reference** instead of being
  inlined into argv. `format_requirements_turn` gained a `mode` parameter and a
  new automation field `guidance_delivery` (`reference`, the default, or
  `inline`, the legacy form). In reference mode the requirements turn carries
  the workspace-relative guidance PATHS plus a mandatory "read these first"
  preamble; the agent loads the bodies with its own file tools, so the turn
  text — and therefore argv — stays a few hundred bytes no matter how large the
  guidance grows. This kills the argv-inlining failure class at the root: a
  single argv element at/over Linux's `MAX_ARG_STRLEN` (131072 bytes = 128 KiB)
  fails `execve` with E2BIG, silently, before the agent boots — exactly how
  channels-check died when IDENTITY.md was inlined. Verified against the real
  installed `amplifier-agent` (v0.9.3), which does NOT auto-load FILE
  @-mentions in turn text; the reference form drives the agent's read tools
  instead. `check_requirements` still reads every referenced file up front, so
  a missing/empty guidance file is still a loud pre-run failure. Every existing
  automation (which never set `guidance_delivery`) becomes a reference-form
  automation automatically; `inline` stays selectable during migration.
- Turn-size belt guard: `_build_command` now raises a named `TurnTooLargeError`
  when a turn's text would reach `MAX_ARG_STRLEN`, and `_execute_turn` converts
  it into a persisted run failure (run record + failures.log + a new
  `drumbeat:turn_too_large` event) — a named, actionable failure with a remedy
  instead of the kernel's opaque E2BIG. Belt-and-suspenders behind the
  reference default.

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
