"""Tests for logo selection, language fallbacks, and Fanart.tv fallback in enrich.py."""
from catalog import enrich as cat_enrich


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
