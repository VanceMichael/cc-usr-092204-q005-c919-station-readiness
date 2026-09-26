import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src import release
from src.catalog import load_context

DEFAULT_FLIGHT = "CZ2381"


class BadRequest(Exception):
    pass


def _json_response(handler: BaseHTTPRequestHandler, payload, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}

        try:
            if parts.path == "/health":
                _json_response(self, {"status": "ok"})
                return
            if parts.path == "/context":
                _json_response(self, load_context())
                return
            if parts.path == "/release":
                payload = self._release(query)
            elif parts.path == "/confirm":
                payload = self._confirm(query)
            elif parts.path == "/replay":
                payload = self._replay(query)
            elif parts.path == "/maintenance":
                payload = self._maintenance(query)
            else:
                self.send_error(404)
                return
        except LookupError as exc:
            _json_response(self, {"error": str(exc)}, status=404)
            return
        except BadRequest as exc:
            _json_response(self, {"error": str(exc)}, status=400)
            return

        _json_response(self, payload)

    def _release(self, query: dict[str, str]) -> dict:
        capability = release.load_json("capability.json")
        ops = release.load_json("operations.json")
        flight = release.get_flight(ops, query.get("flight", DEFAULT_FLIGHT))
        at = query.get("at")
        if at is None:
            raise BadRequest("必须提供 at 评估时刻，如 /release?flight=CZ2381&at=2026-09-26T07:40:00+08:00")
        return release.evaluate_release(flight, capability, ops, at)

    def _confirm(self, query: dict[str, str]) -> dict:
        ops = release.load_json("operations.json")
        flight_no = query.get("flight", DEFAULT_FLIGHT)
        release.get_flight(ops, flight_no)
        result = release.effective_confirmation(ops, flight_no)
        return {"flight_no": flight_no, "effective_confirmation": result}

    def _replay(self, query: dict[str, str]) -> dict:
        capability = release.load_json("capability.json")
        ops = release.load_json("operations.json")
        flight = release.get_flight(ops, query.get("flight", DEFAULT_FLIGHT))
        return release.replay(flight, capability, ops)

    def _maintenance(self, query: dict[str, str]) -> dict:
        ops = release.load_json("operations.json")
        doc_id = query.get("doc")
        role = query.get("role", "")
        if doc_id is None:
            raise BadRequest("必须提供 doc 与 role，如 /maintenance?doc=MRO-C919-2609-17&role=签派员")
        return release.view_maintenance_document(ops, doc_id, role)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
