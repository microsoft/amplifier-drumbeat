"""Isolated per-turn worker: run EXACTLY ONE agent turn, then exit.

VISION §3: every turn executes in its own OS process, and the invocation
imports the agent's engine LIBRARY inside this worker -- no CLI subprocess, no
argv contract, no stream parsing. The worker assembles the documented embedding
surface (``load_and_prepare_cached`` -> provider injection -> ``make_turn_handler``
-> ``Engine`` -> ``boot`` -> ``submit_turn``) and returns typed results to the
parent (``drumbeat.runner``) over its own stdout pipe as drumbeat-shaped NDJSON.

Run as ``python -m drumbeat.agent_worker`` with one of:

  * (default) a task spec on **stdin** as a single JSON object, or via
    ``--spec-file <path>``. The prompt is NEVER on argv -- a turn's text can be
    arbitrarily large and carries no OS per-argument ceiling this way.
  * ``--prewarm``: prepare the bundle cache and exit 0. Used by
    ``drumbeat serve``/``service install`` and ``doctor`` so the first scheduled
    turn never eats the cold ``uv pip install`` prep, and as the engine-library
    health preflight (a non-zero exit means the library could not be imported or
    the bundle could not be prepared).

STDOUT is a strict protocol channel. Every line is one JSON object:

  * a display event, forwarded verbatim from the engine's DisplaySystem as
    ``{"method": <event-type>, "params": {...}}`` (activity narration + usage);
  * exactly one terminal envelope ``{"drumbeat_result": {...}}`` (see
    ``RESULT_ENVELOPE_KEY``) carrying the reply, real token/cost counts, and any
    error.

To keep that channel clean, the worker redirects fd 1 -> fd 2 at startup, so a
stray ``print``/library write to "stdout" lands on stderr (where the parent
captures it as diagnostics) instead of corrupting the protocol. The engine
library itself never writes stdout (stdout-discipline invariant), so the only
writer on the real-stdout fd is this module.

FAIL LOUD: any failure assembling or running the turn is reported as a terminal
envelope with ``ok=False`` and a human-readable ``error`` (and the provider's own
message on stderr, so the parent's ceiling detection keeps working). The worker
never exits without emitting a terminal envelope on the normal path.
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


class _WorkerDisplay:
    """Our DisplaySystem: forward every engine display event to the parent as
    drumbeat-shaped NDJSON on the protocol stream, and accumulate real usage.

    The wire shape per line is ``{"method": <event-type>, "params": <rest>}`` --
    the same typed notification shape ``drumbeat.runner._TurnProgressTracker``
    already consumes for activity narration, so nothing downstream changes.

    Token/cost accounting is done HERE, from the engine's own ``usage`` events,
    because the engine is the authority on what the provider actually charged.
    ``inputTokens``/``outputTokens`` are SUMMED across usage events (a turn can
    emit several; a trailing rollup carries 0/0, which summation absorbs
    harmlessly -- last-wins would zero it). Cost and cache counts take the last
    real value seen. Counts stay ``None`` -- honestly absent, never a fabricated
    0 (VISION §4) -- until at least one usage event carries real numbers.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.saw_usage = False
        self.tokens_in = 0
        self.tokens_out = 0
        self.cache_read: int | None = None
        self.cache_write: int | None = None
        self.cost_usd: str | None = None

    async def emit(self, event: Any) -> None:
        event_dict = dict(event)
        method = event_dict.pop("type", "unknown")
        if method == "usage":
            self._observe_usage(event_dict)
        line = json.dumps({"method": method, "params": event_dict})
        self._stream.write(line + "\n")
        self._stream.flush()

    def _observe_usage(self, params: dict[str, Any]) -> None:
        i = params.get("inputTokens")
        o = params.get("outputTokens")
        if isinstance(i, int) and isinstance(o, int):
            self.tokens_in += i
            self.tokens_out += o
            self.saw_usage = True
        cr = params.get("cacheReadTokens")
        if isinstance(cr, int):
            self.cache_read = (self.cache_read or 0) + cr
        cw = params.get("cacheWriteTokens")
        if isinstance(cw, int):
            self.cache_write = (self.cache_write or 0) + cw
        cost = params.get("cost")
        if isinstance(cost, str) and cost:
            self.cost_usd = cost


def _emit_result(stream: TextIO, payload: dict[str, Any]) -> None:
    """Write the single terminal envelope line to the protocol stream."""
    stream.write(json.dumps({RESULT_ENVELOPE_KEY: payload}) + "\n")
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


def _read_host_config(host_config_path: str | None) -> dict[str, Any] | None:
    """Parse the materialized agent-config file into the host_config dict.

    The file is what ``drumbeat.agent_config`` materialized (JSON with the
    closed ``provider|providers|mcp|skills|debug`` vocabulary). ``None`` when no
    config was threaded -- the turn runs on the engine defaults.
    """
    if not host_config_path:
        return None
    data = json.loads(Path(host_config_path).read_text(encoding="utf-8"))
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(
            f"host config at {host_config_path} must be a JSON object, "
            f"got {type(data).__name__}"
        )
    return data


def _select_provider(host_config: dict[str, Any] | None, resolvable: list[str]) -> str:
    """Mirror single_turn.py: explicit ``provider.module`` wins, else the
    bundle-default preference (anthropic), else the first resolvable provider.
    """
    if isinstance(host_config, dict):
        provider_block = host_config.get("provider")
        if isinstance(provider_block, dict):
            module = provider_block.get("module")
            if isinstance(module, str) and module.strip():
                return module.strip()
    if "anthropic" in resolvable:
        return "anthropic"
    if not resolvable:
        raise RuntimeError(
            "no provider credentials are resolvable in this environment -- "
            "the engine cannot run a turn without a provider (set e.g. "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY in the engine's environment)"
        )
    return resolvable[0]


def _session_state_dir(session_id: str, cwd: Path) -> Path:
    """Where the library writes this session's state -- the SAME directory
    ``drumbeat.runner._session_dir`` reads (VISION §3 / requirement g).
    """
    from drumbeat.paths import amplifier_agent_home, derive_workspace_slug

    slug = derive_workspace_slug(cwd)
    return amplifier_agent_home() / "state" / "workspaces" / slug / "sessions" / session_id


async def _prewarm() -> int:
    """Prepare the bundle cache and exit. Doubles as the engine-library health
    preflight: a non-zero exit means the library is unimportable or the bundle
    could not be prepared (cold prep shells ``uv pip install``)."""
    from amplifier_agent_lib import __version__
    from amplifier_agent_lib.bundle.cache import load_and_prepare_cached

    await load_and_prepare_cached(aaa_version=__version__)
    return 0


async def _run_turn(spec: dict[str, Any], out: TextIO) -> None:
    """Assemble the embedding surface and run exactly one turn."""
    from amplifier_agent_lib import __version__
    from amplifier_agent_lib import protocol as _proto
    from amplifier_agent_lib._runtime import make_turn_handler
    from amplifier_agent_lib.bundle.cache import load_and_prepare_cached
    from amplifier_agent_lib.engine import Engine
    from amplifier_agent_lib.protocol_points.defaults_http import HttpAutoApprovalSystem
    from amplifier_agent_cli import provider_sources

    session_id = spec["session_id"]
    turn_id = spec.get("turn_id") or f"{session_id}-turn"
    cwd = Path(spec["cwd"]).expanduser()
    prompt = spec["prompt"]
    resume = bool(spec.get("resume", False))
    mode = spec.get("mode")
    host_config = _read_host_config(spec.get("host_config_path"))

    # Fresh turn: wipe any prior state for this session id first, exactly as the
    # reference single-turn flow does before building the handler (the library
    # exposes no "fresh" API; a fresh turn must not resume a stale transcript).
    if not resume:
        shutil.rmtree(_session_state_dir(session_id, cwd), ignore_errors=True)

    prepared = await load_and_prepare_cached(aaa_version=__version__)

    resolvable = provider_sources.enumerate_resolvable_providers()
    provider = _select_provider(host_config, resolvable)
    prepared.mount_plan["providers"] = []
    provider_sources.inject_provider(
        prepared,
        provider,
        extra_config=provider_sources.provider_config_from_host(host_config),
    )
    provider_sources.inject_routing_matrix(prepared, provider)

    handler = make_turn_handler(
        prepared,
        cwd=str(cwd),
        is_resumed=resume,
        host_config=host_config,
        workspace=None,  # let the lib derive the slug from cwd/env == drumbeat.paths
        mode=mode,
    )

    display = _WorkerDisplay(out)
    engine = Engine(
        turn_handler=handler,
        # ApprovalSystem is always configured (the engine fails loud if it is
        # not): a headless auto-approve that matches current behavior -- the
        # shipped bundle drops the interactive approval hook, so tools run.
        protocol_points={"approval": HttpAutoApprovalSystem(log_requests=False), "display": display},
    )

    boot_params: dict[str, Any] = {
        "protocolVersion": getattr(_proto, "PROTOCOL_VERSION", None),
        "clientInfo": {"name": "drumbeat-agent-worker", "version": "1"},
        "sessionId": session_id,
        "resume": resume,
    }
    caps = getattr(_proto, "server_default_capabilities", None)
    if caps is not None:
        boot_params["capabilities"] = dict(caps())

    await engine.boot(boot_params, bundle_override=prepared)
    result = await engine.submit_turn(
        {"sessionId": session_id, "turnId": turn_id, "prompt": prompt}
    )
    try:
        await engine.shutdown()
    except Exception:  # noqa: BLE001 - shutdown is best-effort; the turn already produced a result
        pass

    reply = result.get("reply")
    # Prefer the engine's own result-level token/cost fields when the library
    # populates them (newer engines); otherwise fall back to the usage the
    # DisplaySystem accumulated. Either way the numbers are the engine's, never
    # a drumbeat-side re-derivation.
    tokens_in = _first_int(result.get("tokensIn"), display.tokens_in if display.saw_usage else None)
    tokens_out = _first_int(result.get("tokensOut"), display.tokens_out if display.saw_usage else None)
    cost_usd = result.get("costUsd")
    if not (isinstance(cost_usd, str) and cost_usd):
        cost_usd = display.cost_usd

    _emit_result(
        out,
        {
            "ok": True,
            "reply": reply if isinstance(reply, str) else "",
            "error": None,
            "code": None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
            "cache_read_tokens": display.cache_read,
            "cache_write_tokens": display.cache_write,
        },
    )


def _first_int(*candidates: Any) -> int | None:
    for c in candidates:
        if isinstance(c, int):
            return c
    return None


def _error_payload(exc: BaseException) -> dict[str, Any]:
    """Terminal envelope for a turn that failed to assemble or run.

    The provider's own message (e.g. a context-ceiling refusal) is preserved in
    ``error`` verbatim, so the parent's ceiling detection keeps matching, and is
    ALSO printed to stderr (the parent captures it) for the run's stderr.log.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if not isinstance(code, str):
        code = type(exc).__name__
    return {
        "ok": False,
        "reply": "",
        "error": message,
        "code": code,
        "tokens_in": None,
        "tokens_out": None,
        "cost_usd": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m drumbeat.agent_worker")
    parser.add_argument("--spec-file", default=None, help="path to a task-spec JSON file (default: read stdin)")
    parser.add_argument("--prewarm", action="store_true", help="prepare the bundle cache and exit")
    args = parser.parse_args(argv)

    # Claim the protocol channel before anything can print.
    out = _redirect_stdout_to_stderr()

    if args.prewarm:
        # Prewarm writes nothing to the protocol channel; its signal is the exit
        # code. Failures print to stderr and exit non-zero so serve/doctor see
        # them loudly.
        try:
            return asyncio.run(_prewarm())
        except BaseException as exc:  # noqa: BLE001 - report loudly, exit non-zero
            print(f"[agent_worker] prewarm failed: {exc!r}", file=sys.stderr)
            return 1

    try:
        spec = _load_spec(args)
    except Exception as exc:  # noqa: BLE001
        _emit_result(out, _error_payload(exc))
        return 0

    try:
        asyncio.run(_run_turn(spec, out))
    except BaseException as exc:  # noqa: BLE001 - every failure becomes a terminal envelope
        print(f"[agent_worker] turn failed: {exc!r}", file=sys.stderr)
        _emit_result(out, _error_payload(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
