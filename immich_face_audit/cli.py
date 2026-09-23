"""immich-face-audit: find and fix misassigned faces in Immich.

    immich-face-audit --workdir ~/face-audit     # opens the web app: every step is in there

The same steps from the command line, for scripting:

    immich-face-audit extract --latest-backup    (or: extract immich-db-backup-XXXX.sql.gz)
    immich-face-audit baseline
    immich-face-audit score
    immich-face-audit apply                      # dry run
    immich-face-audit apply --write
    immich-face-audit undo --write
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import time

from . import app as app_mod
from . import apply as apply_mod
from . import baseline, extract, score
from .config import Immich, Workdir, load_env


def _print(d: dict) -> None:
    for k, v in d.items():
        print(f"  {k}: {v}")


def _backup_client() -> Immich:
    """Backup downloads need an admin key with `maintenance` + `backup.download`.
    Prefer a dedicated IMMICH_BACKUP_API_KEY so the everyday key can stay narrow."""
    if os.environ.get("IMMICH_BACKUP_API_KEY"):
        return Immich.from_env(key_var="IMMICH_BACKUP_API_KEY")
    return Immich.from_env()


def cmd_extract(wd: Workdir, args) -> None:
    if bool(args.dump) == bool(args.latest_backup):
        raise SystemExit("give either a backup file or --latest-backup")
    if args.latest_backup:
        stats = extract.extract_from_immich(_backup_client(), wd)
    else:
        print(f"extracting from {args.dump} ...")
        stats = extract.extract(Path(args.dump), wd)
    _print(stats)
    immich = Immich.from_env(required=False)
    if immich:
        print("refreshing names / birth dates from the Immich API ...")
        stats["people_updated"] = extract.refresh_people(wd, immich)
        print(f"  {stats['people_updated']} people updated")
    else:
        print("  (IMMICH_URL / IMMICH_API_KEY not set: names and birth dates are as of the dump)")
    wd.update_meta(extract=stats, extract_at=time.time())


def cmd_baseline(wd: Workdir, args) -> None:
    print("building references ...")
    b = baseline.build(wd)
    wd.update_meta(baseline=b, baseline_at=time.time())
    _print(b)


def cmd_score(wd: Workdir, args) -> None:
    print("scoring all faces ...")
    s = score.score(wd)
    wd.update_meta(score=s, score_at=time.time())
    _print(s)


def cmd_app(wd: Workdir, args) -> None:
    app_mod.serve(wd, port=getattr(args, "port", 8091), open_browser=not getattr(args, "no_browser", False))


def cmd_apply(wd: Workdir, args, undo: bool = False) -> None:
    faces = args.faces.split(",") if args.faces else None
    _print(apply_mod.run(wd, Immich.from_env(), undo=undo, write=args.write, limit=args.limit, faces=faces))


def cmd_run(wd: Workdir, args) -> None:
    cmd_extract(wd, args)
    cmd_baseline(wd, args)
    cmd_app(wd, args)


def main(argv: list[str] | None = None) -> None:
    load_env(Path.cwd() / ".env")
    ap = argparse.ArgumentParser(prog="immich-face-audit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", default=os.environ.get("FACE_AUDIT_DIR", "face-audit-data"),
                    help="where extracted data and review state live (default: %(default)s)")
    ap.add_argument("--port", type=int, default=8091, help="web app port (default: %(default)s)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    sub = ap.add_subparsers(dest="cmd")

    def source_opts(p):
        p.add_argument("dump", nargs="?", help="local immich-db-backup-*.sql.gz")
        p.add_argument("--latest-backup", action="store_true",
                       help="stream the newest backup from the Immich server instead of a local file")

    source_opts(sub.add_parser("extract", help="read faces/embeddings/people from an Immich DB backup"))
    sub.add_parser("baseline", help="propose reference faces per person and period")
    sub.add_parser("score", help="flag suspicious faces")
    sub.add_parser("app", aliases=["review"], help="open the web app (the default with no command)")
    for name, h in (("apply", "write accepted decisions to Immich"), ("undo", "revert what apply wrote")):
        p = sub.add_parser(name, help=h + " (dry run unless --write)")
        p.add_argument("--write", action="store_true", help="actually change Immich")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--faces", default="", help="comma-separated face ids to restrict to")
    source_opts(sub.add_parser("run", help="extract + baseline, then open the web app"))

    args = ap.parse_args(argv)
    wd = Workdir(Path(args.workdir).expanduser())
    load_env(wd.root / ".env")
    handlers = {"extract": cmd_extract, "baseline": cmd_baseline, "score": cmd_score, "app": cmd_app,
                "review": cmd_app, "apply": cmd_apply, "undo": lambda w, a: cmd_apply(w, a, undo=True),
                "run": cmd_run, None: cmd_app}
    handlers[args.cmd](wd, args)


if __name__ == "__main__":
    sys.exit(main())
