"""Attack on Titan (TMDB 1429) catalog normalization helpers.

Scoped strictly to Attack on Titan so other catalog content remains untouched:
- Maps OVA/OAD files and Season 4 Part 3 finale specials into TMDB Season 0.
- Rebases continuous absolute episode numbers in Seasons 2-4 to season-relative indices.
- Inherits 1080p/WEBRip/x265/Dual Audio attributes from season folder names.
- Emits TMDB metadata for missing/broken main-season episodes without broken stream versions.
"""
from __future__ import annotations

import re

from .names import parse_release, season_from_folder

AOT_TMDB_ID = 1429
AOT_TID = "t1429"
AOT_WORK_KEY = "s|disk7/ANIME - PART TWO/Attack On Titan/"
AOT_REGULAR_SEASONS = (1, 2, 3, 4)

_CREDITLESS_SEG = re.compile(r"(?:^|/)(?:NC|Extras)(?:/|$)", re.I)
_PART3_RE = re.compile(r"Part\s*3\s*-\s*Part\s*([12])\b", re.I)
_LOST_GIRLS_RE = re.compile(r"\bOAD\s*-\s*Lost\s*Girls\s*0?([1-3])\b", re.I)
_OAD_NUM_RE = re.compile(r"\bOAD\s*-\s*0?([1-5])\b", re.I)

_OAD_TO_TMDB_S0 = {1: 7, 2: 13, 3: 14, 4: 15, 5: 16}
_LOST_GIRLS_TO_TMDB_S0 = {1: 17, 2: 18, 3: 19}
_PART3_TO_TMDB_S0 = {1: 36, 2: 37}


def is_creditless_extra(path: str) -> bool:
    """Return True for creditless OP/ED extras that are not show episodes."""
    return bool(_CREDITLESS_SEG.search(path))


def resolve_aot_season_episode(
    path: str,
    name: str,
    raw_season: int | None,
    raw_episode: int | None,
    season_counts: dict[int, int],
) -> tuple[int, int] | None:
    """Map an Attack on Titan file to its TMDB (season, episode), or None to skip."""
    if is_creditless_extra(path):
        return None
    if m := _PART3_RE.search(name):
        return (0, _PART3_TO_TMDB_S0[int(m.group(1))])
    if m := _LOST_GIRLS_RE.search(name):
        return (0, _LOST_GIRLS_TO_TMDB_S0[int(m.group(1))])
    if m := _OAD_NUM_RE.search(name):
        return (0, _OAD_TO_TMDB_S0[int(m.group(1))])
    if raw_episode is None:
        return None

    season = raw_season
    if season is None:
        parts = path.split("/")
        for folder in reversed(parts[:-1]):
            season = season_from_folder(folder)
            if season is not None:
                break
    if season is None:
        season = 1
    if season == 0:
        return None

    ep = int(raw_episode)
    if season > 1:
        offset = sum(season_counts.get(k, 0) for k in range(1, season))
        scount = season_counts.get(season, 0)
        if offset > 0 and ep > offset and (scount == 0 or ep > scount):
            ep -= offset
    return (season, ep)


def inherit_aot_parsed(dir_path: str, parsed: dict) -> tuple[dict, list[str]]:
    """Inherit quality and audio hints from AoT's parent season folders."""
    out = dict(parsed)
    langs = list(out.get("languages") or [])
    rel = dir_path[len("disk7/ANIME - PART TWO/Attack On Titan"):].strip("/")
    for folder in [p for p in rel.split("/") if p]:
        fp = parse_release(folder)
        for attr in ("resolution", "source", "video_codec", "hdr", "bit_depth"):
            val = getattr(fp, attr)
            if out.get(attr) is None and val is not None:
                out[attr] = val
        out["dual_audio"] = bool(out.get("dual_audio") or fp.dual_audio)
        out["subs_hint"] = bool(out.get("subs_hint") or fp.subs_hint)
        for lang in fp.languages:
            if lang not in langs:
                langs.append(lang)
    if not langs:
        langs = ["ja", "en"] if out.get("dual_audio") else ["ja"]
    return out, langs


def make_aot_episode(ep_id: str, season: int, episode: int, ep_meta: dict | None, added_at: int | None) -> dict:
    """Build a catalog episode dict from TMDB episode metadata."""
    meta = ep_meta or {}
    return {
        "id": ep_id,
        "title_id": AOT_TID,
        "season_number": season,
        "episode_number": episode,
        "name": meta.get("name") or f"Episode {episode}",
        "overview": meta.get("overview") or None,
        "still_path": meta.get("still_path"),
        "air_date": meta.get("air_date"),
        "runtime_min": meta.get("runtime_min"),
        "metadata_pending": ep_meta is None,
        "added_at": added_at,
    }


def fill_missing_aot_episodes(episodes: dict[str, dict], seasons_raw: dict[tuple[int, int], dict]) -> None:
    """Ensure all regular TMDB episodes exist for Attack on Titan (even without playable files)."""
    for sn in AOT_REGULAR_SEASONS:
        sdata = seasons_raw.get((AOT_TMDB_ID, sn)) or {}
        for ep_meta in sdata.get("episodes", []):
            e = ep_meta["episode_number"]
            ep_id = f"{AOT_TID}:s{sn}e{e}"
            if ep_id not in episodes:
                episodes[ep_id] = make_aot_episode(ep_id, sn, e, ep_meta, added_at=None)
