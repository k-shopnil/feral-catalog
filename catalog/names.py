"""Release-name normalization, parsing and classification (plan §5.4)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from guessit import guessit

LANG_WORDS = {
    "english": "en", "eng": "en", "hindi": "hi", "hin": "hi", "tamil": "ta", "tam": "ta", "telugu": "te", "tel": "te",
    "malayalam": "ml", "mal": "ml", "kannada": "kn", "kan": "kn", "bengali": "bn", "bangla": "bn", "urdu": "ur",
    "punjabi": "pa", "marathi": "mr", "gujarati": "gu", "korean": "ko", "japanese": "ja", "chinese": "zh",
    "mandarin": "zh", "cantonese": "zh", "spanish": "es", "french": "fr", "turkish": "tr", "indonesian": "id",
    "thai": "th", "arabic": "ar", "german": "de", "italian": "it", "russian": "ru", "portuguese": "pt",
}
_LANG_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted(LANG_WORDS, key=len, reverse=True)) + r")(?![A-Za-z])", re.I)
_JUNK = [
    re.compile(p, re.I)
    for p in (
        r"\[DDN\]", r"~\s*TombDoc", r"-RARBG\b", r"\[TGx\]", r"-GalaxyTV", r"\[Org[^\]]*\]", r"\bMSubs\b",
        r"\bESubs?\b", r"\[\s*\]", r"-NTG\b", r"\bRZeroX\b", r"\bPanda\b",
    )
]
_LEADING_JUNK = re.compile(r"^(#+|\d{1,2}_)\s*")
_SXXEXX = re.compile(r"\bS(\d{1,2})[ ._-]?E(\d{1,3})(?:v\d)?(?:[ ._-]?-?E(\d{1,3})(?:v\d)?)?(?![0-9])", re.I)
# "Title (YYYY)" / "Title [YYYY]": everything before the bracketed year is the title (guessit splits on " - ").
_TITLE_BEFORE_YEAR = re.compile(r"^(.+?)[\s.]*[(\[]((?:19|20)\d{2})[)\]]")
_SEASON_FOLDER = re.compile(r"(?:\bS(\d{1,2})\b|\bSeason[ ._-]?(\d{1,2})\b)", re.I)
_ABS_EP = re.compile(r"\s-\s(\d{1,4})(?:v\d)?\b")
_SPECIAL_WORDS = re.compile(
    r"\b(WWE|UFC|WrestleMania|SmackDown|Monday Night Raw|PPV|Hell in a Cell|Money in the Bank|Royal Rumble|"
    r"Academy Awards|Oscars|Grammy|Golden Globe|FIFA|World Cup|Cricket|IPL|Premier League|Champions League)\b",
    re.I,
)
_SPECIAL_EP = re.compile(r"\b(OVA|OAD|OAV|Special|SP\d+)\b", re.I)
_EDITION = re.compile(r"\b(Extended(?: Cut| Edition)?|Director'?s Cut|Unrated|Theatrical|Super Duper Cut|Unleashed Extended|Remastered|IMAX)\b", re.I)
_CAM = re.compile(r"\b(HDCAM|CAM|HDTS|TS|TELESYNC|PreDVD|HDTC|TC)\b")
_RES = re.compile(r"\b(2160p|4K|1080p|720p|576p|480p)\b", re.I)
_SRC = [
    ("BluRay", re.compile(r"\b(BluRay|Blu-Ray|BDRip|BRRip|Bluray)\b", re.I)),
    ("WEB-DL", re.compile(r"\bWEB[-. ]?DL\b", re.I)),
    ("WEBRip", re.compile(r"\bWEB[-. ]?Rip\b|\bWEB\b", re.I)),
    ("HDTV", re.compile(r"\bHDTV\b", re.I)),
    ("DVDRip", re.compile(r"\bDVD(Rip)?\b", re.I)),
    ("HDRip", re.compile(r"\bHDRip\b", re.I)),
]


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s.replace(" ", " ").replace(" ", " "))


def normalize(name: str) -> str:
    s = nfc(name)
    for rx in _JUNK:
        s = rx.sub(" ", s)
    s = _LEADING_JUNK.sub("", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def norm_key(title: str) -> str:
    """Normalized title used for grouping keys."""
    s = unicodedata.normalize("NFKD", title.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_ext(name: str) -> str:
    return re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name)


def languages_in(text: str) -> list[str]:
    out: list[str] = []
    for m in _LANG_RE.finditer(text):
        code = LANG_WORDS[m.group(1).lower()]
        if code not in out:
            out.append(code)
    return out


@dataclass
class Parsed:
    kind: str = "unknown"          # movie | episode | special | extra | sample | unknown
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    episode_title: str | None = None
    absolute_episode: int | None = None
    resolution: int | None = None
    source: str | None = None
    video_codec: str | None = None
    hdr: str | None = None
    bit_depth: int | None = None
    audio_hint: str | None = None
    languages: list[str] = field(default_factory=list)
    dual_audio: bool = False
    subs_hint: bool = False
    edition: str | None = None
    cam: bool = False
    release_group: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _quality(text: str, p: Parsed) -> None:
    m = _RES.search(text)
    if m:
        v = m.group(1).lower()
        p.resolution = 2160 if v in ("2160p", "4k") else int(v[:-1])
    for label, rx in _SRC:
        if rx.search(text):
            p.source = label
            break
    if _CAM.search(text):
        p.cam = True
        p.source = p.source or "CAM"
    if re.search(r"\b(x265|HEVC|H\.?265)\b", text, re.I):
        p.video_codec = "hevc"
    elif re.search(r"\b(x264|H\.?264|AVC)\b", text, re.I):
        p.video_codec = "h264"
    elif re.search(r"\bAV1\b", text, re.I):
        p.video_codec = "av1"
    if re.search(r"\b(DV|DoVi|Dolby[ .]?Vision)\b", text, re.I):
        p.hdr = "DV"
    elif re.search(r"\bHDR10\+|HDR10Plus\b", text, re.I):
        p.hdr = "HDR10+"
    elif re.search(r"\bHDR(10)?\b", text, re.I):
        p.hdr = "HDR10"
    if re.search(r"\b10[ -]?bit\b", text, re.I):
        p.bit_depth = 10
    m = re.search(r"\b(DDP?\s?5\.1(?:\s?Atmos)?|DD\+?\s?5\.1|AAC\s?5\.1|5\.1|7\.1|Atmos|DTS(?:-HD)?(?:\s?MA)?|TrueHD)\b", text, re.I)
    if m:
        p.audio_hint = m.group(1)
    p.dual_audio = bool(re.search(r"\bDual[ .-]?Audio\b", text, re.I))
    p.subs_hint = bool(re.search(r"\b(ESubs?|MSubs|Subs?)\b", text, re.I))
    m = _EDITION.search(text)
    if m:
        p.edition = m.group(1)


def parse_release(name: str, prefer: str | None = None) -> Parsed:
    """Parse one file or folder name. `prefer`='movie'|'episode' hints guessit."""
    raw = nfc(name)
    clean = normalize(strip_ext(raw) if "." in raw[-6:] else raw)
    p = Parsed()
    _quality(raw, p)
    p.languages = languages_in(raw)
    opts = {"type": prefer} if prefer in ("movie", "episode") else {}
    try:
        g = guessit(clean, opts)
    except Exception:
        g = {}
    title = str(g.get("title", "")).strip()
    p.title = re.sub(r"\s+", " ", title)
    y = g.get("year")
    p.year = int(y) if isinstance(y, int) else None
    m = _TITLE_BEFORE_YEAR.match(clean)
    if m and not _SXXEXX.search(m.group(1)):
        t = m.group(1).strip(" -_")
        if " " not in t and t.count(".") >= 1:
            t = t.replace(".", " ")
        p.title = re.sub(r"\s+", " ", t)
        p.year = int(m.group(2))
    if p.year is None:
        m = re.search(r"[(\[. ](19[2-9]\d|20[0-4]\d)[)\]. ]", f" {clean} ")
        if m:
            p.year = int(m.group(1))
    rg = g.get("release_group")
    p.release_group = str(rg) if rg else None
    m = _SXXEXX.search(clean)
    if m:
        p.season, p.episode = int(m.group(1)), int(m.group(2))
        p.episode_end = int(m.group(3)) if m.group(3) else None
    else:
        s, e = g.get("season"), g.get("episode")
        if isinstance(s, int):
            p.season = s
        if isinstance(e, int):
            p.episode = e
        elif isinstance(e, list) and e:
            p.episode, p.episode_end = int(e[0]), int(e[-1])
    et = g.get("episode_title")
    if et:
        p.episode_title = str(et)
    if p.episode is None:
        m = _ABS_EP.search(clean)
        if m and p.release_group:
            p.absolute_episode = int(m.group(1))
    if _SPECIAL_WORDS.search(re.sub(r"[._]+", " ", clean)):
        p.kind = "special"
    elif p.episode is not None or p.absolute_episode is not None:
        p.kind = "episode"
    elif g.get("type") == "episode" and p.season is not None:
        p.kind = "episode"
    else:
        p.kind = "movie"
    if _SPECIAL_EP.search(clean) and p.kind == "episode" and p.season is None:
        p.season = 0
    if re.search(r"\bsample\b", clean, re.I):
        p.kind = "sample"
    return p


def season_from_folder(folder: str) -> int | None:
    m = _SEASON_FOLDER.search(nfc(folder))
    if m:
        return int(m.group(1) or m.group(2))
    if re.search(r"\b(OVA|OAD|Specials?)\b", folder, re.I):
        return 0
    return None


def show_title_from_folder(folder: str) -> tuple[str, int | None]:
    s = normalize(folder)
    s = re.sub(r"\s*\((OVA|OAD|Specials?)\)\s*$", "", s, flags=re.I)
    year = None
    m = re.search(r"\((\d{4})\)", s)
    if m:
        year = int(m.group(1))
        s = s[: m.start()].strip()
    return s.strip(" -._"), year
