"""Tests for logo selection, language fallbacks, dark-logo guard, and Fanart.tv fallback in enrich.py."""
import struct
import zlib

from catalog import enrich as cat_enrich


def _make_png(color_type: int, rows: list[bytes], plte: bytes = b"", trns: bytes = b"") -> bytes:
    """Build a minimal valid non-interlaced 8-bit PNG in memory using stdlib only."""
    height = len(rows)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    width = len(rows[0]) // channels

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    raw = b"".join(b"\x00" + r for r in rows)
    parts = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", ihdr)]
    if plte:
        parts.append(chunk(b"PLTE", plte))
    if trns:
        parts.append(chunk(b"tRNS", trns))
    parts.append(chunk(b"IDAT", zlib.compress(raw)))
    parts.append(chunk(b"IEND", b""))
    return b"".join(parts)


def test_best_logo_prefers_png_and_original_language():
    logos = [
        {"iso_639_1": "en", "file_path": "/en.svg", "vote_average": 9.0, "vote_count": 10, "width": 800},
        {"iso_639_1": "hi", "file_path": "/hi.png", "vote_average": 5.0, "vote_count": 2, "width": 500},
    ]
    assert cat_enrich._best_logo(logos, "hi") == "/hi.png"

    logos_both_png = [
        {"iso_639_1": "en", "file_path": "/en.svg", "vote_average": 9.0, "vote_count": 10, "width": 800},
        {"iso_639_1": "en", "file_path": "/en.png", "vote_average": 5.0, "vote_count": 1, "width": 500},
        {"iso_639_1": "hi", "file_path": "/hi.png", "vote_average": 8.0, "vote_count": 5, "width": 700},
    ]
    assert cat_enrich._best_logo(logos_both_png, "hi") == "/en.png"

    # Unrelated language (French on a Hindi film) must NOT be picked.
    logos_wrong_lang = [{"iso_639_1": "fr", "file_path": "/fr.png", "vote_average": 8.0, "vote_count": 3, "width": 500}]
    assert cat_enrich._best_logo(logos_wrong_lang, "hi") is None

    # SVG still selected if no raster logo exists across allowed languages.
    logos_svg_only = [{"iso_639_1": "en", "file_path": "/only.svg", "vote_average": 5.0, "vote_count": 1, "width": 500}]
    assert cat_enrich._best_logo(logos_svg_only, "en") == "/only.svg"


def test_fanart_fallback_respects_missing_key_and_picks_best(monkeypatch):
    monkeypatch.delenv("FANART_API_KEY", raising=False)
    monkeypatch.delenv("FANART_API", raising=False)
    assert cat_enrich._fanart_logo("movie", 123, "tt123", None, "en") is None

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "hdmovielogo": [
                    {"lang": "fr", "url": "https://assets.fanart.tv/fr.png", "likes": "9"},
                    {"lang": "en", "url": "https://assets.fanart.tv/en1.png", "likes": "2"},
                    {"lang": "en", "url": "https://assets.fanart.tv/en2.png", "likes": "7"},
                ]
            }

    class FakeClient:
        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setenv("FANART_API", "dummy")
    monkeypatch.setattr(cat_enrich, "_fanart_client", lambda: FakeClient())
    assert cat_enrich._fanart_logo("movie", 123, "tt123", None, "en") == "https://assets.fanart.tv/en2.png"


def test_png_is_dark_guard_across_formats():
    # Pure black RGBA pixels with transparent background -> dark
    black_rgba = _make_png(6, [b"\x00\x00\x00\xff\x00\x00\x00\x00", b"\x05\x05\x05\xff\x00\x00\x00\x00"])
    assert cat_enrich._png_is_dark(black_rgba) is True

    # Grayscale+Alpha (TMDB color_type 4, like Delhi Crime /7t2vHq...) -> black is dark, white is not
    black_ga = _make_png(4, [b"\x00\xff\x00\x00", b"\x10\xff\x00\x00"])
    white_ga = _make_png(4, [b"\xeb\xff\x00\x00", b"\xf5\xff\x00\x00"])
    assert cat_enrich._png_is_dark(black_ga) is True
    assert cat_enrich._png_is_dark(white_ga) is False

    # Indexed palette (color_type 3) with tRNS
    dark_idx = _make_png(3, [b"\x00\x01"], plte=b"\x00\x00\x00\x04\x04\x04", trns=b"\x00\xff")
    bright_idx = _make_png(3, [b"\x00\x01"], plte=b"\x00\x00\x00\xf0\xf0\xf0", trns=b"\x00\xff")
    assert cat_enrich._png_is_dark(dark_idx) is True
    assert cat_enrich._png_is_dark(bright_idx) is False

    # Black logo with crisp white highlight/outline (>=10% bright pixels) must NOT be rejected
    outlined = _make_png(6, [b"\x00\x00\x00\xff" * 8 + b"\xff\xff\xff\xff" * 2])
    assert cat_enrich._png_is_dark(outlined) is False

    # Bright red/accent logo must NOT be rejected
    red_rgba = _make_png(6, [b"\xe5\x09\x14\xff\xe5\x09\x14\xff"])
    assert cat_enrich._png_is_dark(red_rgba) is False

    # Non-PNG or truncated bytes fail open (not considered dark)
    assert cat_enrich._png_is_dark(b"<svg></svg>") is False
    assert cat_enrich._png_is_dark(b"not-an-image") is False


def test_best_logo_and_fanart_skip_dark_candidates():
    logos = [
        {"iso_639_1": "en", "file_path": "/black.png", "vote_average": 5.0, "vote_count": 2, "width": 1200},
        {"iso_639_1": "en", "file_path": "/white.png", "vote_average": 0.0, "vote_count": 0, "width": 1200},
    ]
    assert cat_enrich._best_logo(logos, "hi", is_dark=lambda p: p == "/black.png") == "/white.png"

    # If all raster logos are dark, return None (allowing Fanart.tv or crisp white text fallback) instead of invisible art
    all_dark = [{"iso_639_1": "en", "file_path": "/black.png", "vote_average": 5.0, "vote_count": 2, "width": 1200}]
    assert cat_enrich._best_logo(all_dark, "en", is_dark=lambda _: True) is None


def test_fanart_logo_skips_dark_candidate(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "hdtvlogo": [
                    {"lang": "en", "url": "https://assets.fanart.tv/black.png", "likes": "10"},
                    {"lang": "en", "url": "https://assets.fanart.tv/white.png", "likes": "2"},
                ]
            }

    class FakeClient:
        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setenv("FANART_API", "dummy")
    monkeypatch.setattr(cat_enrich, "_fanart_client", lambda: FakeClient())
    picked = cat_enrich._fanart_logo("tv", 87508, "tt9398466", 358000, "hi", is_dark=lambda u: u.endswith("black.png"))
    assert picked == "https://assets.fanart.tv/white.png"
