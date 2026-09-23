"""Write review decisions to Immich, or undo them.

Only "accept" (reassign to the suggested person) and "unassign" (move to a
new unnamed person, like Immich's "not this person") change anything.
Before each change the face's current person is read back and the change is
skipped if it no longer matches what was reviewed. Every change is appended
to apply_log.jsonl *before* it is verified, so anything that landed can be
undone.
"""
from __future__ import annotations

import csv
import json
import time

from .config import Immich, ImmichError, Workdir


def _log_entries(wd: Workdir) -> list[dict]:
    if not wd.apply_log.exists():
        return []
    return [json.loads(l) for l in wd.apply_log.read_text().splitlines() if l.strip()]


def _live(wd: Workdir) -> dict[str, dict]:
    """face -> its latest apply entry that has not been undone."""
    live: dict[str, dict] = {}
    for e in _log_entries(wd):
        if e["op"] == "apply":
            live[e["face"]] = e
        else:
            live.pop(e["face"], None)
    return live


def plan(wd: Workdir, undo: bool) -> list[dict]:
    live = _live(wd)
    if undo:
        return [{"face": e["face"], "asset": e["asset"], "expected": e["after"], "target": e["before"],
                 "label": f"{e.get('toName') or 'unnamed'} -> back to {e.get('fromName') or 'unnamed'}", **e}
                for e in reversed(list(live.values()))]
    out = []
    for d in wd.read_json(wd.decisions, {}).values():
        if d["action"] not in ("accept", "unassign") or d["face"] in live:
            continue
        out.append({**d, "expected": d["from"] or "", "target": d["to"] if d["action"] == "accept" else "",
                    "label": f"{d.get('fromName') or 'unnamed'} -> {d.get('toName') or 'new unnamed person'}"})
    return out


def preview(wd: Workdir) -> dict:
    """What `apply` would do, judged against the latest extracted data (no API
    calls): pending, already applied (e.g. from another folder), or changed in
    Immich since the review. `apply` still re-checks every face live."""
    current: dict[str, str] = {}
    geometry: dict[str, list] = {}
    if wd.faces.exists():
        with open(wd.faces) as f:
            for r in csv.DictReader(f, delimiter="\t"):
                current[r["face_id"]] = r["person_id"]
                geometry[r["face_id"]] = [r["face_id"], r["asset_id"], int(r["image_w"]), int(r["image_h"]),
                                          int(r["x1"]), int(r["y1"]), int(r["x2"]), int(r["y2"])]
    groups: dict[str, list] = {"pending": [], "done": [], "changed": [], "unknown": []}
    for t in plan(wd, undo=False):
        cur = current.get(t["face"])
        if cur is None:
            state = "unknown"  # not in the extracted data: deleted, or data older than the decision
        elif t["action"] == "accept" and cur == t["target"]:
            state = "done"
        elif cur != t["expected"]:
            state = "changed"
        else:
            state = "pending"
        groups[state].append({k: t.get(k) for k in ("face", "asset", "action", "fromName", "toName", "reason")}
                             | {"geometry": geometry.get(t["face"])})
    live = _live(wd)
    return {**groups, "undoable": len(live),
            "recent": [{k: e.get(k) for k in ("face", "asset", "fromName", "toName", "reason", "t")}
                       | {"geometry": geometry.get(e["face"])}
                       for e in sorted(live.values(), key=lambda e: -e["t"])[:60]]}


def run(wd: Workdir, immich: Immich, undo: bool = False, write: bool = False,
        limit: int = 0, faces: list[str] | None = None, log=print, progress=None) -> dict:
    todo = plan(wd, undo)
    if faces is not None:  # an empty selection means nothing, not everything
        wanted = set(faces)
        todo = [t for t in todo if t["face"] in wanted]
    if limit:
        todo = todo[:limit]
    op = "undo" if undo else "apply"
    log(f"{op}: {len(todo)} faces{'' if write else ' (dry run: nothing is written)'}")

    done = skipped = failed = 0
    for n, t in enumerate(todo, 1):
        if progress:
            progress(n, len(todo))
        face, asset, target = t["face"], t["asset"], t["target"]
        try:
            cur = immich.current_person(asset, face)
            if cur != t["expected"]:
                skipped += 1
                log(f"  skip {face}: now on {cur!r}, expected {t['expected']!r}")
                continue
            if not write:
                done += 1
                if n <= 20:
                    log(f"  would {op} {face}  {t['label']}")
                continue
            created = None
            if not target:  # Immich can't set a face to "no person": use a fresh unnamed one
                target = created = immich.new_person()
            immich.assign(face, target)
            with open(wd.apply_log, "a") as f:
                f.write(json.dumps({"op": op, "t": time.time(), "face": face, "asset": asset,
                                    "before": t["expected"], "after": target, "created_person": created,
                                    "fromName": t.get("fromName"), "toName": t.get("toName"),
                                    "reason": t.get("reason")}) + "\n")
            after = immich.current_person(asset, face)
            if after != target:
                raise ImmichError(f"read back {after!r} after assigning {target!r}")
            done += 1
            if n <= 5 or n % 100 == 0:
                log(f"  {n}/{len(todo)} {op} {face}  {t['label']}")
        except ImmichError as e:
            failed += 1
            log(f"  FAIL {face}: {e}")
            if failed >= 5 and done == 0:
                raise SystemExit("stopping: the first 5 attempts all failed (API key permissions?)")
    return {"done": done, "skipped": skipped, "failed": failed, "written": write}
