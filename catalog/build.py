"""Assemble published catalog entities (titles, seasons, episodes, versions) and rows from state (plan §5.8, §8)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import defaultdict
from datetime import date

from . import aot
from .match import apply_override
from .names import LANG_WORDS
from .settings import load_sources, load_yaml, source_of
from .tmdb import Tmdb

log = logging.getLogger("build")

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "kn": "Kannada", "bn": "Bangla",
    "ur": "Urdu", "pa": "Punjabi", "mr": "Marathi", "gu": "Gujarati", "ko": "Korean", "ja": "Japanese", "zh": "Chinese",
    "es": "Spanish", "fr": "French", "tr": "Turkish", "id": "Indonesian", "th": "Thai", "ar": "Arabic", "de": "German",
    "it": "Italian", "ru": "Russian", "pt": "Portuguese", "und": "Unknown",
}
_SUB_LANG = re.compile(r"[._ -](eng|en|english|ben|bn|bangla|bengali|hin|hi|hindi|ara|ar|spa|es|fre|fr)(?:[._ -](?:sdh|cc|forced))?$", re.I)
_SUB_CODES = {"eng": "en", "english": "en", "ben": "bn", "bangla": "bn", "bengali": "bn", "hin": "hi", "hindi": "hi",
              "ara": "ar", "spa": "es", "fre": "fr"}
RES_RANK = {2160: 5, 1080: 4, 720: 3, 576: 2, 480: 1}
SRC_RANK = {"BluRay": 5, "WEB-DL": 4, "WEBRip": 3, "HDTV": 2, "HDRip": 2, "DVDRip": 1, "CAM": 0}


def _vid(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def _sub_lang(sub_name: str, video_stem: str, video_parsed: dict) -> str:
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", sub_name)
    rest = stem[len(video_stem):] if stem.startswith(video_stem) else stem
    m = _SUB_LANG.search(rest) or _SUB_LANG.search(stem)
    if m:
        t = m.group(1).lower()
        return _SUB_CODES.get(t, t if len(t) == 2 else LANG_WORDS.get(t, "und"))
    if video_parsed.get("subs_hint"):
        return "en"  # "ESub" releases ship English subtitles
    return "und"


def _label(p: dict, langs: list[str]) -> str:
    parts = []
    res = p.get("resolution")
    if res:
        parts.append("4K" if res == 2160 else f"{res}p")
    if p.get("hdr"):
        parts[-1:] = [f"{parts[-1]} {p['hdr']}" if parts else p["hdr"]]
    if p.get("cam"):
        parts.append("CAM")
    if langs:
        parts.append(" + ".join(LANG_NAMES.get(l, l) for l in langs[:3]))
    a = p.get("audio_hint") or ""
    if re.search(r"7\.1", a):
        parts.append("7.1")
    elif re.search(r"5\.1|Atmos", a, re.I):
        parts.append("5.1")
    if p.get("edition"):
        parts.append(p["edition"])
    return " · ".join(parts) or "Original"


def _quality_score(p: dict) -> int:
    s = RES_RANK.get(p.get("resolution") or 0, 2) * 100 + SRC_RANK.get(p.get("source") or "", 2) * 10
    if p.get("hdr") and p.get("hdr") != "DV":
        s += 5
    if re.search(r"5\.1|7\.1|Atmos", p.get("audio_hint") or "", re.I):
        s += 3
    if p.get("cam"):
        s -= 500
    return s


def _health(size: int | None, runtime_min: int | None, kind: str) -> str:
    if size is None:
        return "ok"
    if size < (20 if kind == "episode" else 60) * 1024**2:
        return "broken"
    if runtime_min and runtime_min > 0:
        bps = size / (runtime_min * 60)
        if bps < 25_000:
            return "broken"
        if bps < 60_000:
            return "suspect"
    return "ok"


def _with_server(label: str, server: str | None) -> str:
    return f"{label} · {server}" if server else label


def _drop_cross_source_duplicates(rows: list, src_of: dict) -> list:
    """The same release (same TMDB title, season/episode and file name) on several servers is published once,
    from the highest-priority server that has it. Files within one server are never collapsed (unchanged
    behaviour for the primary server, whose versions and ids predate multi-source support)."""
    def key(r):
        return (r["wkind"], r["tmdb_id"], r["season"], r["episode"], r["name"].casefold())

    best: dict = {}
    for r in rows:
        k, p = key(r), src_of[r["path"]].priority
        if p < best.get(k, 1 << 30):
            best[k] = p
    return [r for r in rows if src_of[r["path"]].priority == best[key(r)]]


def trending(tmdb: Tmdb) -> dict[str, int]:
    ranks: dict[str, int] = {}
    n = 0
    for page in range(1, 6):
        data = tmdb.get("/trending/all/week", {"page": page, "language": "en-US"}, ttl=6 * 3600) or {}
        for r in data.get("results", []):
            mt = r.get("media_type")
            if mt in ("movie", "tv"):
                n += 1
                ranks.setdefault(("m" if mt == "movie" else "t") + str(r["id"]), n)
    return ranks


def build(conn, tmdb: Tmdb | None) -> dict:
    now = int(time.time())
    titles_raw = {(r["kind"], r["tmdb_id"]): json.loads(r["data"]) for r in conn.execute("SELECT * FROM tmdb_title")}
    seasons_raw = {(r["tmdb_id"], r["season"]): json.loads(r["data"]) for r in conn.execute("SELECT * FROM tmdb_season") if r["data"]}
    rows = conn.execute(
        "SELECT f.path,f.url,f.name,f.ext,f.dir_path,f.size_bytes,f.added_at,f.first_seen_at,f.parse_json,f.kind,f.season,f.episode,f.episode_end,"
        "w.tmdb_id,w.kind AS wkind,w.status,w.tags,w.key AS wkey "
        "FROM file f JOIN work w ON w.key=f.work_key "
        "WHERE f.role='video' AND (f.kind IN ('movie','episode') OR (w.key=? AND f.kind='extra')) AND f.missing_since IS NULL "
        "AND w.status IN ('auto','auto_low','manual') AND w.tmdb_id IS NOT NULL",
        (aot.AOT_WORK_KEY,),
    ).fetchall()
    sources = load_sources()
    overrides = load_yaml("overrides.yaml", []) or []
    src_of = {r["path"]: source_of(sources, r["path"]) for r in rows}
    rows = _drop_cross_source_duplicates(rows, src_of)
    subs_by_dir = defaultdict(list)
    for s in conn.execute("SELECT name,url,dir_path FROM file WHERE role='subtitle' AND missing_since IS NULL"):
        subs_by_dir[s["dir_path"]].append(s)
    videos_per_dir = defaultdict(int)
    for r in rows:
        videos_per_dir[r["dir_path"]] += 1

    titles: dict[str, dict] = {}
    versions: dict[str, dict] = {}
    episodes: dict[str, dict] = {}
    t_versions = defaultdict(list)
    t_tags = defaultdict(set)
    t_status = {}
    t_override: dict[str, dict] = {}

    aot_seen = False
    aot_season_counts = {
        s["season_number"]: s.get("episode_count") or len((seasons_raw.get((aot.AOT_TMDB_ID, s["season_number"])) or {}).get("episodes", []))
        for s in (titles_raw.get(("tv", aot.AOT_TMDB_ID)) or {}).get("seasons", [])
        if s.get("season_number", 0) > 0
    }

    for r in rows:
        tk = "movie" if r["wkind"] == "movie" else "tv"
        td = titles_raw.get((tk, r["tmdb_id"]))
        if td is None:
            continue
        tid = ("m" if tk == "movie" else "t") + str(r["tmdb_id"])
        is_aot = tid == aot.AOT_TID
        p = json.loads(r["parse_json"] or "{}")
        langs = p.get("languages") or []
        ep_id = None
        runtime = td.get("runtime_min")
        if tk == "tv":
            if is_aot:
                resolved = aot.resolve_aot_season_episode(r["path"], r["name"], r["season"], r["episode"], aot_season_counts)
                if resolved is None:
                    continue
                s, e = resolved
                p, langs = aot.inherit_aot_parsed(r["dir_path"], p)
                aot_seen = True
                t_tags[tid].update(json.loads(r["tags"] or "[]"))
                t_status[tid] = r["status"]
            else:
                if r["episode"] is None:
                    continue
                s, e = int(r["season"] or 1), int(r["episode"])
            ep_id = f"{tid}:s{s}e{e}"
            sdata = seasons_raw.get((r["tmdb_id"], s)) or {}
            ep = next((x for x in sdata.get("episodes", []) if x["episode_number"] == e), None)
            if ep and ep.get("runtime_min"):
                runtime = ep["runtime_min"]
            file_added = r["added_at"] or r["first_seen_at"]
            health_preview = _health(r["size_bytes"], runtime, "episode") if is_aot else "ok"
            if ep_id not in episodes:
                episodes[ep_id] = {
                    "id": ep_id, "title_id": tid, "season_number": s, "episode_number": e,
                    "name": (ep or {}).get("name") or f"Episode {e}",
                    "overview": (ep or {}).get("overview") or None,
                    "still_path": (ep or {}).get("still_path"),
                    "air_date": (ep or {}).get("air_date"),
                    "runtime_min": (ep or {}).get("runtime_min"),
                    "metadata_pending": ep is None,
                    "added_at": None if (is_aot and health_preview == "broken") else file_added,
                }
            elif not (is_aot and health_preview == "broken"):
                cur_added = episodes[ep_id]["added_at"]
                episodes[ep_id]["added_at"] = file_added if cur_added is None else min(cur_added, file_added)
        health = _health(r["size_bytes"], runtime, r["kind"])
        if health == "broken":
            continue
        stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", r["name"])
        subs = []
        for sub in subs_by_dir.get(r["dir_path"], []):
            same_stem = sub["name"].startswith(stem)
            if same_stem or videos_per_dir[r["dir_path"]] == 1:
                lang = _sub_lang(sub["name"], stem, p)
                subs.append({"url": sub["url"], "lang": lang, "label": LANG_NAMES.get(lang, lang)})
        vid = _vid(r["path"])
        versions[vid] = {
            "id": vid, "title_id": tid, "episode_id": ep_id, "url": r["url"], "container": r["ext"],
            "size_bytes": r["size_bytes"], "resolution": p.get("resolution"), "source": p.get("source"),
            "video_codec": p.get("video_codec"), "hdr": p.get("hdr"), "bit_depth": p.get("bit_depth"),
            "audio_hint": p.get("audio_hint"), "languages": langs, "dual_audio": bool(p.get("dual_audio")),
            "cam": bool(p.get("cam")), "edition": p.get("edition"), "episode_end": r["episode_end"],
            "external_subs": subs, "label": _with_server(_label(p, langs), src_of[r["path"]].label),
            # minus the server priority: on equal quality the app's best-version pick stays on the first server
            "quality_score": _quality_score(p) - src_of[r["path"]].priority,
            "health": health, "probe": "filename", "added_at": r["added_at"] or r["first_seen_at"],
        }
        t_versions[tid].append(versions[vid])
        t_tags[tid].update(json.loads(r["tags"] or "[]"))
        t_status[tid] = r["status"]
        if overrides and tid not in t_override:
            ov = apply_override({"key": r["wkey"], "sample_path": r["path"]}, overrides)
            if ov and (ov.get("title") or ov.get("year") is not None):
                t_override[tid] = ov

    if aot_seen:
        aot.fill_missing_aot_episodes(episodes, seasons_raw)

    trend = {}
    if tmdb is not None:
        try:
            trend = trending(tmdb)
        except Exception as e:  # noqa: BLE001
            log.warning("trending fetch failed: %s", e)

    rated = [t for t in titles_raw.values() if t.get("tmdb_votes")]
    C = sum(t["tmdb_rating"] or 0 for t in rated) / len(rated) if rated else 6.5
    M = 300
    today = date.today().toordinal()

    seasons: dict[str, dict] = {}
    for tid, vs in t_versions.items():
        tk = "movie" if tid.startswith("m") else "tv"
        tmdb_id = int(tid[1:])
        td = titles_raw[(tk, tmdb_id)]
        ok = [v for v in vs if v["health"] == "ok"] or vs
        best = max(ok, key=lambda v: v["quality_score"])
        tags = set(t_tags[tid])
        gids = {g["id"] for g in td.get("genres", [])}
        if 16 in gids and td.get("original_language") == "ja":
            tags.add("anime")
        if all(v["cam"] for v in vs):
            tags.add("cam_only")
        v_votes, R = td.get("tmdb_votes") or 0, td.get("tmdb_rating") or 0
        bayes = round((v_votes / (v_votes + M)) * R + (M / (v_votes + M)) * C, 3) if v_votes else None
        audio = list(dict.fromkeys(l for v in vs for l in v["languages"]))
        subl = list(dict.fromkeys(s["lang"] for v in vs for s in v["external_subs"] if s["lang"] != "und"))
        added = [v["added_at"] for v in vs if v["added_at"]]
        t = {
            "id": tid, "kind": "movie" if tk == "movie" else "show", "tmdb_id": tmdb_id, "imdb_id": td.get("imdb_id"),
            "match_status": t_status.get(tid),
            "name": td.get("name"), "original_name": td.get("original_name"), "alt_names": td.get("alt_names", []),
            "overview": td.get("overview"), "tagline": td.get("tagline"),
            "year": int(td["release_date"][:4]) if td.get("release_date") else None,
            "release_date": td.get("release_date"), "runtime_min": td.get("runtime_min"),
            "genres": td.get("genres", []), "keywords": td.get("keywords", []),
            "original_language": td.get("original_language"), "audio_languages": audio, "subtitle_languages": subl,
            "certification": td.get("certification"),
            "tmdb_rating": td.get("tmdb_rating"), "tmdb_votes": td.get("tmdb_votes"), "popularity": td.get("popularity"),
            "bayes_rating": bayes,
            "poster_path": td.get("poster_path"), "backdrop_path": td.get("backdrop_path"),
            "card_backdrop_path": td.get("card_backdrop_path"), "logo_path": td.get("logo_path"),
            "card_backdrop_textless": bool(td.get("card_backdrop_textless")),
            "trailer_keys": td.get("trailer_keys", []), "cast": td.get("cast", []), "directors": td.get("directors", []),
            "collection": td.get("collection"), "show_status": td.get("status") if tk == "tv" else None,
            "tags": sorted(tags),
            "best_quality": "4K" if (best.get("resolution") or 0) >= 2160 else (f"{best['resolution']}p" if best.get("resolution") else None),
            "has_hdr": any(v["hdr"] in ("HDR10", "HDR10+") for v in vs),
            "has_5_1": any(re.search(r"5\.1|7\.1|Atmos", v["audio_hint"] or "", re.I) for v in vs),
            "first_available_at": min(added) if added else None,
            "last_added_at": max(added) if added else None,
            "trending_rank": trend.get(tid),
            "daily_shuffle": int(hashlib.md5(f"{tid}{today}".encode()).hexdigest()[:6], 16),
            "_rec": [("m" if tk == "movie" else "t") + str(i) for i in td.get("rec_ids", []) + td.get("similar_ids", [])],
        }
        if tk == "tv":
            eps = [e for e in episodes.values() if e["title_id"] == tid and (tid == aot.AOT_TID or any(v["episode_id"] == e["id"] for v in vs))]
            by_season = defaultdict(list)
            for e in eps:
                by_season[e["season_number"]].append(e)
            t["season_count"] = len(by_season)
            t["episode_count_available"] = len(eps)
            tseasons = {s["season_number"]: s for s in td.get("seasons", [])}
            for sn, elist in by_season.items():
                sd = seasons_raw.get((tmdb_id, sn)) or {}
                ts = tseasons.get(sn, {})
                sid = f"{tid}:s{sn}"
                seasons[sid] = {
                    "id": sid, "title_id": tid, "season_number": sn,
                    "name": sd.get("name") or ts.get("name") or (f"Season {sn}" if sn else "Specials"),
                    "overview": sd.get("overview") or ts.get("overview") or None,
                    "poster_path": sd.get("poster_path") or ts.get("poster_path"),
                    "air_date": sd.get("air_date") or ts.get("air_date"),
                    "episode_count_tmdb": ts.get("episode_count") or len(sd.get("episodes", [])) or None,
                    "episode_count_available": len(elist),
                }
        titles[tid] = t
        ov = t_override.get(tid)
        if ov:
            if ov.get("title"):
                t["name"] = str(ov["title"])
            if ov.get("year") is not None:
                t["year"] = int(ov["year"])

    for t in titles.values():
        t["similar_ids"] = [x for x in dict.fromkeys(t.pop("_rec")) if x in titles and x != t["id"]][:20]

    live_eps = {v["episode_id"] for v in versions.values() if v["episode_id"]}
    episodes = {k: v for k, v in episodes.items() if (k in live_eps or v["title_id"] == aot.AOT_TID) and v["title_id"] in titles}
    return {"titles": titles, "seasons": seasons, "episodes": episodes, "versions": versions}


# ---------------- rows ----------------

def _get(t: dict, field: str):
    return t.get(field)


def _match_where(t: dict, where: dict, now: int) -> bool:
    for field, cond in (where or {}).items():
        val = _get(t, field)
        if not isinstance(cond, dict):
            cond = {"eq": cond}
        for op, arg in cond.items():
            if op == "eq" and val != arg:
                return False
            if op == "in" and val not in arg:
                return False
            if op == "not_in" and val in arg:
                return False
            if op == "gte" and (val is None or val < arg):
                return False
            if op == "lte" and (val is None or val > arg):
                return False
            if op == "within_days" and (val is None or val < now - arg * 86400):
                return False
            if op == "exists" and (val not in (None, [], "")) != bool(arg):
                return False
            if op == "contains":
                items = val or []
                ids = {i.get("id") if isinstance(i, dict) else i for i in items}
                names = {i.get("name") if isinstance(i, dict) else i for i in items}
                if arg not in ids and arg not in names:
                    return False
            if op == "not_contains":
                items = val or []
                if arg in items or arg in {i.get("id") for i in items if isinstance(i, dict)}:
                    return False
    return True


def _sort(items: list[dict], order: list[dict]) -> list[dict]:
    for o in reversed(order or []):
        f, desc = o["field"], o.get("dir", "desc") == "desc"
        items.sort(key=lambda t: (t.get(f) is None, -(t.get(f) or 0) if desc else (t.get(f) or 0)))
    return items


def build_rows(cat: dict) -> dict:
    cfg = load_yaml("rows.yaml", {}) or {}
    now = int(time.time())
    titles = [t for t in cat["titles"].values()]
    defs = {r["id"]: r for r in cfg.get("rows", [])}
    used_hero: dict[str, list[str]] = {}
    pages_out: dict[str, list[dict]] = {}
    genre_names = {}
    for t in titles:
        for g in t["genres"]:
            genre_names[g["id"]] = g["name"]

    def resolve(rdef: dict, scope: dict, extra_where: dict | None = None, title_override: str | None = None):
        if rdef.get("type") == "personal":
            return {"id": rdef["id"], "title": rdef["title"], "style": rdef.get("style", "landscape"), "type": "personal", "kind": rdef["kind"]}
        where = dict(scope)
        where.update(rdef.get("where", {}))
        if extra_where:
            where.update(extra_where)
        pool = [t for t in titles if _match_where(t, where, now)]
        if "cam_only" not in str(where):
            pool = [t for t in pool if "cam_only" not in t["tags"]]
        items = _sort(pool, rdef.get("order_by", [{"field": "popularity"}]))[: rdef.get("limit", 40)]
        if len(items) < rdef.get("min_items", 6):
            return None
        return {"id": rdef["id"], "title": title_override or rdef["title"], "style": rdef.get("style", "landscape"),
                "type": "list", "items": [t["id"] for t in items]}

    for page, spec in (cfg.get("pages") or {}).items():
        scope = spec.get("scope", {})
        out = []
        for entry in spec.get("rows", []):
            if entry == "by_genre" or entry == "by_language":
                tmpl = defs[entry]
                if entry == "by_genre":
                    counts = defaultdict(int)
                    for t in titles:
                        if _match_where(t, scope, now):
                            for g in t["genres"]:
                                counts[g["id"]] += 1
                    for gid, _ in sorted(counts.items(), key=lambda x: -x[1])[: tmpl.get("max_rows", 8)]:
                        r = resolve({**tmpl, "id": f"genre_{gid}"}, scope, {"genres": {"contains": gid}}, genre_names[gid])
                        if r:
                            out.append(r)
                else:
                    for code in tmpl.get("languages", []):
                        r = resolve({**tmpl, "id": f"lang_{code}"}, scope, {"original_language": {"in": [code]}},
                                    tmpl["title"].replace("{language}", LANG_NAMES.get(code, code)))
                        if r:
                            out.append(r)
                continue
            rdef = defs.get(entry)
            if not rdef:
                log.warning("rows.yaml: unknown row %s", entry)
                continue
            r = resolve(rdef, scope)
            if r:
                out.append(r)
        pages_out[page] = out
        hero_def = spec.get("hero")
        if hero_def:
            hd = defs.get(hero_def)
            r = resolve(hd, scope) if hd else None
            used_hero[page] = r["items"] if r else []
    rows = {"pages": pages_out, "hero": used_hero}
    rows["version"] = hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]
    return rows
