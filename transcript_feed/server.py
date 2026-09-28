"""A tiny stdlib HTTP server: serve the page, and regenerate the feed on every request.

No framework, no template engine, no dependency. Binds loopback by default — this shows
one local transcript; it is not an authenticated public endpoint, and it deliberately
serves nothing but the page and the feed.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_INDEX = Path(__file__).parent / "web" / "index.html"


def make_handler(composer, index_path=WEB_INDEX):
    index_bytes = index_path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                self._send(200, index_bytes, "text/html; charset=utf-8")
            elif route == "/feed.json":
                body = json.dumps(composer.build()).encode("utf-8")
                self._send(200, body, "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        do_HEAD = do_GET

        def log_message(self, *args):
            pass  # quiet by default

    return Handler


def serve(composer, host="127.0.0.1", port=8000, index_path=WEB_INDEX):
    httpd = ThreadingHTTPServer((host, port), make_handler(composer, index_path))
    print("claude-transcript-feed serving %s on http://%s:%d  (Ctrl-C to stop)"
          % (composer.path, host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        httpd.server_close()
