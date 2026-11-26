"""Multiple storage servers (namespaced state paths) and the stalled `stable = 0` re-list."""
import json
import sqlite3
from datetime import datetime

import pytest

from catalog import build as cat_build
from catalog import crawler as crawler_mod
from catalog.autoindex import parse_listing
from catalog.crawler import Crawler
from catalog.db import SCHEMA
from catalog.process import classify_file
from catalog.settings import Root, Source, load_sources, source_of

NGINX = """<html>
<head><title>Index of /English/</title></head>
<body bgcolor="white">
<h1>Index of /English/</h1><hr><pre><a href="../">../</a>
<a href="2073.2024.1080p.WEBRip.x264%20%5BDDN%5D/">2073.2024.1080p.WEBRip.x264 [DDN]/</a>             12-Jan-2025 07:29       -
<a href="A%20Very%20Long%20Name%20That%20Nginx%20Truncates%20In%20The%20Label.mkv">A Very Long Name That Nginx Truncates In The..&gt;</a> 09-Jan-2025 08:10       2G
<a href="sub.srt">sub.srt</a>                                            02-Aug-2025 01:47     110K
</pre><hr></body>
</html>
"""


def test_nginx_listing_parsed_into_apache_form():
    es = parse_listing(NGINX)
    assert [(e.name, e.is_dir, e.mtime) for e in es] == [
        ("2073.2024.1080p.WEBRip.x264 [DDN]", True, "2025-01-12 07:29"),
        ("A Very Long Name That Nginx Truncates In The Label.mkv", False, "2025-01-09 08:10"),
        ("sub.srt", False, "2025-08-02 01:47"),
    ]
    assert es[1].size_bytes == 2 * 1024**3
    assert es[2].size_bytes == 110 * 1024
    assert es[0].size_bytes is None


def test_apache_listing_unchanged():
    html = ('<tr><td valign="top"><img src="/icons/folder.gif" alt="[DIR]"></td><td><a href="English%20Movies/">'
            'English Movies/</a></td><td align="right">2026-11-25 09:10  </td><td align="right">  - </td></tr>')
    (e,) = parse_listing(html)
    assert (e.name, e.is_dir, e.mtime, e.size_human) == ("English Movies", True, "2026-11-25 09:10", "-")


def test_escaped_ampersand_in_href_is_unescaped():
    apache = ('<tr><td><a href="Khanna%20&amp;%20Iyer%20(2007).mp4">Khanna &amp; Iyer (2007).mp4</a></td>'
              '<td align="right">2020-01-01 10:00  </td><td align="right">1.2G</td></tr>')
    nginx = '<a href="Bob&amp;Co/">Bob&amp;Co/</a>    25-Nov-2026 17:42       -\n'
    (a,), (n,) = parse_listing(apache), parse_listing(nginx)
    assert (a.href, a.name) == ("Khanna%20&%20Iyer%20(2007).mp4", "Khanna & Iyer (2007).mp4")
    assert (n.href, n.name) == ("Bob&Co/", "Bob&Co")


def test_sources_yaml_keeps_primary_unprefixed_and_namespaces_the_rest():
    srcs = load_sources()
    assert srcs[0].id == "ctgfun" and srcs[0].prefix == "" and srcs[0].label is None
    assert all(r.path == r.rel for r in srcs[0].roots)  # legacy state paths untouched
    others = srcs[1:]
    assert others and all(s.prefix == f"{s.id}:" and s.label for s in others)
    assert all(r.path == s.prefix + r.rel and r.source == s.id for s in others for r in s.roots)
    assert len({s.prefix for s in srcs}) == len(srcs)
    assert source_of(srcs, "disk1/English Movies/x.mkv").id == "ctgfun"
    assert source_of(srcs, "data:disk1/disk1/x.mkv").id == "data"
    assert source_of(srcs, "ftp:English/x.mkv").id == "ftp"


# ---------------- crawler ----------------

def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _src(sid, base, prefix, roots, exclude=(), priority=0):
    return Source(id=sid, base_url=base, max_concurrency=2, user_agent="t", exclude=list(exclude), prefix=prefix,
                  priority=priority, roots=[Root(path=prefix + rel, kind=kind, rel=rel, source=sid) for rel, kind in roots])


def _apache(rows):
    return "\n".join(
        f'<tr><td><a href="{href}">{href}</a></td><td align="right">{mtime}  </td><td align="right">{size}</td></tr>'
        for href, mtime, size in rows
    )


def _crawl(conn, src, listings, now):
    c = Crawler(conn, src)
    c.now = now
    c.fetch = lambda url: listings.get(url)
    return c.run()


def _ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp())


def test_fresh_upload_turns_stable_on_a_later_run_without_folder_change(monkeypatch):
    monkeypatch.setattr(crawler_mod.time, "sleep", lambda *_: None)
    src = _src("ctgfun", "https://m/", "", [("disk1/Eng/", "movie")])
    uploaded = "2026-11-26 10:00"
    listings = {
        "https://m/disk1/Eng/": _apache([("Film%20(2026)/", uploaded, "-")]),
        "https://m/disk1/Eng/Film%20(2026)/": _apache([("Film.2026.1080p.mkv", uploaded, "2.1G")]),
    }
    conn = _conn()
    t0 = _ts(uploaded) + 30 * 60  # discovered 30 min after upload -> not yet stable
    _crawl(conn, src, listings, t0)
    f = conn.execute("SELECT stable FROM file").fetchone()
    assert f["stable"] == 0

    # 5 minutes later: too soon to re-list just for stability
    stats = _crawl(conn, src, listings, t0 + 300)
    assert stats["unstable_dirs"] == 0
    assert conn.execute("SELECT stable FROM file").fetchone()["stable"] == 0

    # 11 minutes after the observation: the folder's mtime hasn't changed, but it is re-listed and the file flips
    stats = _crawl(conn, src, listings, t0 + 660)
    assert stats["unstable_dirs"] == 1
    assert conn.execute("SELECT stable FROM file").fetchone()["stable"] == 1

    # stable now: no more forced re-lists
    assert _crawl(conn, src, listings, t0 + 2000)["unstable_dirs"] == 0


def test_growing_upload_stays_unstable_until_size_settles(monkeypatch):
    monkeypatch.setattr(crawler_mod.time, "sleep", lambda *_: None)
    src = _src("ctgfun", "https://m/", "", [("disk6/TV/", "show")])
    m = "2026-11-26 10:00"
    root = {"https://m/disk6/TV/": _apache([("Show/", m, "-")]),
            "https://m/disk6/TV/Show/": _apache([("Season%201/", m, "-")])}
    season = "https://m/disk6/TV/Show/Season%201/"
    conn = _conn()
    t0 = _ts(m) + 60
    _crawl(conn, src, root | {season: _apache([("Show.S01E01.mkv", m, "300M")])}, t0)
    _crawl(conn, src, root | {season: _apache([("Show.S01E01.mkv", m, "700M")])}, t0 + 700)
    row = conn.execute("SELECT stable, size_human FROM file").fetchone()
    assert (row["stable"], row["size_human"]) == (0, "700M")  # season folder (depth 2) was re-listed
    _crawl(conn, src, root | {season: _apache([("Show.S01E01.mkv", m, "700M")])}, t0 + 1400)
    assert conn.execute("SELECT stable FROM file").fetchone()["stable"] == 1


def test_two_servers_with_the_same_folder_names_do_not_collide(monkeypatch):
    monkeypatch.setattr(crawler_mod.time, "sleep", lambda *_: None)
    m = "2020-01-01 10:00"
    a = _src("ctgfun", "https://movie/", "", [("disk1/", "auto")])
    b = _src("data", "https://data/", "data:", [("disk1/", "auto")], exclude=["**/GAMES/"], priority=1)
    conn = _conn()
    _crawl(conn, a, {"https://movie/disk1/": _apache([("X.2019.mkv", m, "1G")])}, _ts(m) + 86400)
    _crawl(conn, b, {
        "https://data/disk1/": _apache([("Y.2019.mkv", m, "1G"), ("GAMES/", m, "-")]),
    }, _ts(m) + 86400)
    files = {r["path"]: (r["url"], r["root"], r["dir_path"]) for r in conn.execute("SELECT * FROM file")}
    assert files == {
        "disk1/X.2019.mkv": ("https://movie/disk1/X.2019.mkv", "disk1/", "disk1/"),
        "data:disk1/Y.2019.mkv": ("https://data/disk1/Y.2019.mkv", "data:disk1/", "data:disk1/"),
    }
    assert {r["path"] for r in conn.execute("SELECT path FROM dir_node")} == {"disk1/", "data:disk1/"}

    # the primary server's listing does not mark the other server's files missing (and vice versa)
    _crawl(conn, a, {"https://movie/disk1/": _apache([("X.2019.mkv", m, "1G")])}, _ts(m) + 90000)
    assert conn.execute("SELECT COUNT(*) FROM file WHERE missing_since IS NOT NULL").fetchone()[0] == 0


def test_classify_show_roots_are_namespaced_and_movies_shared():
    srcs = load_sources()
    legacy = classify_file(srcs, "disk9/TV SERIES/Dark/Season 1/Dark.S01E01.mkv", "Dark.S01E01.mkv")
    assert legacy["work_key"] == "s|disk9/TV SERIES/Dark/"
    data = classify_file(srcs, "data:disk5/TV SERIES/Dark/Dark.S01.720p/Dark.S01E01.720p.mkv", "Dark.S01E01.720p.mkv")
    assert data["work_key"] == "s|data:disk5/TV SERIES/Dark/" and data["work"]["root"] == "data:disk5/TV SERIES/"
    ftp = classify_file(srcs, "ftp:English/Heat (1995) 720p/Heat.1995.720p.BluRay.mkv", "Heat.1995.720p.BluRay.mkv")
    legacy_movie = classify_file(srcs, "disk1/English Movies/Heat (1995)/Heat.1995.1080p.BluRay.mkv",
                                 "Heat.1995.1080p.BluRay.mkv")
    assert ftp["work_key"] == legacy_movie["work_key"]  # same movie work -> one TMDB match
    assert "en" in ftp["work"]["langs"]


# ---------------- build ----------------

def _build_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO tmdb_title VALUES ('movie', 949, ?, 1)", (json.dumps({"name": "Heat", "genres": []}),))
    conn.execute("INSERT INTO work(key,kind,title,status,tmdb_id,tags) VALUES ('m|heat|1995','movie','Heat','auto',949,'[]')")
    return conn


def _add(conn, path, name):
    p = {"resolution": 1080 if "1080" in name else 720, "languages": ["en"]}
    conn.execute(
        "INSERT INTO file(path,url,dir_path,root,name,ext,role,size_bytes,added_at,first_seen_at,last_seen_at,parse_json,kind,work_key) "
        "VALUES (?,?,?,'',?,'mkv','video',2000000000,1000,1000,1000,?,'movie','m|heat|1995')",
        (path, f"https://x/{path}", path.rsplit("/", 1)[0] + "/", name, json.dumps(p)),
    )


def test_build_merges_servers_into_one_title_dedupes_and_labels():
    conn = _build_db()
    _add(conn, "disk1/English Movies/Heat (1995)/Heat.1995.1080p.BluRay.mkv", "Heat.1995.1080p.BluRay.mkv")
    _add(conn, "ftp:English/Heat (1995)/Heat.1995.1080p.BluRay.mkv", "Heat.1995.1080p.BluRay.mkv")    # same release
    _add(conn, "data:disk9/2006/Heat (1995)/Heat.1995.720p.BluRay.mkv", "Heat.1995.720p.BluRay.mkv")  # backup 720p
    _add(conn, "ftp:Others/Heat (1995)/Heat.1995.720p.BluRay.mkv", "Heat.1995.720p.BluRay.mkv")      # dup of data's
    cat = cat_build.build(conn, None)
    vs = sorted(cat["versions"].values(), key=lambda v: v["url"])
    assert list(cat["titles"]) == ["m949"]
    assert [(v["url"], v["label"]) for v in vs] == [
        ("https://x/data:disk9/2006/Heat (1995)/Heat.1995.720p.BluRay.mkv", "720p · English · Data server"),
        ("https://x/disk1/English Movies/Heat (1995)/Heat.1995.1080p.BluRay.mkv", "1080p · English"),
    ]
    assert vs[0]["quality_score"] == cat_build._quality_score({"resolution": 720}) - 1
    assert vs[1]["quality_score"] == cat_build._quality_score({"resolution": 1080})
    # the primary server's version id is the pre-multi-source one (hash of the un-prefixed path)
    assert cat_build._vid("disk1/English Movies/Heat (1995)/Heat.1995.1080p.BluRay.mkv") in cat["versions"]


def test_build_keeps_same_server_duplicates_as_before():
    conn = _build_db()
    _add(conn, "disk1/English Movies/Heat (1995)/Heat.1995.1080p.BluRay.mkv", "Heat.1995.1080p.BluRay.mkv")
    _add(conn, "disk7/IMDB TOP MOVIES/Heat (1995)/Heat.1995.1080p.BluRay.mkv", "Heat.1995.1080p.BluRay.mkv")
    assert len(cat_build.build(conn, None)["versions"]) == 2


@pytest.mark.parametrize("bad", [
    "sources:\n  - {id: a, base_url: 'https://a/', roots: []}\n  - {id: b, base_url: 'https://b/', path_prefix: '', roots: []}\n",
    "sources:\n  - {id: a, base_url: 'https://a/', roots: []}\n  - {id: a, base_url: 'https://b/', roots: []}\n",
])
def test_invalid_source_config_is_refused(bad, tmp_path, monkeypatch):
    from catalog import settings
    (tmp_path / "sources.yaml").write_text(bad, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG", tmp_path)
    with pytest.raises(SystemExit):
        settings.load_sources()
