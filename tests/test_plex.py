"""Tests for the Plex HTTP helpers and Metadata mapping (``app/plex.py``).

Covers the cached GET helper, reachability probe, image/GUID extraction, the
uniform item mapping, and subtitle upload — both happy paths and the failure /
None-return branches.
"""

import httpx
import respx

from app import plex
from app.config import PLEX_URL


# ---- plex_configured --------------------------------------------------------

def test_plex_configured_true():
    assert plex.plex_configured() is True


def test_plex_configured_false_without_url(monkeypatch):
    monkeypatch.setattr(plex, "PLEX_URL", "")
    assert plex.plex_configured() is False


def test_plex_configured_false_without_token(monkeypatch):
    monkeypatch.setattr(plex, "PLEX_TOKEN", "")
    assert plex.plex_configured() is False


# ---- plex_reachable ---------------------------------------------------------

def _clear_reachable():
    plex._reachable_cache = None


async def test_plex_reachable_false_when_unconfigured(monkeypatch):
    _clear_reachable()
    monkeypatch.setattr(plex, "PLEX_URL", "")
    assert await plex.plex_reachable() is False


@respx.mock
async def test_plex_reachable_true_on_200():
    _clear_reachable()
    route = respx.get(f"{PLEX_URL}/identity").mock(return_value=httpx.Response(200))
    assert await plex.plex_reachable() is True
    assert route.called


@respx.mock
async def test_plex_reachable_false_on_401():
    _clear_reachable()
    respx.get(f"{PLEX_URL}/identity").mock(return_value=httpx.Response(401))
    assert await plex.plex_reachable() is False


@respx.mock
async def test_plex_reachable_false_on_exception():
    _clear_reachable()
    respx.get(f"{PLEX_URL}/identity").mock(side_effect=httpx.ConnectError("boom"))
    assert await plex.plex_reachable() is False


@respx.mock
async def test_plex_reachable_uses_cache():
    _clear_reachable()
    route = respx.get(f"{PLEX_URL}/identity").mock(return_value=httpx.Response(200))
    assert await plex.plex_reachable() is True
    # Second call is served from the cache; no additional Plex round-trip.
    assert await plex.plex_reachable() is True
    assert route.call_count == 1


# ---- plex_get ---------------------------------------------------------------

async def test_plex_get_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(plex, "PLEX_URL", "")
    assert await plex.plex_get("/library/sections") is None


@respx.mock
async def test_plex_get_success():
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert await plex.plex_get("/library/sections") == {"ok": True}


@respx.mock
async def test_plex_get_non_200_returns_none():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(500))
    assert await plex.plex_get("/library/sections") is None


@respx.mock
async def test_plex_get_exception_returns_none():
    respx.get(f"{PLEX_URL}/library/sections").mock(side_effect=httpx.ConnectError("x"))
    assert await plex.plex_get("/library/sections") is None


@respx.mock
async def test_plex_get_caches_by_ttl():
    plex._plex_cache.clear()
    route = respx.get(f"{PLEX_URL}/library/all").mock(
        return_value=httpx.Response(200, json={"n": 1})
    )
    first = await plex.plex_get("/library/all", params={"a": "1", "b": "2"}, cache_ttl=30)
    second = await plex.plex_get("/library/all", params={"b": "2", "a": "1"}, cache_ttl=30)
    assert first == second == {"n": 1}
    # Cache hit (same key regardless of param order) → only one upstream call.
    assert route.call_count == 1


@respx.mock
async def test_plex_get_cache_evicts_when_full():
    plex._plex_cache.clear()
    # Pre-fill the cache past the max so the next store triggers a clear().
    for i in range(plex._PLEX_CACHE_MAX + 1):
        plex._plex_cache[f"stale-{i}"] = (2**31, {})
    respx.get(f"{PLEX_URL}/library/fresh").mock(
        return_value=httpx.Response(200, json={"fresh": True})
    )
    assert await plex.plex_get("/library/fresh", cache_ttl=30) == {"fresh": True}
    # The clear() dropped the stale entries; only the fresh key remains.
    assert any(k.startswith("/library/fresh") for k in plex._plex_cache)
    assert not any(k.startswith("stale-") for k in plex._plex_cache)


# ---- plex_image -------------------------------------------------------------

def test_plex_image_found():
    m = {"Image": [{"type": "clearLogo", "url": "/logo.png"}, {"type": "background"}]}
    assert plex.plex_image(m, "clearLogo") == "/logo.png"


def test_plex_image_missing_type_returns_none():
    m = {"Image": [{"type": "background", "url": "/bg.png"}]}
    assert plex.plex_image(m, "clearLogo") is None


def test_plex_image_no_image_array_returns_none():
    assert plex.plex_image({}, "clearLogo") is None
    assert plex.plex_image({"Image": None}, "clearLogo") is None


# ---- tmdb_from_metadata -----------------------------------------------------

def test_tmdb_from_metadata_parses_tmdb_guid():
    m = {"Guid": [{"id": "imdb://tt1"}, {"id": "tmdb://603"}]}
    assert plex.tmdb_from_metadata(m) == 603


def test_tmdb_from_metadata_no_tmdb_guid_returns_none():
    m = {"Guid": [{"id": "imdb://tt1"}, {"id": "tvdb://99"}]}
    assert plex.tmdb_from_metadata(m) is None


def test_tmdb_from_metadata_empty_or_missing_returns_none():
    assert plex.tmdb_from_metadata({}) is None
    assert plex.tmdb_from_metadata({"Guid": None}) is None
    assert plex.tmdb_from_metadata({"Guid": []}) is None


def test_tmdb_from_metadata_non_int_returns_none():
    m = {"Guid": [{"id": "tmdb://not-a-number"}]}
    assert plex.tmdb_from_metadata(m) is None


def test_tmdb_from_metadata_guid_without_id():
    m = {"Guid": [{}, {"id": "tmdb://12"}]}
    assert plex.tmdb_from_metadata(m) == 12


# ---- map_plex_item ----------------------------------------------------------

def test_map_plex_item_full():
    m = {
        "ratingKey": 500,
        "Guid": [{"id": "tmdb://42"}],
        "title": "Movie",
        "type": "movie",
        "year": 2020,
        "thumb": "/thumb",
        "art": "/art",
        "Image": [{"type": "clearLogo", "url": "/logo"}],
        "summary": "sum",
        "contentRating": "PG",
        "addedAt": 123,
        "duration": 999,
        "childCount": 3,
    }
    out = plex.map_plex_item(m, rating=8.456, sources={"imdb": {"score": 8}})
    assert out["rating_key"] == "500"
    assert out["tmdb_id"] == 42
    assert out["title"] == "Movie"
    assert out["type"] == "movie"
    assert out["year"] == 2020
    assert out["thumb"] == "/thumb"
    assert out["art"] == "/art"
    assert out["clear_logo"] == "/logo"
    assert out["content_rating"] == "PG"
    assert out["child_count"] == 3
    assert out["rating"] == 8.5  # rounded
    assert out["sources"] == {"imdb": {"score": 8}}


def test_map_plex_item_defaults_and_leafcount_fallback():
    # childCount absent → falls back to leafCount; no rating → None; no sources → {}.
    m = {"leafCount": 7}
    out = plex.map_plex_item(m)
    assert out["rating_key"] == ""
    assert out["tmdb_id"] is None
    assert out["title"] == ""
    assert out["type"] == ""
    assert out["year"] is None
    assert out["child_count"] == 7
    assert out["rating"] is None
    assert out["sources"] == {}
    assert out["clear_logo"] is None


# ---- plex_upload_subtitle ---------------------------------------------------

async def test_plex_upload_subtitle_unconfigured(monkeypatch):
    monkeypatch.setattr(plex, "PLEX_URL", "")
    assert await plex.plex_upload_subtitle("1", b"data", "en", "srt", "t") is False


@respx.mock
async def test_plex_upload_subtitle_success_200():
    route = respx.post(f"{PLEX_URL}/library/metadata/1/subtitles").mock(
        return_value=httpx.Response(200)
    )
    assert await plex.plex_upload_subtitle("1", b"data", "en", "srt", "t") is True
    assert route.called


@respx.mock
async def test_plex_upload_subtitle_success_201():
    respx.post(f"{PLEX_URL}/library/metadata/2/subtitles").mock(
        return_value=httpx.Response(201)
    )
    assert await plex.plex_upload_subtitle("2", b"data", "en", "srt", "t") is True


@respx.mock
async def test_plex_upload_subtitle_bad_status_returns_false():
    respx.post(f"{PLEX_URL}/library/metadata/3/subtitles").mock(
        return_value=httpx.Response(500, text="nope")
    )
    assert await plex.plex_upload_subtitle("3", b"data", "en", "srt", "t") is False


@respx.mock
async def test_plex_upload_subtitle_exception_returns_false():
    respx.post(f"{PLEX_URL}/library/metadata/4/subtitles").mock(
        side_effect=httpx.ConnectError("x")
    )
    assert await plex.plex_upload_subtitle("4", b"data", "en", "srt", "t") is False
