"""Tests for Attack on Titan (TMDB 1429) catalog normalization and isolation."""
import json
import sqlite3

from catalog import aot, build as cat_build


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tmdb_title (kind TEXT, tmdb_id INTEGER, data TEXT, fetched_at INTEGER, PRIMARY KEY (kind, tmdb_id));
        CREATE TABLE tmdb_season (tmdb_id INTEGER, season INTEGER, data TEXT, fetched_at INTEGER, PRIMARY KEY (tmdb_id, season));
        CREATE TABLE work (key TEXT PRIMARY KEY, kind TEXT, title TEXT, year INTEGER, langs TEXT, root TEXT, tags TEXT, sample_path TEXT, status TEXT, tmdb_id INTEGER, score REAL, margin REAL, candidates TEXT, attempts INTEGER, last_attempt_at INTEGER, updated_at INTEGER);
        CREATE TABLE file (path TEXT PRIMARY KEY, url TEXT, dir_path TEXT, root TEXT, name TEXT, ext TEXT, role TEXT, size_human TEXT, size_bytes INTEGER, listed_mtime INTEGER, added_at INTEGER, first_seen_at INTEGER, last_seen_at INTEGER, missing_since INTEGER, obs_sig TEXT, obs_at INTEGER, stable INTEGER, parsed INTEGER, parse_json TEXT, kind TEXT, work_key TEXT, season INTEGER, episode INTEGER, episode_end INTEGER);
        """
    )
    return conn


def test_aot_resolution_and_isolation():
    counts = {1: 25, 2: 12, 3: 22, 4: 28}
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan (OVA)/[Anime Time] Attack On Titan OAD - 02.mkv",
        "[Anime Time] Attack On Titan OAD - 02.mkv", 0, 2, counts
    ) == (0, 13)
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan (OVA)/[Anime Time] Attack On Titan OAD - Lost Girls 02.mkv",
        "[Anime Time] Attack On Titan OAD - Lost Girls 02.mkv", 0, 2, counts
    ) == (0, 18)
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan S04 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan (Season 4) Part 3/[Anime Time] Attack On Titan - The Final Season Part 3 - Part 1.mkv",
        "[Anime Time] Attack On Titan - The Final Season Part 3 - Part 1.mkv", None, None, counts
    ) == (0, 36)
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan S02 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan Season 2 - 26.mkv",
        "[Anime Time] Attack On Titan Season 2 - 26.mkv", 2, 26, counts
    ) == (2, 1)
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan S03 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan Season 3 - 50.mkv",
        "[Anime Time] Attack On Titan Season 3 - 50.mkv", 3, 50, counts
    ) == (3, 13)
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan S04 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan (Season 4) Part 2/[Anime Time] Attack On Titan Season 4 - 87.mkv",
        "[Anime Time] Attack On Titan Season 4 - 87.mkv", 4, 87, counts
    ) == (4, 28)
    assert aot.resolve_aot_season_episode(
        "disk7/ANIME - PART TWO/Attack On Titan/Attack On Titan S01 Dual Audio 1080p WEBRip x265 ESub [DDN]/Extras/Attack On Titan S1 - Ncop 1-3.mkv",
        "Attack On Titan S1 - Ncop 1-3.mkv", None, None, counts
    ) is None


def test_build_aot_missing_episodes_and_non_aot_untouched():
    conn = _make_db()
    conn.execute(
        "INSERT INTO tmdb_title VALUES ('tv', 1429, ?, 1)",
        (json.dumps({
            "name": "Attack on Titan", "genres": [{"id": 16, "name": "Animation"}], "original_language": "ja",
            "seasons": [
                {"season_number": 0, "name": "Specials", "episode_count": 37},
                {"season_number": 1, "name": "Season 1", "episode_count": 2},
                {"season_number": 2, "name": "Season 2", "episode_count": 1},
            ],
        }),),
    )
    conn.execute("INSERT INTO tmdb_season VALUES (1429, 0, ?, 1)", (json.dumps({
        "name": "Specials",
        "episodes": [
            {"episode_number": 7, "name": "Ilse's Notebook", "runtime_min": 24},
            {"episode_number": 36, "name": "The Final Chapters Special (1)", "runtime_min": 61},
        ],
    }),))
    conn.execute("INSERT INTO tmdb_season VALUES (1429, 1, ?, 1)", (json.dumps({
        "name": "Season 1",
        "episodes": [
            {"episode_number": 1, "name": "Ep 1", "overview": "First", "runtime_min": 24},
            {"episode_number": 2, "name": "Ep 2", "overview": "Second", "runtime_min": 24},
        ],
    }),))
    conn.execute("INSERT INTO tmdb_season VALUES (1429, 2, ?, 1)", (json.dumps({
        "name": "Season 2",
        "episodes": [{"episode_number": 1, "name": "Beast Titan", "overview": "S2E1", "runtime_min": 24}],
    }),))
    # Another show to verify untouched behavior (keeps episode=26 as-is and skips kind='extra')
    conn.execute(
        "INSERT INTO tmdb_title VALUES ('tv', 999999, ?, 1)",
        (json.dumps({"name": "Other Show", "genres": [], "seasons": [{"season_number": 2, "episode_count": 12}]}),),
    )
    conn.execute("INSERT INTO tmdb_season VALUES (999999, 2, ?, 1)", (json.dumps({"name": "Season 2", "episodes": []}),))
    conn.execute("INSERT INTO work(key, kind, title, status, tmdb_id, tags) VALUES (?, 'show', 'Attack On Titan', 'manual', 1429, '[\"anime\"]')", (aot.AOT_WORK_KEY,))
    conn.execute("INSERT INTO work(key, kind, title, status, tmdb_id, tags) VALUES ('s|other/', 'show', 'Other Show', 'auto', 999999, '[]')")

    files = [
        (aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan S01 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan - 01.mkv",
         aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan S01 Dual Audio 1080p WEBRip x265 ESub [DDN]", "[Anime Time] Attack On Titan - 01.mkv", 500_000_000, "episode", aot.AOT_WORK_KEY, 1, 1),
        (aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan S02 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan Season 2 - 03.mkv",
         aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan S02 Dual Audio 1080p WEBRip x265 ESub [DDN]", "[Anime Time] Attack On Titan Season 2 - 03.mkv", 500_000_000, "episode", aot.AOT_WORK_KEY, 2, 3),
        (aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan (OVA)/[Anime Time] Attack On Titan OAD - 01.mkv",
         aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan (OVA)", "[Anime Time] Attack On Titan OAD - 01.mkv", 400_000_000, "episode", aot.AOT_WORK_KEY, 0, 1),
        (aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan S04 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan (Season 4) Part 3/[Anime Time] Attack On Titan - The Final Season Part 3 - Part 1.mkv",
         aot.AOT_WORK_KEY.removeprefix("s|") + "Attack On Titan S04 Dual Audio 1080p WEBRip x265 ESub [DDN]/[Anime Time] Attack On Titan (Season 4) Part 3", "[Anime Time] Attack On Titan - The Final Season Part 3 - Part 1.mkv", 1_200_000_000, "extra", aot.AOT_WORK_KEY, None, None),
        ("other/S02/ep26.mkv", "other/S02", "ep26.mkv", 400_000_000, "episode", "s|other/", 2, 26),
        ("other/S02/part1.mkv", "other/S02", "part1.mkv", 400_000_000, "extra", "s|other/", None, None),
    ]
    for path, dp, name, size, kind, wk, s, e in files:
        conn.execute(
            "INSERT INTO file(path, url, dir_path, root, name, ext, role, size_bytes, added_at, first_seen_at, parse_json, kind, work_key, season, episode) VALUES (?, ?, ?, '', ?, 'mkv', 'video', ?, 1000, 1000, '{}', ?, ?, ?, ?)",
            (path, f"https://x/{path}", dp, name, size, kind, wk, s, e),
        )

    cat = cat_build.build(conn, None)
    assert "t1429:s1e2" in cat["episodes"]
    assert cat["episodes"]["t1429:s1e2"]["name"] == "Ep 2"
    assert not any(v["episode_id"] == "t1429:s1e2" for v in cat["versions"].values())
    assert cat["episodes"]["t1429:s2e1"]["name"] == "Beast Titan"
    assert cat["episodes"]["t1429:s0e7"]["name"] == "Ilse's Notebook"
    assert cat["episodes"]["t1429:s0e36"]["name"] == "The Final Chapters Special (1)"
    assert cat["titles"]["t1429"]["best_quality"] == "1080p"
    # Non-AoT show remains untouched
    assert "t999999:s2e26" in cat["episodes"]
    assert len([v for v in cat["versions"].values() if v["title_id"] == "t999999"]) == 1
