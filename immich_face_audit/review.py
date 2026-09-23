"""Local web app for the two manual steps: validating references and
deciding on flags. Binds to 127.0.0.1 only.

The only Immich call it proxies is GET /api/assets/<uuid>/thumbnail, with the
API key added server-side so it never reaches the browser.
"""
from __future__ import annotations

import http.server
import json
import re
import threading
import traceback
import webbrowser
from importlib import resources

from . import baseline, score
from .config import Immich, ImmichError, Workdir

THUMB = re.compile(r"^/api/assets/[0-9a-f-]{36}/thumbnail\?size=(thumbnail|preview)$")
PAGES = {"/": "references.html", "/references": "references.html", "/flags": "flags.html"}


def serve(wd: Workdir, immich: Immich, port: int = 8091, open_browser: bool = True) -> None:
    states = {"rejections": wd.rejections, "decisions": wd.decisions}
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            p = self.path
            if p in PAGES:
                body = resources.files("immich_face_audit.static").joinpath(PAGES[p]).read_bytes()
                return self._send(200, body, "text/html; charset=utf-8")
            if THUMB.match(p):
                try:
                    body, ctype = immich.request("GET", p)
                    return self._send(200, body, ctype or "image/jpeg", cache="private, max-age=86400")
                except ImmichError as e:
                    return self._json(502, {"error": str(e)})
            if p == "/audit/config":
                return self._json(200, {"immich": immich.base})
            if p == "/audit/references":
                return self._json(200, baseline.references_for_review(wd))
            if p == "/audit/flags":
                return self._json(200, wd.read_json(wd.flags, None))
            if p.startswith("/audit/state/") and p[13:] in states:
                return self._json(200, wd.read_json(states[p[13:]], {}))
            self._json(404, {"error": "not found"})

        def do_POST(self):
            p = self.path
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
            if p.startswith("/audit/state/") and p[13:] in states:
                with lock:
                    wd.write_json(states[p[13:]], json.loads(body))
                return self._json(200, {})
            if p == "/audit/score":
                try:
                    with lock:
                        return self._json(200, score.score(wd, log=lambda *_: None))
                except (Exception, SystemExit) as e:  # surface it in the UI rather than a dead request
                    traceback.print_exc()
                    return self._json(500, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def _json(self, code: int, data) -> None:
            self._send(code, json.dumps(data, separators=(",", ":")).encode(), "application/json")

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            if a and not str(a[1]).startswith(("2", "3")):
                print(" ", *a)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"review app: {url}   (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
