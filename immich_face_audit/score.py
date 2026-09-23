"""Score every face against the validated references and flag discrepancies.

For a face taken in year Y, every named person with references within
+/- CROSS_YEARS of Y (and born by then) gets a score: the mean of the TOP_K
highest cosine similarities to that person's references. Rejected references
are left out, and a face never scores against itself.

Flags:
  swap          tagged X, but Y's references are clearly closer
  before-birth  photo dated before the tagged person's birth date
  twice         same person tagged twice in one photo; the weaker face
  not-them      tagged X, X scores low and nobody else fits either
  suggest       unassigned / unnamed-cluster face that clearly matches someone
"""
from __future__ import annotations

import collections
import csv

import numpy as np

from .config import Workdir
from .data import WINDOW_YEARS, load

TOP_K = 3
CROSS_YEARS = 3
CHUNK = 4000

SWAP_MARGIN = 0.05  # another person beats the tagged one by at least this ...
SWAP_MIN = 0.45  # ... and is a plausible match in absolute terms
NOT_THEM_MAX = 0.30  # tagged person scores below this
SUGGEST_MIN = 0.55
SUGGEST_MARGIN = 0.10

ORDER = ["swap", "before-birth", "twice", "not-them", "suggest"]


def score(wd: Workdir, log=print) -> dict:
    ds = load(wd)
    N = len(ds.faces)
    fid = {f["face_id"]: i for i, f in enumerate(ds.faces)}
    rejected = wd.read_json(wd.rejections, {}).get("rejected", {})
    with open(wd.baseline) as f:
        base = [r for r in csv.DictReader(f, delimiter="\t")
                if r["verdict"] == "reference" and r["face_id"] not in rejected and r["face_id"] in fid]
    refs = np.array(sorted(fid[r["face_id"]] for r in base))
    if not len(refs):
        raise SystemExit("no references: run `immich-face-audit baseline` first")
    births = {pid: p["birth_date"] for pid, p in ds.people.items() if p["birth_date"]}
    dates = np.array([f["taken_at"][:10] for f in ds.faces])

    ref_pid = ds.person[refs]
    people = np.unique(ref_pid)
    cols_of = {pid: np.flatnonzero(ref_pid == pid) for pid in people}

    best = np.full((N, 2), np.nan, dtype=np.float32)  # best, second-best score
    best_pid = np.full((N, 2), "", dtype=object)
    own = np.full(N, np.nan, dtype=np.float32)

    for w in np.unique(ds.window):
        rows_all = np.flatnonzero(ds.window == w)
        near = np.abs(ds.window[refs] - w) <= CROSS_YEARS
        cand = [pid for pid in people if near[cols_of[pid]].any()]
        if not cand:
            continue
        for s in range(0, len(rows_all), CHUNK):
            rows = rows_all[s:s + CHUNK]
            S = ds.X[rows] @ ds.X[refs].T
            S[rows[:, None] == refs[None, :]] = -np.inf
            scores = np.full((len(rows), len(cand)), np.nan, dtype=np.float32)
            for j, pid in enumerate(cand):
                cols = cols_of[pid][near[cols_of[pid]]]
                k = min(TOP_K, len(cols))
                sc = np.partition(S[:, cols], -k, axis=1)[:, -k:].mean(axis=1)
                sc[~np.isfinite(sc)] = np.nan
                if pid in births:  # nobody is photographed before being born
                    sc[dates[rows] < births[pid]] = np.nan
                scores[:, j] = sc
                mine = ds.person[rows] == pid
                own[rows[mine]] = sc[mine]
            filled = np.nan_to_num(scores, nan=-9)
            order = np.argsort(-filled, axis=1)[:, :2]
            for t, r in enumerate(rows):
                for slot in range(min(2, len(cand))):
                    j = order[t, slot]
                    if filled[t, j] > -9:
                        best[r, slot], best_pid[r, slot] = scores[t, j], cand[j]

    # the weaker face when the same person is tagged twice in one photo
    twice = set()
    per_photo = collections.defaultdict(list)
    for i in np.flatnonzero(ds.named):
        per_photo[(ds.faces[i]["asset_id"], ds.person[i])].append(i)
    for idx in per_photo.values():
        if len(idx) > 1:
            keep = max(idx, key=lambda i: np.nan_to_num(own[i], nan=-9))
            twice.update(i for i in idx if i != keep)

    flags = []
    for i in range(N):
        pid = ds.person[i]
        if ds.named[i]:
            slot = 1 if best_pid[i, 0] == pid else 0
            other, osc = best_pid[i, slot], float(best[i, slot])
            plausible = bool(other) and osc >= SWAP_MIN
            b = births.get(pid)
            if b and dates[i] < b:
                reason = "before-birth"
            elif plausible and (np.isnan(own[i]) or osc - own[i] >= SWAP_MARGIN):
                reason = "swap"
            elif i in twice:
                reason = "twice"
            elif not np.isnan(own[i]) and own[i] < NOT_THEM_MAX and not plausible:
                reason = "not-them"
            else:
                continue
            flags.append((reason, i, pid, own[i], other if plausible else "", osc))
        else:
            b0, b1 = best[i]
            if best_pid[i, 0] and b0 >= SUGGEST_MIN and (np.isnan(b1) or b0 - b1 >= SUGGEST_MARGIN):
                flags.append(("suggest", i, pid, np.nan, best_pid[i, 0], float(b0)))

    groups = _cards(ds, base, fid, flags)
    wd.write_json(wd.flags, groups)
    counts = collections.Counter(f[0] for f in flags)
    return {"flags": len(flags), **{k: counts.get(k, 0) for k in ORDER}, "groups": len(groups)}


def _cards(ds, base: list[dict], fid: dict, flags: list) -> list[dict]:
    """Group flags by (reason, tagged, suggested); attach the closest-in-time
    reference of each person so the reviewer can compare side by side."""
    best_ref: dict[tuple[str, int], tuple[float, int]] = {}
    for r in base:
        k, c = (r["person_id"], int(r["window"])), float(r["consistency"])
        if k not in best_ref or c > best_ref[k][0]:
            best_ref[k] = (c, fid[r["face_id"]])

    def ref(pid: str, w: int):
        for d in (0, -1, 1, -2, 2, -3, 3):
            hit = best_ref.get((pid, w + d * WINDOW_YEARS))
            if hit:
                return ds.geometry(hit[1]) + [ds.faces[hit[1]]["taken_at"][:10]]
        return None

    def fmt(x) -> str:
        return "" if x is None or np.isnan(x) else f"{x:.2f}"

    groups: dict[tuple, list] = collections.defaultdict(list)
    for reason, i, pid, own, sug, ssc in flags:
        a_name = ds.name(pid) if ds.named[i] else ""
        groups[(reason, a_name or "—", ds.name(sug) if sug else "—")].append({
            "face": ds.geometry(i), "date": ds.faces[i]["taken_at"][:10], "reason": reason,
            "a": pid, "aName": a_name, "aScore": fmt(own),  # pid may be an unnamed cluster or ""
            "s": sug, "sName": ds.name(sug) if sug else "", "sScore": fmt(ssc) if sug else "",
            "aRef": ref(pid, ds.window[i]) if a_name else None,
            "sRef": ref(sug, ds.window[i]) if sug else None,
        })
    out = [{"reason": k[0], "a": k[1], "s": k[2], "cards": sorted(v, key=lambda c: c["date"])}
           for k, v in groups.items()]
    out.sort(key=lambda g: (ORDER.index(g["reason"]), -len(g["cards"])))
    return out
