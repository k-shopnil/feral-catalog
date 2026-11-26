from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# FERAL_HOME / FERAL_CONFIG relocate the writable state and the config (the Android app sets them; the PC
# leaves them unset, so everything stays under the repo as before).
HOME = Path(os.environ["FERAL_HOME"]) if os.environ.get("FERAL_HOME") else ROOT
DATA = HOME / "data"
SITE = HOME / "site"
CONFIG = Path(os.environ["FERAL_CONFIG"]) if os.environ.get("FERAL_CONFIG") else ROOT / "config"
LOGS = HOME / "logs"
REPORTS = HOME / "reports"

PAGES_REPO = "https://github.com/k-shopnil/feral-catalog.git"
PAGES_REPO_SLUG = "k-shopnil/feral-catalog"
PAGES_BRANCH = "gh-pages"
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


def fanart_key() -> str | None:
    return (os.environ.get("FANART_API_KEY") or os.environ.get("FANART_API") or "").strip() or None


@dataclass
class Root:
    path: str               # state key: the source's path prefix + rel (e.g. "ftp:English/"); ends with '/'
    kind: str               # movie | show | auto
    lang_hint: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    exclude_children: list[str] = field(default_factory=list)
    rel: str = ""           # decoded, relative to the source's base_url, ends with '/'
    source: str = ""        # Source.id

    def __post_init__(self):
        if not self.rel:
            self.rel = self.path


@dataclass
class Source:
    id: str
    base_url: str
    max_concurrency: int
    user_agent: str
    roots: list[Root]
    exclude: list[str]
    # Every state path (dir_node.path, file.path, root, work keys of show roots) of this source starts with
    # `prefix`. The first source keeps "" so state.db rows, work keys, overrides and version ids from before
    # multi-source support stay valid with no migration; every other source is namespaced as "<id>:", because
    # the servers share folder names (disk1/ .. disk11/).
    prefix: str = ""
    label: str | None = None    # appended to Version.label for files from this source (None for the first)
    priority: int = 0           # 0 = first source in sources.yaml; lower wins when the same file is on several

    def rel_of(self, path: str) -> str:
        return path[len(self.prefix):] if path.startswith(self.prefix) else path


def load_sources() -> list[Source]:
    cfg = yaml.safe_load((CONFIG / "sources.yaml").read_text(encoding="utf-8"))
    out: list[Source] = []
    for i, s in enumerate(cfg["sources"]):
        sid = s["id"]
        prefix = s.get("path_prefix", "" if i == 0 else f"{sid}:")
        if i > 0 and not prefix:
            raise SystemExit(f"sources.yaml: source {sid!r} needs a path_prefix (only the first source may use none)")
        if any(o.id == sid or o.prefix == prefix for o in out):
            raise SystemExit(f"sources.yaml: duplicate source id or path_prefix for {sid!r}")
        roots = [
            Root(
                path=prefix + r["path"],
                kind=r.get("kind", "auto"),
                lang_hint=r.get("lang_hint", []) or [],
                tags=r.get("tags", []) or [],
                exclude_children=r.get("exclude_children", []) or [],
                rel=r["path"],
                source=sid,
            )
            for r in s["roots"]
        ]
        out.append(Source(
            id=sid,
            base_url=s["base_url"],
            max_concurrency=int(s.get("max_concurrency", 6)),
            user_agent=s.get("user_agent", "Feral-Indexer/1.0"),
            roots=roots,
            exclude=s.get("exclude", []) or [],
            prefix=prefix,
            label=s.get("label") if i > 0 else None,
            priority=i,
        ))
    return out


def load_source() -> Source:
    """The first (primary) source."""
    return load_sources()[0]


def all_roots(sources: list[Source]) -> list[Root]:
    return [r for s in sources for r in s.roots]


def source_of(sources: list[Source], path: str) -> Source:
    """Source owning a state path: the longest matching non-empty prefix, else the unprefixed first source."""
    best = None
    for s in sources:
        if s.prefix and path.startswith(s.prefix) and (best is None or len(s.prefix) > len(best.prefix)):
            best = s
    return best or sources[0]


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
