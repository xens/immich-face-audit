"""Work directory layout, .env loading and the Immich API client."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workdir:
    """Everything the audit reads and writes lives in one directory.

    It holds face embeddings (biometric data) and names: keep it private.
    """
    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    faces = property(lambda s: s.root / "faces.tsv")
    embeddings = property(lambda s: s.root / "embeddings.npy")
    people = property(lambda s: s.root / "people.tsv")
    baseline = property(lambda s: s.root / "baseline.tsv")
    rejections = property(lambda s: s.root / "baseline_rejections.json")
    flags = property(lambda s: s.root / "flags.json")
    decisions = property(lambda s: s.root / "decisions.json")
    apply_log = property(lambda s: s.root / "apply_log.jsonl")

    def read_json(self, path: Path, default):
        return json.loads(path.read_text()) if path.exists() else default

    def write_json(self, path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        tmp.replace(path)


def load_env(*paths: Path) -> None:
    """Minimal KEY=VALUE .env reader; real environment variables win."""
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.lstrip().startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


class ImmichError(RuntimeError):
    pass


class Immich:
    """The handful of Immich API calls the audit needs."""

    def __init__(self, url: str, key: str) -> None:
        self.base = url.rstrip("/")
        self.key = key

    @classmethod
    def from_env(cls, required: bool = True) -> "Immich | None":
        url, key = os.environ.get("IMMICH_URL", ""), os.environ.get("IMMICH_API_KEY", "")
        if url and key:
            return cls(url, key)
        if required:
            raise SystemExit("IMMICH_URL and IMMICH_API_KEY must be set (environment or .env)")
        return None

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[bytes, str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"x-api-key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise ImmichError(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}") from e

    def call(self, method: str, path: str, body: dict | None = None):
        raw, _ = self.request(method, path, body)
        return json.loads(raw) if raw else None

    def people(self) -> list[dict]:
        out, page = [], 1
        while True:
            body = self.call("GET", f"/api/people?withHidden=true&page={page}&size=1000")
            out += body["people"]
            if not body.get("hasNextPage"):
                return out
            page += 1

    def current_person(self, asset: str, face: str) -> str | None:
        """Person id currently on `face`: "" if none, None if the face no longer exists."""
        for f in self.call("GET", f"/api/faces?id={asset}"):
            if f["id"] == face:
                return (f.get("person") or {}).get("id", "")
        return None

    def assign(self, face: str, person: str) -> None:
        # Immich's reassign endpoint takes the *person* in the path and the face in the body.
        self.call("PUT", f"/api/faces/{person}", {"id": face})

    def new_person(self) -> str:
        return self.call("POST", "/api/people", {})["id"]
