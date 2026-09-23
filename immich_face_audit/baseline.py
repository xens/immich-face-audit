"""Propose a reference set: the most self-consistent faces of each named
person, per time window.

Candidates exclude faces that break a hard rule (dated before the person's
birth date, or the same person tagged twice in one photo). Within each
(person, window) every face is scored by its mean cosine similarity to its K
nearest same-person neighbours; the top of that ranking becomes the reference
set, skipping near-duplicates (burst shots). A second pass drops references
that look more like another person's references from the same period than
like their own.
"""
from __future__ import annotations

import collections
import csv
import math

import numpy as np

from .config import Workdir
from .data import WINDOW_YEARS, load

K = 10
MAX_REFS = 40  # per (person, window)
MIN_FACES = 3  # windows with fewer faces get no references
SHARE = 0.3  # top share of a window kept as references
NEAR_DUP = 0.93  # skip a candidate this similar to an already chosen reference
CROSS_WINDOWS = 1  # contamination check against other people's refs within +/- this many windows

COLUMNS = ["face_id", "person_id", "name", "window", "taken_at", "consistency",
           "own_ref_sim", "rival_ref_sim", "rival", "verdict"]


def build(wd: Workdir, log=print) -> dict:
    ds = load(wd)
    N = len(ds.faces)

    # --- hard-rule exclusions -------------------------------------------------
    reason = [""] * N
    for i, f in enumerate(ds.faces):
        b = ds.birth(ds.person[i])
        if ds.named[i] and b and f["taken_at"][:10] < b:
            reason[i] = "before-birth"
    per_photo = collections.defaultdict(list)
    for i in np.flatnonzero(ds.named):
        per_photo[(ds.faces[i]["asset_id"], ds.person[i])].append(i)
    for idx in per_photo.values():
        if len(idx) > 1:
            for i in idx:
                reason[i] = reason[i] or "twice-in-photo"
    excluded = np.array([bool(r) for r in reason])

    # --- per (person, window) self-consistency -------------------------------
    groups = collections.defaultdict(list)
    for i in np.flatnonzero(ds.named & ~excluded):
        groups[(ds.person[i], ds.window[i])].append(i)

    score = np.full(N, np.nan, dtype=np.float32)
    is_ref = np.zeros(N, dtype=bool)
    for idx in groups.values():
        idx = np.array(idx)
        if len(idx) < MIN_FACES:
            continue
        S = ds.X[idx] @ ds.X[idx].T
        np.fill_diagonal(S, -np.inf)
        k = min(K, len(idx) - 1)
        s = np.partition(S, -k, axis=1)[:, -k:].mean(axis=1)
        score[idx] = s
        keep = max(MIN_FACES, min(MAX_REFS, math.ceil(SHARE * len(idx))))
        chosen: list[int] = []
        for j in np.argsort(-s):
            if len(chosen) == keep:
                break
            if not chosen or S[j, chosen].max() < NEAR_DUP:
                chosen.append(j)
        is_ref[idx[chosen]] = True

    # --- cross-person contamination pass -------------------------------------
    refs = np.flatnonzero(is_ref)
    own = np.full(N, np.nan, dtype=np.float32)
    rival = np.full(N, np.nan, dtype=np.float32)
    rival_pid = np.full(N, "", dtype=object)
    for w in np.unique(ds.window[refs]):
        here = refs[ds.window[refs] == w]
        near = refs[np.abs(ds.window[refs] - w) <= CROSS_WINDOWS * WINDOW_YEARS]
        S = ds.X[here] @ ds.X[near].T
        S[here[:, None] == near[None, :]] = -np.inf
        near_pid = ds.person[near]
        for pid in np.unique(near_pid):
            cols = near_pid == pid
            k = min(3, int(cols.sum()))
            top = np.partition(S[:, cols], -k, axis=1)[:, -k:].mean(axis=1)
            mine = ds.person[here] == pid
            own[here[mine]] = top[mine]
            better = ~mine & ~(rival[here] >= top)
            rival[here[better]] = top[better]
            rival_pid[here[better]] = pid
    contaminated = is_ref & (rival > own)
    is_ref &= ~contaminated

    with open(wd.baseline, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(COLUMNS)
        for i in np.flatnonzero(ds.named):
            verdict = ("reference" if is_ref[i] else "contaminated" if contaminated[i]
                       else reason[i] or ("small-window" if np.isnan(score[i]) else "candidate"))
            w.writerow([ds.faces[i]["face_id"], ds.person[i], ds.name(ds.person[i]), ds.window[i],
                        ds.faces[i]["taken_at"][:10], f"{score[i]:.3f}", f"{own[i]:.3f}",
                        f"{rival[i]:.3f}", ds.name(rival_pid[i]) if rival_pid[i] else "", verdict])

    wins = {(ds.person[i], ds.window[i]) for i in refs if is_ref[i]}
    return {"references": int(is_ref.sum()), "windows": len(wins), "people": len({p for p, _ in wins}),
            "contaminated": int(contaminated.sum()),
            "excluded": dict(collections.Counter(r for r in reason if r))}


def references_for_review(wd: Workdir) -> list[dict]:
    """Reference faces grouped by person and window, most confusable people first
    (highest similarity of their references to someone else's)."""
    ds = load(wd)
    fid = {f["face_id"]: i for i, f in enumerate(ds.faces)}
    with open(wd.baseline) as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    totals = collections.Counter((r["person_id"], r["window"]) for r in rows)
    people: dict[str, dict] = {}
    for r in rows:
        if r["verdict"] != "reference":
            continue
        p = people.setdefault(r["person_id"], {"id": r["person_id"], "name": r["name"],
                                               "rival": 0.0, "windows": {}})
        rv = float(r["rival_ref_sim"])
        if not math.isnan(rv):
            p["rival"] = max(p["rival"], rv)
        p["windows"].setdefault(r["window"], []).append(
            ds.geometry(fid[r["face_id"]]) + [r["taken_at"], float(r["consistency"])])
    out = sorted(people.values(), key=lambda p: -p["rival"])
    for p in out:
        p["windows"] = [{"start": int(w), "total": totals[(p["id"], w)],
                         "refs": sorted(refs, key=lambda x: -x[9])}
                        for w, refs in sorted(p["windows"].items())]
    return out
