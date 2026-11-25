"""State hand-off between devices through the owner's Google Drive.

Whichever device runs the indexer (PC or phone) first takes a lock, downloads the newest state.db from Drive if it
is newer than its own, runs, then uploads its state.db and releases the lock. No manual export/import.

Drive layout (scope drive.file: the app can only see files it created itself):
  feral-catalog-state/
    state-g000012-v8.db.gz   gzip of a consistent state.db copy; appProperties: generation, catalog_version,
                             device, sha256 (of the uncompressed db). The newest KEEP generations are kept.
    lock.json                one per holder; appProperties: owner, expires. Expired locks are ignored.

Generation: a counter in meta('state_generation'), +1 on every upload. A device pulls when Drive's newest
generation is higher than its own.

Credentials (env, from feral-catalog/.env on the PC, baked into the phone app):
  GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN   (python -m catalog drive-auth creates the last one)
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .settings import DATA, ROOT

log = logging.getLogger("drive")

SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER = "feral-catalog-state"
KEEP = 10                       # state generations kept on Drive (rollback points)
LOCAL_BACKUPS = 1               # state.db.bak-* files kept on this device after a pull
LOCK_TTL = 4 * 3600             # a lock older than this is treated as abandoned
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024         # resumable upload chunk (multiple of 256 KiB)


class DriveError(RuntimeError):
    pass


class LockedError(DriveError):
    pass


def configured() -> bool:
    return all(os.environ.get(k, "").strip() for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"))


def device_name() -> str:
    return os.environ.get("FERAL_DEVICE", "").strip() or socket.gethostname() or "pc"


# ------------------------------------------------------------------------------------------------ HTTP

class Drive:
    def __init__(self):
        if not configured():
            raise DriveError("Google Drive is not set up (GDRIVE_CLIENT_ID / _SECRET / _REFRESH_TOKEN missing)")
        self.http = httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True)
        self._token = ""
        self._token_exp = 0.0
        self._folder = None

    def close(self):
        self.http.close()

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.http.post(TOKEN_URL, data={
            "client_id": os.environ["GDRIVE_CLIENT_ID"].strip(),
            "client_secret": os.environ["GDRIVE_CLIENT_SECRET"].strip(),
            "refresh_token": os.environ["GDRIVE_REFRESH_TOKEN"].strip(),
            "grant_type": "refresh_token",
        })
        if r.status_code != 200:
            raise DriveError(f"Google sign-in refresh failed: HTTP {r.status_code} {r.text[:200]} "
                             "(re-run: python -m catalog drive-auth)")
        d = r.json()
        self._token, self._token_exp = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        return self._token

    def req(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(4):
            h = {"Authorization": f"Bearer {self.token()}", **kw.pop("headers", {})}
            try:
                r = self.http.request(method, url, headers=h, **kw)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise DriveError(f"Drive not reachable: {type(e).__name__}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise DriveError(f"Drive {method} {url.split('?')[0]}: HTTP {r.status_code} {r.text[:300]}")
            return r
        raise DriveError("unreachable")

    # ------------------------------------------------------------------ files
    def folder(self) -> str:
        if self._folder:
            return self._folder
        q = f"name = '{FOLDER}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        files = self.req("GET", f"{API}/files", params={"q": q, "fields": "files(id)", "spaces": "drive"}).json()["files"]
        if files:
            self._folder = files[0]["id"]
        else:
            r = self.req("POST", f"{API}/files", params={"fields": "id"},
                         json={"name": FOLDER, "mimeType": "application/vnd.google-apps.folder"})
            self._folder = r.json()["id"]
            log.info("drive: created folder %s", FOLDER)
        return self._folder

    def list(self, name_q: str) -> list[dict]:
        q = f"'{self.folder()}' in parents and trashed = false and {name_q}"
        out, page = [], None
        while True:
            p = {"q": q, "fields": "nextPageToken, files(id,name,size,createdTime,appProperties)", "pageSize": 100}
            if page:
                p["pageToken"] = page
            d = self.req("GET", f"{API}/files", params=p).json()
            out += d.get("files", [])
            page = d.get("nextPageToken")
            if not page:
                return out

    def delete(self, file_id: str) -> None:
        try:
            self.req("DELETE", f"{API}/files/{file_id}")
        except DriveError as e:
            if "HTTP 404" not in str(e):
                raise

    def states(self) -> list[dict]:
        """State uploads, newest generation first."""
        files = self.list("name contains 'state-g'")
        for f in files:
            f["generation"] = int((f.get("appProperties") or {}).get("generation") or 0)
        return sorted(files, key=lambda f: f["generation"], reverse=True)

    def head(self) -> dict | None:
        s = self.states()
        return s[0] if s else None

    def download(self, f: dict, dest: Path) -> None:
        total = int(f.get("size") or 0)
        done, last = 0, 0.0
        with self.http.stream("GET", f"{API}/files/{f['id']}", params={"alt": "media"},
                              headers={"Authorization": f"Bearer {self.token()}"}) as r:
            if r.status_code != 200:
                raise DriveError(f"download {f['name']}: HTTP {r.status_code}")
            with open(dest, "wb") as out:
                for chunk in r.iter_bytes(1024 * 1024):
                    out.write(chunk)
                    done += len(chunk)
                    if time.monotonic() - last > 1:
                        last = time.monotonic()
                        log.info("drive: downloaded %d/%d KB", done // 1024, max(total, done) // 1024)
        log.info("drive: downloaded %d/%d KB", done // 1024, max(total, done) // 1024)

    def upload(self, src: Path, name: str, props: dict) -> dict:
        size = src.stat().st_size
        meta = {"name": name, "parents": [self.folder()], "appProperties": {k: str(v) for k, v in props.items()}}
        r = self.req("POST", UPLOAD, params={"uploadType": "resumable", "fields": "id,name,size"},
                     headers={"X-Upload-Content-Type": "application/gzip", "X-Upload-Content-Length": str(size)},
                     json=meta)
        session = r.headers["Location"]
        sent = 0
        with open(src, "rb") as fh:
            while sent < size:
                fh.seek(sent)
                chunk = fh.read(CHUNK)
                end = sent + len(chunk) - 1
                for attempt in range(4):
                    try:
                        resp = self.http.put(session, content=chunk, headers={
                            "Content-Length": str(len(chunk)), "Content-Range": f"bytes {sent}-{end}/{size}"})
                    except httpx.TransportError as e:
                        if attempt == 3:
                            raise DriveError(f"upload interrupted: {e}") from e
                        time.sleep(2 ** attempt)
                        continue
                    break
                if resp.status_code in (200, 201):
                    log.info("drive: uploaded %d/%d KB", size // 1024, size // 1024)
                    return resp.json()
                if resp.status_code != 308:
                    raise DriveError(f"upload {name}: HTTP {resp.status_code} {resp.text[:200]}")
                rng = resp.headers.get("Range")  # bytes=0-N received so far
                sent = int(rng.split("-")[1]) + 1 if rng else 0
                log.info("drive: uploaded %d/%d KB", sent // 1024, size // 1024)
        raise DriveError(f"upload {name}: ended without a final response")

    # ------------------------------------------------------------------ lock
    def acquire_lock(self, owner: str) -> str:
        now = int(time.time())
        live = []
        for f in self.list("name = 'lock.json'"):
            p = f.get("appProperties") or {}
            if int(p.get("expires") or 0) < now:
                log.warning("drive: removing abandoned lock held by %s", p.get("owner"))
                self.delete(f["id"])
            else:
                live.append(f)
        others = [f for f in live if (f.get("appProperties") or {}).get("owner") != owner]
        if others:
            p = others[0].get("appProperties") or {}
            raise LockedError(f"{p.get('owner')} is running the indexer (since "
                              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(int(p.get('since') or 0)))}). "
                              "Wait for it to finish, or clear a stuck lock with: python -m catalog drive-unlock")
        for f in live:  # our own leftover lock from a crashed run
            self.delete(f["id"])
        nonce = secrets.token_hex(8)
        r = self.req("POST", f"{API}/files", params={"fields": "id"}, json={
            "name": "lock.json", "parents": [self.folder()],
            "appProperties": {"owner": owner, "since": str(now), "expires": str(now + LOCK_TTL), "nonce": nonce}})
        lock_id = r.json()["id"]
        # two devices starting in the same second: the lock created first wins, the other backs off
        live = sorted(self.list("name = 'lock.json'"), key=lambda f: (f["createdTime"], f["id"]))
        if live and live[0]["id"] != lock_id:
            self.delete(lock_id)
            p = live[0].get("appProperties") or {}
            raise LockedError(f"{p.get('owner')} started the indexer at the same moment; try again later")
        log.info("drive: lock taken by %s", owner)
        return lock_id

    def release_lock(self, lock_id: str) -> None:
        self.delete(lock_id)
        log.info("drive: lock released")


# ------------------------------------------------------------------------------------------------ state

def _local_generation(db: Path) -> int | None:
    """Generation of the state.db at `db`, or None when there is no usable local state."""
    if not db.exists():
        return None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = c.execute("SELECT value FROM meta WHERE key = 'state_generation'").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        return None
    finally:
        c.close()


def _prune_local_backups() -> None:
    baks = sorted(DATA.glob("state.db.bak-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in baks[LOCAL_BACKUPS:]:
        p.unlink(missing_ok=True)


def pull_if_newer(drive: Drive) -> dict:
    """Replace data/state.db with Drive's newest state if that is newer. Call before the db is opened."""
    from .__main__ import import_state

    db = DATA / "state.db"
    local = _local_generation(db)
    head = drive.head()
    info = {"local_generation": local, "drive_generation": head["generation"] if head else None, "pulled": False}
    if not head:
        log.info("drive: no state on Drive yet; this device's state will be the first upload")
        return info
    if local is not None and local >= head["generation"]:
        log.info("drive: local state (generation %s) is current", local)
        return info
    p = head.get("appProperties") or {}
    log.info("drive: pulling %s (generation %s from %s, catalog v%s; local generation %s)",
             head["name"], head["generation"], p.get("device"), p.get("catalog_version"), local)
    gz, raw = DATA / "state.db.download.gz", DATA / "state.db.download"
    try:
        drive.download(head, gz)
        h = hashlib.sha256()
        with gzip.open(gz, "rb") as src, open(raw, "wb") as out:
            while b := src.read(1024 * 1024):
                h.update(b)
                out.write(b)
        if p.get("sha256") and h.hexdigest() != p["sha256"]:
            raise DriveError(f"{head['name']} failed its checksum; local state left unchanged")
        import_state(str(raw))
        _prune_local_backups()
    finally:
        # the read-only open in import_state leaves -wal/-shm next to a WAL-mode copy
        for f in (gz, raw, raw.with_name(raw.name + "-wal"), raw.with_name(raw.name + "-shm")):
            f.unlink(missing_ok=True)
    info.update(pulled=True, catalog_version=p.get("catalog_version"), from_device=p.get("device"))
    return info


def push(drive: Drive, conn, device: str) -> dict:
    """Upload a consistent copy of the open state db as the next generation, then trim old generations."""
    from .db import get_meta, set_meta
    from .publish import local_catalog_version

    head = drive.head()
    gen = max(head["generation"] if head else 0, int(get_meta(conn, "state_generation") or 0)) + 1
    set_meta(conn, "state_generation", gen)
    conn.commit()
    version = local_catalog_version(conn)
    snap, gz = DATA / "state.db.upload", DATA / "state.db.upload.gz"
    try:
        snap.unlink(missing_ok=True)
        out = sqlite3.connect(snap)
        with out:
            conn.backup(out)
        out.close()
        h = hashlib.sha256()
        with open(snap, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            while b := src.read(1024 * 1024):
                h.update(b)
                dst.write(b)
        name = f"state-g{gen:06d}-v{version}.db.gz"
        log.info("drive: uploading %s (%d MB compressed)", name, gz.stat().st_size // (1024 * 1024))
        drive.upload(gz, name, {"generation": gen, "catalog_version": version, "device": device,
                                "sha256": h.hexdigest(), "uploaded_at": int(time.time())})
    finally:
        snap.unlink(missing_ok=True)
        gz.unlink(missing_ok=True)
    removed = 0
    for old in drive.states()[KEEP:]:
        drive.delete(old["id"])
        removed += 1
    log.info("drive: state generation %d uploaded (catalog v%s); %d old generation(s) removed", gen, version, removed)
    return {"generation": gen, "catalog_version": version, "removed": removed}


def status() -> dict:
    d = Drive()
    try:
        states = d.states()
        locks = d.list("name = 'lock.json'")
        return {
            "local_generation": _local_generation(DATA / "state.db"),
            "states": [{"name": s["name"], "generation": s["generation"], "size_mb": round(int(s.get("size") or 0) / 1e6, 1),
                        "device": (s.get("appProperties") or {}).get("device"), "created": s["createdTime"]} for s in states],
            "locks": [f.get("appProperties") for f in locks],
        }
    finally:
        d.close()


def unlock() -> int:
    d = Drive()
    try:
        locks = d.list("name = 'lock.json'")
        for f in locks:
            d.delete(f["id"])
        return len(locks)
    finally:
        d.close()


# ------------------------------------------------------------------------------------------------ one-time sign-in

def authorize(client_file: str, env_file: Path | None = None) -> None:
    """Loopback OAuth sign-in (run once on the PC). Saves the Drive credentials into feral-catalog/.env."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    cfg = json.loads(Path(client_file).read_text(encoding="utf-8"))
    cfg = cfg.get("installed") or cfg.get("web") or cfg
    cid, csecret = cfg["client_id"], cfg["client_secret"]
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("feral catalog: signed in. You can close this tab.".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("Open this link in your browser on this PC and allow access:\n" + url, flush=True)
    srv.timeout = 900
    deadline = time.time() + 900
    while not got and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise SystemExit(f"sign-in did not complete: {got.get('error', 'timed out')}")
    r = httpx.post(TOKEN_URL, data={"client_id": cid, "client_secret": csecret, "code": got["code"],
                                    "code_verifier": verifier, "grant_type": "authorization_code",
                                    "redirect_uri": redirect}, timeout=30)
    if r.status_code != 200 or "refresh_token" not in r.json():
        raise SystemExit(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    env_file = env_file or ROOT / ".env"
    values = {"GDRIVE_CLIENT_ID": cid, "GDRIVE_CLIENT_SECRET": csecret, "GDRIVE_REFRESH_TOKEN": r.json()["refresh_token"]}
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values] + [f"{k}={v}" for k, v in values.items()]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for k, v in values.items():
        os.environ[k] = v
    print(f"Signed in. Drive credentials saved to {env_file}", flush=True)
