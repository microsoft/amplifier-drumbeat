---
pack_format: 1
name: minimal
description: >
  The engine's minimal test drumpack -- the smallest thing that exercises the
  consumer-facing contracts, and the copyable `inject:` exemplar. Real
  drumpacks are consumer-owned and normally private, so the pattern ships here
  instead: consumers must be able to COPY it, not just read about it. See
  docs/DRUMPACKS.md.
tools:
  - minimal-state
activity:
  # Optional consumer-owned progress narration: subcommand -> the human phrase
  # the engine shows while that subcommand runs. Mechanism, not policy -- the
  # engine hardcodes no consumer vocabulary. `minimal-state` runs as a bare
  # `inject:` tool here, so this line is illustrative: a real CLI would map one
  # entry per verb (`state`, `sessions`, `send`, ...). Undeclared tools and
  # subcommands narrate with a generic phrase.
  state: "Checking minimal state…"
---

# minimal (the engine's test drumpack)

One tool, `minimal-state`: prints a one-line state snapshot on stdout, or the
byte-exact `INJECT_IDLE` sentinel when there is nothing to inject
(`MINIMAL_STATE_IDLE=1`). Exit non-zero on any failed read; errors go to
stderr, never stdout -- stdout is the injection channel.

The optional `activity:` map above declares the human phrasing the engine
narrates while one of this drumpack's tools runs -- keyed by subcommand, owned
by the drumpack, never hardcoded in the engine.

See `automations/inject-exemplar.md` for the copyable `inject:` declaration.
