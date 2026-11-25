"""Parser for Apache mod_autoindex HTML table listings (plan §5.3.1)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

# <a href="NAME">label</a></td><td align="right">2026-11-17 23:45  </td><td align="right">1.8G</td>
_ROW = re.compile(
    r'<a href="([^"?][^"]*)">[^<]*</a></td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>',
    re.IGNORECASE,
)
# Fallback for ?F=0 plain lists: <li><a href="NAME"> NAME</a></li>
_LI = re.compile(r'<li><a href="([^"?][^"]*)">', re.IGNORECASE)

_SIZE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?)\s*$", re.IGNORECASE)
_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


@dataclass(frozen=True)
class Entry:
    href: str        # raw href exactly as served (used to build URLs)
    name: str        # decoded, NFC-normalized, without trailing '/'
    is_dir: bool
    mtime: str       # 'YYYY-MM-DD HH:MM' or ''
    size_human: str  # '1.8G', '71K', '-' or ''

    @property
    def size_bytes(self) -> int | None:
        m = _SIZE.match(self.size_human or "")
        if not m:
            return None
        return int(float(m.group(1)) * _MULT[m.group(2).upper()])


def _decode(href: str) -> str:
    name = unquote(href.rstrip("/"), errors="replace")
    return unicodedata.normalize("NFC", name)


def parse_listing(html: str) -> list[Entry]:
    entries: list[Entry] = []
    for href, mtime, size in _ROW.findall(html):
        if href.startswith("/") or href.startswith("../"):
            continue  # parent directory
        entries.append(
            Entry(
                href=href,
                name=_decode(href),
                is_dir=href.endswith("/"),
                mtime=mtime.strip().replace("&nbsp;", ""),
                size_human=size.strip().replace("&nbsp;", ""),
            )
        )
    if entries or "Index of" not in html:
        return entries
    for href in _LI.findall(html):
        if href.startswith("/") or href.startswith("../"):
            continue
        entries.append(Entry(href, _decode(href), href.endswith("/"), "", ""))
    return entries
