"""Thread-safe TMDB v3 client with SQLite response cache and a global rate limit (plan §5.5.1)."""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from .settings import tmdb_token

log = logging.getLogger("tmdb")
BASE = "https://api.themoviedb.org/3"


class Tmdb:
    def __init__(self, conn, rps: float = 25.0):
        self.conn = conn
        self.lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.min_interval = 1.0 / rps
        self.next_at = 0.0
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {tmdb_token()}", "accept": "application/json"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
        )
        self.calls = 0
        self.cache_hits = 0

    def _throttle(self):
        with self.rate_lock:
            now = time.monotonic()
            wait = self.next_at - now
            self.next_at = max(now, self.next_at) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def get(self, path: str, params: dict | None = None, ttl: int = 7 * 86400, cache: bool = True):
        """GET with cache. cache=False skips the response cache (callers store trimmed data themselves)."""
        params = dict(params or {})
        key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        now = int(time.time())
        row = None
        if cache:
            with self.lock:
                row = self.conn.execute("SELECT json,fetched_at FROM tmdb_cache WHERE key=?", (key,)).fetchone()
            if row and now - row["fetched_at"] < ttl:
                self.cache_hits += 1
                return json.loads(row["json"]) if row["json"] else None
        data = None
        for attempt in range(4):
            self._throttle()
            try:
                r = self.client.get(BASE + path, params=params)
            except httpx.HTTPError as e:
                log.debug("tmdb %s error %s (attempt %d)", path, e, attempt + 1)
                time.sleep(1 + attempt * 2)
                continue
            self.calls += 1
            if r.status_code == 200:
                data = r.json()
                break
            if r.status_code == 404:
                data = None
                break
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", "2")))
                continue
            if r.status_code == 401:
                raise SystemExit("TMDB rejected the token (401). Check TMDB_READ_ACCESS.")
            time.sleep(1 + attempt * 2)
        else:
            # all attempts failed: fall back to a stale cached copy if we have one
            return json.loads(row["json"]) if row and row["json"] else None
        if not cache:
            return data
        with self.lock:
            self.conn.execute(
                "INSERT INTO tmdb_cache(key,json,fetched_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
                (key, json.dumps(data) if data is not None else None, now),
            )
        return data

    def close(self):
        self.client.close()
