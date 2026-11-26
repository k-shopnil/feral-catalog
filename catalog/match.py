"""Match works to TMDB (plan §5.5). Filename-only MVP: no runtime score (no probe yet)."""
from __future__ import annotations

import fnmatch
import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from rapidfuzz import fuzz

from .names import norm_key, strip_season_suffix
from .settings import load_yaml
from .tmdb import Tmdb

log = logging.getLogger("match")

AUTO = 0.85
AUTO_MARGIN = 0.08
LOW = 0.70
RETRY_AFTER = 3 * 86400  # re-try unmatched works every 3 days

_ROMAN = {" ii": " 2", " iii": " 3", " iv": " 4", " v": " 5"}


def _variants(title: str) -> list[str]:
    out = [title]
    if re.search(r"\bAKA\b", title, re.I):
        out += [p.strip() for p in re.split(r"\bAKA\b", title, flags=re.I) if p.strip()]
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", title)
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    t = re.sub(r"\b(Extended|Unrated|Director'?s Cut|Remastered|Super Duper Cut)\b", "", title, flags=re.I).strip()
    if t and t not in out:
        out.append(t)
    for sep in (" - ", ": "):
        if sep in title:
            out.append(title.split(sep)[0].strip())
    low = " " + title.lower()
    for k, v in _ROMAN.items():
        if low.endswith(k):
            out.append(title[: -len(k) + 1] + v.strip())
    if "&" in title:
        out.append(title.replace("&", "and"))
    # works created before folder titles dropped their season tag ("Bigg Boss S15") still search as "Bigg Boss"
    out.append(strip_season_suffix(title))
    return [o for i, o in enumerate(out) if o and o not in out[:i]]


def _year_of(c: dict) -> int | None:
    d = c.get("release_date") or c.get("first_air_date") or ""
    return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None


def _title_sim(work_title: str, names: list[str]) -> float:
    best = 0.0
    for variant in _variants(work_title):
        wt = norm_key(variant)
        for n in names:
            if not n:
                continue
            nk = norm_key(n)
            s = fuzz.token_set_ratio(wt, nk) / 100.0
            # penalize big length mismatch that token_set_ratio ignores ("Up" vs "Up in the Air")
            a, b = len(wt), len(nk)
            if min(a, b) and max(a, b) / min(a, b) > 1.8:
                s *= 0.85
            best = max(best, s)
    return best


def score(work: dict, c: dict, alt_titles: list[str] | None = None) -> float:
    names = [c.get("title") or c.get("name") or "", c.get("original_title") or c.get("original_name") or ""]
    names += alt_titles or []
    title_sim = _title_sim(work["title"], names)
    y, cy = work.get("year"), _year_of(c)
    if y and cy:
        year_score = 1.0 if y == cy else 0.75 if abs(y - cy) == 1 else 0.0
    else:
        year_score = 0.5
    langs = set(work.get("langs") or [])
    ol = c.get("original_language")
    if not langs:
        lang_score = 0.5
    elif ol in langs:
        lang_score = 1.0
    else:
        lang_score = 0.35
    pop = min(1.0, math.log10((c.get("vote_count") or 0) + 1) / 4)
    if not y:
        # no year in the name (typical for show folders): title + popularity must carry the decision
        return 0.70 * title_sim + 0.12 * lang_score + 0.18 * pop
    # weights from §5.5.3 with runtime removed and re-normalized
    return 0.50 * title_sim + 0.28 * year_score + 0.13 * lang_score + 0.09 * pop


def _search(tmdb: Tmdb, work: dict) -> list[dict]:
    kind = "movie" if work["kind"] == "movie" else "tv"
    year_param = "primary_release_year" if kind == "movie" else "first_air_date_year"
    seen, results = set(), []
    for title in _variants(work["title"]):
        attempts = []
        if work.get("year"):
            attempts.append({"query": title, year_param: work["year"]})
            if kind == "movie":
                attempts.append({"query": title, "year": work["year"]})
        attempts.append({"query": title})
        for params in attempts:
            data = tmdb.get(f"/search/{kind}", {**params, "include_adult": "false", "language": "en-US"}, ttl=30 * 86400)
            for c in (data or {}).get("results", [])[:10]:
                if c["id"] not in seen:
                    seen.add(c["id"])
                    results.append(c)
            if results:
                break
        if len(results) >= 3:
            break
    return results


def _alt_titles(tmdb: Tmdb, kind: str, tmdb_id: int) -> list[str]:
    path = f"/{'movie' if kind == 'movie' else 'tv'}/{tmdb_id}/alternative_titles"
    data = tmdb.get(path, ttl=30 * 86400) or {}
    items = data.get("titles") or data.get("results") or []
    return [t.get("title") for t in items if t.get("title")][:30]


def load_overrides() -> list[dict]:
    return load_yaml("overrides.yaml", []) or []


def apply_override(work_row, overrides: list[dict]) -> dict | None:
    target = work_row["sample_path"] or ""
    key = work_row["key"]
    for o in overrides:
        pat = o.get("match", "")
        if pat and (fnmatch.fnmatch(target, pat) or fnmatch.fnmatch(key, pat) or target.startswith(pat.rstrip("*"))):
            return o
    return None


def match_one(tmdb: Tmdb, work: dict) -> tuple[str, int | None, float, float, list]:
    cands = _search(tmdb, work)
    if not cands:
        return "unmatched", None, 0.0, 0.0, []
    scored = sorted(((score(work, c), c) for c in cands), key=lambda x: -x[0])
    if scored[0][0] < AUTO or (len(scored) > 1 and scored[0][0] - scored[1][0] < AUTO_MARGIN):
        rescored = []
        for s, c in scored[:3]:
            rescored.append((score(work, c, _alt_titles(tmdb, work["kind"], c["id"])), c))
        scored = sorted(rescored + scored[3:], key=lambda x: -x[0])
    top_s, top = scored[0]
    margin = top_s - (scored[1][0] if len(scored) > 1 else 0.0)
    brief = [
        {"id": c["id"], "name": c.get("title") or c.get("name"), "year": _year_of(c),
         "lang": c.get("original_language"), "score": round(s, 3)}
        for s, c in scored[:5]
    ]
    if top_s >= AUTO and margin >= AUTO_MARGIN:
        status = "auto"
    elif top_s >= LOW:
        status = "auto_low"
    else:
        status = "unmatched"
    return status, top["id"] if status != "unmatched" else None, top_s, margin, brief


def match_works(conn, tmdb: Tmdb, workers: int = 8, limit: int | None = None) -> dict:
    now = int(time.time())
    overrides = load_overrides()
    rows = conn.execute(
        "SELECT * FROM work WHERE status='new' OR (status IN ('unmatched','error') AND (last_attempt_at IS NULL OR last_attempt_at < ?)) "
        "OR status='manual'",
        (now - RETRY_AFTER,),
    ).fetchall()
    stats = {"works": 0, "auto": 0, "auto_low": 0, "unmatched": 0, "manual": 0, "ignored": 0, "error": 0}
    todo = []
    for r in rows:
        o = apply_override(r, overrides)
        if o:
            if o.get("ignore"):
                conn.execute("UPDATE work SET status='ignored', updated_at=? WHERE key=?", (now, r["key"]))
                stats["ignored"] += 1
            elif o.get("tmdb"):
                conn.execute("UPDATE work SET status='manual', tmdb_id=?, score=1.0, margin=1.0, updated_at=? WHERE key=?",
                             (int(o["tmdb"]["id"]), now, r["key"]))
                stats["manual"] += 1
            continue
        if r["status"] == "manual":
            continue  # manual without matching override anymore: leave as-is
        todo.append(dict(r) | {"langs": json.loads(r["langs"] or "[]")})
    if limit:
        todo = todo[:limit]
    conn.commit()

    def run(w):
        try:
            return w, match_one(tmdb, w)
        except Exception as e:  # noqa: BLE001
            log.warning("match failed for %s: %s", w["key"], e)
            return w, ("error", None, 0.0, 0.0, [])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (w, (status, tid, s, m, brief)) in enumerate(pool.map(run, todo)):
            stats["works"] += 1
            stats[status] += 1
            conn.execute(
                "UPDATE work SET status=?, tmdb_id=?, score=?, margin=?, candidates=?, attempts=attempts+1, last_attempt_at=?, updated_at=? WHERE key=?",
                (status, tid, round(s, 4), round(m, 4), json.dumps(brief), now, now, w["key"]),
            )
            if i % 100 == 0:
                conn.commit()
                if i:
                    log.info("matched %d/%d", i, len(todo))
    conn.commit()
    return stats
