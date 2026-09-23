"""Load the extracted dataset: faces, people and L2-normalized embeddings."""
from __future__ import annotations

import csv
from dataclasses import dataclass

import numpy as np

from .config import Workdir

WINDOW_YEARS = 3  # faces are compared with references from the same period
WINDOW_ORIGIN = 1980  # any year works; floor division handles earlier photos


def window(year: int) -> int:
    """First year of the WINDOW_YEARS-wide period containing `year`."""
    return WINDOW_ORIGIN + WINDOW_YEARS * ((year - WINDOW_ORIGIN) // WINDOW_YEARS)


@dataclass
class Dataset:
    faces: list[dict]
    people: dict[str, dict]  # person_id -> row
    X: np.ndarray  # (N, 512), unit length
    year: np.ndarray  # (N,)
    window: np.ndarray  # (N,)
    person: np.ndarray  # (N,) person_id or ""
    named: np.ndarray  # (N,) bool: assigned to a person that has a name

    def name(self, pid: str) -> str:
        return self.people.get(pid, {}).get("name", "") or "(unnamed)"

    def birth(self, pid: str) -> str:
        return self.people.get(pid, {}).get("birth_date", "")

    def geometry(self, i: int) -> list:
        """[face_id, asset_id, image_w, image_h, x1, y1, x2, y2] for the review UI."""
        f = self.faces[i]
        return [f["face_id"], f["asset_id"], int(f["image_w"]), int(f["image_h"]),
                int(f["x1"]), int(f["y1"]), int(f["x2"]), int(f["y2"])]


def load(wd: Workdir) -> Dataset:
    if not wd.faces.exists():
        raise SystemExit(f"no extracted data in {wd.root}: run `immich-face-audit extract` first")
    with open(wd.people) as f:
        people = {r["person_id"]: r for r in csv.DictReader(f, delimiter="\t")}
    with open(wd.faces) as f:
        faces = list(csv.DictReader(f, delimiter="\t"))
    X = np.load(wd.embeddings)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    year = np.array([int(r["taken_at"][:4]) for r in faces])
    person = np.array([r["person_id"] for r in faces], dtype=object)
    named = np.array([bool(people.get(p, {}).get("name")) for p in person], dtype=bool)
    return Dataset(faces, people, X, year, np.array([window(y) for y in year]), person, named)
