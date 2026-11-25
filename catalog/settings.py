from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SITE = ROOT / "site"
CONFIG = ROOT / "config"
LOGS = ROOT / "logs"
REPORTS = ROOT / "reports"

PAGES_REPO = "https://github.com/k-shopnil/feral-catalog.git"
PAGES_BASE_URL = "https://k-shopnil.github.io/feral-catalog/"

for d in (DATA, SITE, LOGS, REPORTS):
    d.mkdir(parents=True, exist_ok=True)

# Secrets: this repo's .env first, then the app repo's root .env (owner keeps the token there).
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / "ctgmovies-tv" / ".env")


def tmdb_token() -> str:
    tok = os.environ.get("TMDB_READ_ACCESS", "").strip()
    if not tok:
        raise SystemExit("TMDB_READ_ACCESS is not set (expected in .env). Aborting.")
    return tok


@dataclass
class Root:
    path: str               # decoded, relative to base, ends with '/'
    kind: str               # movie | show | auto
    lang_hint: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    exclude_children: list[str] = field(default_factory=list)


@dataclass
class Source:
    id: str
    base_url: str
    max_concurrency: int
    user_agent: str
    roots: list[Root]
    exclude: list[str]


def load_source() -> Source:
    cfg = yaml.safe_load((CONFIG / "sources.yaml").read_text(encoding="utf-8"))
    s = cfg["sources"][0]
    roots = [
        Root(
            path=r["path"],
            kind=r.get("kind", "auto"),
            lang_hint=r.get("lang_hint", []) or [],
            tags=r.get("tags", []) or [],
            exclude_children=r.get("exclude_children", []) or [],
        )
        for r in s["roots"]
    ]
    return Source(
        id=s["id"],
        base_url=s["base_url"],
        max_concurrency=int(s.get("max_concurrency", 6)),
        user_agent=s.get("user_agent", "Feral-Indexer/1.0"),
        roots=roots,
        exclude=s.get("exclude", []) or [],
    )


def load_yaml(name: str, default=None):
    p = CONFIG / name
    if not p.exists():
        return default
    return yaml.safe_load(p.read_text(encoding="utf-8")) or default


def setup_logging(verbose: bool = False) -> None:
    from datetime import datetime

    logfile = LOGS / f"run-{datetime.now():%Y%m%d}.log"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(logfile, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("rebulk").setLevel(logging.WARNING)
