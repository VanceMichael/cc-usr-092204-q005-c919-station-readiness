import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.catalog import load_context
from src.release import (
    ReleaseError,
    arbitrate,
    dispatcher_view,
    load_release,
    redact_for_role,
    timeline,
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                payload = {"status": "ok"}
            elif parsed.path == "/context":
                payload = load_context()
            elif parsed.path == "/release":
                packet = load_release()
                role = query.get("role", ["dispatcher"])[0]
                payload = redact_for_role(dispatcher_view(packet), role)
            elif parsed.path == "/confirmations":
                packet = load_release()
                payload = {"confirmations": arbitrate(packet["confirmations"])}
            elif parsed.path == "/timeline":
                packet = load_release()
                stream = query.get("stream", [None])[0]
                payload = {"events": timeline(packet["events"], stream)}
            else:
                self.send_error(404)
                return
        except ReleaseError as exc:
            self._send_json({"error": str(exc)}, status=422)
            return

        self._send_json(payload)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
