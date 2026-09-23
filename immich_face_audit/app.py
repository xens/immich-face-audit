"""The web app: connect → data → references → flags → apply, all in the browser.

Binds to 127.0.0.1 only. Because it holds API keys and can write to Immich,
every request must carry the expected Host header (defeats DNS rebinding),
and every POST must carry an X-Face-Audit header, which a page on another
site cannot add without a CORS preflight this server never grants.

API keys are written to <workdir>/.env (0600) and never sent back to the browser.
The only Immich call proxied for the browser is GET /api/assets/<uuid>/thumbnail.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import time
import webbrowser
from importlib import resources
from pathlib import Path

from . import apply, baseline, extract, score
from .config import (BACKUP_KEY_PERMISSIONS, MAIN_KEY_PERMISSIONS, Immich, ImmichError, Workdir,
                     missing_permissions, write_env)
from .jobs import Busy, Jobs

CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
THUMB = re.compile(r"^/api/assets/[0-9a-f-]{36}/thumbnail\?size=(thumbnail|preview)$")
PAGES = {"/connect": "connect.html", "/data": "data.html", "/references": "references.html",
         "/flags": "flags.html", "/apply": "apply.html"}
ASSETS = {"/static/app.css": ("app.css", "text/css"), "/static/app.js": ("app.js", "text/javascript")}
CSRF_HEADER = "X-Face-Audit"


class App:
    """Everything the handler needs, independent of HTTP."""

    def __init__(self, wd: Workdir) -> None:
        self.wd = wd
        self.jobs = Jobs()
        self.reload_clients()

    def reload_clients(self) -> None:
        self.immich = Immich.from_env(required=False)
        self.backup = Immich.from_env(required=False, key_var="IMMICH_BACKUP_API_KEY")

    # --- connect ---------------------------------------------------------------
    def connection(self) -> dict:
        return {"url": os.environ.get("IMMICH_URL", ""),
                "main": self._key_report(self.immich, MAIN_KEY_PERMISSIONS),
                "backup": self._key_report(self.backup, BACKUP_KEY_PERMISSIONS)}

    @staticmethod
    def _key_report(client: Immich | None, need: list[str]) -> dict:
        if not client:
            return {"set": False, "need": need}
        try:
            info = client.key_info()
        except (ImmichError, OSError) as e:
            return {"set": True, "ok": False, "need": need, "error": str(e)[:200]}
        have = info.get("permissions", [])
        missing = missing_permissions(have, need)
        return {"set": True, "ok": not missing, "name": info.get("name", ""), "permissions": have,
                "missing": missing, "need": need}

    def connect(self, body: dict) -> dict:
        url = (body.get("url") or os.environ.get("IMMICH_URL", "")).strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", url):
            raise ValueError("Immich URL must look like http://host:port")
        values: dict[str, str | None] = {"IMMICH_URL": url}
        for field, var, need in (("key", "IMMICH_API_KEY", MAIN_KEY_PERMISSIONS),
                                 ("backupKey", "IMMICH_BACKUP_API_KEY", BACKUP_KEY_PERMISSIONS)):
            key = (body.get(field) or "").strip()
            if key:
                report = self._key_report(Immich(url, key), need)
                if not report.get("ok", False) and "error" in report:
                    raise ValueError(f"{var}: Immich rejected the key or is unreachable ({report['error']})")
                values[var] = key
        if body.get("forgetBackup"):
            values["IMMICH_BACKUP_API_KEY"] = None
        write_env(self.wd.env, values)
        for k, v in values.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self.reload_clients()
        return self.connection()

    # --- status ----------------------------------------------------------------
    def _backfill_meta(self) -> dict:
        """Folders from before meta.json: derive stats and times from the files once."""
        wd, m = self.wd, self.wd.read_json(self.wd.meta, {})
        new = {}
        if wd.faces.exists() and "extract" not in m:
            with open(wd.faces) as f:
                rows = f.read().count("\n") - 1
            with open(wd.faces) as f:
                unassigned = sum(1 for line in f if line.split("\t")[2:3] == [""])
            with open(wd.people) as f:
                named = sum(1 for line in list(f)[1:] if line.split("\t")[1:2] != [""])
            new |= {"extract": {"backup": "(earlier run)", "faces": rows, "named": named, "unassigned": unassigned},
                    "extract_at": wd.faces.stat().st_mtime}
        if wd.baseline.exists() and "baseline" not in m:
            with open(wd.baseline) as f:
                refs = sum(1 for line in f if line.rstrip("\n").endswith("\treference"))
            new |= {"baseline": {"references": refs}, "baseline_at": wd.baseline.stat().st_mtime}
        if wd.flags.exists() and "score" not in m:
            new |= {"score": {}, "score_at": wd.flags.stat().st_mtime}
        if new:
            wd.update_meta(**new)
            m |= new
        return m

    def status(self) -> dict:
        wd, m = self.wd, self._backfill_meta()
        rej = wd.read_json(wd.rejections, {})
        dec = wd.read_json(wd.decisions, {})
        rej_at = wd.rejections.stat().st_mtime if wd.rejections.exists() else 0
        flags_at = m.get("score_at", 0)
        return {
            "workdir": str(wd.root.resolve()),
            "connected": bool(self.immich),
            "backupKey": bool(self.backup),
            "data": m.get("extract") if wd.faces.exists() else None,
            "references": (m.get("baseline") or {}) | {"built": True} if wd.baseline.exists() else None,
            "referencesStale": wd.baseline.exists() and m.get("baseline_at", 0) < m.get("extract_at", 0),
            "reviewedPeople": len(rej.get("reviewed", {})),
            "flags": (m.get("score") or {}) | {"computed": True} if wd.flags.exists() else None,
            "flagsStale": wd.flags.exists() and flags_at < max(m.get("baseline_at", 0), rej_at),
            "decisions": len(dec),
            "applied": len(apply._live(wd)),
            "job": self.jobs.snapshot(),
        }

    def first_step(self) -> str:
        s = self.status()
        if not s["connected"]:
            return "/connect"
        if not s["data"]:
            return "/data"
        if not s["flags"]:
            return "/references"
        return "/flags"

    # --- jobs ------------------------------------------------------------------
    def start(self, kind: str, body: dict) -> dict:
        wd = self.wd
        if kind == "extract":
            source = body.get("source")
            if source == "latest" and not self.backup and not self.immich:
                raise ValueError("connect to Immich first")
            path = Path(os.path.expanduser(body.get("path") or ""))
            if source == "file" and not path.is_file():
                raise ValueError(f"no such file: {path}")

            def run(job):
                if source == "latest":
                    stats = extract.extract_from_immich(self.backup or self.immich, wd, job.log, job.set_progress)
                else:
                    stats = extract.extract(path, wd, job.log, job.set_progress)
                if self.immich:
                    job.log("refreshing names and birth dates from Immich ...")
                    stats["people_updated"] = extract.refresh_people(wd, self.immich, job.log)
                wd.update_meta(extract=stats, extract_at=time.time())
                job.log("building references ...")
                job.progress = None
                b = baseline.build(wd, job.log)
                wd.update_meta(baseline=b, baseline_at=time.time())
                return {"extract": stats, "baseline": b}
        elif kind == "baseline":
            def run(job):
                b = baseline.build(wd, job.log)
                wd.update_meta(baseline=b, baseline_at=time.time())
                return b
        elif kind == "score":
            def run(job):
                s = score.score(wd, job.log)
                wd.update_meta(score=s, score_at=time.time())
                return s
        elif kind in ("apply", "undo"):
            if not self.immich:
                raise ValueError("connect to Immich first")
            limit = int(body.get("limit") or 0)
            undo = kind == "undo"

            def run(job):
                faces = None
                if not undo:  # only what the preview says is still to do; each face is re-checked live
                    p = apply.preview(wd)
                    faces = [r["face"] for r in p["pending"] + p["unknown"]]
                return apply.run(wd, self.immich, undo=undo, write=True, limit=limit, faces=faces,
                                 log=job.log, progress=job.set_progress)
        else:
            raise ValueError(f"unknown job {kind}")
        return self.jobs.start(kind, run)


def serve(wd: Workdir, port: int = 8091, open_browser: bool = True) -> None:
    app = App(wd)
    states = {"rejections": wd.rejections, "decisions": wd.decisions}
    lock = threading.Lock()
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    static = resources.files("immich_face_audit.static")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _guard(self, post: bool) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "unexpected Host header"})
                return False
            if post and self.headers.get(CSRF_HEADER) != "1":
                self._json(403, {"error": f"missing {CSRF_HEADER} header"})
                return False
            return True

        def do_GET(self):
            if not self._guard(post=False):
                return
            p = self.path
            if p == "/":
                self.send_response(302)
                self.send_header("Location", app.first_step())
                self.send_header("Content-Length", "0")
                return self.end_headers()
            if p in PAGES:
                return self._send(200, static.joinpath(PAGES[p]).read_bytes(), "text/html; charset=utf-8")
            if p in ASSETS:
                name, ctype = ASSETS[p]
                return self._send(200, static.joinpath(name).read_bytes(), ctype)
            if THUMB.match(p):
                if not app.immich:
                    return self._json(503, {"error": "not connected"})
                try:
                    body, ctype = app.immich.request("GET", p)
                    return self._send(200, body, ctype or "image/jpeg", cache="private, max-age=86400")
                except ImmichError as e:
                    return self._json(502, {"error": str(e)})
            routes = {
                "/audit/status": app.status,
                "/audit/config": lambda: {"immich": os.environ.get("IMMICH_URL", "")},
                "/audit/connect": app.connection,
                "/audit/references": lambda: baseline.references_for_review(wd) if wd.baseline.exists() else [],
                "/audit/flags": lambda: wd.read_json(wd.flags, None),
                "/audit/apply-preview": lambda: apply.preview(wd),
                "/audit/job": app.jobs.snapshot,
            }
            if p in routes:
                return self._json(200, routes[p]())
            if p.startswith("/audit/state/") and p[13:] in states:
                return self._json(200, wd.read_json(states[p[13:]], {}))
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard(post=True):
                return
            p = self.path
            try:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                body = json.loads(raw) if raw else {}
                if p.startswith("/audit/state/") and p[13:] in states:
                    with lock:
                        wd.write_json(states[p[13:]], body)
                    return self._json(200, {})
                if p == "/audit/connect":
                    return self._json(200, app.connect(body))
                if p.startswith("/audit/jobs/"):
                    return self._json(200, app.start(p[12:], body))
            except Busy as e:
                return self._json(409, {"error": str(e)})
            except (ValueError, ImmichError, OSError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def _json(self, code: int, data) -> None:
            self._send(code, json.dumps(data, separators=(",", ":")).encode(), "application/json")

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            try:
                self.wfile.write(body)
            except CLIENT_GONE:
                pass  # the browser cancelled it (lazy images scrolled away, page switched)

        def log_message(self, fmt, *a):
            if a and not str(a[1]).startswith(("2", "3")):
                print(" ", *a)

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            if not isinstance(sys.exc_info()[1], CLIENT_GONE):
                super().handle_error(request, client_address)

    server = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"immich-face-audit: {url}   (work folder: {wd.root}, Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
