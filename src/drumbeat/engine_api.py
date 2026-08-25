"""The engine's HTTP face: automations, prompts, runs, capabilities, health.

Step 2 moved the automations/prompts/runs CRUD into this repo as library
functions the consumer called in-process. Step 3 gives them a port. Nothing
about the *behaviour* changes here -- these are the same
``management_api`` functions, reached over a socket instead of an import, so
the consumer's service can stop being the engine's composition root.

Route shapes are deliberately IDENTICAL to the ones the consumer already
serves, so re-pointing its UI at the engine is a proxy and not a rewrite.

Two properties this file is responsible for, both from section 3:

- **Loopback bind only.** The engine gains no public surface. The consumer
  stays the one public face and proxies what the phone needs.
- **X-API-Key on every mutating request, including from loopback.** See
  ``drumbeat.api_key`` for why the bypass that is correct at N=1 service is
  wrong at N>=2 co-hosted engines. Enforcement lives in ONE place here --
  ``_authorize``, called by every verb dispatcher before it looks at the
  path -- so a new endpoint cannot be added without inheriting it.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from drumbeat import api_key, capabilities, drain, engine_events, management_api, turns
from drumbeat import staleness as staleness_mod
from drumbeat.scheduler import SchedulerState, scheduler_lock_path

# The delivery worker's cursor, mirrored by the CONSUMER into a file the
# engine can read without importing it (see section 6: the outbox lag is
# deliberately visible from both sides, so either side going quiet is
# detectable from the other). Read-only here, always; the consumer is the
# single writer. Absent file => honest null, never a fabricated zero.
WORKER_CURSOR_FILENAME = ".delivery-worker-cursor.json"

_ENGINE_VERSION = "0.1.0"


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_worker_cursor(runs_dir: Path) -> dict[str, Any]:
    """The consumer's published delivery-worker cursor, or an honest unknown.

    Returns ``{"cursor": int|None, "updated_at": str|None, "source": str}``.
    A missing/unparseable file yields ``cursor: None`` and a ``source`` that
    says so -- never 0, which would render as "lag = whole file" or "fully
    caught up" depending on which way the reader squints. Both are lies.
    """
    path = Path(runs_dir).expanduser() / WORKER_CURSOR_FILENAME
    if not path.is_file():
        return {
            "cursor": None,
            "updated_at": None,
            "source": f"{path} does not exist -- the consumer's delivery worker "
            "has not published a cursor (it may never have run)",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "cursor": None,
            "updated_at": None,
            "source": f"{path} is unreadable: {exc}",
        }
    cursor = payload.get("cursor")
    return {
        "cursor": cursor if isinstance(cursor, int) else None,
        "updated_at": payload.get("updated_at"),
        "halt_reason": payload.get("halt_reason"),
        "source": str(path),
    }


class EngineServer(ThreadingHTTPServer):
    """One thread per request.

    A "run now" that kicks off an agent turn can take many minutes; it must
    never block a health check or an automation edit.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        *,
        ctx: management_api.EngineContext,
        workspace: Path,
        api_key_value: str,
        scheduler_state: SchedulerState,
        staleness_service: str,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.ctx = ctx
        self.workspace = workspace
        self.api_key = api_key_value
        self.scheduler_state = scheduler_state
        self.staleness_service = staleness_service


class EngineRequestHandler(BaseHTTPRequestHandler):
    server: EngineServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # stdlib signature
        sys.stderr.write(f"[drumbeat-api] {self.address_string()} - {format % args}\n")

    # ---- response helpers ----

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise json.JSONDecodeError("expected a JSON object", raw.decode("utf-8"), 0)
        return parsed

    def _handle_management_error(self, exc: management_api.ManagementError) -> None:
        self._send_json(exc.status, {"error": exc.message})

    # ---- auth ----

    def _authorize(self, path: str) -> bool:
        """Auth gate for one request. Sends 401 and returns False if denied.

        Called FIRST by every verb dispatcher, before any routing decision.
        The method is passed through to ``api_key.check_request`` because the
        read/write distinction -- not the path -- is what decides whether
        loopback is sufficient.
        """
        error = api_key.check_request(
            client_address=self.client_address[0],
            method=self.command,
            path=path,
            header_value=self.headers.get("X-API-Key"),
            expected_key=self.server.api_key,
        )
        if error is None:
            return True
        self._send_json(401, {"error": error})
        return False

    # ---- routing ----

    def do_GET(self) -> None:  # stdlib method name
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        if not self._authorize(path):
            return

        if path == "/api/health":
            self._send_json(200, self._health_payload())
            return

        parts = [p for p in path.split("/") if p]

        if path == "/api/capabilities":
            self._route_capabilities()
            return
        if parts[:2] == ["api", "automations"]:
            self._route_automations_get(parts[2:], query)
            return
        if parts[:2] == ["api", "prompts"]:
            self._route_prompts_get(parts[2:])
            return
        if parts[:2] == ["api", "runs"]:
            self._route_runs_get(parts[2:], query)
            return
        if parts[:2] == ["api", "turns"] and len(parts) == 3:
            self._route_turns_get(parts[2])
            return

        self._send_json(404, {"error": f"no such route: GET {path}"})

    def do_POST(self) -> None:  # stdlib method name
        path = urlsplit(self.path).path

        if not self._authorize(path):
            return

        try:
            body = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"malformed JSON body: {exc}"})
            return

        parts = [p for p in path.split("/") if p]

        if parts[:2] == ["api", "automations"] and len(parts) == 2:
            self._route_automations_create(body)
            return
        if parts[:3] == ["api", "automations", "validate"]:
            self._route_automations_validate(body)
            return
        if parts[:3] == ["api", "automations", "import"]:
            self._route_automations_import(body)
            return
        if (
            parts[:2] == ["api", "automations"]
            and len(parts) == 4
            and parts[3] == "enable"
        ):
            self._route_automations_enable(parts[2], body)
            return
        if (
            parts[:2] == ["api", "automations"]
            and len(parts) == 4
            and parts[3] == "run"
        ):
            self._route_automations_run(parts[2])
            return
        if parts[:2] == ["api", "turns"] and len(parts) == 2:
            self._route_turns_create(body)
            return

        self._send_json(404, {"error": f"no such route: POST {path}"})

    def do_PUT(self) -> None:  # stdlib method name
        path = urlsplit(self.path).path

        if not self._authorize(path):
            return

        try:
            body = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"malformed JSON body: {exc}"})
            return

        parts = [p for p in path.split("/") if p]

        if parts[:2] == ["api", "automations"] and len(parts) == 3:
            self._route_automations_update(parts[2], body)
            return
        if parts[:2] == ["api", "prompts"] and len(parts) == 3:
            self._route_prompts_put(parts[2], body)
            return

        self._send_json(404, {"error": f"no such route: PUT {path}"})

    def do_DELETE(self) -> None:  # stdlib method name
        path = urlsplit(self.path).path

        if not self._authorize(path):
            return

        parts = [p for p in path.split("/") if p]

        if parts[:2] == ["api", "automations"] and len(parts) == 3:
            self._route_automations_delete(parts[2])
            return

        self._send_json(404, {"error": f"no such route: DELETE {path}"})

    # ---- health ----

    def _health_payload(self) -> dict[str, Any]:
        """The engine's honest self-report.

        Three fields here each close a hole named in section 5:

        - ``scheduler_lock``: an engine that is up but holds no lock is a
          silent double-firing hazard. Reported, not assumed.
        - ``workspace``: at N>=2 engine instances per host, "which consumer
          is this one serving?" must be answerable without reading argv.
        - ``outbox``: the delivery worker is a thread inside another
          process; this readout is half of its only liveness surface.
        """
        state = self.server.scheduler_state
        runs_dir = self.server.ctx.runs_dir

        report = staleness_mod.check_staleness(self.server.staleness_service, runs_dir)
        worker = read_worker_cursor(runs_dir)
        outbox = engine_events.outbox_status(runs_dir, cursor=worker["cursor"])
        outbox["worker_cursor_source"] = worker["source"]
        outbox["worker_cursor_updated_at"] = worker.get("updated_at")
        outbox["worker_halt_reason"] = worker.get("halt_reason")
        # Name the field section 5 names, alongside engine_events' own key,
        # rather than making a reader translate between two vocabularies.
        outbox["worker_cursor_lag"] = outbox.get("lag_bytes")

        drain_request = drain.drain_state(runs_dir)

        return {
            "status": "ok",
            "service": "drumbeat serve",
            "version": _ENGINE_VERSION,
            "time": _iso_now(),
            "pid": os.getpid(),
            "workspace": str(self.server.workspace),
            "dirs": {
                "automations": str(self.server.ctx.automations_dir),
                "prompts": str(self.server.ctx.prompts_dir),
                "runs": str(runs_dir),
                "cwd": str(self.server.ctx.cwd),
            },
            "scheduler_lock": "held" if state.lock_held else "not_held",
            "scheduler_lock_path": state.lock_path
            or str(scheduler_lock_path(runs_dir)),
            "scheduler": {
                "scheduling": "draining" if drain_request else "active",
                "drain_reason": (
                    drain_request.get("reason") if drain_request else None
                ),
                "started_at": state.started_at,
                "last_tick_at": state.last_tick_at,
                "ticks": state.ticks,
                "runs_started": state.runs_started,
                "current_run": state.current_run,
                "last_run": state.last_run,
                "registered": state.registered,
                "load_failures": state.load_failures,
            },
            "doctor": {
                "status": report.status.upper(),
                "reason": report.reason,
                "pid": report.pid,
                "started_at": report.started_at,
                "changed": [c.path for c in report.changed],
            },
            "outbox": outbox,
        }

    # ---- capabilities ----

    def _route_capabilities(self) -> None:
        tools = capabilities.resolve_tools(self.server.ctx.automations_dir)
        # `path` is now the TURN path (declared pack bins + workspace bin +
        # the pinned base), not this process's ambient os.environ["PATH"].
        # Reporting our own environment here was the endpoint answering a
        # question nobody asked: what matters is what the AGENT can reach,
        # and before step 4 those two strings were only accidentally
        # related. The pinned base is reported separately under `packs`.
        pack_report = capabilities.resolve_packs(self.server.ctx.automations_dir)
        self._send_json(
            200,
            {
                "tools": tools,
                "count": len(tools),
                "timezone": capabilities.server_timezone(),
                "path": pack_report["turn_path"],
                "packs": pack_report,
            },
        )

    # ---- automations ----

    def _route_automations_get(
        self, rest: list[str], query: dict[str, list[str]]
    ) -> None:
        try:
            if not rest:
                items = management_api.list_automations(self.server.ctx)
                self._send_json(200, {"automations": items, "count": len(items)})
                return
            if len(rest) == 1:
                detail = management_api.get_automation_detail(rest[0], self.server.ctx)
                self._send_json(200, detail)
                return
            if len(rest) == 2 and rest[1] == "export":
                result = management_api.export_automation(rest[0], self.server.ctx)
                self._send_json(200, result)
                return
            if len(rest) == 2 and rest[1] == "runs":
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    self._send_json(400, {"error": "'limit' must be an integer"})
                    return
                items = management_api.list_runs(
                    limit=limit, automation_filter=rest[0], ctx=self.server.ctx
                )
                self._send_json(200, {"runs": items, "count": len(items)})
                return
            self._send_json(
                404, {"error": f"no such route: GET /api/automations/{'/'.join(rest)}"}
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_create(self, body: dict[str, Any]) -> None:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            self._send_json(
                400,
                {"error": "'content' is required and must be non-empty markdown text"},
            )
            return
        try:
            self._send_json(
                201, management_api.create_automation(content, self.server.ctx)
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_update(self, slug: str, body: dict[str, Any]) -> None:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            self._send_json(
                400,
                {"error": "'content' is required and must be non-empty markdown text"},
            )
            return
        try:
            self._send_json(
                200, management_api.update_automation(slug, content, self.server.ctx)
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_enable(self, slug: str, body: dict[str, Any]) -> None:
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "'enabled' is required and must be a bool"})
            return
        try:
            self._send_json(
                200,
                management_api.set_automation_enabled(slug, enabled, self.server.ctx),
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_run(self, slug: str) -> None:
        try:
            self._send_json(
                202, management_api.run_automation_async(slug, self.server.ctx)
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_validate(self, body: dict[str, Any]) -> None:
        content = body.get("content")
        if not isinstance(content, str):
            self._send_json(
                400, {"error": "'content' is required and must be a string"}
            )
            return
        try:
            self._send_json(
                200,
                management_api.validate_automation_content(content, self.server.ctx),
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_import(self, body: dict[str, Any]) -> None:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            self._send_json(
                400,
                {"error": "'content' is required and must be non-empty markdown text"},
            )
            return
        try:
            self._send_json(
                201, management_api.import_automation(content, self.server.ctx)
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_automations_delete(self, slug: str) -> None:
        try:
            management_api.delete_automation(slug, self.server.ctx)
            self._send_json(200, {"deleted": slug})
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    # ---- prompts ----

    def _route_prompts_get(self, rest: list[str]) -> None:
        try:
            if not rest:
                items = management_api.list_prompt_files(self.server.ctx)
                self._send_json(200, {"prompts": items, "count": len(items)})
                return
            if len(rest) == 1:
                self._send_json(
                    200, management_api.read_prompt_file(rest[0], self.server.ctx)
                )
                return
            self._send_json(
                404, {"error": f"no such route: GET /api/prompts/{'/'.join(rest)}"}
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    def _route_prompts_put(self, name: str, body: dict[str, Any]) -> None:
        content = body.get("content")
        if not isinstance(content, str):
            self._send_json(
                400, {"error": "'content' is required and must be a string"}
            )
            return
        try:
            self._send_json(
                200, management_api.write_prompt_file(name, content, self.server.ctx)
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)

    # ---- turns (decomposition step 5; docs/ARCHITECTURE.md) ----
    #
    # The consumer's reply path lands here. Section 7.3's split means this
    # handler knows nothing about notifications, items, or chat history --
    # it takes a session id (or an automation slug) and appends a turn.
    # Every refusal is the engine's own, named in the body: 400 malformed,
    # 404 unknown session/automation, 423 locked. Never a guess.

    def _handle_turn_error(self, exc: turns.TurnError) -> None:
        payload: dict[str, Any] = {"error": exc.message, **exc.extra}
        if exc.status == 423:
            # A machine-readable retry hint alongside the prose, so a
            # client re-offering the user's draft has a number rather than
            # a guess. The text itself was never accepted here -- see
            # turns.py's module docstring on the section 7.3 flag.
            self.send_response(423)
            body = json.dumps(payload).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", str(turns.RETRY_AFTER_SECONDS))
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(exc.status, payload)

    def _route_turns_create(self, body: dict[str, Any]) -> None:
        try:
            self._send_json(202, turns.submit_turn(body, self.server.ctx))
        except turns.TurnError as exc:
            self._handle_turn_error(exc)

    def _route_turns_get(self, turn_id: str) -> None:
        try:
            self._send_json(200, turns.get_turn(turn_id, self.server.ctx))
        except turns.TurnError as exc:
            self._handle_turn_error(exc)

    # ---- runs ----

    def _route_runs_get(self, rest: list[str], query: dict[str, list[str]]) -> None:
        try:
            if not rest:
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    self._send_json(400, {"error": "'limit' must be an integer"})
                    return
                items = management_api.list_runs(
                    limit=limit,
                    automation_filter=query.get("automation", [None])[0],
                    ctx=self.server.ctx,
                )
                self._send_json(200, {"runs": items, "count": len(items)})
                return
            if len(rest) == 2:
                slug, run_id = rest
                self._send_json(
                    200, management_api.get_run_detail(slug, run_id, self.server.ctx)
                )
                return
            if len(rest) == 3 and rest[2] == "stderr":
                slug, run_id, _ = rest
                self._send_json(
                    200, management_api.get_run_stderr(slug, run_id, self.server.ctx)
                )
                return
            self._send_json(
                404, {"error": f"no such route: GET /api/runs/{'/'.join(rest)}"}
            )
        except management_api.ManagementError as exc:
            self._handle_management_error(exc)


__all__ = [
    "WORKER_CURSOR_FILENAME",
    "EngineRequestHandler",
    "EngineServer",
    "read_worker_cursor",
]
