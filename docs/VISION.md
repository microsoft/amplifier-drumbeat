# VISION

The desired end state this repo converges toward, written as though already true.
This page is never edited to record what shipped — status belongs in the issue
queue and the ledgers, not here. Amendments carry evidence (a failure this
framing would have caught, or a cost it retires) and land in the dated changelog
at the bottom. Work follows the governance loop: **amend here first → file work
items against the amendment → execute.** A drifted tree is debt on arrival,
never grounds to edit this page into agreement.

## What drumbeat is

An automation engine for long-running agent sessions. You write an automation as
a single markdown file — a schedule, a policy, an ordered list of steps — and
drumbeat runs it in a pinned agent session, on time, unattended, with the tools
you bring. Most of what is here exists so an automation can run unattended for
months without silently going wrong.

## Principles

### 1. The automation file is a contract

The machine surface of an automation is structured data with a **closed
vocabulary**: every frontmatter key is registered, validated on save, and an
unknown key is refused loudly with a remedy — never ignored. Steps are
structured objects in the frontmatter (see `contracts/automation-file.v1.md`),
not prose the engine has to parse out of a document body. The body is for
humans; the engine never executes it. `contracts/` governs this surface; the
contract is amended before the code moves.

### 2. Policy lives in prose, mechanism lives in schema

A step is deliberately minimal — an id and a prompt. Judgment (what counts as
eligible, when to re-surface, how to act) belongs in the prompt and in guidance
documents the automation loads and maintains. The schema carries only what the
engine can actually enforce: order, identity, scheduling, refusal, rotation.
Structure that cannot be checked by a machine does not get schema.

### 3. Turns run isolated

Every turn executes in its own OS process. A wedged, crashed, or runaway turn
dies alone and becomes a recorded run failure; the engine keeps its schedule.
The invocation imports the agent's engine library inside an isolated per-turn
worker process — no CLI subprocess, no argv contract, no stream parsing: the
worker assembles the documented embedding surface and returns typed results
over the engine's own pipe. One turn per process keeps the library's
process-global state (env writes, import-time bindings) harmless by
construction, and the isolation property itself is not up for negotiation.
Long-lived in-process embedding waits on the upstream library contract — a
public turn-handler API and no process-global writes.

### 4. Unattended honesty

The enemy is the successful-looking run that did nothing. Every failure is
loud, recorded, and carries a remedy. Verification gates prove real behavior —
a service install is verified by executing a real turn, not by a health
endpoint. Numbers the engine reports are real or absent; a metric that is
silently always zero is a defect.

### 5. The repo teaches agents

`skills/` is a first-class surface: an AI agent can learn to operate drumbeat,
author automations, and build drumpacks from this repo alone. Skills carry the
how-to layer and point at the canonical docs — one source of truth per concept.

### 6. Consumer-agnostic seams

The engine knows no consumer's vocabulary. Drumpacks bring tools, profiles name
model policies, injectors bring turn context, guidance files carry domain
judgment — all owner-supplied. Anything consumer-shaped in the engine is a leak.

## What this repo deliberately resists

- **Speculative schema.** Control-flow fields (conditions, retries, branching)
  stay out until a real automation demonstrates the need — then the contract is
  amended with that evidence.
- **Compatibility shims.** Format changes are clean cuts with migration notes,
  not dual-reads. Old keys are refused, not quietly honored.
- **Pinning its agent dependency.** drumbeat tracks amplifier-agent main and
  accepts the breakage risk deliberately; breakage is worked when it lands, not
  deferred behind a pin.
- **Status creep in this file.** What shipped lives in the tracker and the
  changelog of releases, never here.

## Changelog

- **2026-08-27** — §3 amended: turn invocation moves from the subprocess SDK to
  the agent's engine library, imported in an isolated per-turn worker process.
  Evidence: amplifier-agent v0.17.0 leads its integration story with the
  documented embedding surface (engine-api.md) and fixed the usage/argv defects
  we filed; a one-turn-per-process worker preserves this section's isolation
  clause while deleting the CLI argv/envelope surface and its failure modes.
  Isolation unchanged.
- **2026-08-25** — Initial vision. Encodes the negotiated decisions: structured
  steps in frontmatter (contract v1), SDK-isolated turn execution with
  lib-when-upstream-ships posture, and skills as a first-class surface.
