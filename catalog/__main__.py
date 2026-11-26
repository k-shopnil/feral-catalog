"""CLI: python -m catalog <command>

Commands
  run       crawl -> process -> match -> enrich -> build -> publish (-> push unless --no-push)
  crawl     crawl only            [--root PREFIX ...] [--full]
  process   parse/group stable files
  match     match works to TMDB   [--limit N]
  enrich    fetch TMDB details/seasons
  publish   build + publish       [--no-push]
  report    counts + reports/unmatched.csv + reports/low_confidence.csv
  doctor    connectivity checks
  export-state  consistent copy of data/state.db   --file PATH   (to move the indexer to another device)
  import-state  replace data/state.db with a copy  --file PATH   (the old one is kept as state.db.bak-<time>)
  drive-auth    one-time Google sign-in for the Drive state hand-off   --file client_secret_*.json
  drive-status  state generations and locks on Drive
  drive-pull    download Drive's newest state if newer        drive-push   upload this state now
  drive-unlock  clear a stuck lock

With Drive set up (GDRIVE_* in .env), `run` takes the Drive lock, pulls a newer state.db, runs, uploads its
state.db and releases the lock, so PC and phone hand over without export/import. --no-drive skips that.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time

import httpx

from . import __version__, gdrive
from .build import build, build_rows
from .crawler import Crawler
from .db import connect, get_meta, set_meta
from .enrich import enrich
from .match import match_works
from .process import process_files
from .publish import ensure_not_behind, fetch_remote_manifest, publish, push_pages, sync_site_from_pages
from .settings import DATA, REPORTS, load_sources, setup_logging, tmdb_token
from .tmdb import Tmdb

log = logging.getLogger("catalog")


def cmd_crawl(conn, args):
    """Crawl every source in sources.yaml in turn. Returns summed stats plus per-source stats under "sources"."""
    total: dict = {}
    per: dict = {}
    for src in load_sources():
        if args.root and not any(r.path.startswith(o) for r in src.roots for o in args.root):
            continue
        stats = Crawler(conn, src).run(only_roots=args.root, full=args.full)
        log.info("crawl %s: %s", src.id, stats)
        per[src.id] = stats
        for k, v in stats.items():
            total[k] = total.get(k, 0) + v
    total["sources"] = per
    return total


def cmd_report(conn, _args=None):
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    print("files by role/kind:")
    for r in q("SELECT role, kind, COUNT(*) n FROM file WHERE missing_since IS NULL GROUP BY role, kind ORDER BY n DESC"):
        print(f"  {r['role']:<10} {str(r['kind']):<10} {r['n']}")
    print("works by kind/status:")
    for r in q("SELECT kind, status, COUNT(*) n FROM work GROUP BY kind, status ORDER BY kind, n DESC"):
        print(f"  {r['kind']:<6} {r['status']:<10} {r['n']}")
    with open(REPORTS / "unmatched.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "kind", "title", "year", "langs", "sample_path", "candidates"])
        for r in q("SELECT * FROM work WHERE status IN ('unmatched','error') ORDER BY kind, title"):
            w.writerow([r["key"], r["kind"], r["title"], r["year"], r["langs"], r["sample_path"], r["candidates"]])
    with open(REPORTS / "low_confidence.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["key", "kind", "title", "year", "score", "margin", "tmdb_id", "sample_path", "candidates"])
        for r in q("SELECT * FROM work WHERE status='auto_low' ORDER BY score"):
            w.writerow([r["key"], r["kind"], r["title"], r["year"], r["score"], r["margin"], r["tmdb_id"], r["sample_path"], r["candidates"]])
    print(f"reports written to {REPORTS}")


def cmd_doctor(_conn, _args=None):
    ok = True
    for src in load_sources():
        try:
            r = httpx.get(src.base_url, headers={"User-Agent": src.user_agent}, timeout=20)
            print(f"storage server {src.id} ({src.base_url}): HTTP {r.status_code} ({len(r.text)} bytes)")
            ok &= r.status_code == 200
        except Exception as e:  # noqa: BLE001
            print(f"storage server {src.id} ({src.base_url}): FAILED {e}")
            ok = False
    try:
        r = httpx.get("https://api.themoviedb.org/3/authentication",
                      headers={"Authorization": f"Bearer {tmdb_token()}"}, timeout=20)
        print(f"tmdb: {r.json().get('success')}")
        ok &= r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"tmdb: FAILED {e}")
        ok = False
    print("doctor:", "OK" if ok else "PROBLEMS")
    return ok


def _no_stage(*_a, **_k):
    pass


def preflight(conn, on_stage=_no_stage) -> dict | None:
    """Refuse to run on a state.db older than what is published, then fetch any published files site/ lacks."""
    on_stage("sync", "start")
    remote = fetch_remote_manifest()
    ensure_not_behind(conn, remote)
    fetched = sync_site_from_pages(remote)
    info = {"published_version": (remote or {}).get("catalog_version"), "fetched_files": fetched,
            "manifest_reachable": remote is not None}
    log.info("sync: %s", info)
    on_stage("sync", "done", info)
    return remote


def cmd_publish(conn, args, tmdb=None, run_info=None, pusher=push_pages, on_stage=_no_stage, preflight_done=False):
    if not preflight_done:
        preflight(conn, on_stage)
    tmdb = tmdb or Tmdb(conn)
    on_stage("publish", "start")
    cat = build(conn, tmdb)
    rows = build_rows(cat)
    res = publish(conn, cat, rows, run_info or {"finished_at": int(time.time())})
    log.info("publish: %s (titles=%d episodes=%d versions=%d)", res, len(cat["titles"]), len(cat["episodes"]), len(cat["versions"]))
    on_stage("publish", "done", res)
    res["pushed"] = None  # None = no push needed or asked for; True/False = push result
    if not args.no_push and (res["changes"] or args.force_push or get_meta(conn, "pushed_version") != str(res["version"])):
        on_stage("push", "start")
        res["pushed"] = bool(pusher())
        if res["pushed"]:
            set_meta(conn, "pushed_version", res["version"])
            conn.commit()
        on_stage("push", "done" if res["pushed"] else "failed", {"version": res["version"]})
    else:
        on_stage("push", "skipped", {"reason": "--no-push" if args.no_push else "already published"})
    return res


def cmd_run(conn, args, pusher=push_pages, on_stage=_no_stage):
    started = int(time.time())
    budget_end = started + args.budget_minutes * 60 if args.budget_minutes else None
    set_meta(conn, "last_run_started_at", started)
    conn.commit()
    preflight(conn, on_stage)
    on_stage("crawl", "start")
    crawl = cmd_crawl(conn, args)
    on_stage("crawl", "done", crawl)
    on_stage("process", "start")
    proc = process_files(conn, load_sources())
    log.info("process: %s", proc)
    on_stage("process", "done", proc)
    tmdb = Tmdb(conn)
    on_stage("match", "start")
    m = match_works(conn, tmdb, limit=None)
    log.info("match: %s", m)
    on_stage("match", "done", m)
    if budget_end and time.time() > budget_end:
        log.warning("budget exhausted after matching; continuing to publish what is ready")
    on_stage("enrich", "start")
    e = enrich(conn, tmdb)
    log.info("enrich: %s (tmdb calls=%d, cache hits=%d)", e, tmdb.calls, tmdb.cache_hits)
    on_stage("enrich", "done", e)
    info = {"started_at": started, "finished_at": int(time.time()), "crawl": crawl, "process": proc, "match": m, "enrich": e,
            "indexer_version": __version__}
    res = cmd_publish(conn, args, tmdb, info, pusher=pusher, on_stage=on_stage, preflight_done=True)
    set_meta(conn, "last_run_finished_at", int(time.time()))
    conn.commit()
    tmdb.close()
    return res


def export_state(conn, dest: str) -> None:
    """Consistent single-file copy of state.db (SQLite backup API, so WAL contents are included)."""
    import sqlite3

    out = sqlite3.connect(dest)
    with out:
        conn.backup(out)
    out.close()
    log.info("state exported to %s", dest)


def import_state(src: str) -> str:
    """Replace data/state.db with the database at src. Returns the path of the backup of the old state ('' if none)."""
    import sqlite3

    probe = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        v = probe.execute("SELECT MAX(version) FROM catalog_version").fetchone()[0]
        probe.execute("SELECT 1 FROM file LIMIT 1")
    except sqlite3.DatabaseError as e:
        probe.close()
        raise SystemExit(f"{src} is not a feral catalog state.db: {e}") from e
    dest = DATA / "state.db"
    backup = ""
    if dest.exists():
        backup = str(DATA / f"state.db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        cur = sqlite3.connect(dest)
        b = sqlite3.connect(backup)
        cur.backup(b)
        b.close()
        cur.close()
    tmp = DATA / "state.db.importing"
    tmp.unlink(missing_ok=True)
    t = sqlite3.connect(tmp)
    probe.backup(t)
    t.close()
    probe.close()
    for suffix in ("-wal", "-shm"):
        (DATA / f"state.db{suffix}").unlink(missing_ok=True)
    tmp.replace(dest)
    log.info("state imported from %s (catalog version %s); previous state kept at %s", src, v, backup or "-")
    return backup


def run_with_handoff(args) -> dict:
    """`run` wrapped in the Drive hand-off: lock, pull newer state, run, upload state, unlock."""
    t0 = time.time()
    device = gdrive.device_name()
    drive = gdrive.Drive()
    lock = drive.acquire_lock(device)
    try:
        log.info("drive: %s", gdrive.pull_if_newer(drive))
        conn = connect()
        try:
            res = cmd_run(conn, args)
            log.info("drive: %s", gdrive.push(drive, conn, device))
        finally:
            conn.close()
    finally:
        drive.release_lock(lock)
        drive.close()
    log.info("run done in %.1fs", time.time() - t0)
    return res


def cmd_drive(args, ap) -> None:
    if args.command == "drive-auth":
        if not args.file:
            ap.error("drive-auth needs --file client_secret_*.json")
        gdrive.authorize(args.file)
        return
    if not gdrive.configured():
        raise SystemExit("Drive is not set up: run  python -m catalog drive-auth --file client_secret_*.json")
    if args.command == "drive-status":
        print(json.dumps(gdrive.status(), indent=2))
    elif args.command == "drive-unlock":
        log.info("drive: removed %d lock(s)", gdrive.unlock())
    else:
        drive = gdrive.Drive()
        lock = drive.acquire_lock(gdrive.device_name())
        try:
            if args.command == "drive-pull":
                log.info("drive: %s", gdrive.pull_if_newer(drive))
            else:
                conn = connect()
                try:
                    log.info("drive: %s", gdrive.push(drive, conn, gdrive.device_name()))
                finally:
                    conn.close()
        finally:
            drive.release_lock(lock)
            drive.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="catalog")
    ap.add_argument("command", choices=["run", "crawl", "process", "match", "enrich", "publish", "report", "doctor",
                                        "export-state", "import-state", "drive-auth", "drive-status", "drive-pull",
                                        "drive-push", "drive-unlock"])
    ap.add_argument("--file", help="state.db path for export-state / import-state")
    ap.add_argument("--root", action="append", help="limit crawl to roots starting with this prefix")
    ap.add_argument("--full", action="store_true", help="re-list every directory")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--force-push", action="store_true")
    ap.add_argument("--refresh-logos", action="store_true", help="re-enrich titles missing logos or using .svg logos")
    ap.add_argument("--no-drive", action="store_true", help="run without the Google Drive state hand-off")
    ap.add_argument("--budget-minutes", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    if args.command == "import-state":
        if not args.file:
            ap.error("import-state needs --file")
        import_state(args.file)
        return
    if args.command.startswith("drive-"):
        cmd_drive(args, ap)
        return
    if args.command == "run" and not args.no_drive and gdrive.configured():
        run_with_handoff(args)
        return
    conn = connect()
    t0 = time.time()
    if args.command == "crawl":
        cmd_crawl(conn, args)
    elif args.command == "process":
        log.info("process: %s", process_files(conn, load_sources(), limit=args.limit))
    elif args.command == "match":
        tmdb = Tmdb(conn)
        log.info("match: %s", match_works(conn, tmdb, limit=args.limit))
    elif args.command == "enrich":
        tmdb = Tmdb(conn)
        log.info("enrich: %s", enrich(conn, tmdb, refresh_logos=args.refresh_logos))
    elif args.command == "publish":
        cmd_publish(conn, args)
    elif args.command == "report":
        cmd_report(conn)
    elif args.command == "doctor":
        sys.exit(0 if cmd_doctor(conn) else 1)
    elif args.command == "run":
        cmd_run(conn, args)
    elif args.command == "export-state":
        if not args.file:
            ap.error("export-state needs --file")
        export_state(conn, args.file)
    log.info("%s done in %.1fs", args.command, time.time() - t0)


if __name__ == "__main__":
    main()
