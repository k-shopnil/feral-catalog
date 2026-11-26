"""Incremental crawler over Apache autoindex listings (plan §5.3, PC mode §4.6.1).

Rules:
- Root dirs are listed every run.
- Show folders (direct children of `show` roots) are re-listed every run, because adding an
  episode to a season folder does not change the show folder's mtime in its parent listing.
- Any other dir is re-listed when its mtime in the parent listing changed, when it is new,
  or when it hasn't been listed for COLD_AFTER seconds (capped per run, oldest first).
- A dir holding a video that is still `stable = 0` and was last observed >= STABLE_AFTER seconds ago is
  re-listed, at any depth. Without this a fresh upload (seen < 2 h after its mtime, so `stable = 0`) is not
  observed again once its folder's mtime stops changing, and stays unpublished until the 24 h cold pass.
- A failed listing never marks children missing.
- One Crawler per source. State paths are `source.prefix + rel`; URLs and exclude patterns use `rel`.
"""
from __future__ import annotations

import fnmatch
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from urllib.parse import urljoin

import httpx

from .autoindex import Entry, parse_listing
from .settings import Root, Source

log = logging.getLogger("crawl")

COLD_AFTER = 24 * 3600
COLD_CAP_PER_RUN = 800
STABLE_AFTER = 600          # same size+mtime this long after the last observation -> stable
UNSTABLE_CAP_PER_RUN = 2000
MAX_DEPTH = 6

VIDEO_EXT = {"mkv", "mp4", "m4v", "avi", "webm", "ts", "m2ts", "mov", "wmv", "flv", "mpg", "mpeg"}
SUB_EXT = {"srt", "vtt", "ass", "ssa"}
PARTIAL_EXT = {"filepart", "part", "tmp", "crdownload", "!qb", "aria2"}


def classify_ext(name: str) -> tuple[str, str]:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in PARTIAL_EXT:
        return ext, "ignored"
    if ext in VIDEO_EXT:
        return ext, "video"
    if ext in SUB_EXT:
        return ext, "subtitle"
    return ext, "ignored"


def parse_mtime(s: str) -> int | None:
    try:
        return int(datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").timestamp())  # server local ≈ PC local (BD)
    except Exception:
        return None


class Crawler:
    def __init__(self, conn, source: Source):
        self.conn = conn
        self.src = source
        self.client = httpx.Client(
            headers={"User-Agent": source.user_agent},
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=source.max_concurrency, max_keepalive_connections=source.max_concurrency),
        )
        self.now = int(time.time())
        self.stats = {"listed": 0, "failed": 0, "new_dirs": 0, "new_files": 0, "missing": 0, "unstable_dirs": 0}

    # ---------- helpers ----------
    def excluded(self, path: str) -> bool:
        path = self.src.rel_of(path)
        for pat in self.src.exclude:
            if pat.startswith("**/"):
                seg = pat[3:].rstrip("/")
                if f"/{seg}/" in f"/{path}":
                    return True
            elif path.startswith(pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def root_for(self, path: str) -> Root | None:
        best = None
        for r in self.src.roots:
            if path.startswith(r.path) and (best is None or len(r.path) > len(best.path)):
                best = r
        return best

    def fetch(self, url: str) -> str | None:
        for attempt in range(3):
            try:
                r = self.client.get(url)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (403, 404):
                    log.warning("listing %s -> HTTP %s", url, r.status_code)
                    return None
            except httpx.HTTPError as e:
                log.debug("listing %s failed (%s), retry %d", url, e, attempt + 1)
            time.sleep(2 * (attempt + 1))
        return None

    # ---------- main ----------
    def run(self, only_roots: list[str] | None = None, full: bool = False) -> dict:
        conn = self.conn
        roots = [r for r in self.src.roots if not only_roots or any(r.path.startswith(o) for o in only_roots)]
        # root path -> (url, root, depth)
        queue: list[tuple[str, str, Root, int]] = []
        root_paths = {r.path for r in self.src.roots}
        for r in roots:
            url = urljoin(self.src.base_url, "/".join(_quote_seg(p) for p in r.rel.rstrip("/").split("/")) + "/")
            conn.execute(
                "INSERT INTO dir_node(path,url,parent_path,root,depth,last_seen_at) VALUES(?,?,?,?,0,?) "
                "ON CONFLICT(path) DO UPDATE SET url=excluded.url",
                (r.path, url, None, r.path, self.now),
            )
            queue.append((r.path, url, r, 0))
        conn.commit()

        byroot = {r.path: r for r in roots}
        if not full:
            # dirs with videos still waiting to turn stable: re-list so they can flip on this run
            unstable = conn.execute(
                "SELECT d.path,d.url,d.root,d.depth,MIN(f.obs_at) AS since FROM file f "
                "JOIN dir_node d ON d.path=f.dir_path "
                "WHERE f.role='video' AND f.stable=0 AND f.missing_since IS NULL AND f.obs_at<=? "
                "AND d.missing_since IS NULL GROUP BY d.path ORDER BY since LIMIT ?",
                (self.now - STABLE_AFTER, UNSTABLE_CAP_PER_RUN),
            ).fetchall()
            for row in unstable:
                r = byroot.get(row["root"])
                if r:
                    queue.append((row["path"], row["url"], r, row["depth"]))
                    self.stats["unstable_dirs"] += 1
            # cold re-verification of stale dirs (oldest first)
            cold = conn.execute(
                "SELECT path,url,root,depth FROM dir_node WHERE missing_since IS NULL AND depth>0 "
                "AND (last_listed_at IS NULL OR last_listed_at < ?) ORDER BY last_listed_at LIMIT ?",
                (self.now - COLD_AFTER, COLD_CAP_PER_RUN),
            ).fetchall()
            for row in cold:
                r = byroot.get(row["root"])
                if r:
                    queue.append((row["path"], row["url"], r, row["depth"]))

        seen: set[str] = set()
        pending: dict[Future, tuple[str, str, Root, int]] = {}
        with ThreadPoolExecutor(max_workers=self.src.max_concurrency) as pool:
            def submit(item):
                if item[0] in seen:
                    return
                seen.add(item[0])
                pending[pool.submit(self.fetch, item[1])] = item

            for item in queue:
                submit(item)
            while pending:
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for fut in done:
                    path, url, root, depth = pending.pop(fut)
                    html = fut.result()
                    if html is None:
                        self.stats["failed"] += 1
                        continue
                    self.stats["listed"] += 1
                    for child in self.apply_listing(path, url, root, depth, parse_listing(html), full, root_paths):
                        submit(child)
                    if self.stats["listed"] % 200 == 0:
                        conn.commit()
                        log.info("listed %d dirs (%d pending)", self.stats["listed"], len(pending))
        conn.commit()
        self.client.close()
        return self.stats

    def apply_listing(self, path, url, root: Root, depth, entries: list[Entry], full, root_paths):
        conn, now = self.conn, self.now
        conn.execute("UPDATE dir_node SET last_listed_at=?, missing_since=NULL WHERE path=?", (now, path))
        children_to_list = []
        present_dirs, present_files = set(), set()
        for e in entries:
            child_path = f"{path}{e.name}/" if e.is_dir else f"{path}{e.name}"
            if e.name.startswith(".") or self.excluded(child_path):
                continue
            if depth == 0 and any(child_path.startswith(path + x) for x in root.exclude_children):
                continue
            if e.is_dir and child_path in root_paths and child_path != path:
                continue  # handled as its own root
            child_url = urljoin(url, e.href)
            if e.is_dir:
                present_dirs.add(child_path)
                row = conn.execute("SELECT listed_mtime,last_listed_at FROM dir_node WHERE path=?", (child_path,)).fetchone()
                if row is None:
                    self.stats["new_dirs"] += 1
                    conn.execute(
                        "INSERT INTO dir_node(path,url,parent_path,root,depth,listed_mtime,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                        (child_path, child_url, path, root.path, depth + 1, e.mtime, now),
                    )
                    changed = True
                else:
                    changed = row["listed_mtime"] != e.mtime or row["last_listed_at"] is None
                    conn.execute(
                        "UPDATE dir_node SET listed_mtime=?, last_seen_at=?, missing_since=NULL, url=? WHERE path=?",
                        (e.mtime, now, child_url, child_path),
                    )
                show_folder = root.kind == "show" and depth == 0
                if depth + 1 <= MAX_DEPTH and (changed or show_folder or full):
                    children_to_list.append((child_path, child_url, root, depth + 1))
            else:
                present_files.add(child_path)
                ext, role = classify_ext(e.name)
                sig = f"{e.size_human}|{e.mtime}"
                added = parse_mtime(e.mtime)
                row = conn.execute("SELECT obs_sig,obs_at,stable FROM file WHERE path=?", (child_path,)).fetchone()
                if row is None:
                    self.stats["new_files"] += 1
                    old_enough = added is not None and added < now - 2 * 3600
                    conn.execute(
                        "INSERT INTO file(path,url,dir_path,root,name,ext,role,size_human,size_bytes,listed_mtime,added_at,"
                        "first_seen_at,last_seen_at,obs_sig,obs_at,stable) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (child_path, child_url, path, root.path, e.name, ext, role, e.size_human, e.size_bytes,
                         e.mtime, added, now, now, sig, now, 1 if old_enough else 0),
                    )
                else:
                    if row["obs_sig"] != sig:
                        conn.execute(
                            "UPDATE file SET size_human=?,size_bytes=?,listed_mtime=?,added_at=?,obs_sig=?,obs_at=?,stable=0,"
                            "parsed=0,last_seen_at=?,missing_since=NULL,url=? WHERE path=?",
                            (e.size_human, e.size_bytes, e.mtime, added, sig, now, now, child_url, child_path),
                        )
                    else:
                        stable = 1 if (row["stable"] or now - (row["obs_at"] or now) >= STABLE_AFTER) else 0
                        conn.execute(
                            "UPDATE file SET last_seen_at=?, missing_since=NULL, stable=?, url=? WHERE path=?",
                            (now, stable, child_url, child_path),
                        )
        # mark vanished children (only direct children of this successfully listed dir)
        for row in conn.execute("SELECT path FROM dir_node WHERE parent_path=? AND missing_since IS NULL", (path,)).fetchall():
            if row["path"] not in present_dirs:
                self._mark_missing_tree(row["path"])
        for row in conn.execute("SELECT path FROM file WHERE dir_path=? AND missing_since IS NULL", (path,)).fetchall():
            if row["path"] not in present_files:
                conn.execute("UPDATE file SET missing_since=? WHERE path=?", (now, row["path"]))
                self.stats["missing"] += 1
        return children_to_list

    def _mark_missing_tree(self, dpath: str):
        now = self.now
        n = len(dpath)  # exact prefix match (LIKE would treat '_' in names as a wildcard)
        self.conn.execute("UPDATE dir_node SET missing_since=? WHERE substr(path,1,?)=? AND missing_since IS NULL",
                          (now, n, dpath))
        cur = self.conn.execute("UPDATE file SET missing_since=? WHERE substr(path,1,?)=? AND missing_since IS NULL",
                                (now, n, dpath))
        self.stats["missing"] += cur.rowcount


def _quote_seg(seg: str) -> str:
    from urllib.parse import quote
    return quote(seg, safe="()-_.!~*'")
