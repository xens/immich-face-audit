"""Read faces, embeddings and people out of an Immich database backup.

Immich writes nightly `pg_dump` backups (`immich-db-backup-*.sql.gz`). They
are plain SQL with one `COPY ... FROM stdin;` block per table, so they can be
streamed without restoring Postgres. Only four tables are read; everything
else, including users and credentials, is skipped.

Handles both schemas seen in the wild:
  Immich v3:    asset / asset_face."personGroupId" / person."personGroupId"
  Immich < v3:  asset(s) / asset_face(s)."personId" / person.id
"""
from __future__ import annotations

import csv
import gzip
import re
import sys
from pathlib import Path

import numpy as np

from .config import Immich, Workdir

COPY_RE = re.compile(r'^COPY public\."?(\w+)"? \((.*)\) FROM stdin;$')
TABLES = {"asset": "asset", "assets": "asset", "asset_face": "face", "asset_faces": "face",
          "face_search": "embedding", "person": "person"}
_ESC = re.compile(r"\\(.)")
_ESC_MAP = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}

FACE_COLUMNS = ["face_id", "asset_id", "person_id", "owner_id", "taken_at", "source_type",
                "image_w", "image_h", "x1", "y1", "x2", "y2"]
PEOPLE_COLUMNS = ["person_id", "name", "birth_date", "is_hidden"]


def _unescape(v: str) -> str | None:
    if v == r"\N":
        return None
    return _ESC.sub(lambda m: _ESC_MAP.get(m.group(1), m.group(1)), v) if "\\" in v else v


def _rows(fh, columns: list[str]):
    for line in fh:
        line = line.rstrip("\n")
        if line == r"\.":
            return
        yield dict(zip(columns, (_unescape(v) for v in line.split("\t"))))


def _visible_asset(r: dict) -> bool:
    if r.get("deletedAt"):
        return False
    if r.get("visibility") in ("locked", "hidden"):
        return False
    return r.get("isVisible", "t") != "f"


def extract(dump: Path, wd: Workdir, log=print) -> dict:
    assets: dict[str, tuple[str, str]] = {}
    faces: dict[str, list] = {}
    people: dict[str, list] = {}
    emb_ids: list[str] = []
    embs: list[np.ndarray] = []

    opener = gzip.open if dump.suffix == ".gz" else open
    with opener(dump, "rt", encoding="utf-8") as fh:
        for line in fh:
            m = COPY_RE.match(line.rstrip("\n"))
            if not m or m.group(1) not in TABLES:
                continue
            kind = TABLES[m.group(1)]
            cols = [c.strip().strip('"') for c in m.group(2).split(",")]
            log(f"reading {m.group(1)} ...")
            for r in _rows(fh, cols):
                if kind == "asset":
                    if _visible_asset(r):
                        assets[r["id"]] = (r["ownerId"], r.get("localDateTime") or r["fileCreatedAt"])
                elif kind == "face":
                    if r.get("deletedAt") or r.get("isVisible") == "f" or r["assetId"] not in assets:
                        continue
                    owner, taken = assets[r["assetId"]]
                    person = r.get("personGroupId", r.get("personId")) or ""
                    faces[r["id"]] = [r["id"], r["assetId"], person, owner, taken, r.get("sourceType", ""),
                                      r["imageWidth"], r["imageHeight"], r["boundingBoxX1"],
                                      r["boundingBoxY1"], r["boundingBoxX2"], r["boundingBoxY2"]]
                elif kind == "embedding":
                    if r["faceId"] in faces:
                        emb_ids.append(r["faceId"])
                        embs.append(np.array(r["embedding"][1:-1].split(","), dtype=np.float32))
                elif kind == "person":
                    pid = r.get("personGroupId") or r["id"]
                    # multi-user v3 libraries have one row per owner; keep a named one
                    if pid not in people or (r["name"] and not people[pid][1]):
                        people[pid] = [pid, r["name"] or "", r.get("birthDate") or "", r.get("isHidden", "f")]

    if not embs:
        raise SystemExit("no face embeddings found: is this an Immich database dump?")
    np.save(wd.embeddings, np.vstack(embs))
    _write_tsv(wd.faces, FACE_COLUMNS, (faces[i] for i in emb_ids))
    _write_tsv(wd.people, PEOPLE_COLUMNS, people.values())
    assigned = sum(1 for i in emb_ids if faces[i][2])
    return {"assets": len(assets), "faces": len(emb_ids), "assigned": assigned,
            "unassigned": len(emb_ids) - assigned, "people": len(people),
            "named": sum(1 for p in people.values() if p[1])}


def refresh_people(wd: Workdir, immich: Immich, log=print) -> int:
    """Overlay live names / birth dates / hidden flags from the API onto people.tsv,
    so edits made in Immich after the dump are picked up."""
    live = {p["id"]: p for p in immich.people()}
    with open(wd.people) as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    known = {r["person_id"] for r in rows}
    overlap = len(known & live.keys())
    if overlap < 0.9 * min(len(known), len(live)):
        raise SystemExit(f"API person ids don't match the dump ({overlap} in common); not refreshing")
    changed = 0
    for r in rows:
        p = live.get(r["person_id"])
        if not p:
            continue
        new = {"name": p.get("name") or "", "birth_date": (p.get("birthDate") or "")[:10],
               "is_hidden": "t" if p.get("isHidden") else "f"}
        if any(r[k] != v for k, v in new.items()):
            changed += 1
            r.update(new)
    _write_tsv(wd.people, PEOPLE_COLUMNS, ([r[c] for c in PEOPLE_COLUMNS] for r in rows))
    return changed


def _write_tsv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    print(extract(Path(sys.argv[1]), Workdir(Path(sys.argv[2]))))
