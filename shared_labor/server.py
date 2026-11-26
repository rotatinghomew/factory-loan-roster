from __future__ import annotations

import json
import os
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import LaborError, UnauthorizedError
from .service import Service

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "labor_20261203.json"


def viewer_from_request(handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> dict[str, Any]:
    headers = handler.headers
    actor_type = headers.get("X-Actor-Type") or first(query, "actor_type")
    actor_id = headers.get("X-Actor-Id") or first(query, "actor_id")
    factory = headers.get("X-Actor-Factory") or first(query, "factory") or first(query, "as_factory")
    person = headers.get("X-Actor-Person") or first(query, "person") or first(query, "as_person")
    actor = first(query, "as")
    if factory:
        return {"type": "factory", "id": factory}
    if person:
        return {"type": "person", "id": person}
    if actor_type and actor_id:
        return {"type": actor_type, "id": actor_id}
    if actor:
        # Convenience for local curl: W... is a worker, other IDs are factories.
        return {"type": "person" if actor.startswith("W") else "factory", "id": actor}
    if actor_type == "admin":
        return {"type": "admin", "id": None}
    # The path/query id is used as the local demo caller when no explicit
    # authentication headers are supplied.  Explicit mismatched headers are
    # still rejected by the service layer.
    return {"type": "anonymous", "id": None}


def first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def create_handler(service: Service):
    class LaborHandler(BaseHTTPRequestHandler):
        server_version = "SharedLabor/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep unittest output clean; errors are still returned as JSON.
            if getattr(service, "verbose_http", False):
                super().log_message(fmt, *args)

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LaborError("Request body must be UTF-8 JSON", code="invalid_json", status_code=400) from exc
            if not isinstance(value, dict):
                raise LaborError("Request body must be a JSON object", code="invalid_json")
            return value

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._handle("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._handle("PATCH")

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                # Body-supplied actor is useful in integration tests.
                viewer = body.pop("viewer", None) or body.pop("actor", None)
                if isinstance(viewer, str):
                    viewer = Service.viewer(viewer)
                if viewer is None and path not in ("/health", "/api", "/"):
                    viewer = viewer_from_request(self, query)
                at = first(query, "at") or body.get("at")
                with service.lock:
                    result = route(method, path, query, body, viewer or {"type": "admin"}, service, at)
                if result is None:
                    raise LaborError(f"No route for {method} {path}", code="not_found", status_code=404)
                created = bool(isinstance(result, dict) and result.pop("_created", False))
                status = 201 if method == "POST" and created else 200
                if isinstance(result, tuple):
                    status, result = result
                self._send(status, result)
            except LaborError as exc:
                self._send(exc.status_code, exc.to_dict())
            except (KeyError, ValueError, TypeError) as exc:
                self._send(400, {"error": "validation_error", "message": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send(500, {"error": "internal_error", "message": str(exc)})

    return LaborHandler


def route(method, path, query, body, viewer, service: Service, at):
    # Common singular aliases.
    if path.startswith("/api/worker/"):
        path = "/api/workers/" + path[len("/api/worker/") :]
    elif path.startswith("/api/factory/"):
        path = "/api/factories/" + path[len("/api/factory/") :]

    # Meta and health -------------------------------------------------------
    if method == "GET" and path == "/health":
        return {"ok": True, "service": "shared-labor", "clock": service.now_label(at)}
    if method == "GET" and path in ("/api", "/"):
        return {
            "name": "shared-labor backend",
            "endpoints": [
                "/api/workers/{id}/record",
                "/api/factories/{id}",
                "/api/shifts",
                "/api/shifts/{id}/staffing",
                "/api/availability",
                "/api/consents",
                "/api/recalls",
                "/api/attendance",
                "/api/pay-acks",
                "/api/settlement",
                "/api/disputes",
            ],
        }

    # Workers ---------------------------------------------------------------
    if path == "/api/workers" and method == "GET":
        return {"workers": [service._public(p) for p in service.db["people"]]}
    if path == "/api/workers" and method == "POST":
        return service.add_person(body, viewer)
    if path == "/api/me" and method == "GET":
        person = first(query, "person") or (viewer or {}).get("id")
        return service.worker_record(person, viewer, at)
    if path.startswith("/api/workers/"):
        parts = path.split("/")
        # /api/workers/W1 or /api/workers/W1/record
        person_id = parts[3]
        caller = {"type": "person", "id": person_id} if viewer.get("type") == "anonymous" else viewer
        if len(parts) == 4 and method == "GET":
            return service.worker_record(person_id, caller, at)
        if len(parts) == 5 and parts[4] in ("record", "dashboard") and method == "GET":
            return service.worker_record(person_id, caller, at)
        if len(parts) == 5 and parts[4] == "skills" and method in ("POST", "PUT", "PATCH"):
            return service.set_skills(person_id, body.get("skills", []), caller)

    # Factories -------------------------------------------------------------
    if path == "/api/factories" and method == "GET":
        return {"factories": service.list_factories(viewer)}
    if path.startswith("/api/factories/"):
        factory_id = path.split("/")[3]
        caller = {"type": "factory", "id": factory_id} if viewer.get("type") == "anonymous" else viewer
        if len(path.split("/")) == 4 and method == "GET":
            return service.factory_board(factory_id, caller, at)

    # Availability ----------------------------------------------------------
    if path == "/api/availability" and method == "GET":
        person = first(query, "person")
        rows = service.db["availability"]
        if person:
            rows = [a for a in rows if a["person"] == person]
        factory = first(query, "factory")
        if factory:
            service.require_factory(factory, viewer)
            rows = [a for a in rows if a.get("factory") == factory]
        return {"availability": [service._public(a) for a in rows]}
    if path == "/api/availability" and method == "POST":
        return service.add_availability(body, viewer)
    if path.startswith("/api/availability/") and path.endswith("/revoke") and method == "POST":
        availability_id = path.split("/")[3]
        return service.revoke_availability(availability_id, body, viewer)
    if path == "/api/commitments/revoke-uncommitted" and method == "POST":
        return service.revoke_uncommitted(body, viewer)
    if path == "/api/consent-requests" and method == "GET":
        rows = service.db["consent_requests"]
        person = first(query, "person")
        if person:
            rows = [r for r in rows if r["person"] == person]
        return {"consent_requests": [service._public(r) for r in rows]}

    # Shifts ----------------------------------------------------------------
    if path == "/api/shifts" and method == "GET":
        factory = first(query, "factory")
        rows = service.db["shifts"]
        if factory:
            rows = [s for s in rows if s["host"] == factory]
        if first(query, "readiness") in ("1", "true", "yes"):
            return {"shifts": [service.get_shift_report(s["id"], at, viewer) for s in rows]}
        return {"shifts": [service._public(s) for s in rows]}
    if path == "/api/shifts" and method == "POST":
        result = service.create_shift(body, viewer)
        result["_created"] = True
        return result
    if path.startswith("/api/shifts/"):
        parts = path.split("/")
        shift_id = parts[3]
        if len(parts) == 4 and method == "GET":
            return service.get_shift_report(shift_id, at, viewer)
        if len(parts) == 5 and parts[4] in ("staffing", "readiness") and method == "GET":
            return service.staffing(shift_id, at)
        if len(parts) == 5 and parts[4] == "terms" and method == "POST":
            return service.revise_terms(shift_id, body, viewer)
        if len(parts) == 5 and parts[4] in ("ask", "requests") and method == "POST":
            return service.ask_consent(shift_id, body, viewer)

    # Consents --------------------------------------------------------------
    if path == "/api/consents" and method == "GET":
        person = first(query, "person")
        return {"consents": service.list_consents(viewer, person)}
    if path == "/api/consents" and method == "POST":
        return service.record_consent(body, viewer)

    # Recalls ---------------------------------------------------------------
    if path == "/api/recalls" and method == "GET":
        return {"recalls": service.list_recalls(viewer, at)}
    if path == "/api/recalls" and method == "POST":
        result = service.request_recall(body, viewer)
        return {"_created": True, **result}
    if path == "/api/recalls/process-due" and method == "POST":
        return service.process_due_recalls(body.get("at") or at, viewer)
    if path.startswith("/api/recalls/"):
        parts = path.split("/")
        recall_id = parts[3]
        if len(parts) == 4 and method == "GET":
            return service.recall_view(recall_id, at)
        if len(parts) == 5 and parts[4] == "worker-response" and method == "POST":
            return service.worker_respond_recall(recall_id, body, viewer)
        if len(parts) == 5 and parts[4] == "host-confirmation" and method == "POST":
            return service.host_confirm_recall(recall_id, body, viewer)
        if len(parts) == 5 and parts[4] == "replacement-request" and method == "POST":
            return service.request_replacement(recall_id, body, viewer)
        if len(parts) == 5 and parts[4] == "retry" and method == "POST":
            return service.retry_replacement_request(recall_id, viewer)
        if len(parts) == 5 and parts[4] == "force-stop" and method == "POST":
            return service.force_stop_recall(recall_id, body, viewer, at=body.get("at") or at)

    # Attendance ------------------------------------------------------------
    if path == "/api/attendance" and method == "GET":
        person = first(query, "person")
        shift = first(query, "shift")
        if person:
            return service.attendance_view(person, shift, viewer)
        return {"attendance": [service._public(a) for a in service.db["attendance"]]}
    if path == "/api/attendance" and method == "POST":
        return service.add_attendance(body, viewer)

    # Pay -------------------------------------------------------------------
    if path == "/api/pay-acks" and method == "GET":
        return {"pay_ack": service.list_pay_acks(viewer, first(query, "person"))}
    if path == "/api/pay-acks" and method == "POST":
        return service.add_pay_ack(body, viewer)
    if path == "/api/settlement" and method == "GET":
        return service.settlement(
            person_id=first(query, "person"),
            shift_id=first(query, "shift"),
            factory=first(query, "factory"),
            viewer=viewer,
        )
    if path == "/api/pay-difference" and method == "POST":
        return service.pay_difference(
            body["person"], body["shift"], body.get("revised_interval"), body.get("revised_amount")
        )

    # Disputes / shortfalls / liabilities ----------------------------------
    if path == "/api/disputes" and method == "GET":
        return {"disputes": service.list_disputes(viewer)}
    if path.startswith("/api/disputes/"):
        dispute_id = path.split("/")[3]
        if len(path.split("/")) == 5 and path.split("/")[4] == "check" and method == "POST":
            return service.check_dispute(dispute_id, body, viewer)
        if len(path.split("/")) == 5 and path.split("/")[4] == "resolve" and method == "POST":
            return service.resolve_dispute(dispute_id, body, viewer)
    if path == "/api/shortfalls" and method == "GET":
        return {"shortfalls": [service._public(x) for x in service.db["shortfalls"]]}
    if path == "/api/liabilities" and method == "GET":
        return {"liabilities": [service._public(x) for x in service.db["liabilities"]]}
    if path.startswith("/api/liabilities/") and path.endswith("/ack") and method == "POST":
        liability_id = path.split("/")[3]
        return service.acknowledge_liability(liability_id, body, viewer)

    return None


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    *,
    data: dict[str, Any] | None = None,
    seed_path: str | Path | None = DEFAULT_DATA,
    db_path: str | Path | None = None,
    service: Service | None = None,
    clock: str | None = None,
):
    if service is None:
        if db_path and Path(db_path).exists():
            service = Service.from_file(db_path, clock=clock)
        else:
            seed = data
            if seed is None and seed_path and Path(seed_path).exists():
                seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
            service = Service(seed, clock=clock, persist_path=db_path)
    handler = create_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service
    return httpd


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Shared-labor recall and settlement backend")
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    parser.add_argument("--host", default=os.environ.get("HOST", default_host))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--seed", default=str(DEFAULT_DATA))
    parser.add_argument("--db", default=None, help="optional JSON state file")
    parser.add_argument("--clock", default=None, help="fixed park clock HH:MM for demos")
    args = parser.parse_args(argv)
    httpd = create_server(args.host, args.port, seed_path=args.seed, db_path=args.db, clock=args.clock)
    print(f"Shared-labor backend listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
