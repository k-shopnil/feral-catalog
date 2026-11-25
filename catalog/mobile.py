"""Entry points for the Android "feral catalog" app (called from Kotlin through Chaquopy).

The phone runs exactly the same pipeline as the PC (`python -m catalog run`): this module only
  * points the indexer at the app's private storage (FERAL_HOME / FERAL_CONFIG) and passes the tokens,
  * runs connectivity checks over the phone's current network,
  * forwards stages, progress and log lines to the app through a listener object,
  * publishes through the GitHub API instead of `git push` (no git binary on the phone).

Listener (a Kotlin object) methods used here:
  onCheck(id, label, status, detail)       status: "ok" | "fail" | "skip"
  onStage(id, state, detail_json)          state:  "start" | "done" | "failed" | "skipped"
  onProgress(id, done, total)
  onLog(level, line)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from argparse import Namespace

STAGES = ("checks", "handoff", "sync", "crawl", "process", "match", "enrich", "publish", "push", "upload")

_PROGRESS = [
    (re.compile(r"^listed (\d+) dirs \((\d+) pending\)"), "crawl", lambda a, b: (a, a + b)),
    (re.compile(r"^matched (\d+)/(\d+)"), "match", lambda a, b: (a, b)),
    (re.compile(r"^details (\d+)/(\d+)"), "enrich", lambda a, b: (a, b)),
    (re.compile(r"^drive: downloaded (\d+)/(\d+) KB"), "handoff", lambda a, b: (a, b)),
    (re.compile(r"^drive: uploaded (\d+)/(\d+) KB"), "upload", lambda a, b: (a, b)),
]


def _configure(home: str, config_dir: str, tmdb_token: str = "", fanart_key: str = "", drive: str = "") -> None:
    """Must run before the first import of catalog.settings (paths are fixed at import time)."""
    os.environ["FERAL_HOME"] = home
    os.environ["FERAL_CONFIG"] = config_dir
    # rapidfuzz ships no Android build; its bundled pure-Python implementation gives the same scores.
    os.environ["RAPIDFUZZ_IMPLEMENTATION"] = "python"
    if tmdb_token:
        os.environ["TMDB_READ_ACCESS"] = tmdb_token.strip()
    if fanart_key:
        # optional: Fanart.tv PNG title logos when TMDB has none (catalog/enrich.py)
        os.environ["FANART_API_KEY"] = fanart_key.strip()
    if drive:
        # Google Drive state hand-off (catalog/gdrive.py): JSON {client_id, client_secret, refresh_token}
        d = json.loads(drive)
        for k in ("client_id", "client_secret", "refresh_token"):
            if (d.get(k) or "").strip():
                os.environ[f"GDRIVE_{k.upper()}"] = d[k].strip()
    os.environ.setdefault("FERAL_DEVICE", "phone")


class _Bridge(logging.Handler):
    def __init__(self, listener):
        super().__init__(logging.INFO)
        self.listener = listener
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = record.getMessage()
            for rx, stage, conv in _PROGRESS:
                m = rx.match(msg)
                if m:
                    done, total = conv(int(m.group(1)), int(m.group(2)))
                    self.listener.onProgress(stage, done, total)
            self.listener.onLog(record.levelname, self.format(record))
        except Exception:  # noqa: BLE001 - logging must never break the run
            pass


def _json(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


# ------------------------------------------------------------------------------------------------ checks

def _checks(listener, github_token: str, publish: bool) -> bool:
    import httpx

    from .crawler import _quote_seg
    from .db import DB_PATH, connect
    from .publish import fetch_remote_manifest, local_catalog_version
    from .settings import PAGES_BRANCH, PAGES_REPO_SLUG, fanart_key, load_source, tmdb_token

    log = logging.getLogger("checks")
    ok_all = True

    def report(cid, label, status, detail):
        nonlocal ok_all
        if status == "fail":
            ok_all = False
        (log.info if status != "fail" else log.error)("%s: %s - %s", label, status.upper(), detail)
        listener.onCheck(cid, label, status, detail)

    src = load_source()
    ua = {"User-Agent": src.user_agent}

    # 1. storage server root listing
    t0 = time.monotonic()
    try:
        r = httpx.get(src.base_url, headers=ua, timeout=httpx.Timeout(20.0, connect=10.0))
        disks = len(set(re.findall(r'href="(disk\d+/)"', r.text)))
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200 and disks:
            report("storage", "Storage server", "ok", f"{src.base_url} HTTP 200 in {ms} ms, {disks} disks listed")
        else:
            report("storage", "Storage server", "fail", f"{src.base_url} HTTP {r.status_code}, {disks} disks in listing")
    except Exception as e:  # noqa: BLE001
        report("storage", "Storage server", "fail",
               f"{src.base_url} not reachable from this network ({type(e).__name__}: {e}). "
               "The server is only reachable from BDIX-connected networks.")

    # 2. one indexed folder (proves deep paths list, not only the root page)
    root = src.roots[0]
    url = src.base_url + "/".join(_quote_seg(p) for p in root.path.rstrip("/").split("/")) + "/"
    t0 = time.monotonic()
    try:
        r = httpx.get(url, headers=ua, timeout=httpx.Timeout(60.0, connect=10.0))
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            report("folder", "Indexed folder", "ok", f"{root.path} HTTP 200, {len(r.content) // 1024} KB in {ms} ms")
        else:
            report("folder", "Indexed folder", "fail", f"{root.path} HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        report("folder", "Indexed folder", "fail", f"{root.path}: {type(e).__name__}: {e}")

    # 3. TMDB
    try:
        tok = tmdb_token()
    except SystemExit:
        tok = ""
    if not tok:
        report("tmdb", "TMDB", "fail", "TMDB token is not set (Setup → TMDB read access token)")
    else:
        try:
            r = httpx.get("https://api.themoviedb.org/3/authentication",
                          headers={"Authorization": f"Bearer {tok}", "accept": "application/json"}, timeout=20)
            if r.status_code == 200 and r.json().get("success"):
                report("tmdb", "TMDB", "ok", "token accepted")
            else:
                report("tmdb", "TMDB", "fail", f"HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:  # noqa: BLE001
            report("tmdb", "TMDB", "fail", f"{type(e).__name__}: {e}")

    # 3b. Fanart.tv (optional logo fallback: never fails the checks)
    fkey = fanart_key()
    if not fkey:
        report("fanart", "Fanart.tv logos", "skip", "no key: logos come from TMDB only")
    else:
        try:
            r = httpx.get("https://webservice.fanart.tv/v3/movies/603", params={"api_key": fkey}, timeout=20)
            if r.status_code == 200:
                report("fanart", "Fanart.tv logos", "ok", "key accepted")
            else:
                report("fanart", "Fanart.tv logos", "skip", f"HTTP {r.status_code}: logos come from TMDB only")
        except Exception as e:  # noqa: BLE001
            report("fanart", "Fanart.tv logos", "skip", f"not reachable ({type(e).__name__}): logos come from TMDB only")

    # 4. published catalog (GitHub Pages)
    remote = fetch_remote_manifest()
    if remote:
        gen = time.strftime("%Y-%m-%d %H:%M", time.localtime(remote.get("generated_at") or 0))
        report("pages", "Published catalog", "ok", f"version {remote.get('catalog_version')}, generated {gen}")
    else:
        report("pages", "Published catalog", "fail", "manifest.json on GitHub Pages could not be fetched")

    # 5. GitHub write access
    if not publish:
        report("github", "GitHub publish", "skip", "publishing is off for this run")
    elif not github_token.strip():
        report("github", "GitHub publish", "fail", "GitHub token is not set (Setup → GitHub token)")
    else:
        try:
            h = {"Authorization": f"Bearer {github_token.strip()}", "Accept": "application/vnd.github+json",
                 "User-Agent": "feral-catalog"}
            r = httpx.get(f"https://api.github.com/repos/{PAGES_REPO_SLUG}", headers=h, timeout=20)
            if r.status_code != 200:
                report("github", "GitHub publish", "fail", f"token can't read {PAGES_REPO_SLUG}: HTTP {r.status_code}")
            else:
                perms = r.json().get("permissions") or {}
                ref = httpx.get(f"https://api.github.com/repos/{PAGES_REPO_SLUG}/git/ref/heads/{PAGES_BRANCH}",
                                headers=h, timeout=20)
                if perms and not perms.get("push"):
                    report("github", "GitHub publish", "fail", "token has no write (push) access to the repo")
                elif ref.status_code not in (200, 404):
                    report("github", "GitHub publish", "fail", f"reading {PAGES_BRANCH}: HTTP {ref.status_code}")
                else:
                    report("github", "GitHub publish", "ok",
                           f"token can access {PAGES_REPO_SLUG}; write access is confirmed when publishing")
        except Exception as e:  # noqa: BLE001
            report("github", "GitHub publish", "fail", f"{type(e).__name__}: {e}")

    # 5b. Google Drive state hand-off
    from . import gdrive

    drive_head = None
    if not gdrive.configured():
        report("drive", "Drive hand-off", "skip", "not set up: state moves between devices by export/import")
    else:
        d = None
        try:
            d = gdrive.Drive()
            drive_head = d.head()
            locks = [f.get("appProperties") or {} for f in d.list("name = 'lock.json'")]
            busy = [lk for lk in locks if lk.get("owner") != gdrive.device_name()
                    and int(lk.get("expires") or 0) > time.time()]
            if busy:
                report("drive", "Drive hand-off", "fail", f"{busy[0].get('owner')} is running the indexer right now")
            elif drive_head:
                hp = drive_head.get("appProperties") or {}
                report("drive", "Drive hand-off", "ok", f"newest state: generation {drive_head['generation']} "
                       f"(catalog v{hp.get('catalog_version')}, from {hp.get('device')})")
            else:
                report("drive", "Drive hand-off", "ok", "signed in; no state uploaded yet")
        except Exception as e:  # noqa: BLE001
            report("drive", "Drive hand-off", "fail", f"{type(e).__name__}: {e}")
        finally:
            if d:
                d.close()
    drive_version = int(((drive_head or {}).get("appProperties") or {}).get("catalog_version") or 0)
    published = int((remote or {}).get("catalog_version") or 0)

    # 6. local state vs published
    if not DB_PATH.exists() and drive_head:
        report("state", "Catalog state", "ok", f"no local state yet: the run downloads Drive's (catalog v{drive_version})")
    elif not DB_PATH.exists():
        report("state", "Catalog state", "fail", "no state.db yet: import the PC's state.db first (Setup → Import state)")
    else:
        conn = connect()
        try:
            local = local_catalog_version(conn)
        finally:
            conn.close()
        size = DB_PATH.stat().st_size // (1024 * 1024)
        if remote and published > local and drive_version >= published:
            report("state", "Catalog state", "ok",
                   f"state.db version {local} is older than published {published}; the run downloads Drive's newer state")
        elif remote and published > local:
            report("state", "Catalog state", "fail",
                   f"state.db is at version {local} but version {published} is published: another device has "
                   "published since this copy. Import its newer state.db.")
        else:
            report("state", "Catalog state", "ok", f"state.db version {local} ({size} MB), published {published or '?'}")
    return ok_all


def checks(listener, home: str, config_dir: str, tmdb_token: str, github_token: str, publish: bool,
           fanart_key: str = "", drive: str = "") -> bool:
    _configure(home, config_dir, tmdb_token, fanart_key, drive)
    root = logging.getLogger()
    bridge = _Bridge(listener)
    root.addHandler(bridge)
    root.setLevel(logging.INFO)
    try:
        return _checks(listener, github_token, publish)
    except Exception:  # noqa: BLE001 - shown to the user instead of crashing the app
        listener.onLog("ERROR", traceback.format_exc())
        return False
    finally:
        root.removeHandler(bridge)


# ------------------------------------------------------------------------------------------------ run

def run(listener, home: str, config_dir: str, tmdb_token: str, github_token: str,
        publish: bool = True, full_crawl: bool = False, force_push: bool = False, fanart_key: str = "",
        drive: str = "") -> str:
    """Full catalog update. Returns a JSON summary: {ok, message, log_file, result}."""
    _configure(home, config_dir, tmdb_token, fanart_key, drive)
    from . import __version__
    from .settings import LOGS, setup_logging

    setup_logging(False)
    root = logging.getLogger()
    bridge = _Bridge(listener)
    log_file = LOGS / f"phone-run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(bridge)
    root.addHandler(fh)
    log = logging.getLogger("phone")
    conn = None
    drv, lock = None, None
    current = {"stage": "checks"}

    def on_stage(stage, state, detail=None):
        current["stage"] = stage
        listener.onStage(stage, state, _json(detail or {}))

    try:
        log.info("feral catalog phone run (indexer %s), publish=%s, full_crawl=%s, force_push=%s",
                 __version__, publish, full_crawl, force_push)
        on_stage("checks", "start")
        if not _checks(listener, github_token, publish):
            on_stage("checks", "failed")
            msg = "Connection checks failed; nothing was changed. See the failed checks above."
            log.error(msg)
            return _json({"ok": False, "message": msg, "log_file": str(log_file), "stage": "checks"})
        on_stage("checks", "done")

        from . import gdrive
        from .__main__ import cmd_run
        from .db import connect
        from .publish import BehindError, push_pages_api

        if gdrive.configured():
            on_stage("handoff", "start")
            drv = gdrive.Drive()
            try:
                lock = drv.acquire_lock(gdrive.device_name())
            except gdrive.LockedError as e:
                on_stage("handoff", "failed", {"error": str(e)})
                log.error("%s", e)
                return _json({"ok": False, "message": str(e), "log_file": str(log_file), "stage": "handoff"})
            on_stage("handoff", "done", gdrive.pull_if_newer(drv))
        else:
            on_stage("handoff", "skipped", {"reason": "Drive not set up"})

        conn = connect()
        args = Namespace(root=None, full=full_crawl, no_push=not publish, force_push=force_push, budget_minutes=0, limit=None)
        try:
            res = cmd_run(conn, args, pusher=lambda: push_pages_api(github_token.strip()), on_stage=on_stage)
        except BehindError as e:
            on_stage(current["stage"], "failed", {"error": str(e)})
            log.error("%s", e)
            return _json({"ok": False, "message": str(e), "log_file": str(log_file), "stage": current["stage"]})

        upload_note = ""
        if drv is not None:
            on_stage("upload", "start")
            try:
                up = gdrive.push(drv, conn, gdrive.device_name())
                on_stage("upload", "done", up)
            except Exception as e:  # noqa: BLE001 - the catalog itself is already published
                on_stage("upload", "failed", {"error": str(e)})
                log.error("drive upload failed: %s", e)
                upload_note = (" Uploading the state to Drive failed, so the other device will not see this run "
                               "until the next successful upload.")
        else:
            on_stage("upload", "skipped", {"reason": "Drive not set up"})

        if res.get("pushed") is False:
            msg = (f"Catalog version {res['version']} was built but publishing to GitHub failed; the TVs still see the "
                   "previous version. The next run will retry the publish.")
            log.error(msg)
            return _json({"ok": False, "message": msg, "log_file": str(log_file), "stage": "push", "result": res})
        if res.get("changes"):
            msg = f"Published catalog version {res['version']} ({res['changes']} changes)."
        elif res.get("pushed"):
            msg = f"No catalog changes; re-published version {res['version']}."
        else:
            msg = f"No catalog changes; version {res['version']} is already live."
        if not publish:
            msg += " Publishing was off: nothing was sent to GitHub."
        msg += upload_note
        log.info("%s: %s", "FAILED" if upload_note else "SUCCESS", msg)
        return _json({"ok": not upload_note, "message": msg, "log_file": str(log_file), "result": res})
    except BaseException as e:  # noqa: BLE001 - SystemExit from tmdb_token() etc. must reach the UI too
        listener.onStage(current["stage"], "failed", _json({"error": str(e)}))
        log.error("FAILED during %s: %s\n%s", current["stage"], e, traceback.format_exc())
        return _json({"ok": False, "message": f"Failed during {current['stage']}: {e}", "log_file": str(log_file),
                      "stage": current["stage"]})
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if drv is not None:
            try:
                if lock:
                    drv.release_lock(lock)
            except Exception as e:  # noqa: BLE001 - an abandoned lock expires on its own
                log.error("could not release the Drive lock: %s", e)
            drv.close()
        root.removeHandler(bridge)
        root.removeHandler(fh)
        fh.close()


# ------------------------------------------------------------------------------------------------ state

def state_info(home: str, config_dir: str) -> str:
    _configure(home, config_dir)
    from .db import DB_PATH, connect, get_meta
    from .publish import local_catalog_version

    if not DB_PATH.exists():
        return _json({"exists": False})
    conn = connect()
    try:
        return _json({
            "exists": True,
            "bytes": DB_PATH.stat().st_size,
            "version": local_catalog_version(conn),
            "pushed_version": get_meta(conn, "pushed_version"),
            "last_run_finished_at": get_meta(conn, "last_run_finished_at"),
            "files": conn.execute("SELECT COUNT(*) FROM file WHERE missing_since IS NULL").fetchone()[0],
        })
    finally:
        conn.close()


def import_state(home: str, config_dir: str, src: str) -> str:
    _configure(home, config_dir)
    from .__main__ import import_state as _import

    try:
        backup = _import(src)
    except SystemExit as e:
        raise ValueError(str(e)) from None
    return backup


def export_state(home: str, config_dir: str, dest: str) -> None:
    _configure(home, config_dir)
    from .__main__ import export_state as _export
    from .db import connect

    conn = connect()
    try:
        _export(conn, dest)
    finally:
        conn.close()
