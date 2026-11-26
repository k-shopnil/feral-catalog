"""Parser for Apache mod_autoindex HTML table listings (plan §5.3.1)."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

# <a href="NAME">label</a></td><td align="right">2026-11-17 23:45  </td><td align="right">1.8G</td>
_ROW = re.compile(
    r'<a href="([^"?][^"]*)">[^<]*</a></td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>',
    re.IGNORECASE,
)
# nginx autoindex (data/ftp.ctgfun.com): <a href="NAME">label</a>     25-Nov-2026 17:42       1G
# The label is truncated by nginx; the href always carries the full name. Times are server local time (checked
# against Last-Modified: listing 08:10 = 02:10 GMT, i.e. Asia/Dhaka), same as the Apache listings.
_NGINX_ROW = re.compile(
    r'<a href="([^"?][^"]*)">[^<]*</a>\s+(\d{1,2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})\s+(\S+)',
)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
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


def _nginx_mtime(s: str) -> str:
    """'25-Nov-2026 17:42' -> '2026-11-25 17:42' (the Apache form the crawler stores and parses)."""
    day, mon, rest = s.split("-", 2)
    year, hm = rest.split(" ", 1)
    m = _MONTHS.get(mon.lower())
    return f"{year}-{m:02d}-{int(day):02d} {hm}" if m else ""


def parse_listing(page: str) -> list[Entry]:
    entries: list[Entry] = []
    for href, mtime, size in _ROW.findall(page):
        href = html.unescape(href)  # '&amp;' in the attribute: the real URL has '&' (the escaped one is a 404)
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
    if entries:
        return entries
    for href, mtime, size in _NGINX_ROW.findall(page):
        href = html.unescape(href)
        if href.startswith("/") or href.startswith("../"):
            continue
        entries.append(Entry(href, _decode(href), href.endswith("/"), _nginx_mtime(mtime), size))
    if entries or "Index of" not in page:
        return entries
    for href in map(html.unescape, _LI.findall(page)):
        if href.startswith("/") or href.startswith("../"):
            continue
        entries.append(Entry(href, _decode(href), href.endswith("/"), "", ""))
    return entries
