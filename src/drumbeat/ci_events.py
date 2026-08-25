"""Direct, synchronous, best-effort delivery of the ENGINE's OWN
orchestration events to the LOCAL Context Intelligence server -- the
decisions the engine makes AROUND an amplifier-agent session, which the
session-scoped ``hook-context-intelligence`` capture (see ``ci_upload.py``)
never sees.

WHY THIS MODULE EXISTS
=======================
``hook-context-intelligence`` gives full visibility INSIDE one
amplifier-agent session's turns -- every tool call, every LLM iteration.
But the engine's own orchestration decisions happen OUTSIDE any session's
turns, in the engine's own Python code: which automation fired and why, a
schedule tick that found nothing due yet, a ``requires:`` gate abort before
any turn ever ran, a would-be notification mechanically suppressed as a
duplicate, two processes contending for the same session's advisory lock,
an agent choosing literal silence (``NOTHING_TO_REPORT``). None of these
are visible to the hook, because none of them are amplifier-agent tool
calls -- they are decisions the engine itself makes. Without this module
they happen and leave no durable, queryable trace; the only record is a
scroll of stderr logs.

NAMESPACE (renamed 2026-08-10)
==============================
Events emitted here are namespaced ``drumbeat:*``. They carried the first
consumer's own prefix until the engine was extracted from it, at which point
that prefix named a consumer that does not emit them -- these are engine
telemetry about engine mechanics, and the honest namespace is the
engine's. Namespace follows the emitter: a consumer's own domain events
keep the consumer's prefix. Historical CI queries split at the rename
date; that cost was accepted now rather than after more history had
accumulated under the wrong name.

This module posts them into the SAME graph, under the SAME workspace tag
the hook already uses, so one Cypher query can span both layers -- e.g.
"how many notifications did we suppress this week, and what tool calls did
the agent make in each suppressed run's session."

SECURITY / DATA-RESIDENCY INVARIANT
====================================
Same invariant as ``ci_upload.py`` (see that module's docstring for the
full rationale): ``_SERVER_URL`` is a hardcoded constant, never read from
an environment variable, config file, or CLI flag. There are two Context
Intelligence servers reachable from this machine and only one of them is
this user's own.

WIRE FORMAT
===========
Uses the exact same ``POST /events`` wire format
``hook-context-intelligence`` itself posts (body: ``{event, workspace,
idempotency_key, data}``, ``data.timestamp`` required) -- a format already
proven in production by that module, not a new protocol invented here. The
idempotency-key scheme (sha256 of the canonical envelope, see
``_idempotency_key`` below) is reimplemented as five lines of stdlib code
rather than taken as a dependency on the ``context_intelligence`` client
package, which pulls in requests/httpx/azure-identity -- weight this
project's dependency list has no reason to carry for one hash computation.

FAIL LOUD, NO SILENT FALLBACKS
===============================
``emit`` never raises: a down or unreachable CI server must never break
the engine's actual job (running automations, emitting notification
intents). Every
failure (missing API key, connection refused, non-2xx response, timeout)
is printed to stderr with the real reason -- never swallowed without a
trace.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from drumbeat.paths import derive_workspace_slug

# Hardcoded. See module docstring -- this is the ONLY place this URL is
# ever written. NEVER read from an env var, settings file, or CLI flag.
_SERVER_URL = "http://localhost:8100"
_API_KEY_ENV_VAR = "CONTEXT_INTELLIGENCE_PERSONAL"
_TIMEOUT_SECONDS = 5.0


def _canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _idempotency_key(event: str, workspace: str, data: dict[str, Any]) -> str:
    """Same scheme ``hook-context-intelligence`` itself uses for its own
    ``POST /events`` calls (see that module's ``upload.py``:
    ``build_payload``): sha256 of the canonical ``{event, workspace, data}``
    envelope, prefixed ``aci-event-v1:``. Deterministic from content, so
    posting the exact same event twice (e.g. a caller retry) is a safe
    no-op server-side rather than a duplicate.
    """
    canonical = _canonical_json({"event": event, "workspace": workspace, "data": data})
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"aci-event-v1:{digest}"


def emit(event: str, data: dict[str, Any], *, cwd: Path) -> None:
    """Best-effort: post one ``drumbeat:*`` orchestration event to the LOCAL CI server.

    ``event`` should be namespaced ``drumbeat:<snake_case_name>`` (e.g.
    ``drumbeat:notification_delivered``) so it's unambiguous in
    query results which layer produced it. ``data`` is any JSON-serializable
    mapping describing the signal (automation name, run id, session id,
    etc.) -- a ``timestamp`` field is added automatically if not already
    present.

    Never raises. See module docstring for the fail-loud-no-fallback
    rationale.
    """
    api_key = os.environ.get(_API_KEY_ENV_VAR, "").strip()
    if not api_key:
        print(
            f"[ci_events] SKIPPED {event}: ${_API_KEY_ENV_VAR} is not set in the environment",
            file=sys.stderr,
        )
        return

    workspace = derive_workspace_slug(cwd)
    payload_data: dict[str, Any] = {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **data,
    }
    body = {
        "event": event,
        "workspace": workspace,
        "idempotency_key": _idempotency_key(event, workspace, payload_data),
        "data": payload_data,
    }

    request = urllib.request.Request(
        f"{_SERVER_URL}/events",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"[ci_events] FAILED to post {event}: {exc}", file=sys.stderr)


__all__ = ["emit"]
