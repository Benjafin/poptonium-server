"""Tests for the transparent Plex reverse-proxy (``app/plex_proxy.py``).

Mounts only the proxy router on a fresh app and drives it via ASGITransport so
the inner httpx calls to Plex ARE intercepted by respx. Covers the auth gate,
request/header/path forwarding, JSON rating enrichment, passthrough for
non-JSON, upstream errors, and the "Plex not configured" branch.
"""

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import plex_proxy
from app.config import PLEX_URL


def _app():
    app = FastAPI()
    app.include_router(plex_proxy.router)
    return app


async def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")


def _mock_auth_gate():
    """The proxy is gated by require_plex_user, which validates the caller's token
    against GET /library/sections. Accept any token this test presents."""
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))


# ---- auth gate --------------------------------------------------------------

@respx.mock
async def test_rejects_missing_token():
    # No token → require_plex_user 401s before any upstream forward.
    async with await _client() as ac:
        resp = await ac.get("/plex/status/sessions")
    assert resp.status_code == 401


@respx.mock
async def test_rejects_bad_token():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    async with await _client() as ac:
        resp = await ac.get("/plex/status/sessions", headers={"X-Plex-Token": "bad"})
    assert resp.status_code == 401


async def test_503_when_plex_unconfigured(monkeypatch):
    # require_plex_user itself 503s when Plex isn't configured (fails closed).
    monkeypatch.setattr("app.client_auth.plex_configured", lambda: False)
    async with await _client() as ac:
        resp = await ac.get("/plex/status/sessions", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 503


@respx.mock
async def test_503_when_proxy_sees_unconfigured(monkeypatch):
    # Auth gate passes (token validated against Plex), but the handler's own
    # plex_configured() gate trips → 503.
    _mock_auth_gate()
    monkeypatch.setattr(plex_proxy, "plex_configured", lambda: False)
    async with await _client() as ac:
        resp = await ac.get("/plex/status/sessions", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 503


# ---- forwarding -------------------------------------------------------------

@respx.mock
async def test_proxies_get_passthrough_non_json():
    _mock_auth_gate()
    up = respx.get(f"{PLEX_URL}/photo/thumb.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8imgbytes",
                                    headers={"content-type": "image/jpeg"})
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/photo/thumb.jpg", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8imgbytes"
    assert resp.headers["content-type"] == "image/jpeg"
    assert up.called


@respx.mock
async def test_forwards_path_and_client_headers():
    _mock_auth_gate()
    route = respx.get(f"{PLEX_URL}/status/sessions").mock(
        return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    )
    async with await _client() as ac:
        await ac.get("/plex/status/sessions",
                     headers={"X-Plex-Token": "tok", "X-Custom": "hello"})
    req = route.calls.last.request
    # Path forwarded verbatim, and the client's custom header carried through.
    assert req.url.path == "/status/sessions"
    assert req.headers.get("X-Custom") == "hello"
    # Hop-by-hop request headers must be dropped.
    assert "content-length" not in {k.lower() for k in req.headers}


@respx.mock
async def test_injects_includeguids_for_library_get():
    _mock_auth_gate()
    route = respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    )
    async with await _client() as ac:
        await ac.get("/plex/library/sections/1/all", headers={"X-Plex-Token": "tok"})
    assert route.calls.last.request.url.params.get("includeGuids") == "1"


@respx.mock
async def test_no_includeguids_for_non_library_get():
    _mock_auth_gate()
    route = respx.get(f"{PLEX_URL}/status/sessions").mock(
        return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    )
    async with await _client() as ac:
        await ac.get("/plex/status/sessions", headers={"X-Plex-Token": "tok"})
    assert "includeGuids" not in route.calls.last.request.url.params


@respx.mock
async def test_forwards_post_body():
    _mock_auth_gate()
    route = respx.post(f"{PLEX_URL}/playQueues").mock(
        return_value=httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    )
    async with await _client() as ac:
        await ac.post("/plex/playQueues", headers={"X-Plex-Token": "tok"}, content=b"payload")
    assert route.calls.last.request.content == b"payload"


# ---- upstream errors --------------------------------------------------------

@respx.mock
async def test_upstream_http_error_becomes_502():
    _mock_auth_gate()
    respx.get(f"{PLEX_URL}/status/sessions").mock(side_effect=httpx.ConnectError("down"))
    async with await _client() as ac:
        resp = await ac.get("/plex/status/sessions", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 502


@respx.mock
async def test_upstream_non_200_status_passed_through():
    _mock_auth_gate()
    respx.get(f"{PLEX_URL}/library/metadata/999").mock(
        return_value=httpx.Response(404, content=b"nope", headers={"content-type": "text/plain"})
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/metadata/999", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 404


# ---- JSON rating enrichment -------------------------------------------------

def _stub_ratings(monkeypatch, cfg=None, cache=None):
    async def _cfg():
        return cfg if cfg is not None else {"formula": {"missing_mdblist": "zero"}}

    async def _for(pairs):
        return cache or {}

    monkeypatch.setattr(plex_proxy, "get_rating_config", _cfg)
    monkeypatch.setattr(plex_proxy, "ratings_for_tmdb", _for)


@respx.mock
async def test_enriches_json_with_ratings(monkeypatch):
    _mock_auth_gate()
    cache = {
        (603, "movie"): {"sources": {"imdb": {"score": 8.5, "votes": 100},
                                     "mdblist": {"score": 80, "votes": None}}},
    }
    _stub_ratings(monkeypatch, cfg={"formula": {"missing_mdblist": "zero", "preset": "mdblist"}},
                  cache=cache)
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [
                {"type": "movie", "title": "M", "Guid": [{"id": "tmdb://603"}]},
            ]},
        }, headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/sections/1/all", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    meta = resp.json()["MediaContainer"]["Metadata"][0]
    assert meta["mdblistRating"] == 80.0
    assert "mdblistSources" in meta


@respx.mock
async def test_enrich_noop_when_no_metadata(monkeypatch):
    _mock_auth_gate()
    _stub_ratings(monkeypatch)
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"size": 0}},
                                    headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/sections", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json() == {"MediaContainer": {"size": 0}}


@respx.mock
async def test_enrich_skips_items_without_tmdb(monkeypatch):
    _mock_auth_gate()
    # No pairs (no tmdb guids) → ratings_for_tmdb never consulted; item unchanged.
    _stub_ratings(monkeypatch, cache={(1, "movie"): {"sources": {}}})
    respx.get(f"{PLEX_URL}/hubs/home").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [{"type": "movie", "title": "no-guid"}]},
        }, headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/hubs/home", headers={"X-Plex-Token": "tok"})
    meta = resp.json()["MediaContainer"]["Metadata"][0]
    assert "mdblistRating" not in meta


@respx.mock
async def test_enrich_skips_when_no_matching_cache_row(monkeypatch):
    _mock_auth_gate()
    _stub_ratings(monkeypatch, cache={})  # tmdb present but no cached rating
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [
                {"type": "movie", "title": "M", "Guid": [{"id": "tmdb://999"}]},
            ]},
        }, headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/sections/1/all", headers={"X-Plex-Token": "tok"})
    meta = resp.json()["MediaContainer"]["Metadata"][0]
    assert "mdblistRating" not in meta


@respx.mock
async def test_enrich_skips_untagged_item_in_mixed_list(monkeypatch):
    _mock_auth_gate()
    # First item has a tmdb (so pairs is non-empty and the enrich loop runs),
    # the second has none → it's skipped in the second pass.
    cache = {(603, "movie"): {"sources": {"mdblist": {"score": 80, "votes": None}}}}
    _stub_ratings(monkeypatch, cfg={"formula": {"missing_mdblist": "zero", "preset": "mdblist"}},
                  cache=cache)
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [
                {"type": "movie", "title": "tagged", "Guid": [{"id": "tmdb://603"}]},
                {"type": "movie", "title": "untagged"},
            ]},
        }, headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/sections/1/all", headers={"X-Plex-Token": "tok"})
    metas = resp.json()["MediaContainer"]["Metadata"]
    assert metas[0]["mdblistRating"] == 80.0
    assert "mdblistRating" not in metas[1]


@respx.mock
async def test_enrich_skips_when_effective_sources_empty(monkeypatch):
    _mock_auth_gate()
    # Row exists but yields no effective sources → item left unmodified.
    cache = {(603, "movie"): {"sources": {}}}
    _stub_ratings(monkeypatch, cfg={"formula": {"missing_mdblist": "zero"}}, cache=cache)
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [
                {"type": "movie", "title": "M", "Guid": [{"id": "tmdb://603"}]},
            ]},
        }, headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/sections/1/all", headers={"X-Plex-Token": "tok"})
    meta = resp.json()["MediaContainer"]["Metadata"][0]
    assert "mdblistRating" not in meta
    assert "mdblistSources" not in meta


@respx.mock
async def test_malformed_json_falls_through_to_passthrough(monkeypatch):
    _mock_auth_gate()
    _stub_ratings(monkeypatch)
    # content-type says json but the body is invalid → parse fails → passthrough.
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, content=b"not json",
                                    headers={"content-type": "application/json"}),
    )
    async with await _client() as ac:
        resp = await ac.get("/plex/library/sections", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    assert resp.content == b"not json"
