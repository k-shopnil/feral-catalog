"""Turn stable, unparsed video files into parsed records grouped under works (plan §5.4.4)."""
from __future__ import annotations

import json
import logging
import re
import time

from .names import norm_key, parse_release, season_from_folder, show_title_from_folder
from .settings import Root, Source, all_roots

log = logging.getLogger("process")

_YEAR_DIR = re.compile(r"^(19|20)\d{2}$")


def _roots(src: Source | list[Source]) -> list[Root]:
    return src.roots if isinstance(src, Source) else all_roots(src)


def _root_of(src: Source | list[Source], path: str) -> Root | None:
    best = None
    for r in _roots(src):
        if path.startswith(r.path) and (best is None or len(r.path) > len(best.path)):
            best = r
    return best


def classify_file(src: Source | list[Source], path: str, name: str) -> dict | None:
    root = _root_of(src, path)
    if root is None:
        return None
    rel = path[len(root.path):].split("/")
    folders, fname = rel[:-1], rel[-1]
    fp = parse_release(fname)
    tags = list(root.tags)
    langs = list(dict.fromkeys(fp.languages + root.lang_hint))

    if fp.kind in ("sample", "special"):
        return {"kind": fp.kind, "parsed": fp.to_dict()}

    show_root = root.kind == "show" and len(folders) >= 1
    if show_root:
        show_folder = folders[0]
        title, year = show_title_from_folder(show_folder)
        season = fp.season
        if season is None:
            # nearest folder first, the show folder last ("BIGG BOSS S17/BBS17EP1.mkv" -> season 17, not 1)
            for f in reversed(folders):
                season = season_from_folder(f)
                if season is not None:
                    break
        episode = fp.episode
        if episode is None and fp.absolute_episode is not None:
            episode = fp.absolute_episode
        if episode is None:
            return {"kind": "extra", "parsed": fp.to_dict(), "work_key": f"s|{root.path}{show_folder}/"}
        if season is None:
            season = 1
        for f in folders[1:]:
            langs = list(dict.fromkeys(langs + parse_release(f).languages))
        return {
            "kind": "episode",
            "work_key": f"s|{root.path}{show_folder}/",
            "work": {"kind": "show", "title": title or fp.title, "year": year, "langs": langs, "root": root.path, "tags": tags},
            "season": season, "episode": episode, "episode_end": fp.episode_end,
            "parsed": fp.to_dict(),
        }

    if fp.kind == "episode":
        # season pack or loose episode outside a show root: group by parsed show title
        pack = parse_release(folders[-1]) if folders else None
        title = fp.title or (pack.title if pack else "")
        if not title:
            return {"kind": "unknown", "parsed": fp.to_dict()}
        season = fp.season if fp.season is not None else (pack.season if pack and pack.season is not None else 1)
        return {
            "kind": "episode",
            "work_key": f"s|auto|{norm_key(title)}",
            "work": {"kind": "show", "title": title, "year": pack.year if pack else None, "langs": langs,
                     "root": root.path, "tags": tags},
            "season": season, "episode": fp.episode or fp.absolute_episode, "episode_end": fp.episode_end,
            "parsed": fp.to_dict(),
        }

    # movie
    title, year = fp.title, fp.year
    if folders:
        parent = folders[-1]
        if not _YEAR_DIR.match(parent):
            dp = parse_release(parent, prefer="movie")
            if dp.title and dp.year:
                title, year = dp.title, dp.year
                langs = list(dict.fromkeys(langs + dp.languages))
                for attr in ("resolution", "source", "video_codec", "hdr", "edition"):
                    if getattr(fp, attr) is None and getattr(dp, attr) is not None:
                        setattr(fp, attr, getattr(dp, attr))
                fp.cam = fp.cam or dp.cam
                fp.dual_audio = fp.dual_audio or dp.dual_audio
    if not title:
        return {"kind": "unknown", "parsed": fp.to_dict()}
    fp.languages = langs
    return {
        "kind": "movie",
        "work_key": f"m|{norm_key(title)}|{year or ''}",
        "work": {"kind": "movie", "title": title, "year": year, "langs": langs, "root": root.path, "tags": tags},
        "parsed": fp.to_dict(),
    }


def process_files(conn, src: Source | list[Source], limit: int | None = None) -> dict:
    now = int(time.time())
    rows = conn.execute(
        "SELECT path,name FROM file WHERE role='video' AND stable=1 AND parsed=0 AND missing_since IS NULL"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    stats = {"files": 0, "movie": 0, "episode": 0, "special": 0, "extra": 0, "sample": 0, "unknown": 0}
    for i, r in enumerate(rows):
        c = classify_file(src, r["path"], r["name"]) or {"kind": "unknown", "parsed": {}}
        stats["files"] += 1
        stats[c["kind"]] = stats.get(c["kind"], 0) + 1
        conn.execute(
            "UPDATE file SET parsed=1, parse_json=?, kind=?, work_key=?, season=?, episode=?, episode_end=? WHERE path=?",
            (json.dumps(c["parsed"]), c["kind"], c.get("work_key"), c.get("season"), c.get("episode"),
             c.get("episode_end"), r["path"]),
        )
        w = c.get("work")
        if w:
            existing = conn.execute("SELECT langs,status FROM work WHERE key=?", (c["work_key"],)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO work(key,kind,title,year,langs,root,tags,sample_path,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (c["work_key"], w["kind"], w["title"], w["year"], json.dumps(w["langs"]), w["root"],
                     json.dumps(w["tags"]), r["path"], "new", now),
                )
            else:
                merged = list(dict.fromkeys(json.loads(existing["langs"] or "[]") + w["langs"]))
                conn.execute("UPDATE work SET langs=?, updated_at=? WHERE key=?", (json.dumps(merged), now, c["work_key"]))
        if i % 500 == 0:
            conn.commit()
    conn.commit()
    return stats
