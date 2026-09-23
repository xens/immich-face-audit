"""End-to-end tests on a synthetic Immich dump: extract -> baseline -> score -> apply/undo."""
from __future__ import annotations

import pytest

from immich_face_audit import apply, baseline, extract, score
from immich_face_audit.config import Workdir


def flagged(wd: Workdir) -> dict[str, dict]:
    return {c["face"][0]: c for g in wd.read_json(wd.flags, []) for c in g["cards"]}


def test_extract_skips_users_and_reads_both_schemas(library):
    wd = library["wd"]
    stats = extract.extract(library["dump"], wd, log=lambda *_: None)
    assert stats["faces"] == 123 and stats["unassigned"] == 1 and stats["named"] == 3
    assert "secret@example.com" not in wd.faces.read_text() + wd.people.read_text()


def test_baseline_and_flags(library):
    wd = library["wd"]
    extract.extract(library["dump"], wd, log=lambda *_: None)
    b = baseline.build(wd, log=lambda *_: None)
    assert b["people"] == 3 and b["references"] > 0
    assert b["excluded"] == {"before-birth": 1}

    s = score.score(wd, log=lambda *_: None)
    flags = flagged(wd)
    a, bob, carol = library["ids"]
    assert flags[library["swapped"]]["reason"] == "swap"
    assert flags[library["swapped"]]["s"] == bob
    assert flags[library["unnamed"]]["reason"] == "suggest"
    assert flags[library["unnamed"]]["s"] == carol
    assert flags[library["early"]]["reason"] == "before-birth"
    assert s["flags"] == 3  # nothing else in a clean library


def test_rejected_references_are_ignored(library):
    wd = library["wd"]
    extract.extract(library["dump"], wd, log=lambda *_: None)
    baseline.build(wd, log=lambda *_: None)
    refs = [c[0] for p in baseline.references_for_review(wd) for w in p["windows"] for c in w["refs"]]
    wd.write_json(wd.rejections, {"rejected": {f: 1 for f in refs}})
    with pytest.raises(SystemExit):
        score.score(wd, log=lambda *_: None)


class FakeImmich:
    def __init__(self, faces: dict[str, str]) -> None:
        self.faces = dict(faces)  # face -> person
        self.created = 0

    def current_person(self, asset, face):
        return self.faces.get(face)

    def assign(self, face, person):
        self.faces[face] = person

    def new_person(self):
        self.created += 1
        return f"new-{self.created}"


def test_apply_dry_run_write_skip_and_undo(tmp_path):
    wd = Workdir(tmp_path)
    wd.write_json(wd.decisions, {
        "f1": {"action": "accept", "face": "f1", "asset": "a1", "from": "alice", "to": "bob"},
        "f2": {"action": "unassign", "face": "f2", "asset": "a2", "from": "alice", "to": None},
        "f3": {"action": "keep", "face": "f3", "asset": "a3", "from": "alice", "to": None},
        "f4": {"action": "accept", "face": "f4", "asset": "a4", "from": "alice", "to": "bob"},
    })
    im = FakeImmich({"f1": "alice", "f2": "alice", "f3": "alice", "f4": "carol"})  # f4 changed since review
    quiet = dict(log=lambda *_: None)

    assert apply.run(wd, im, **quiet) == {"done": 2, "skipped": 1, "failed": 0, "written": False}
    assert im.faces["f1"] == "alice"  # dry run wrote nothing

    apply.run(wd, im, write=True, **quiet)
    assert im.faces == {"f1": "bob", "f2": "new-1", "f3": "alice", "f4": "carol"}
    assert apply.run(wd, im, **quiet)["done"] == 0  # already applied: nothing left to do

    apply.run(wd, im, undo=True, write=True, **quiet)
    assert im.faces["f1"] == "alice" and im.faces["f2"] == "alice"
    assert apply.run(wd, im, **quiet)["done"] == 2  # undone, so applicable again


def test_extract_streams_latest_backup_from_immich(library, tmp_path):
    """--latest-backup: list backups, pick the newest dated one, stream it through the parser."""
    import http.server
    import json
    import threading

    from immich_face_audit.config import Immich

    payload = library["dump"].read_bytes()
    backups = [
        {"filename": "immich-db-backup-20260101T020000-v3.2.0-pg14.19.sql.gz", "filesize": 1, "timezone": "UTC"},
        {"filename": "immich-db-backup-20260922T020000-v3.2.0-pg14.19.sql.gz", "filesize": len(payload), "timezone": "UTC"},
        {"filename": "uploaded-something.sql.gz", "filesize": 1, "timezone": "UTC"},
    ]
    seen = []

    class Fake(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("x-api-key")))
            if self.path == "/api/admin/database-backups":
                body, ctype = json.dumps({"backups": backups}).encode(), "application/json"
            elif self.path == "/api/admin/database-backups/" + backups[1]["filename"]:
                body, ctype = payload, "application/octet-stream"
            else:
                body, ctype = b'{"message":"nope"}', "application/json"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        im = Immich(f"http://127.0.0.1:{srv.server_address[1]}", "backup-key")
        stats = extract.extract_from_immich(im, library["wd"], log=lambda *_: None)
    finally:
        srv.shutdown()
    assert stats["backup"] == backups[1]["filename"]  # newest dated one, not the upload
    assert stats["faces"] == 123 and stats["named"] == 3
    assert all(key == "backup-key" for _, key in seen)
    assert not list(tmp_path.glob("work/*.sql*"))  # streamed, never saved


def test_apply_with_an_empty_selection_changes_nothing(tmp_path):
    """The app passes the preview's pending faces; when that list is empty, nothing may be applied."""
    wd = Workdir(tmp_path)
    wd.write_json(wd.decisions, {"f1": {"action": "accept", "face": "f1", "asset": "a1", "from": "alice", "to": "bob"}})
    im = FakeImmich({"f1": "alice"})
    assert apply.run(wd, im, write=True, faces=[], log=lambda *_: None)["done"] == 0
    assert im.faces["f1"] == "alice"
