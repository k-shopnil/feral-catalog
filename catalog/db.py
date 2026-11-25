from __future__ import annotations

import sqlite3
from pathlib import Path

from .settings import DATA

DB_PATH = DATA / "state.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS dir_node (
  path TEXT PRIMARY KEY,          -- decoded, relative to base, ends with '/'
  url TEXT NOT NULL,
  parent_path TEXT,
  root TEXT NOT NULL,
  depth INTEGER NOT NULL,         -- 0 = root itself
  listed_mtime TEXT,              -- mtime string shown in the parent listing
  last_listed_at INTEGER,
  last_seen_at INTEGER,
  missing_since INTEGER
);

CREATE TABLE IF NOT EXISTS file (
  path TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  dir_path TEXT NOT NULL,
  root TEXT NOT NULL,
  name TEXT NOT NULL,
  ext TEXT NOT NULL,
  role TEXT NOT NULL,             -- video | subtitle | audio_extract | ignored
  size_human TEXT,
  size_bytes INTEGER,
  listed_mtime TEXT,
  added_at INTEGER,               -- epoch from listed mtime (server local time)
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  missing_since INTEGER,
  obs_sig TEXT,                   -- size|mtime at last observation
  obs_at INTEGER,
  stable INTEGER NOT NULL DEFAULT 0,
  parsed INTEGER NOT NULL DEFAULT 0,
  parse_json TEXT,
  kind TEXT,                      -- movie | episode | special | extra | sample | unknown
  work_key TEXT,
  season INTEGER,
  episode INTEGER,
  episode_end INTEGER
);
CREATE INDEX IF NOT EXISTS file_work ON file(work_key);
CREATE INDEX IF NOT EXISTS file_dir ON file(dir_path);

CREATE TABLE IF NOT EXISTS work (
  key TEXT PRIMARY KEY,           -- m|<norm title>|<year>  or  s|<root>/<show folder>
  kind TEXT NOT NULL,             -- movie | show
  title TEXT NOT NULL,
  year INTEGER,
  langs TEXT,                     -- JSON list
  root TEXT,
  tags TEXT,                      -- JSON list
  sample_path TEXT,
  status TEXT NOT NULL DEFAULT 'new',  -- new | auto | auto_low | manual | unmatched | ignored | error
  tmdb_id INTEGER,
  score REAL,
  margin REAL,
  candidates TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at INTEGER,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS tmdb_cache (
  key TEXT PRIMARY KEY,
  json TEXT,
  fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tmdb_title (     -- trimmed TMDB details (plan §5.6.1)
  kind TEXT NOT NULL,                       -- movie | tv
  tmdb_id INTEGER NOT NULL,
  data TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (kind, tmdb_id)
);

CREATE TABLE IF NOT EXISTS tmdb_season (    -- trimmed season details
  tmdb_id INTEGER NOT NULL,
  season INTEGER NOT NULL,
  data TEXT,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (tmdb_id, season)
);

CREATE TABLE IF NOT EXISTS published (
  entity TEXT NOT NULL,
  id TEXT NOT NULL,
  hash TEXT NOT NULL,
  version INTEGER NOT NULL,
  PRIMARY KEY (entity, id)
);

CREATE TABLE IF NOT EXISTS catalog_version (
  version INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  changes INTEGER NOT NULL,
  kind TEXT NOT NULL              -- delta | snapshot
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
