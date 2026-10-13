"""Tests for app/share.py: readable share slugs, the public preview page and its artwork.

Plex is mocked with respx. The caller's own token decides whether they may share an
item (their library access); the page and artwork are fetched with the admin token.
"""

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
from app import plex, share
from app.config import settings

PLEX = "http://plex.test"

MOVIE = {"ratingKey": "101", "type": "movie", "title": "Amélie", "year": 2001,
         "summary": "A shy waitress <decides> to change the lives of those around her.",
         "duration": 7_320_000, "Genre": [{"tag": "Comedy"}, {"tag": "Romance"}],
         "thumb": "/library/metadata/101/thumb/1", "art": "/library/metadata/101/art/1"}
SHOW = {"ratingKey": "200", "type": "show", "title": "Breaking Bad", "year": 2008, "childCount": 5,
        "thumb": "/library/metadata/200/thumb/1", "art": "/library/metadata/200/art/1"}
EPISODE = {"ratingKey": "203", "type": "episode", "title": "...And the Bag's in the River",
           "grandparentTitle": "Breaking Bad", "parentIndex": 1, "index": 3,
           "thumb": "/library/metadata/203/thumb/1", "grandparentThumb": "/library/metadata/200/thumb/1"}


def _client():
    app = FastAPI()
    app.include_router(share.router)
    app.include_router(share.router, prefix="/poptonium")
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _db_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "share.db"))
    plex._plex_cache.clear()
    yield


def _mock_items(*items, readable_by=("user-token",)):
    """Plex /library/metadata/{rk}: 200 for the admin token and any token in
    `readable_by`, 401 for others; 404 for unknown keys."""
    by_key = {m["ratingKey"]: m for m in items}

    def _responder(request):
        rk = request.url.path.rsplit("/", 1)[-1]
        tok = request.headers.get("X-Plex-Token")
        if tok != settings.PLEX_TOKEN and tok not in readable_by:
            return httpx.Response(401)
        if rk not in by_key:
            return httpx.Response(404)
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [by_key[rk]]}})

    respx.get(url__regex=rf"{PLEX}/library/metadata/\d+$").mock(side_effect=_responder)
    # validate_plex_token probes /library/sections.
    respx.get(f"{PLEX}/library/sections").mock(
        side_effect=lambda r: httpx.Response(
            200 if r.headers.get("X-Plex-Token") in (*readable_by, "other-token") else 401, json={}))


async def _share(c, rk, token="user-token", **kw):
    return await c.post("/share", json={"rating_key": rk}, headers={"X-Plex-Token": token}, **kw)


# ---- slugs -------------------------------------------------------------------

def test_slugs():
    assert share.base_slug(MOVIE) == "amelie-2001"
    assert share.base_slug(SHOW) == "breaking-bad"
    assert share.base_slug(EPISODE) == "breaking-bad-s01e03"
    assert share.base_slug({"ratingKey": "9", "type": "movie", "title": "千と千尋"}) == "movie-9"


# ---- create ------------------------------------------------------------------

@respx.mock
async def test_create_returns_readable_url_and_reuses_slug():
    _mock_items(MOVIE)
    async with _client() as c:
        r = await _share(c, "101")
        assert r.status_code == 200
        assert r.json()["url"] == "http://test/s/amelie-2001"
        assert r.json()["title"] == "Amélie (2001)"
        again = await _share(c, "101")
        assert again.json()["slug"] == "amelie-2001"


@respx.mock
async def test_slug_collision_gets_suffix():
    other = {**MOVIE, "ratingKey": "102"}
    _mock_items(MOVIE, other)
    async with _client() as c:
        assert (await _share(c, "101")).json()["slug"] == "amelie-2001"
        assert (await _share(c, "102")).json()["slug"] == "amelie-2001-2"


@respx.mock
async def test_public_url_setting_and_proxy_prefix(monkeypatch):
    _mock_items(SHOW)
    async with _client() as c:
        r = await c.post("/poptonium/share", json={"rating_key": "200"},
                         headers={"X-Plex-Token": "user-token", "X-Forwarded-Proto": "https",
                                  "X-Forwarded-Host": "plex.example.com"})
        assert r.json()["url"] == "https://plex.example.com/poptonium/s/breaking-bad"
        monkeypatch.setitem(settings._values, "PUBLIC_URL", "https://share.example.com/pop/")
        r = await _share(c, "200")
        assert r.json()["url"] == "https://share.example.com/pop/s/breaking-bad"


@respx.mock
async def test_requires_token_and_item_access():
    _mock_items(MOVIE)
    async with _client() as c:
        assert (await c.post("/share", json={"rating_key": "101"})).status_code == 401
        # A valid server user whose libraries don't include the item.
        assert (await _share(c, "101", token="other-token")).status_code == 404
        assert (await _share(c, "999")).status_code == 404
        assert (await _share(c, "abc")).status_code == 400


# ---- page --------------------------------------------------------------------

@respx.mock
async def test_page_has_open_graph_tags_and_escapes():
    _mock_items(MOVIE)
    async with _client() as c:
        await _share(c, "101")
        r = await c.get("/s/amelie-2001")
    assert r.status_code == 200
    body = r.text
    assert '<meta property="og:title" content="Amélie (2001)">' in body
    assert '<meta property="og:type" content="video.movie">' in body
    assert '<meta property="og:image" content="http://test/s/amelie-2001/image.jpg">' in body
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert "Movie · 2001 · Comedy · Romance · 2h 2m" in body
    assert "&lt;decides&gt;" in body and "<decides>" not in body
    assert 'href="poptonium://item/101"' in body


@respx.mock
async def test_episode_page_title():
    _mock_items(EPISODE)
    async with _client() as c:
        await _share(c, "203")
        r = await c.get("/s/breaking-bad-s01e03")
    assert "Breaking Bad · S1 E3 · ...And the Bag&#x27;s in the River" in r.text
    assert 'content="video.episode"' in r.text


@respx.mock
async def test_unknown_slug_is_404_page():
    _mock_items()
    async with _client() as c:
        r = await c.get("/s/nope")
    assert r.status_code == 404
    assert "<html" in r.text


# ---- artwork -----------------------------------------------------------------

@respx.mock
async def test_image_proxies_plex_transcode_with_admin_token():
    _mock_items(EPISODE)
    route = respx.get(url__startswith=f"{PLEX}/photo/:/transcode").mock(
        return_value=httpx.Response(200, content=b"jpeg", headers={"content-type": "image/jpeg"}))
    async with _client() as c:
        await _share(c, "203")
        card = await c.get("/s/breaking-bad-s01e03/image.jpg")
        poster = await c.get("/s/breaking-bad-s01e03/poster.jpg")
        bad = await c.get("/s/breaking-bad-s01e03/other.jpg")
    assert card.status_code == 200 and card.content == b"jpeg"
    assert "max-age" in card.headers["cache-control"]
    first, second = route.calls[0].request, route.calls[1].request
    assert first.headers["X-Plex-Token"] == settings.PLEX_TOKEN
    assert first.url.params["url"] == "/library/metadata/203/thumb/1"
    assert first.url.params["width"] == "1200"
    assert second.url.params["url"] == "/library/metadata/200/thumb/1"
    assert poster.status_code == 200
    assert bad.status_code == 404


# ---- open-in-app fallback ----------------------------------------------------

@respx.mock
async def test_page_falls_back_to_plex_when_app_missing():
    _mock_items(MOVIE)
    respx.get(f"{PLEX}/identity").mock(return_value=httpx.Response(
        200, json={"MediaContainer": {"machineIdentifier": "abc123"}}))
    async with _client() as c:
        await _share(c, "101")
        body = (await c.get("/s/amelie-2001")).text
    plex_url = "https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F101"
    # Visible Open in Plex button, and the scripted fallback for Open in Poptonium.
    assert f'href="{plex_url}">Open in Plex</a>' in body
    assert f'fallbackUrl = "{plex_url}"' in body
    # Android: Chrome intent opens the app if installed, else the Plex URL.
    assert ("intent://item/101#Intent;scheme=poptonium;package=games.benja.poptonium;"
            "S.browser_fallback_url=https%3A%2F%2Fapp.plex.tv%2Fdesktop%2F%23%21%2Fserver%2Fabc123") in body


@respx.mock
async def test_page_without_machine_id_has_no_plex_fallback():
    _mock_items(MOVIE)
    respx.get(f"{PLEX}/identity").mock(return_value=httpx.Response(500))
    async with _client() as c:
        await _share(c, "101")
        body = (await c.get("/s/amelie-2001")).text
    assert "Open in Plex" not in body
    assert "fallbackUrl = null" in body
    # Android then falls back to the Play Store listing.
    assert "S.browser_fallback_url=https%3A%2F%2Fplay.google.com" in body


def test_js_literal_cannot_close_script():
    assert share._js("</script><b>") == '"\\u003c/script\\u003e\\u003cb\\u003e"'


# ---- universal / app links -----------------------------------------------------

async def test_apple_app_site_association(monkeypatch):
    monkeypatch.setitem(settings._values, "PUBLIC_URL", "https://x.example/media/pop")
    async with _client() as c:
        r = await c.get("/.well-known/apple-app-site-association")
    assert r.headers["content-type"] == "application/json"
    detail = r.json()["applinks"]["details"][0]
    assert detail["appIDs"] == ["RGYYV9YYGV.games.benja.poptonium"]
    paths = [(comp["/"], comp.get("exclude", False)) for comp in detail["components"]]
    assert ("/poptonium/s/*.jpg", True) in paths and ("/poptonium/s/*", False) in paths
    assert ("/media/pop/s/*", False) in paths
    # Each prefix's exclusion comes before its catch-all (first match wins).
    assert paths.index(("/poptonium/s/*.jpg", True)) < paths.index(("/poptonium/s/*", False))


async def test_asset_links_include_configured_play_key(monkeypatch):
    monkeypatch.setitem(settings._values, "ANDROID_CERT_SHA256", " aa:bb , " + share.ANDROID_CERT_SHA256[0])
    async with _client() as c:
        r = await c.get("/.well-known/assetlinks.json")
    target = r.json()[0]["target"]
    assert target["package_name"] == "games.benja.poptonium"
    assert target["sha256_cert_fingerprints"] == [*share.ANDROID_CERT_SHA256, "AA:BB"]


@respx.mock
async def test_share_lookup_for_app_links():
    _mock_items(EPISODE)
    async with _client() as c:
        await _share(c, "203")
        r = await c.get("/poptonium/share/breaking-bad-s01e03")
        missing = await c.get("/share/nope")
    assert r.json() == {"slug": "breaking-bad-s01e03", "rating_key": "203", "type": "episode",
                        "title": "Breaking Bad · S1 E3 · ...And the Bag's in the River"}
    assert missing.status_code == 404
