"""The web app end to end, against a fake Immich over real HTTP:
connect → extract (backup download) → score → apply → undo, plus its guards."""
from __future__ import annotations

import http.client
import http.server
import json
import re
import socket
import threading
import time
import urllib.request

import pytest

from immich_face_audit import app as app_mod
from immich_face_audit.config import BACKUP_KEY_PERMISSIONS, MAIN_KEY_PERMISSIONS, Workdir

KEYS = {"main-key": MAIN_KEY_PERMISSIONS, "backup-key": BACKUP_KEY_PERMISSIONS}
BACKUP = "immich-db-backup-20260922T020000-v3.2.0-pg14.19.sql.gz"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeImmich:
    """Just enough of the Immich API: key info, backups, people, faces, thumbnails."""

    def __init__(self, library, thumb_size: int = 2000) -> None:
        lib = library["lib"]
        self.dump = library["dump"].read_bytes()
        self.faces = {f: [a, p] for f, a, p in lib.faces}  # face -> [asset, person]
        self.people = [{"id": p, "name": n, "birthDate": b or None, "isHidden": False} for p, n, b in lib.people]
        self.thumb = b"\xff" * thumb_size
        self.created = 0
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def _out(self, code, body, ctype="application/json"):
                body = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _perm(self, perm):
                have = KEYS.get(self.headers.get("x-api-key"))
                if have is None:
                    self._out(401, {"message": "Invalid API key"})
                    return False
                if perm and perm not in have:
                    self._out(403, {"message": f"Missing required permission: {perm}"})
                    return False
                return True

            def do_GET(self):
                p = self.path
                if p == "/api/api-keys/me":
                    if self._perm(None):
                        self._out(200, {"id": "k", "name": "test", "permissions": KEYS[self.headers["x-api-key"]]})
                elif p == "/api/admin/database-backups":
                    if self._perm("maintenance"):
                        self._out(200, {"backups": [{"filename": BACKUP, "filesize": len(fake.dump), "timezone": "UTC"}]})
                elif p == f"/api/admin/database-backups/{BACKUP}":
                    if self._perm("backup.download"):
                        self._out(200, fake.dump, "application/octet-stream")
                elif p.startswith("/api/people?"):
                    if self._perm("person.read"):
                        self._out(200, {"people": fake.people, "hasNextPage": False})
                elif p.startswith("/api/faces?id="):
                    if self._perm("face.read"):
                        asset = p.split("=", 1)[1]
                        self._out(200, [{"id": f, "person": {"id": per} if per else None}
                                        for f, (a, per) in fake.faces.items() if a == asset])
                elif re.match(r"^/api/assets/[0-9a-f-]{36}/thumbnail", p):
                    if self._perm("asset.view"):
                        self._out(200, fake.thumb, "image/jpeg")
                else:
                    self._out(404, {"message": "nope"})

            def do_PUT(self):
                m = re.match(r"^/api/faces/([^/]+)$", self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if m and self._perm("face.update"):
                    fake.faces[body["id"]][1] = m.group(1)
                    self._out(200, {})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/api/people" and self._perm("person.create"):
                    fake.created += 1
                    self._out(201, {"id": f"00000000-0000-0000-0000-{fake.created:012d}"})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def req(self, method: str, path: str, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json", **(headers or {})}
        c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheader("Location"), data

    def get(self, path):
        status, _, data = self.req("GET", path)
        assert status == 200, (path, status, data[:200])
        return json.loads(data)

    def post(self, path, body=None):
        status, _, data = self.req("POST", path, body or {}, {"X-Face-Audit": "1"})
        assert status == 200, (path, status, data[:200])
        return json.loads(data)

    def job(self, kind, body=None):
        self.post(f"/audit/jobs/{kind}", body)
        for _ in range(300):
            j = self.get("/audit/job")
            if j["state"] != "running":
                assert j["state"] == "done", j
                return j["result"]
            time.sleep(0.1)
        raise AssertionError("job did not finish")


@pytest.fixture
def running(library, tmp_path, monkeypatch):
    for var in ("IMMICH_URL", "IMMICH_API_KEY", "IMMICH_BACKUP_API_KEY"):
        monkeypatch.setenv(var, "")  # isolated from the developer's env; restored afterwards
    fake = FakeImmich(library)
    wd = Workdir(tmp_path / "app")
    port = free_port()
    threading.Thread(target=app_mod.serve, args=(wd, port, False), daemon=True).start()
    client = Client(port)
    for _ in range(50):
        try:
            client.req("GET", "/audit/status")
            break
        except OSError:
            time.sleep(0.1)
    yield client, fake, wd, library
    fake.server.shutdown()


def test_full_flow_in_the_app(running):
    c, fake, wd, library = running

    # nothing configured: the app starts at step 1
    assert c.req("GET", "/")[1] == "/connect"

    conn = c.post("/audit/connect", {"url": fake.url, "key": "main-key", "backupKey": "backup-key"})
    assert conn["main"]["ok"] and conn["backup"]["ok"]
    assert oct(wd.env.stat().st_mode & 0o777) == "0o600"
    assert "main-key" in wd.env.read_text()
    status, _, raw = c.req("GET", "/audit/connect")
    assert b"main-key" not in raw and b"backup-key" not in raw  # keys never go back to the browser
    assert c.req("GET", "/")[1] == "/data"

    # step 2: backup download + extract + references, in one job
    r = c.job("extract", {"source": "latest"})
    assert r["extract"]["backup"] == BACKUP and r["extract"]["faces"] == 123
    assert r["baseline"]["references"] > 0
    assert c.req("GET", "/")[1] == "/references"
    assert c.get("/audit/references")

    # step 3 → 4
    s = c.job("score")
    assert s["swap"] == 1
    st = c.get("/audit/status")
    assert st["flags"] and not st["flagsStale"]
    card = next(card for g in c.get("/audit/flags") for card in g["cards"] if card["reason"] == "swap")
    c.post("/audit/state/decisions", {card["face"][0]: {
        "action": "accept", "face": card["face"][0], "asset": card["face"][1], "from": card["a"], "to": card["s"],
        "fromName": card["aName"], "toName": card["sName"], "reason": card["reason"]}})

    # step 5: preview, apply, undo
    p = c.get("/audit/apply-preview")
    assert len(p["pending"]) == 1 and p["undoable"] == 0
    alice, bob, _ = library["ids"]
    assert fake.faces[library["swapped"]][1] == alice
    assert c.job("apply")["done"] == 1
    assert fake.faces[library["swapped"]][1] == bob
    p = c.get("/audit/apply-preview")
    assert not p["pending"] and p["undoable"] == 1
    assert c.job("apply")["done"] == 0  # nothing left: never re-applies
    assert c.job("undo")["done"] == 1
    assert fake.faces[library["swapped"]][1] == alice

    # the thumbnail proxy uses the saved key
    asset = card["face"][1]
    status, _, body = c.req("GET", f"/api/assets/{asset}/thumbnail?size=thumbnail")
    assert status == 200 and body == fake.thumb


def test_guards(running):
    c, fake, wd, _ = running
    # a page on another site can't POST here: no custom header → refused
    assert c.req("POST", "/audit/connect", {"url": fake.url, "key": "main-key"})[0] == 403
    # DNS rebinding: an unexpected Host header is refused, even for reads
    assert c.req("GET", "/audit/status", host="evil.example:8091")[0] == 403
    assert not wd.env.exists()
    # a wrong key is rejected and nothing is saved
    status, _, body = c.req("POST", "/audit/connect", {"url": fake.url, "key": "nope"}, {"X-Face-Audit": "1"})
    assert status == 400 and b"rejected" in body and not wd.env.exists()
    # applying without a connection is refused
    assert c.req("POST", "/audit/jobs/apply", {}, {"X-Face-Audit": "1"})[0] == 400
    # only thumbnails are proxied
    c.post("/audit/connect", {"url": fake.url, "key": "main-key"})
    assert c.req("GET", "/api/people?page=1")[0] == 404


def test_missing_permissions_are_reported(running, monkeypatch):
    c, fake, wd, _ = running
    KEYS["narrow-key"] = ["asset.view"]
    try:
        r = c.post("/audit/connect", {"url": fake.url, "key": "narrow-key"})
    finally:
        del KEYS["narrow-key"]
    assert r["main"]["ok"] is False
    assert "face.update" in r["main"]["missing"]


def test_cancelled_thumbnails_print_nothing(running, capfd):
    """Browsers cancel lazy image loads all the time; that must not print tracebacks."""
    c, fake, wd, _ = running
    fake.thumb = b"x" * 8_000_000
    c.post("/audit/connect", {"url": fake.url, "key": "main-key"})
    path = "/api/assets/00000000-0000-0000-0000-000000000000/thumbnail?size=preview"
    for _ in range(5):  # ask for a big image, then hang up without reading it
        s = socket.create_connection(("127.0.0.1", c.port))
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{c.port}\r\n\r\n".encode())
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()
    time.sleep(1)
    status, _, body = c.req("GET", path)
    assert status == 200 and len(body) == 8_000_000
    assert "Traceback" not in capfd.readouterr().err
