"""Fetch TMDB details/seasons for matched works and store trimmed records (plan §5.6.1, §13.4.3)."""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from .settings import fanart_key
from .tmdb import Tmdb

log = logging.getLogger("enrich")

TITLE_TTL = 14 * 86400
SEASON_TTL_ENDED = 30 * 86400
SEASON_TTL_ONGOING = 86400
CERT_COUNTRIES = ("US", "IN", "GB")
IMAGE_LANGS = "en,null,hi,ta,te,ml,kn,bn,ja,ko,zh,es,fr,de,tr,ar,ru,id,th,pt,it"
_fanart_lock = threading.Lock()
_fanart_http: httpx.Client | None = None


def _fanart_client() -> httpx.Client:
    global _fanart_http
    with _fanart_lock:
        if _fanart_http is None:
            _fanart_http = httpx.Client(
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            )
        return _fanart_http


def _best_image(items: list[dict], langs: tuple) -> str | None:
    for lang in langs:
        pool = [i for i in items if i.get("iso_639_1") == lang and i.get("file_path")]
        if pool:
            pool.sort(key=lambda i: (i.get("vote_average") or 0, i.get("vote_count") or 0, i.get("width") or 0), reverse=True)
            return pool[0].get("file_path")
    return None


def _best_logo(logos: list[dict], orig_lang: str | None) -> str | None:
    """Pick best logo preferring PNG/raster over SVG across en -> null -> original_language."""
    langs = tuple(dict.fromkeys(["en", None, orig_lang] if orig_lang else ["en", None]))
    raster = [i for i in logos if not (i.get("file_path") or "").lower().endswith(".svg")]
    return _best_image(raster, langs) or _best_image(logos, langs)


def _fanart_logo(kind: str, tmdb_id: int, imdb_id: str | None, tvdb_id: int | None, orig_lang: str | None) -> str | None:
    """Optional Fanart.tv fallback for transparent PNG logos when TMDB has none."""
    key = fanart_key()
    if not key:
        return None
    if kind == "movie":
        ident = str(tmdb_id or imdb_id or "")
        url = f"https://webservice.fanart.tv/v3/movies/{ident}" if ident else None
        buckets = ("hdmovielogo", "movielogo")
    else:
        url = f"https://webservice.fanart.tv/v3/tv/{tvdb_id}" if tvdb_id else None
        buckets = ("hdtvlogo", "clearlogo")
    if not url:
        return None
    client = _fanart_client()
    data = None
    for attempt in range(2):
        try:
            r = client.get(url, params={"api_key": key})
            if r.status_code == 200:
                data = r.json()
                break
            if r.status_code == 429 and attempt == 0:
                time.sleep(1.0)
                continue
            return None
        except httpx.HTTPError:
            return None
    if not data:
        return None
    items = []
    for b in buckets:
        items.extend(data.get(b) or [])
    allowed = tuple(dict.fromkeys(["en", "00", "", orig_lang] if orig_lang else ["en", "00", ""]))
    for lang in allowed:
        pool = [i for i in items if (i.get("lang") or "") == lang and i.get("url")]
        if pool:
            pool.sort(key=lambda i: int(i.get("likes") or 0), reverse=True)
            return pool[0]["url"]
    return None


def _trailers(videos: list[dict], original_lang: str | None) -> list[str]:
    yt = [v for v in videos if v.get("site") == "YouTube" and v.get("key")]

    def rank(v):
        t = v.get("type")
        type_rank = 0 if t == "Trailer" else 1 if t == "Teaser" else 2
        lang = v.get("iso_639_1")
        lang_rank = 0 if lang == "en" else 1 if lang == original_lang else 2
        return (type_rank, 0 if v.get("official") else 1, lang_rank, -(v.get("size") or 0))

    keys = []
    for v in sorted(yt, key=rank):
        if v.get("type") in ("Trailer", "Teaser") and v["key"] not in keys:
            keys.append(v["key"])
    return keys[:3]


def _cert_movie(d: dict) -> str | None:
    res = {r["iso_3166_1"]: r for r in (d.get("release_dates") or {}).get("results", [])}
    for c in CERT_COUNTRIES:
        for rd in (res.get(c) or {}).get("release_dates", []):
            if rd.get("certification"):
                return rd["certification"]
    return None


def _cert_tv(d: dict) -> str | None:
    res = {r["iso_3166_1"]: r.get("rating") for r in (d.get("content_ratings") or {}).get("results", [])}
    for c in CERT_COUNTRIES:
        if res.get(c):
            return res[c]
    return None


def trim_title(kind: str, d: dict, logo_override: str | None = None) -> dict:
    images = d.get("images") or {}
    backdrops = images.get("backdrops") or []
    logos = images.get("logos") or []
    is_movie = kind == "movie"
    orig_lang = d.get("original_language")
    ext = d.get("external_ids") or {}
    if is_movie:
        cast = [
            {"id": c["id"], "name": c["name"], "character": (c.get("character") or "")[:60], "profile_path": c.get("profile_path")}
            for c in (d.get("credits") or {}).get("cast", [])[:10]
        ]
        crew = (d.get("credits") or {}).get("crew", [])
        directors = list(dict.fromkeys(c["name"] for c in crew if c.get("job") == "Director"))[:3]
        kw = [k["name"] for k in (d.get("keywords") or {}).get("keywords", [])][:15]
    else:
        cast = []
        for c in (d.get("aggregate_credits") or {}).get("cast", [])[:10]:
            roles = c.get("roles") or [{}]
            cast.append({"id": c["id"], "name": c["name"], "character": (roles[0].get("character") or "")[:60],
                         "profile_path": c.get("profile_path")})
        directors = [c["name"] for c in d.get("created_by", [])][:3]
        kw = [k["name"] for k in (d.get("keywords") or {}).get("results", [])][:15]
    recs = [r["id"] for r in (d.get("recommendations") or {}).get("results", [])[:20]]
    sims = [r["id"] for r in (d.get("similar") or {}).get("results", [])[:20]]
    coll = d.get("belongs_to_collection") or None
    alt = [t.get("title") for t in ((d.get("alternative_titles") or {}).get("titles") or (d.get("alternative_titles") or {}).get("results") or [])][:15]
    out = {
        "name": d.get("title") if is_movie else d.get("name"),
        "original_name": d.get("original_title") if is_movie else d.get("original_name"),
        "overview": (d.get("overview") or "")[:1200],
        "tagline": d.get("tagline") or None,
        "release_date": (d.get("release_date") if is_movie else d.get("first_air_date")) or None,
        "runtime_min": d.get("runtime") if is_movie else ((d.get("episode_run_time") or [None])[0]),
        "genres": [{"id": g["id"], "name": g["name"]} for g in d.get("genres", [])],
        "keywords": kw,
        "original_language": d.get("original_language"),
        "spoken_languages": [l.get("iso_639_1") for l in d.get("spoken_languages", [])],
        "origin_countries": d.get("origin_country") or [c.get("iso_3166_1") for c in d.get("production_countries", [])],
        "tmdb_rating": round(d.get("vote_average") or 0, 2) or None,
        "tmdb_votes": d.get("vote_count") or 0,
        "popularity": round(d.get("popularity") or 0, 3),
        "poster_path": d.get("poster_path"),
        "backdrop_path": d.get("backdrop_path"),
        "card_backdrop_path": _best_image(backdrops, (None, "en")) or d.get("backdrop_path"),
        # True only when the card image is a language-neutral (text-free) backdrop; the app overlays a
        # title/logo only then, otherwise the artwork's own printed title would appear twice.
        "card_backdrop_textless": _best_image(backdrops, (None,)) is not None,
        "logo_path": _best_logo(logos, orig_lang) or logo_override,
        "trailer_keys": _trailers((d.get("videos") or {}).get("results", []), d.get("original_language")),
        "cast": cast,
        "directors": directors,
        "imdb_id": ext.get("imdb_id") or d.get("imdb_id"),
        "tvdb_id": ext.get("tvdb_id"),
        "certification": _cert_movie(d) if is_movie else _cert_tv(d),
        "collection": {"id": coll["id"], "name": coll["name"]} if coll else None,
        "rec_ids": recs,
        "similar_ids": sims,
        "alt_names": [a for a in alt if a],
        "status": d.get("status"),
    }
    if not is_movie:
        out["number_of_seasons"] = d.get("number_of_seasons")
        out["seasons"] = [
            {"season_number": s["season_number"], "name": s.get("name"), "overview": (s.get("overview") or "")[:600],
             "poster_path": s.get("poster_path"), "air_date": s.get("air_date"), "episode_count": s.get("episode_count")}
            for s in d.get("seasons", [])
        ]
    return out


def trim_season(d: dict) -> dict:
    return {
        "name": d.get("name"),
        "overview": (d.get("overview") or "")[:600],
        "poster_path": d.get("poster_path"),
        "air_date": d.get("air_date"),
        "episodes": [
            {"episode_number": e["episode_number"], "name": e.get("name"), "overview": (e.get("overview") or "")[:500],
             "still_path": e.get("still_path"), "air_date": e.get("air_date"), "runtime_min": e.get("runtime")}
            for e in d.get("episodes", [])
        ],
    }


def fetch_title(tmdb: Tmdb, kind: str, tmdb_id: int) -> dict | None:
    if kind == "movie":
        append = "credits,keywords,images,external_ids,videos,release_dates,recommendations,similar,alternative_titles"
    else:
        append = "aggregate_credits,keywords,images,external_ids,videos,content_ratings,recommendations,similar,alternative_titles"
    d = tmdb.get(f"/{kind}/{tmdb_id}", {"append_to_response": append, "language": "en-US",
                                         "include_image_language": IMAGE_LANGS,
                                         "include_video_language": "en,null,hi,ta,te,ml,ko,ja"},
                 cache=False)
    if not d:
        return None
    out = trim_title(kind, d)
    if not out.get("logo_path"):
        out["logo_path"] = _fanart_logo(kind, tmdb_id, out.get("imdb_id"), out.get("tvdb_id"), out.get("original_language"))
    return out


def enrich(conn, tmdb: Tmdb, workers: int = 8, refresh_logos: bool = False) -> dict:
    now = int(time.time())
    stats = {"titles": 0, "seasons": 0, "failed": 0}
    extra_clause = (
        " OR json_extract(t.data, '$.logo_path') IS NULL OR json_extract(t.data, '$.logo_path') LIKE '%.svg'"
        if refresh_logos else ""
    )
    want = conn.execute(
        "SELECT DISTINCT CASE w.kind WHEN 'movie' THEN 'movie' ELSE 'tv' END AS k, w.tmdb_id AS id FROM work w "
        "LEFT JOIN tmdb_title t ON t.kind = (CASE w.kind WHEN 'movie' THEN 'movie' ELSE 'tv' END) AND t.tmdb_id = w.tmdb_id "
        "WHERE w.status IN ('auto','auto_low','manual') AND w.tmdb_id IS NOT NULL "
        f"AND (t.fetched_at IS NULL OR t.fetched_at < ?{extra_clause})",
        (now - TITLE_TTL,),
    ).fetchall()

    def job(r):
        try:
            return r, fetch_title(tmdb, r["k"], r["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("details %s/%s failed: %s", r["k"], r["id"], e)
            return r, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (r, data) in enumerate(pool.map(job, want)):
            if data is None:
                stats["failed"] += 1
                continue
            conn.execute(
                "INSERT INTO tmdb_title(kind,tmdb_id,data,fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(kind,tmdb_id) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                (r["k"], r["id"], json.dumps(data), now),
            )
            stats["titles"] += 1
            if i % 100 == 0:
                conn.commit()
                if i:
                    log.info("details %d/%d", i, len(want))
    conn.commit()

    # seasons present on disk for matched shows
    seasons = conn.execute(
        "SELECT DISTINCT w.tmdb_id AS id, f.season AS s, t.data AS tdata, ts.fetched_at AS fa, "
        "MAX(f.episode) AS max_ep, ts.data AS sdata "
        "FROM file f JOIN work w ON w.key=f.work_key "
        "LEFT JOIN tmdb_title t ON t.kind='tv' AND t.tmdb_id=w.tmdb_id "
        "LEFT JOIN tmdb_season ts ON ts.tmdb_id=w.tmdb_id AND ts.season=f.season "
        "WHERE w.kind='show' AND w.status IN ('auto','auto_low','manual') AND f.kind='episode' AND f.missing_since IS NULL "
        "GROUP BY w.tmdb_id, f.season"
    ).fetchall()
    todo = []
    for r in seasons:
        if r["fa"] is None:
            todo.append(r)
            continue
        status = json.loads(r["tdata"]).get("status") if r["tdata"] else None
        ttl = SEASON_TTL_ONGOING if status in ("Returning Series", "In Production") else SEASON_TTL_ENDED
        known = {e["episode_number"] for e in (json.loads(r["sdata"]).get("episodes", []) if r["sdata"] else [])}
        if now - r["fa"] > ttl or (r["max_ep"] and r["max_ep"] not in known and now - r["fa"] > 6 * 3600):
            todo.append(r)

    def sjob(r):
        try:
            return r, tmdb.get(f"/tv/{r['id']}/season/{r['s']}", {"language": "en-US"}, cache=False)
        except Exception as e:  # noqa: BLE001
            log.warning("season %s/%s failed: %s", r["id"], r["s"], e)
            return r, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (r, d) in enumerate(pool.map(sjob, todo)):
            conn.execute(
                "INSERT INTO tmdb_season(tmdb_id,season,data,fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(tmdb_id,season) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                (r["id"], r["s"], json.dumps(trim_season(d)) if d else None, now),
            )
            stats["seasons"] += 1
            if i % 100 == 0:
                conn.commit()
    conn.commit()
    return stats
