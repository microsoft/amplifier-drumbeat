"""Isolated per-turn worker: run EXACTLY ONE agent turn, then exit.

VISION §3 / contracts/agent-binding.v1.md: every turn executes in its own OS
process, and the invocation imports the agent LIBRARY inside this worker -- no
CLI, no argv contract, no stream parsing. ``amplifier_agent`` is the only
module of that library drumbeat imports, and this file is the only drumbeat
module that imports it, so the library's process-global state can never outlive
a turn.

The whole embedding surface is four calls::

    create_agent(AgentOptions(...))          # one agent
    agent.create_session(SessionOptions(...))  # or agent.resume_session(id)
    session.start_turn(TurnInput(...))       # one turn
    async for event in turn.events(): ...    # observed, then terminal

Run as ``python -m drumbeat.agent_worker`` with one of:

  * (default) a task spec on **stdin** as a single JSON object, or via
    ``--spec-file <path>``. The prompt is NEVER on argv -- a turn's text can be
    arbitrarily large and carries no OS per-argument ceiling this way.
  * ``--preflight``: import the library and print its version, then exit 0.
    Used by ``drumbeat serve``/``service install`` and ``doctor`` as the
    engine-library health check (a non-zero exit means the library could not be
    imported in the interpreter every turn actually runs under).

STDOUT is a strict protocol channel. Every line is one JSON object:

  * a display event, translated from the library's ``turn.events()`` stream
    into drumbeat's own canonical NDJSON vocabulary as
    ``{"method": <method>, "params": {...}}`` (activity narration + usage);
  * exactly one terminal envelope ``{"drumbeat_result": {...}}`` (see
    ``RESULT_ENVELOPE_KEY``) carrying the reply, real token/cost counts, and any
    error.

To keep that channel clean, the worker redirects fd 1 -> fd 2 at startup, so a
stray ``print``/library write to "stdout" lands on stderr (where the parent
captures it as diagnostics) instead of corrupting the protocol.

FAIL LOUD: any failure assembling or running the turn is reported as a terminal
envelope with ``ok=False`` and a human-readable ``error``. When the library
raises its own typed ``AgentError``, the ``message`` and ``remedy`` are carried
**verbatim** -- an engine that cannot reach a provider must say so in the
library's own words, never in drumbeat's paraphrase and never as a successful
run carrying an apologetic reply. The worker never exits without emitting a
terminal envelope on the normal path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, TextIO

# The stdout line that carries the ONE terminal result. Distinct top-level key
# so the parent never confuses it with a display event (which carries "method").
RESULT_ENVELOPE_KEY = "drumbeat_result"

# The dotted module the parent spawns and that live-turn detection
# (drumbeat.drain / drumbeat.staleness) matches in a running turn's
# /proc/<pid>/cmdline. Defined here, imported there, so the marker and the thing
# it marks can never drift apart.
WORKER_MODULE = "drumbeat.agent_worker"

# The terminal-envelope ``code`` the parent routes to the install hint: the
# agent library itself could not be imported in this interpreter.
ENGINE_LIBRARY_UNAVAILABLE = "engine_library_unavailable"

# The library's turn-event vocabulary (contracts/turn-events.v1.md, closed at
# eleven) mapped onto drumbeat's own canonical NDJSON display methods
# (``runner._CANONICAL_NDJSON_METHODS``). One translation table, here, because
# this is the only place the two vocabularies meet. An event type absent from
# this table is still forwarded -- under its own name -- so a library that grows
# a twelfth type is never silently swallowed; the parent's tracker ignores what
# it does not recognize rather than guessing.
_EVENT_METHOD = {
    "turn_started": "progress",
    "output_delta": "result/delta",
    "reasoning_delta": "thinking/delta",
    "reasoning_final": "thinking/final",
    "tool_call": "tool/started",
    "tool_result": "tool/completed",
    "approval_request": "progress",
    "approval_decision": "progress",
    "progress": "progress",
    "usage": "usage",
    "terminal": "result/final",
}


def _emit_result(stream: TextIO, payload: dict[str, Any]) -> None:
    """Write the single terminal envelope line to the protocol stream."""
    stream.write(json.dumps({RESULT_ENVELOPE_KEY: payload}) + "\n")
    stream.flush()


def _emit_event(stream: TextIO, method: str, params: dict[str, Any]) -> None:
    stream.write(json.dumps({"method": method, "params": params}) + "\n")
    stream.flush()


def _redirect_stdout_to_stderr() -> TextIO:
    """Claim fd 1 as a private protocol channel; send stray stdout to stderr.

    Returns a text stream writing to the ORIGINAL stdout (the parent's pipe).
    After this, ``sys.stdout``/fd 1 point at stderr, so any accidental
    ``print`` or library write cannot corrupt the NDJSON protocol.
    """
    sys.stdout.flush()
    saved_fd = os.dup(1)
    os.dup2(2, 1)  # fd 1 now writes to stderr
    # Rebind sys.stdout so Python-level prints also go to stderr.
    sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
    return os.fdopen(saved_fd, "w", buffering=1)


def _load_spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.spec_file:
        raw = Path(args.spec_file).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("task spec must be a JSON object")
    return spec


def _jsonable(value: Any) -> Any:
    """Best-effort JSON projection of one library record.

    Dataclasses become dicts, ``Decimal`` becomes its exact decimal STRING (so
    monetary precision survives the wire -- never a float), and anything else
    unrecognized becomes ``repr``. Never raises: a display event that cannot be
    projected must not take the turn down with it.
    """
    from dataclasses import fields, is_dataclass
    from decimal import Decimal

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    return repr(value)


def _text_of(parts: Any) -> str:
    """Concatenate a ``list[ContentPart]`` into plain text."""
    if not parts:
        return ""
    out: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _usage_totals(usage: Any) -> dict[str, Any]:
    """Sum one turn's ``Usage.entries`` into drumbeat's recorded counters.

    Every counter stays ``None`` unless the library reported a real number for
    it -- honestly ABSENT, never a fabricated ``0`` (VISION §4). ``cost`` is a
    ``Decimal`` map keyed by ISO 4217; the USD entry is recorded as an exact
    decimal STRING so precision survives persistence.
    """
    from decimal import Decimal

    totals: dict[str, Any] = {
        "tokens_in": None,
        "tokens_out": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": None,
    }
    entries = getattr(usage, "entries", None) or []
    cost_total: Decimal | None = None
    for entry in entries:
        for field in ("tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens"):
            value = getattr(entry, field, None)
            if isinstance(value, int):
                totals[field] = (totals[field] or 0) + value
        cost = getattr(entry, "cost", None)
        if isinstance(cost, dict):
            usd = cost.get("USD")
            if isinstance(usd, Decimal):
                cost_total = usd if cost_total is None else cost_total + usd
    if cost_total is not None:
        totals["cost_usd"] = str(cost_total)
    return totals


def _selection(spec: dict[str, Any]) -> tuple[str | None, str | None]:
    provider = spec.get("provider")
    model = spec.get("model")
    return (
        provider if isinstance(provider, str) and provider else None,
        model if isinstance(model, str) and model else None,
    )


def _mcp_servers(spec: dict[str, Any]) -> list[Any] | None:
    """Project the spec's ``mcp`` list onto the library's ``McpServer`` records."""
    from amplifier_agent import McpServer

    raw = spec.get("mcp")
    if not isinstance(raw, list) or not raw:
        return None
    servers: list[Any] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        servers.append(
            McpServer(
                name=str(entry.get("name") or ""),
                transport=entry.get("transport") or "stdio",
                command=entry.get("command"),
                args=entry.get("args"),
                env=entry.get("env"),
                url=entry.get("url"),
                headers=entry.get("headers"),
            )
        )
    return servers or None


async def _preflight() -> int:
    """Import the library and report its version. The engine-library health check.

    A non-zero exit means the library could not be imported in the interpreter
    every turn actually runs under -- which is the only thing the preflight is
    entitled to claim, since that is the exact import a turn performs.
    """
    import amplifier_agent

    print(amplifier_agent.__version__)
    return 0


async def _run_turn(spec: dict[str, Any], out: TextIO) -> None:
    """Assemble the embedding surface and run exactly one turn."""
    from amplifier_agent import (
        AgentOptions,
        SessionOptions,
        TextPart,
        TurnInput,
        create_agent,
    )

    prompt = spec["prompt"]
    agent_session_id = spec["agent_session_id"]
    resume = bool(spec.get("resume", False))
    storage = Path(spec["storage"]).expanduser()
    provider, model = _selection(spec)
    skills = [str(s) for s in (spec.get("skills") or []) if s]

    # A fresh turn must never resume a stale conversation. drumbeat owns this
    # session's whole storage root (contracts/agent-binding.v1.md section 6), so
    # removing it is the complete, unambiguous reset -- no library call needed,
    # and nothing else lives under that path to lose.
    if not resume:
        shutil.rmtree(storage, ignore_errors=True)
    storage.mkdir(parents=True, exist_ok=True)

    options = AgentOptions(
        provider=provider,
        model=model,
        skills=skills or None,
        mcp_servers=_mcp_servers(spec),
        storage=str(storage),
        # Unattended by construction: there is no human to ask, and the library
        # fails a turn loudly rather than proceeding when no policy is set.
        approvals="allow",
        # A model probing a path that does not exist must not end an otherwise
        # healthy turn as a failure -- see contracts/agent-binding.v1.md §7.
        tool_error_policy="continue",
    )

    async with await create_agent(options) as agent:
        if resume:
            session = await agent.resume_session(agent_session_id)
        else:
            session = await agent.create_session(
                SessionOptions(session_id=agent_session_id, persistence="durable")
            )
        async with session:
            turn = await session.start_turn(
                TurnInput(content=[TextPart(prompt)])
            )
            terminal: Any = None
            async for event in turn.events():
                event_type = getattr(event, "type", "") or ""
                payload = getattr(event, "payload", None)
                if event_type == "terminal":
                    terminal = payload
                _emit_event(
                    out,
                    _EVENT_METHOD.get(event_type, event_type),
                    _display_params(event_type, payload),
                )

    if terminal is None:
        # The stream ended without a terminal event. The contract says one
        # always arrives, including on failure and cancellation, so silence
        # here is a real fault -- reported, never smoothed over into a success.
        raise RuntimeError(
            "the turn produced no terminal event (turn-events/1 requires exactly "
            "one, last, including on failure)"
        )

    state = getattr(terminal, "state", None)
    error = getattr(terminal, "error", None)
    totals = _usage_totals(getattr(terminal, "usage", None))

    if state != "success" or error is not None:
        _emit_result(out, _error_payload(error or state, totals=totals))
        return

    _emit_result(
        out,
        {
            "ok": True,
            "reply": _text_of(getattr(terminal, "content", None)),
            "error": None,
            "code": None,
            **totals,
        },
    )


def _display_params(event_type: str, payload: Any) -> dict[str, Any]:
    """One library event payload, shaped for drumbeat's display protocol.

    ``tool_call`` is flattened to ``{name, args}`` because that is what the
    parent's activity narration reads; ``usage`` is flattened to the summed
    counters. Everything else is forwarded as the projected payload, so a
    consumer of the raw stream loses nothing.
    """
    if event_type == "tool_call":
        call = getattr(payload, "call", None)
        return {
            "name": getattr(call, "name", None),
            "args": _jsonable(getattr(call, "arguments", None)) or {},
            "source": getattr(call, "source", None),
        }
    if event_type == "usage":
        return _usage_totals(getattr(payload, "snapshot", None))
    projected = _jsonable(payload)
    return projected if isinstance(projected, dict) else {"value": projected}


def _error_payload(exc: Any, *, totals: dict[str, Any] | None = None) -> dict[str, Any]:
    """Terminal envelope for a turn that failed to assemble or run.

    When the failure carries the library's own typed ``AgentError``, its
    ``message`` and ``remedy`` are joined and reported **VERBATIM** -- an
    unreachable provider must reach the operator in the library's own words,
    with its own remedy, rather than in drumbeat's paraphrase. The ``code``
    likewise comes from the library when it has one, so a consumer never has to
    substring-match an error message to make a decision.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    remedy = getattr(exc, "remedy", None)
    if isinstance(remedy, str) and remedy:
        message = f"{message} {remedy}"
    if not isinstance(code, str) or not code:
        code = type(exc).__name__ if isinstance(exc, BaseException) else str(exc or "failure")
    payload: dict[str, Any] = {
        "ok": False,
        "reply": "",
        "error": message,
        "code": code,
        "tokens_in": None,
        "tokens_out": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "cost_usd": None,
    }
    if totals:
        payload.update(totals)
    return payload


def _import_failure_payload(exc: BaseException) -> dict[str, Any]:
    payload = _error_payload(exc)
    payload["code"] = ENGINE_LIBRARY_UNAVAILABLE
    payload["error"] = f"the agent library could not be imported: {exc}"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m drumbeat.agent_worker")
    parser.add_argument("--spec-file", default=None, help="path to a task-spec JSON file (default: read stdin)")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="import the agent library, print its version, and exit",
    )
    args = parser.parse_args(argv)

    if args.preflight:
        # The preflight writes to REAL stdout (its whole signal is the version
        # line plus the exit code) and never touches the protocol channel.
        try:
            return asyncio.run(_preflight())
        except BaseException as exc:  # noqa: BLE001 - report loudly, exit non-zero
            print(f"[agent_worker] preflight failed: {exc!r}", file=sys.stderr)
            return 1

    # Claim the protocol channel before anything can print.
    out = _redirect_stdout_to_stderr()

    try:
        spec = _load_spec(args)
    except Exception as exc:  # noqa: BLE001
        _emit_result(out, _error_payload(exc))
        return 0

    try:
        asyncio.run(_run_turn(spec, out))
    except ImportError as exc:
        print(f"[agent_worker] agent library unavailable: {exc!r}", file=sys.stderr)
        _emit_result(out, _import_failure_payload(exc))
    except BaseException as exc:  # noqa: BLE001 - every failure becomes a terminal envelope
        print(f"[agent_worker] turn failed: {exc!r}", file=sys.stderr)
        _emit_result(out, _error_payload(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
