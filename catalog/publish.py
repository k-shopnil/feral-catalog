"""Write versioned static catalog files and push them to the gh-pages branch (plan §4.5.5, §4.6.1, §6)."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .settings import PAGES_BASE_URL, PAGES_REPO, SITE, load_yaml

log = logging.getLogger("publish")

ENTITIES = ("titles", "seasons", "episodes", "versions")
SNAPSHOT_EVERY_CHANGES = 2000
SNAPSHOT_EVERY_SECONDS = 24 * 3600
KEEP_DAYS = 14
SCHEMA = 2  # 2 = NDJSON snapshots/deltas


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


ENTITY_TAG = {"titles": "title", "seasons": "season", "episodes": "episode", "versions": "version"}


def _write_gz(path: Path, obj) -> dict:
    """Write a delta/snapshot as gzipped NDJSON so the TV can stream it line by line (2 GB RAM device).

    Line 1: {"type":"header","from_version":a,"to_version":b}
    Then:   {"t":"title|season|episode|version","op":"u","d":{...}}  or  {"t":...,"op":"d","id":"..."}
    Order: titles, seasons, episodes, versions (parents before children).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "header", "from_version": obj["from_version"], "to_version": obj["to_version"]})]
    for e in ENTITIES:
        tag = ENTITY_TAG[e]
        for oid in obj[e]["delete"]:
            lines.append(json.dumps({"t": tag, "op": "d", "id": oid}, separators=(",", ":")))
        for d in obj[e]["upsert"]:
            lines.append(json.dumps({"t": tag, "op": "u", "d": d}, separators=(",", ":"), ensure_ascii=False))
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    data = gzip.compress(raw, compresslevel=9, mtime=0)
    path.write_bytes(data)
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def publish(conn, cat: dict, rows: dict, run_info: dict) -> dict:
    now = int(time.time())
    last = conn.execute("SELECT MAX(version) AS v FROM catalog_version").fetchone()["v"] or 0
    prev = {(r["entity"], r["id"]): r["hash"] for r in conn.execute("SELECT entity,id,hash FROM published")}
    upserts = {e: [] for e in ENTITIES}
    deletes = {e: [] for e in ENTITIES}
    current = set()
    new_hashes = []
    for e in ENTITIES:
        for oid, obj in cat[e].items():
            h = _hash(obj)
            current.add((e, oid))
            if prev.get((e, oid)) != h:
                upserts[e].append(obj)
                new_hashes.append((e, oid, h))
    for (e, oid) in prev:
        if (e, oid) not in current:
            deletes[e].append(oid)
    changes = sum(len(upserts[e]) + len(deletes[e]) for e in ENTITIES)

    snap_row = conn.execute("SELECT version, created_at FROM catalog_version WHERE kind='snapshot' ORDER BY version DESC LIMIT 1").fetchone()
    version = last
    if changes:
        version = last + 1
        if last:
            payload = {"from_version": last, "to_version": version}
            for e in ENTITIES:
                payload[e] = {"upsert": upserts[e], "delete": deletes[e]}
            _write_gz(SITE / "delta" / f"{last}-{version}.ndjson.gz", payload)
        conn.executemany(
            "INSERT INTO published(entity,id,hash,version) VALUES(?,?,?,?) ON CONFLICT(entity,id) DO UPDATE SET hash=excluded.hash, version=excluded.version",
            [(e, oid, h, version) for e, oid, h in new_hashes],
        )
        for e in ENTITIES:
            conn.executemany("DELETE FROM published WHERE entity=? AND id=?", [(e, oid) for oid in deletes[e]])
        changes_since_snap = changes + sum(
            r["changes"] for r in conn.execute("SELECT changes FROM catalog_version WHERE kind='delta' AND version > ?",
                                               (snap_row["version"] if snap_row else 0,))
        )
        need_snapshot = (snap_row is None or changes_since_snap > SNAPSHOT_EVERY_CHANGES
                         or now - snap_row["created_at"] > SNAPSHOT_EVERY_SECONDS)
        conn.execute("INSERT INTO catalog_version(version,created_at,changes,kind) VALUES(?,?,?,?)",
                     (version, now, changes, "snapshot" if need_snapshot or not last else "delta"))
        if need_snapshot or not last:
            snap = {"from_version": 0, "to_version": version}
            for e in ENTITIES:
                snap[e] = {"upsert": list(cat[e].values()), "delete": []}
            _write_gz(SITE / "snapshot" / f"{version}.ndjson.gz", snap)
        conn.commit()

    # manifest (always rewritten)
    snap_row = conn.execute("SELECT version FROM catalog_version WHERE kind='snapshot' ORDER BY version DESC LIMIT 1").fetchone()
    snap_v = snap_row["version"] if snap_row else version
    snap_path = SITE / "snapshot" / f"{snap_v}.ndjson.gz"
    deltas = []
    for r in conn.execute("SELECT version FROM catalog_version WHERE version > ? ORDER BY version", (snap_v,)):
        p = SITE / "delta" / f"{r['version'] - 1}-{r['version']}.ndjson.gz"
        if p.exists():
            data = p.read_bytes()
            deltas.append({"from": r["version"] - 1, "to": r["version"], "url": f"delta/{p.name}",
                           "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    rows_path = SITE / "rows.json"
    _write_json(rows_path, rows)
    config = load_yaml("app_config.yaml", {}) or {}
    config.setdefault("image_base_url", "https://image.tmdb.org/t/p/")
    config.setdefault("tmdb_attribution", "This product uses the TMDB API but is not endorsed or certified by TMDB.")
    config["version"] = _hash(config)[:12]
    _write_json(SITE / "config.json", config)
    snap_bytes = snap_path.read_bytes() if snap_path.exists() else b""
    manifest = {
        "schema": SCHEMA,
        "catalog_version": version,
        "generated_at": now,
        "snapshot": {"version": snap_v, "url": f"snapshot/{snap_v}.ndjson.gz",
                     "sha256": hashlib.sha256(snap_bytes).hexdigest(), "bytes": len(snap_bytes)},
        "deltas": deltas,
        "rows": {"url": "rows.json", "version": rows["version"]},
        "config": {"url": "config.json", "version": config["version"]},
        "counts": {e: len(cat[e]) for e in ENTITIES},
        "last_run": run_info,
    }
    _write_json(SITE / "manifest.json", manifest)
    (SITE / ".nojekyll").write_text("")
    _prune()
    return {"version": version, "changes": changes, "snapshot": snap_v, "deltas": len(deltas)}


def _prune():
    cutoff = time.time() - KEEP_DAYS * 86400
    for sub in ("delta", "snapshot"):
        files = sorted((SITE / sub).glob("*.ndjson.gz"), key=lambda p: p.stat().st_mtime)
        for p in files[:-2]:  # always keep the newest two
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)


def _git(*args, check=True):
    return subprocess.run(["git", "-C", str(SITE), *args], check=check, capture_output=True, text=True)


def push_pages() -> bool:
    """Force-push site/ as a single orphan commit to gh-pages (keeps branch history at one commit)."""
    if not (SITE / ".git").exists():
        _git("init", "-q", "-b", "gh-pages")
        _git("remote", "add", "origin", PAGES_REPO)
    _git("config", "core.autocrlf", "false")
    _git("checkout", "-q", "--orphan", "tmp-publish")
    _git("add", "-A")
    st = _git("status", "--porcelain")
    _git("commit", "-q", "-m", f"catalog {time.strftime('%Y-%m-%d %H:%M')}", "--allow-empty")
    _git("branch", "-D", "gh-pages", check=False)
    _git("branch", "-m", "gh-pages")
    r = _git("push", "-q", "-f", "origin", "gh-pages", check=False)
    if r.returncode != 0:
        log.error("gh-pages push failed: %s", r.stderr.strip()[:500])
        return False
    _git("gc", "-q", "--prune=now", check=False)
    log.info("pushed site to gh-pages (%s)", PAGES_BASE_URL)
    return True
