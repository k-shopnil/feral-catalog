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

import base64

import httpx

from .settings import PAGES_BASE_URL, PAGES_BRANCH, PAGES_REPO, PAGES_REPO_SLUG, SITE, load_yaml

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


# ---------------------------------------------------------------------------------------------------------------
# Shared state between indexers (PC and phone). Both publish to the same gh-pages branch from their own copy of
# state.db, so a device whose state is older than what is already published must not publish: its catalog
# versions would go backwards and the TVs would sync a stale catalog.

class BehindError(RuntimeError):
    """The published catalog is newer than this device's state.db."""


def fetch_remote_manifest(timeout: float = 20.0) -> dict | None:
    """The manifest currently served by GitHub Pages, or None if it can't be fetched (caller decides)."""
    try:
        r = httpx.get(PAGES_BASE_URL + "manifest.json", params={"t": int(time.time())}, timeout=timeout,
                      headers={"Cache-Control": "no-cache"})
        if r.status_code == 200:
            return r.json()
        log.warning("remote manifest: HTTP %s", r.status_code)
    except Exception as e:  # noqa: BLE001 - network errors are reported, the caller decides whether to go on
        log.warning("remote manifest unavailable: %s", e)
    return None


def local_catalog_version(conn) -> int:
    return conn.execute("SELECT MAX(version) AS v FROM catalog_version").fetchone()["v"] or 0


def ensure_not_behind(conn, remote: dict | None) -> None:
    if not remote:
        return
    local = local_catalog_version(conn)
    published = int(remote.get("catalog_version") or 0)
    if published > local:
        raise BehindError(
            f"published catalog is version {published} but this device's state.db is at {local}. "
            "Another device has published since this state was copied. Import that device's state.db "
            "(python -m catalog import-state / the phone app's Import) before running here."
        )


def sync_site_from_pages(remote: dict | None) -> int:
    """Download the published snapshot/deltas that the manifest lists but site/ lacks (e.g. a fresh phone, or a
    PC after the phone published). Verifies sha256. Returns the number of files fetched."""
    if not remote:
        return 0
    wanted = []
    snap = remote.get("snapshot") or {}
    if snap.get("url"):
        wanted.append((snap["url"], snap.get("sha256")))
    wanted += [(d["url"], d.get("sha256")) for d in remote.get("deltas", []) if d.get("url")]
    fetched = 0
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
        for rel, sha in wanted:
            path = SITE / rel
            if path.exists() and (not sha or hashlib.sha256(path.read_bytes()).hexdigest() == sha):
                continue
            r = client.get(PAGES_BASE_URL + rel)
            r.raise_for_status()
            if sha and hashlib.sha256(r.content).hexdigest() != sha:
                raise RuntimeError(f"sha256 mismatch for {rel} downloaded from Pages")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            fetched += 1
            log.info("fetched %s from Pages (%d bytes)", rel, len(r.content))
    return fetched


# ---------------------------------------------------------------------------------------------------------------
# Publishing through the GitHub API (no git binary needed; used by the phone app).

def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def push_pages_api(token: str, dry_run: bool = False) -> bool:
    """Publish site/ to gh-pages as a single orphan commit, like push_pages(), through the Git Data API.

    Only blobs GitHub doesn't already have are uploaded. delta/ and snapshot/ follow site/ exactly (pruned files
    disappear, as with the git push); any other file that exists only on the branch is kept.
    dry_run: read the branch and report what would be uploaded, without writing anything.
    """
    api = f"https://api.github.com/repos/{PAGES_REPO_SLUG}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "feral-catalog"}
    try:
        with httpx.Client(headers=headers, timeout=httpx.Timeout(120.0, connect=20.0)) as gh:
            remote_tree: dict[str, str] = {}
            r = gh.get(f"{api}/git/ref/heads/{PAGES_BRANCH}")
            branch_exists = r.status_code == 200
            if branch_exists:
                head = r.json()["object"]["sha"]
                tree_sha = gh.get(f"{api}/git/commits/{head}").raise_for_status().json()["tree"]["sha"]
                t = gh.get(f"{api}/git/trees/{tree_sha}", params={"recursive": "1"}).raise_for_status().json()
                if t.get("truncated"):
                    raise RuntimeError("gh-pages tree listing truncated")
                remote_tree = {e["path"]: e["sha"] for e in t["tree"] if e["type"] == "blob"}
            elif r.status_code != 404:
                r.raise_for_status()

            known = set(remote_tree.values())
            entries, uploaded, up_bytes = [], 0, 0
            local_paths = set()
            for p in sorted(SITE.rglob("*")):
                if not p.is_file() or ".git" in p.relative_to(SITE).parts:
                    continue
                rel = p.relative_to(SITE).as_posix()
                data = p.read_bytes()
                sha = _git_blob_sha(data)
                if sha not in known and dry_run:
                    log.info("dry run: would upload %s (%d bytes)", rel, len(data))
                    known.add(sha)
                    uploaded += 1
                    up_bytes += len(data)
                elif sha not in known:
                    res = gh.post(f"{api}/git/blobs", json={"content": base64.b64encode(data).decode("ascii"),
                                                           "encoding": "base64"}).raise_for_status().json()
                    if res["sha"] != sha:
                        raise RuntimeError(f"blob sha mismatch for {rel}")
                    known.add(sha)
                    uploaded += 1
                    up_bytes += len(data)
                entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})
                local_paths.add(rel)
            for rel, sha in remote_tree.items():
                if rel not in local_paths and not rel.startswith(("delta/", "snapshot/")):
                    entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": sha})

            if dry_run:
                dropped = sorted(set(remote_tree) - local_paths - {e["path"] for e in entries})
                log.info("dry run: %d files, %d new blobs (%d bytes); branch files dropped: %s",
                         len(entries), uploaded, up_bytes, dropped or "none")
                return True
            tree = gh.post(f"{api}/git/trees", json={"tree": entries}).raise_for_status().json()["sha"]
            commit = gh.post(f"{api}/git/commits", json={
                "message": f"catalog {time.strftime('%Y-%m-%d %H:%M')}", "tree": tree, "parents": [],
            }).raise_for_status().json()["sha"]
            if branch_exists:
                gh.patch(f"{api}/git/refs/heads/{PAGES_BRANCH}", json={"sha": commit, "force": True}).raise_for_status()
            else:
                gh.post(f"{api}/git/refs", json={"ref": f"refs/heads/{PAGES_BRANCH}", "sha": commit}).raise_for_status()
        log.info("pushed site to gh-pages via API (%d files, %d new blobs, %d bytes uploaded) %s",
                 len(entries), uploaded, up_bytes, PAGES_BASE_URL)
        return True
    except httpx.HTTPStatusError as e:
        log.error("gh-pages API push failed: HTTP %s %s: %s", e.response.status_code, e.request.url.path,
                  e.response.text[:300])
    except Exception as e:  # noqa: BLE001 - reported as a failed push; the run summary shows it
        log.error("gh-pages API push failed: %s", e)
    return False
