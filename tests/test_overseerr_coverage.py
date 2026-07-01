"""Closes the last defensive/branch gaps in app/overseerr.py not covered by
test_overseerr.py or test_overseerr_more.py (kept separate to avoid touching
those files)."""

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import overseerr
from app.config import OVERSEERR_URL, PLEX_TV_USER_URL, PLEX_URL


# ---- _match_user branches ---------------------------------------------------

def test_match_user_non_int_plex_id_no_email_returns_none():
    # int("abc") raises → uid stays None; no email → None.
    assert overseerr._match_user({}, {}, "abc", None) is None


def test_match_user_plex_id_unmatched_falls_through_to_none():
    # plex_id is a valid int but absent from the map, and no email → None.
    assert overseerr._match_user({1: 5}, {}, 999, None) is None


# ---- _import_overseerr_user: skip malformed entries -------------------------

@respx.mock
async def test_import_skips_null_and_non_int_pids():
    overseerr._user_cache.update(expiry=0.0, by_plex_id={}, by_email={})
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(201, json=[
            {"id": 1, "plexId": None},     # pid None → continue
            {"id": 2, "plexId": "xyz"},    # int("xyz") raises → pass
            {"id": 9, "plexId": 4242},     # match
        ])
    )
    assert await overseerr._import_overseerr_user(4242) == 9


# ---- request endpoint branches ---------------------------------------------

def _app():
    app = FastAPI()
    app.include_router(overseerr.router)
    return app


async def _post_request(body):
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post("/overseerr/request", headers={"X-Plex-Token": "tok"}, json=body)


@respx.mock
async def test_request_not_configured_returns_503(monkeypatch):
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    monkeypatch.setattr(overseerr, "OVERSEERR_URL", "")
    monkeypatch.setattr(overseerr, "OVERSEERR_API_KEY", "")
    resp = await _post_request({"tmdb_id": 1, "media_type": "movie"})
    assert resp.status_code == 503


@respx.mock
async def test_request_tv_sends_seasons_and_owner_fallback():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(401))  # identity → None → owner fallback
    route = respx.post(f"{OVERSEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )
    resp = await _post_request({"tmdb_id": 55, "media_type": "show", "seasons": [1, 2]})
    assert resp.status_code == 200
    import json
    sent = json.loads(route.calls.last.request.content)
    assert sent["mediaType"] == "tv"
    assert sent["seasons"] == [1, 2]
    assert "userId" not in sent


@respx.mock
async def test_request_upstream_failure_propagates_status():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(401))
    respx.post(f"{OVERSEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(500, text="boom")
    )
    resp = await _post_request({"tmdb_id": 55, "media_type": "movie"})
    assert resp.status_code == 500


# ---- endpoint exception handlers -------------------------------------------

@respx.mock
async def test_requested_swallows_exception_returns_empty():
    # The media list call raising is caught → empty lists (not a 500).
    respx.get(f"{OVERSEERR_URL}/api/v1/media").mock(side_effect=httpx.ConnectError("down"))
    app = FastAPI()
    app.include_router(overseerr.router)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # requires a valid plex user
        respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
        resp = await ac.get("/overseerr/requested", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json() == {"movie": [], "tv": []}


@respx.mock
async def test_search_exception_returns_500():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{OVERSEERR_URL}/api/v1/search").mock(side_effect=httpx.ConnectError("down"))
    app = FastAPI()
    app.include_router(overseerr.router)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/overseerr/search", params={"query": "dune"},
                            headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 500
