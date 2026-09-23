"""A synthetic Immich library, dumped in either schema, shared by the tests."""
from __future__ import annotations

import gzip
import uuid

import numpy as np
import pytest

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
    return {"dump": dump, "wd": Workdir(tmp_path / "work"), "ids": (a, b, c), "lib": lib,
            "swapped": swapped, "unnamed": unnamed, "early": early}
