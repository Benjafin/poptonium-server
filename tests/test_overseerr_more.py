"""Tests for the remaining app/overseerr.py surface not covered by
tests/test_overseerr.py (which covers _overseerr_user_id_for + the request
attribution flow).

Here: GET /overseerr/requested (pagination + status filtering), /overseerr/search,
/overseerr/details (movie AND show-with-seasons), /overseerr/status, plus the
_overseerr_user_maps pagination/caching/failure-fallback and _import_overseerr_user
helpers.

Endpoints are mounted on a fresh FastAPI() and driven via ASGITransport; the Plex
auth gate (require_plex_user -> {PLEX_URL}/library/sections) is mocked to 200 so we
reach the handler, and inner httpx calls to Overseerr are mocked with respx.
"""

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import overseerr
from app.config import OVERSEERR_URL, PLEX_URL


def _app():
    app = FastAPI()
    app.include_router(overseerr.router)
    return app


def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")


def _pass_auth():
    """Make the require_plex_user gate accept the caller's token."""
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))


_AUTH_HDR = {"X-Plex-Token": "caller-tok"}


# ---- _overseerr_user_maps ---------------------------------------------------

@respx.mock
async def test_user_maps_paginates_across_pages():
    # take=100, so total must exceed 100 to force a second page (loop breaks when
    # skip >= total). Page 1 returns 100 rows -> skip becomes 100 < 150 -> page 2.
    # Filler rows use a high plexId range that won't collide with 100/200/300.
    page1_results = [{"id": 1000 + i, "plexId": 1000 + i, "email": None} for i in range(98)]
    page1_results.append({"id": 1, "plexId": 100, "email": "A@x.com"})
    page1_results.append({"id": 2, "plexId": 200, "email": "b@x.com"})
    page1 = {"pageInfo": {"results": 150}, "results": page1_results}
    page2 = {
        "pageInfo": {"results": 150},
        "results": [{"id": 3, "plexId": 300, "email": None}],
    }
    route = respx.get(f"{OVERSEERR_URL}/api/v1/user").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    by_plex_id, by_email = await overseerr._overseerr_user_maps()
    # Values from both pages are present (page 2's plexId 300 -> id 3).
    assert by_plex_id[100] == 1
    assert by_plex_id[200] == 2
    assert by_plex_id[300] == 3
    assert by_email == {"a@x.com": 1, "b@x.com": 2}  # emails lowercased; None skipped
    assert route.call_count == 2


@respx.mock
async def test_user_maps_skips_bad_ids_and_missing():
    users = [
        {"plexId": 100, "email": "keep@x.com"},   # no id -> skipped entirely
        {"id": 5, "plexId": "not-int", "email": "e@x.com"},  # bad plexId -> plex map skips, email kept
        {"id": 6},  # no plexId, no email
    ]
    respx.get(f"{OVERSEERR_URL}/api/v1/user").mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": len(users)}, "results": users})
    )
    by_plex_id, by_email = await overseerr._overseerr_user_maps()
    assert by_plex_id == {}
    assert by_email == {"e@x.com": 5}


@respx.mock
async def test_user_maps_caches_within_ttl():
    route = respx.get(f"{OVERSEERR_URL}/api/v1/user").mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": 1}, "results": [{"id": 1, "plexId": 9}]})
    )
    await overseerr._overseerr_user_maps()
    await overseerr._overseerr_user_maps()
    assert route.call_count == 1  # second call served from cache


@respx.mock
async def test_user_maps_failure_falls_back_to_prior_cache():
    # Prime the cache with a good response.
    good = respx.get(f"{OVERSEERR_URL}/api/v1/user").mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": 1}, "results": [{"id": 1, "plexId": 9}]})
    )
    by_plex_id, _ = await overseerr._overseerr_user_maps()
    assert by_plex_id == {9: 1}

    # Expire the cache, then have Overseerr fail -> should fall back to prior maps.
    overseerr._user_cache["expiry"] = 0.0
    good.mock(return_value=httpx.Response(500))
    by_plex_id2, _ = await overseerr._overseerr_user_maps()
    assert by_plex_id2 == {9: 1}  # kept previous cache despite the failure


@respx.mock
async def test_user_maps_network_error_returns_prior_cache():
    overseerr._user_cache["expiry"] = 0.0
    respx.get(f"{OVERSEERR_URL}/api/v1/user").mock(side_effect=httpx.ConnectError("boom"))
    by_plex_id, by_email = await overseerr._overseerr_user_maps()
    # Nothing prior cached -> empty maps, no raise.
    assert by_plex_id == {}
    assert by_email == {}


# ---- _import_overseerr_user -------------------------------------------------

@respx.mock
async def test_import_user_returns_new_id():
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(201, json=[{"id": 55, "plexId": 4242}])
    )
    assert await overseerr._import_overseerr_user(4242) == 55


@respx.mock
async def test_import_user_no_match_returns_none():
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(200, json=[{"id": 55, "plexId": 9999}])
    )
    assert await overseerr._import_overseerr_user(4242) is None


@respx.mock
async def test_import_user_error_status_returns_none():
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(500, text="oops")
    )
    assert await overseerr._import_overseerr_user(4242) is None


@respx.mock
async def test_import_user_network_error_returns_none():
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        side_effect=httpx.ConnectError("boom")
    )
    assert await overseerr._import_overseerr_user(4242) is None


@respx.mock
async def test_import_user_non_list_response_returns_none():
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(201, json={"not": "a list"})
    )
    assert await overseerr._import_overseerr_user(4242) is None


# ---- GET /overseerr/requested -----------------------------------------------

@respx.mock
async def test_requested_paginates_and_filters_by_status():
    _pass_auth()
    # page_size=50; total>50 forces a second page ((page+1)*50 >= total breaks).
    page1 = {
        "pageInfo": {"results": 60},
        "results": [
            {"tmdbId": 603, "mediaType": "movie", "status": 5},   # available -> kept
            {"tmdbId": 604, "mediaType": "movie", "status": 1},   # unknown -> dropped
        ],
    }
    page2 = {
        "pageInfo": {"results": 60},
        "results": [
            {"tmdbId": 1399, "mediaType": "tv", "status": 3},     # processing -> kept
            {"tmdbId": 0, "mediaType": "tv", "status": 5},        # falsy tmdbId -> dropped
        ],
    }
    respx.get(f"{OVERSEERR_URL}/api/v1/media").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    async with _client() as ac:
        resp = await ac.get("/overseerr/requested", headers=_AUTH_HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["movie"] == [603]
    assert body["tv"] == [1399]


@respx.mock
async def test_requested_stops_on_empty_results():
    _pass_auth()
    respx.get(f"{OVERSEERR_URL}/api/v1/media").mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": 100}, "results": []})
    )
    async with _client() as ac:
        resp = await ac.get("/overseerr/requested", headers=_AUTH_HDR)
    assert resp.json() == {"movie": [], "tv": []}


@respx.mock
async def test_requested_handles_media_list_error():
    _pass_auth()
    respx.get(f"{OVERSEERR_URL}/api/v1/media").mock(return_value=httpx.Response(500))
    async with _client() as ac:
        resp = await ac.get("/overseerr/requested", headers=_AUTH_HDR)
    assert resp.json() == {"movie": [], "tv": []}


async def test_requested_empty_when_not_configured(monkeypatch):
    _pass_auth_needed = None  # noqa: F841 (documentation only)
    monkeypatch.setattr(overseerr, "OVERSEERR_URL", "")

    @respx.mock
    async def _run():
        respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
        async with _client() as ac:
            return await ac.get("/overseerr/requested", headers=_AUTH_HDR)

    resp = await _run()
    assert resp.json() == {"movie": [], "tv": []}


@respx.mock
async def test_requested_rejects_bad_token():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    async with _client() as ac:
        resp = await ac.get("/overseerr/requested", headers={"X-Plex-Token": "bad"})
    assert resp.status_code == 401


# ---- GET /overseerr/search --------------------------------------------------

@respx.mock
async def test_search_maps_results():
    _pass_auth()
    search_resp = {
        "results": [
            {
                "id": 603, "mediaType": "movie", "title": "The Matrix",
                "posterPath": "/matrix.jpg", "overview": "Neo.",
                "releaseDate": "1999-03-31",
                "mediaInfo": {"status": 5},
            },
            {
                "id": 1399, "mediaType": "tv", "name": "Game of Thrones",
                "posterPath": None, "overview": "GoT.",
                "firstAirDate": "2011-04-17",
            },
            {"id": 5, "mediaType": "person", "name": "Someone"},  # non-media -> skipped
            {"id": 7, "mediaType": "movie", "title": "NoDate", "releaseDate": ""},  # short date -> year None
        ]
    }
    respx.get(url__startswith=f"{OVERSEERR_URL}/api/v1/search").mock(
        return_value=httpx.Response(200, json=search_resp)
    )
    async with _client() as ac:
        resp = await ac.get("/overseerr/search", params={"query": "matrix"}, headers=_AUTH_HDR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    m = body["items"][0]
    assert m["tmdb_id"] == 603
    assert m["media_type"] == "movie"
    assert m["year"] == 1999
    assert m["poster_url"] == "https://image.tmdb.org/t/p/w500/matrix.jpg"
    assert m["media_status"] == 5
    tv = body["items"][1]
    assert tv["title"] == "Game of Thrones"
    assert tv["media_type"] == "show"
    assert tv["poster_url"] is None
    assert tv["year"] == 2011
    assert tv["media_status"] == 0  # no mediaInfo -> default 0
    assert body["items"][2]["year"] is None


@respx.mock
async def test_search_upstream_error_propagates_status():
    _pass_auth()
    respx.get(url__startswith=f"{OVERSEERR_URL}/api/v1/search").mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    async with _client() as ac:
        resp = await ac.get("/overseerr/search", params={"query": "x"}, headers=_AUTH_HDR)
    assert resp.status_code == 502


async def test_search_503_when_not_configured(monkeypatch):
    monkeypatch.setattr(overseerr, "OVERSEERR_URL", "")

    @respx.mock
    async def _run():
        respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
        async with _client() as ac:
            return await ac.get("/overseerr/search", params={"query": "x"}, headers=_AUTH_HDR)

    resp = await _run()
    assert resp.status_code == 503


# ---- GET /overseerr/details -------------------------------------------------

@respx.mock
async def test_details_movie():
    _pass_auth()
    movie = {
        "title": "The Matrix",
        "overview": "Neo.",
        "tagline": "Free your mind.",
        "runtime": 136,
        "releaseDate": "1999-03-31",
        "backdropPath": "/bd.jpg",
        "posterPath": "/p.jpg",
        "genres": [{"name": "Action"}, {"name": None}, {"id": 5}],
        "credits": {"cast": [{"name": "Keanu", "character": "Neo", "profilePath": "/k.jpg"}]},
        "mediaInfo": {"status": 5},
    }
    respx.get(f"{OVERSEERR_URL}/api/v1/movie/603").mock(return_value=httpx.Response(200, json=movie))
    async with _client() as ac:
        resp = await ac.get(
            "/overseerr/details", params={"tmdb_id": 603, "media_type": "movie"}, headers=_AUTH_HDR
        )
    assert resp.status_code == 200
    d = resp.json()
    assert d["title"] == "The Matrix"
    assert d["runtime"] == 136
    assert d["year"] == 1999
    assert d["genres"] == ["Action"]  # None/missing names filtered
    assert d["backdrop_url"] == "https://image.tmdb.org/t/p/w1280/bd.jpg"
    assert d["poster_url"] == "https://image.tmdb.org/t/p/w500/p.jpg"
    assert d["cast"][0] == {
        "name": "Keanu", "character": "Neo",
        "profile_url": "https://image.tmdb.org/t/p/w185/k.jpg",
    }
    assert d["media_status"] == 5
    assert d["seasons"] is None  # movies have no seasons


@respx.mock
async def test_details_show_with_seasons():
    _pass_auth()
    show = {
        "name": "Game of Thrones",
        "firstAirDate": "2011-04-17",
        "episodeRunTime": [55],  # no top-level runtime -> use first episode runtime
        "numberOfSeasons": 8,
        "seasons": [
            {"seasonNumber": 0, "name": "Specials", "episodeCount": 2},  # season 0 skipped
            {"seasonNumber": 2, "name": "Season 2", "episodeCount": 10},
            {"seasonNumber": 1, "name": "Season 1", "episodeCount": 10},
        ],
        "mediaInfo": {"seasons": [{"seasonNumber": 1, "status": 5}]},
    }
    respx.get(f"{OVERSEERR_URL}/api/v1/tv/1399").mock(return_value=httpx.Response(200, json=show))
    async with _client() as ac:
        resp = await ac.get(
            "/overseerr/details", params={"tmdb_id": 1399, "media_type": "show"}, headers=_AUTH_HDR
        )
    assert resp.status_code == 200
    d = resp.json()
    assert d["media_type"] == "show"
    assert d["runtime"] == 55  # from episodeRunTime[0]
    assert d["number_of_seasons"] == 8
    # Specials (season 0) dropped; remaining sorted ascending.
    nums = [s["season_number"] for s in d["seasons"]]
    assert nums == [1, 2]
    s1 = d["seasons"][0]
    assert s1["status"] == 5  # from mediaInfo season status
    s2 = d["seasons"][1]
    assert s2["status"] == 1  # default when not in mediaInfo


@respx.mock
async def test_details_upstream_error_propagates():
    _pass_auth()
    respx.get(f"{OVERSEERR_URL}/api/v1/movie/1").mock(return_value=httpx.Response(404))
    async with _client() as ac:
        resp = await ac.get(
            "/overseerr/details", params={"tmdb_id": 1, "media_type": "movie"}, headers=_AUTH_HDR
        )
    assert resp.status_code == 404


@respx.mock
async def test_details_network_error_500():
    _pass_auth()
    respx.get(f"{OVERSEERR_URL}/api/v1/movie/1").mock(side_effect=httpx.ConnectError("boom"))
    async with _client() as ac:
        resp = await ac.get(
            "/overseerr/details", params={"tmdb_id": 1, "media_type": "movie"}, headers=_AUTH_HDR
        )
    assert resp.status_code == 500


async def test_details_503_when_not_configured(monkeypatch):
    monkeypatch.setattr(overseerr, "OVERSEERR_URL", "")

    @respx.mock
    async def _run():
        respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
        async with _client() as ac:
            return await ac.get(
                "/overseerr/details", params={"tmdb_id": 1, "media_type": "movie"}, headers=_AUTH_HDR
            )

    resp = await _run()
    assert resp.status_code == 503


async def test_details_rejects_bad_media_type():
    @respx.mock
    async def _run():
        respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
        async with _client() as ac:
            return await ac.get(
                "/overseerr/details", params={"tmdb_id": 1, "media_type": "bogus"}, headers=_AUTH_HDR
            )

    resp = await _run()
    assert resp.status_code == 422  # pattern validation failure


# ---- GET /overseerr/status (public, no auth gate) ---------------------------

async def test_status_configured():
    async with _client() as ac:
        resp = await ac.get("/overseerr/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": True}


async def test_status_not_configured(monkeypatch):
    monkeypatch.setattr(overseerr, "OVERSEERR_URL", "")
    async with _client() as ac:
        resp = await ac.get("/overseerr/status")
    assert resp.json() == {"configured": False}
