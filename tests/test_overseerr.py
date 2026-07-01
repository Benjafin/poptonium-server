"""Tests for Overseerr request attribution.

Covers the feature where a request is attributed to the actual requesting Plex
user (matched to / imported into Overseerr) rather than the API-key owner.
"""

import json

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import overseerr
from app.config import OVERSEERR_URL, PLEX_TV_USER_URL, PLEX_URL


def _mock_identity(plex_id, email="user@example.com"):
    respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, json={"id": plex_id, "email": email})
    )


def _mock_user_list(users):
    respx.get(f"{OVERSEERR_URL}/api/v1/user").mock(
        return_value=httpx.Response(200, json={"pageInfo": {"results": len(users)}, "results": users})
    )


# ---- unit-level: _overseerr_user_id_for -------------------------------------

@respx.mock
async def test_matches_existing_user_by_plex_id():
    _mock_identity(4242, "ada@example.com")
    _mock_user_list([{"id": 7, "plexId": 4242, "email": "ada@example.com"}])
    assert await overseerr._overseerr_user_id_for("caller-tok") == 7


@respx.mock
async def test_matches_existing_user_by_email_when_plexid_differs():
    # plexId not in Overseerr, but the email matches an existing account.
    _mock_identity(9999, "ada@example.com")
    _mock_user_list([{"id": 12, "plexId": 1, "email": "ada@example.com"}])
    assert await overseerr._overseerr_user_id_for("caller-tok") == 12


@respx.mock
async def test_imports_user_when_absent_then_returns_new_id():
    _mock_identity(4242, "new@example.com")
    _mock_user_list([])  # caller not in Overseerr yet
    import_route = respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(201, json=[{"id": 21, "plexId": 4242, "email": "new@example.com"}])
    )
    assert await overseerr._overseerr_user_id_for("caller-tok") == 21
    assert import_route.called
    sent = json.loads(import_route.calls.last.request.content)
    assert sent == {"plexIds": ["4242"]}


@respx.mock
async def test_returns_none_when_not_matchable_and_import_empty():
    # User isn't shared to the Plex server -> import creates nobody.
    _mock_identity(4242, "ghost@example.com")
    _mock_user_list([])
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(201, json=[])
    )
    assert await overseerr._overseerr_user_id_for("caller-tok") is None


@respx.mock
async def test_returns_none_when_identity_unresolvable():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(401))
    assert await overseerr._overseerr_user_id_for("caller-tok") is None


# ---- endpoint-level: POST /overseerr/request --------------------------------

def _app():
    app = FastAPI()
    app.include_router(overseerr.router)
    return app


@respx.mock
async def test_request_endpoint_attaches_userid():
    # Auth gate: the caller's token is accepted by Plex.
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    _mock_identity(4242, "ada@example.com")
    _mock_user_list([{"id": 7, "plexId": 4242, "email": "ada@example.com"}])
    request_route = respx.post(f"{OVERSEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/overseerr/request",
            headers={"X-Plex-Token": "caller-tok"},
            json={"tmdb_id": 603, "media_type": "movie"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "requested"
    body = json.loads(request_route.calls.last.request.content)
    assert body["userId"] == 7
    assert body["mediaId"] == 603
    assert body["mediaType"] == "movie"


@respx.mock
async def test_request_endpoint_rejects_bad_token():
    # Plex rejects the token -> the endpoint 401s and never calls Overseerr.
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))

    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/overseerr/request",
            headers={"X-Plex-Token": "bad-tok"},
            json={"tmdb_id": 603, "media_type": "movie"},
        )

    assert resp.status_code == 401
    # Nothing was ever sent to Overseerr.
    assert not any("/api/v1/request" in str(c.request.url) for c in respx.calls)


@respx.mock
async def test_request_endpoint_falls_back_to_owner_when_unmatched():
    # Caller authenticates but can't be matched/imported: request still succeeds,
    # just with no userId (Overseerr attributes it to the API-key owner).
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    _mock_identity(4242, "ghost@example.com")
    _mock_user_list([])
    respx.post(f"{OVERSEERR_URL}/api/v1/user/import-from-plex").mock(
        return_value=httpx.Response(201, json=[])
    )
    request_route = respx.post(f"{OVERSEERR_URL}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/overseerr/request",
            headers={"X-Plex-Token": "caller-tok"},
            json={"tmdb_id": 603, "media_type": "movie"},
        )

    assert resp.status_code == 200
    body = json.loads(request_route.calls.last.request.content)
    assert "userId" not in body
