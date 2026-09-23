"""End-to-end tests on a synthetic Immich dump: extract -> baseline -> score -> apply/undo."""
from __future__ import annotations

import gzip
import uuid

import numpy as np
import pytest

from immich_face_audit import apply, baseline, extract, score
from immich_face_audit.config import Workdir

rng = np.random.default_rng(0)
DIM = 512


def uid() -> str:
    return str(uuid.UUID(int=int(rng.integers(0, 2**63)) << 64 | int(rng.integers(0, 2**63))))


def vec(center: np.ndarray, noise: float = 0.35) -> str:
    v = center + noise * rng.standard_normal(DIM) / np.sqrt(DIM)
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def unit() -> np.ndarray:
    v = rng.standard_normal(DIM)
    return v / np.linalg.norm(v)


class Library:
    """Builds a pg_dump-style text file in either Immich schema."""

    def __init__(self, v3: bool = True) -> None:
        self.v3 = v3
        self.assets, self.faces, self.embs, self.people = [], [], [], []

    def person(self, name: str, birth: str = "") -> str:
        pid = uid()
        self.people.append((pid, name, birth))
        return pid

    def face(self, center: np.ndarray, person: str, date: str, asset: str | None = None) -> str:
        if asset is None:
            asset = uid()
            self.assets.append((asset, date))
        fid = uid()
        self.faces.append((fid, asset, person))
        self.embs.append((fid, vec(center)))
        return fid

    def dump(self, path) -> None:
        owner = uid()
        pcol = "personGroupId" if self.v3 else "personId"
        out = ["-- PostgreSQL database dump", ""]
        out.append('COPY public."user" (id, email, password) FROM stdin;')
        out.append(f"{owner}\tsecret@example.com\t$2b$10$notarealhash")
        out.append("\\.")
        out.append(f'COPY public.{"asset" if self.v3 else "assets"} (id, "ownerId", "fileCreatedAt", '
                   '"localDateTime", "deletedAt", visibility) FROM stdin;')
        out += [f"{a}\t{owner}\t{d}T10:00:00+00\t{d}T10:00:00+00\t\\N\ttimeline" for a, d in self.assets]
        out.append("\\.")
        out.append(f'COPY public.{"asset_face" if self.v3 else "asset_faces"} ("assetId", "{pcol}", '
                   '"imageWidth", "imageHeight", "boundingBoxX1", "boundingBoxY1", "boundingBoxX2", '
                   '"boundingBoxY2", id, "sourceType", "deletedAt", "isVisible") FROM stdin;')
        out += [f"{a}\t{p or chr(92) + 'N'}\t1000\t800\t100\t100\t200\t220\t{f}\tmachine-learning\t\\N\tt"
                for f, a, p in self.faces]
        out.append("\\.")
        out.append('COPY public.face_search ("faceId", embedding) FROM stdin;')
        out += [f"{f}\t{e}" for f, e in self.embs]
        out.append("\\.")
        if self.v3:
            out.append('COPY public.person ("ownerId", name, "isHidden", "birthDate", "personGroupId") FROM stdin;')
            out += [f"{owner}\t{n}\tf\t{b or chr(92) + 'N'}\t{p}" for p, n, b in self.people]
        else:
            out.append('COPY public.person (id, "ownerId", name, "isHidden", "birthDate") FROM stdin;')
            out += [f"{p}\t{owner}\t{n}\tf\t{b or chr(92) + 'N'}" for p, n, b in self.people]
        out.append("\\.")
        with gzip.open(path, "wt") as f:
            f.write("\n".join(out) + "\n")


@pytest.fixture(params=[True, False], ids=["immich-v3", "immich-v2"])
def library(request, tmp_path):
    lib = Library(v3=request.param)
    ca, cb, cc = unit(), unit(), unit()
    a, b, c = lib.person("Alice"), lib.person("Bob"), lib.person("Carol", birth="2019-01-01")
    for year in (2019, 2020, 2021, 2022):
        for m in range(1, 11):
            lib.face(ca, a, f"{year}-{m:02d}-15")
            lib.face(cb, b, f"{year}-{m:02d}-15")
            lib.face(cc, c, f"{year}-{m:02d}-15")
    swapped = lib.face(cb, a, "2020-06-20")  # Bob's face tagged as Alice
    unnamed = lib.face(cc, "", "2021-06-20")  # Carol, never assigned
    early = lib.face(cc, c, "2015-06-20")  # Carol, before her birth date
    dump = tmp_path / "immich-db-backup.sql.gz"
    lib.dump(dump)
    return {"dump": dump, "wd": Workdir(tmp_path / "work"), "ids": (a, b, c),
            "swapped": swapped, "unnamed": unnamed, "early": early}


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
