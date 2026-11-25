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
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time

import httpx

from . import __version__
from .build import build, build_rows
from .crawler import Crawler
from .db import connect, get_meta, set_meta
from .enrich import enrich
from .match import match_works
from .process import process_files
from .publish import publish, push_pages
from .settings import REPORTS, load_source, setup_logging, tmdb_token
from .tmdb import Tmdb

log = logging.getLogger("catalog")


def cmd_crawl(conn, args):
    src = load_source()
    stats = Crawler(conn, src).run(only_roots=args.root, full=args.full)
    log.info("crawl: %s", stats)
    return stats


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
    src = load_source()
    ok = True
    try:
        r = httpx.get(src.base_url, headers={"User-Agent": src.user_agent}, timeout=20)
        print(f"storage server: HTTP {r.status_code} ({len(r.text)} bytes)")
        ok &= r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"storage server: FAILED {e}")
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


def cmd_publish(conn, args, tmdb=None, run_info=None):
    tmdb = tmdb or Tmdb(conn)
    cat = build(conn, tmdb)
    rows = build_rows(cat)
    res = publish(conn, cat, rows, run_info or {"finished_at": int(time.time())})
    log.info("publish: %s (titles=%d episodes=%d versions=%d)", res, len(cat["titles"]), len(cat["episodes"]), len(cat["versions"]))
    if not args.no_push and (res["changes"] or args.force_push or get_meta(conn, "pushed_version") != str(res["version"])):
        if push_pages():
            set_meta(conn, "pushed_version", res["version"])
            conn.commit()
    return res


def cmd_run(conn, args):
    started = int(time.time())
    budget_end = started + args.budget_minutes * 60 if args.budget_minutes else None
    set_meta(conn, "last_run_started_at", started)
    conn.commit()
    crawl = cmd_crawl(conn, args)
    proc = process_files(conn, load_source())
    log.info("process: %s", proc)
    tmdb = Tmdb(conn)
    m = match_works(conn, tmdb, limit=None)
    log.info("match: %s", m)
    if budget_end and time.time() > budget_end:
        log.warning("budget exhausted after matching; continuing to publish what is ready")
    e = enrich(conn, tmdb)
    log.info("enrich: %s (tmdb calls=%d, cache hits=%d)", e, tmdb.calls, tmdb.cache_hits)
    info = {"started_at": started, "finished_at": int(time.time()), "crawl": crawl, "process": proc, "match": m, "enrich": e,
            "indexer_version": __version__}
    res = cmd_publish(conn, args, tmdb, info)
    set_meta(conn, "last_run_finished_at", int(time.time()))
    conn.commit()
    tmdb.close()
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="catalog")
    ap.add_argument("command", choices=["run", "crawl", "process", "match", "enrich", "publish", "report", "doctor"])
    ap.add_argument("--root", action="append", help="limit crawl to roots starting with this prefix")
    ap.add_argument("--full", action="store_true", help="re-list every directory")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--force-push", action="store_true")
    ap.add_argument("--refresh-logos", action="store_true", help="re-enrich titles missing logos or using .svg logos")
    ap.add_argument("--budget-minutes", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    conn = connect()
    t0 = time.time()
    if args.command == "crawl":
        cmd_crawl(conn, args)
    elif args.command == "process":
        log.info("process: %s", process_files(conn, load_source(), limit=args.limit))
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
    log.info("%s done in %.1fs", args.command, time.time() - t0)


if __name__ == "__main__":
    main()
