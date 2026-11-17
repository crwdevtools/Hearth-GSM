"""A stand-in panel for testing the start scripts without Docker: answers /api/auth/state.

    python tools/ci/stub_panel.py [port]
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path != "/api/auth/state":
            self.send_error(404)
            return
        body = json.dumps({"needs_setup": True, "user": None, "setup_allowed": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8000), Handler).serve_forever()
