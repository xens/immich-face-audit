"""immich-face-audit: find and fix misassigned faces in Immich.

    immich-face-audit extract immich-db-backup-XXXX.sql.gz
    immich-face-audit baseline
    immich-face-audit review          # validate references, compute flags, decide
    immich-face-audit apply           # dry run
    immich-face-audit apply --write
    immich-face-audit undo --write

`run DUMP` chains extract + baseline + review.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import apply as apply_mod
from . import baseline, extract, review, score
from .config import Immich, Workdir, load_env


def _print(d: dict) -> None:
    for k, v in d.items():
        print(f"  {k}: {v}")


def cmd_extract(wd: Workdir, args) -> None:
    print(f"extracting from {args.dump} ...")
    _print(extract.extract(Path(args.dump), wd))
    immich = Immich.from_env(required=False)
    if immich:
        print("refreshing names / birth dates from the Immich API ...")
        print(f"  {extract.refresh_people(wd, immich)} people updated")
    else:
        print("  (IMMICH_URL / IMMICH_API_KEY not set: names and birth dates are as of the dump)")


def cmd_baseline(wd: Workdir, args) -> None:
    print("building references ...")
    _print(baseline.build(wd))


def cmd_score(wd: Workdir, args) -> None:
    print("scoring all faces ...")
    _print(score.score(wd))


def cmd_review(wd: Workdir, args) -> None:
    review.serve(wd, Immich.from_env(), port=args.port, open_browser=not args.no_browser)


def cmd_apply(wd: Workdir, args, undo: bool = False) -> None:
    faces = args.faces.split(",") if args.faces else None
    _print(apply_mod.run(wd, Immich.from_env(), undo=undo, write=args.write, limit=args.limit, faces=faces))


def cmd_run(wd: Workdir, args) -> None:
    cmd_extract(wd, args)
    cmd_baseline(wd, args)
    cmd_review(wd, args)


def main(argv: list[str] | None = None) -> None:
    load_env(Path.cwd() / ".env")
    ap = argparse.ArgumentParser(prog="immich-face-audit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", default=os.environ.get("FACE_AUDIT_DIR", "face-audit-data"),
                    help="where extracted data and review state live (default: %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def review_opts(p):
        p.add_argument("--port", type=int, default=8091)
        p.add_argument("--no-browser", action="store_true")

    p = sub.add_parser("extract", help="read faces/embeddings/people from an Immich DB backup")
    p.add_argument("dump")
    sub.add_parser("baseline", help="propose reference faces per person and period")
    sub.add_parser("score", help="flag suspicious faces (also available from the review app)")
    review_opts(sub.add_parser("review", help="open the review web app"))
    for name, h in (("apply", "write accepted decisions to Immich"), ("undo", "revert what apply wrote")):
        p = sub.add_parser(name, help=h + " (dry run unless --write)")
        p.add_argument("--write", action="store_true", help="actually change Immich")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--faces", default="", help="comma-separated face ids to restrict to")
    p = sub.add_parser("run", help="extract + baseline + review")
    p.add_argument("dump")
    review_opts(p)

    args = ap.parse_args(argv)
    wd = Workdir(Path(args.workdir).expanduser())
    load_env(wd.root / ".env")
    handlers = {"extract": cmd_extract, "baseline": cmd_baseline, "score": cmd_score, "review": cmd_review,
                "apply": cmd_apply, "undo": lambda w, a: cmd_apply(w, a, undo=True), "run": cmd_run}
    handlers[args.cmd](wd, args)


if __name__ == "__main__":
    sys.exit(main())
