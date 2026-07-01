"""Tests for app/subtitle_prefs.py — per-series subtitle language preference,
scoped to the Plex account that owns the caller's token.

The owning account id is resolved SERVER-SIDE from the presented Plex token via
``require_plex_account`` (which calls plex.tv), never from the request body. So
each test mocks ``GET {PLEX_TV_USER_URL}`` with respx to hand back an account id
for a given token, then drives the router over ASGI. The DB is a fresh temp
SQLite file per test for isolation.
"""

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
from app import subtitle_prefs
from app.config import PLEX_TV_USER_URL


def _app():
    app = FastAPI()
    app.include_router(subtitle_prefs.router)
    return app


def _client(app):
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _mock_account(token_to_uuid: dict[str, str]):
    """Map presented X-Plex-Token -> resolved account uuid via plex.tv.

    plex.tv is called with the token in the X-Plex-Token header; dispatch on it
    so different tokens resolve to different accounts (for the scoping tests).
    """
    def _responder(request):
        tok = request.headers.get("X-Plex-Token")
        uuid = token_to_uuid.get(tok)
        if uuid is None:
            return httpx.Response(401)
        return httpx.Response(200, json={"uuid": uuid, "id": 1})

    respx.get(PLEX_TV_USER_URL).mock(side_effect=_responder)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "subs.db"))
    return _app()


# ---- set + get roundtrip ----------------------------------------------------

@respx.mock
async def test_set_then_get(app):
    _mock_account({"tok-a": "user-a"})
    headers = {"X-Plex-Token": "tok-a"}
    async with _client(app) as ac:
        put = await ac.put("/subtitle-prefs", json={"series_key": "s1", "language": "en"}, headers=headers)
        get = await ac.get("/subtitle-prefs", headers=headers)
    assert put.status_code == 200
    assert put.json() == {"ok": True}
    assert get.status_code == 200
    assert get.json() == {"prefs": {"s1": "en"}}


@respx.mock
async def test_get_empty_when_no_prefs(app):
    _mock_account({"tok-a": "user-a"})
    async with _client(app) as ac:
        get = await ac.get("/subtitle-prefs", headers={"X-Plex-Token": "tok-a"})
    assert get.status_code == 200
    assert get.json() == {"prefs": {}}


# ---- override (INSERT OR REPLACE) -------------------------------------------

@respx.mock
async def test_override_replaces_existing(app):
    _mock_account({"tok-a": "user-a"})
    headers = {"X-Plex-Token": "tok-a"}
    async with _client(app) as ac:
        await ac.put("/subtitle-prefs", json={"series_key": "s1", "language": "en"}, headers=headers)
        await ac.put("/subtitle-prefs", json={"series_key": "s1", "language": "fr"}, headers=headers)
        get = await ac.get("/subtitle-prefs", headers=headers)
    # Same (user, series) key -> replaced, not duplicated.
    assert get.json() == {"prefs": {"s1": "fr"}}


# ---- "subtitles off" (NULL language) vs. no row -----------------------------

@respx.mock
async def test_subtitles_off_stored_as_null(app):
    _mock_account({"tok-a": "user-a"})
    headers = {"X-Plex-Token": "tok-a"}
    async with _client(app) as ac:
        # Explicit "off": language omitted -> defaults to None -> stored NULL.
        await ac.put("/subtitle-prefs", json={"series_key": "s1"}, headers=headers)
        get = await ac.get("/subtitle-prefs", headers=headers)
    prefs = get.json()["prefs"]
    # The key is PRESENT (a real preference) but its value is null (off).
    assert "s1" in prefs
    assert prefs["s1"] is None


@respx.mock
async def test_no_row_vs_off_are_distinct(app):
    _mock_account({"tok-a": "user-a"})
    headers = {"X-Plex-Token": "tok-a"}
    async with _client(app) as ac:
        await ac.put("/subtitle-prefs", json={"series_key": "off-show", "language": None}, headers=headers)
        get = await ac.get("/subtitle-prefs", headers=headers)
    prefs = get.json()["prefs"]
    # "off-show" is explicitly off (present, null); "other-show" absent = no pref.
    assert prefs == {"off-show": None}
    assert "other-show" not in prefs


# ---- delete -----------------------------------------------------------------

@respx.mock
async def test_delete_removes_pref(app):
    _mock_account({"tok-a": "user-a"})
    headers = {"X-Plex-Token": "tok-a"}
    async with _client(app) as ac:
        await ac.put("/subtitle-prefs", json={"series_key": "s1", "language": "en"}, headers=headers)
        resp = await ac.delete("/subtitle-prefs", params={"series_key": "s1"}, headers=headers)
        get = await ac.get("/subtitle-prefs", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert get.json() == {"prefs": {}}


@respx.mock
async def test_delete_missing_series_key_is_422(app):
    _mock_account({"tok-a": "user-a"})
    async with _client(app) as ac:
        # series_key is a required query param.
        resp = await ac.delete("/subtitle-prefs", headers={"X-Plex-Token": "tok-a"})
    assert resp.status_code == 422


@respx.mock
async def test_delete_nonexistent_is_ok(app):
    _mock_account({"tok-a": "user-a"})
    async with _client(app) as ac:
        resp = await ac.delete("/subtitle-prefs", params={"series_key": "ghost"},
                               headers={"X-Plex-Token": "tok-a"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---- per-user scoping (the IDOR guard) --------------------------------------

@respx.mock
async def test_prefs_scoped_per_account_not_client_id(app):
    # Two callers with distinct tokens -> distinct plex.tv accounts.
    _mock_account({"tok-a": "user-a", "tok-b": "user-b"})
    async with _client(app) as ac:
        # User A sets a pref; crucially A *lies* in the body user_id claiming to be B.
        await ac.put(
            "/subtitle-prefs",
            json={"user_id": "user-b", "series_key": "s1", "language": "en"},
            headers={"X-Plex-Token": "tok-a"},
        )
        a_get = await ac.get("/subtitle-prefs", headers={"X-Plex-Token": "tok-a"})
        b_get = await ac.get("/subtitle-prefs", headers={"X-Plex-Token": "tok-b"})
    # The pref landed under A (the token owner), NOT under B despite the body id.
    assert a_get.json() == {"prefs": {"s1": "en"}}
    assert b_get.json() == {"prefs": {}}


@respx.mock
async def test_users_do_not_see_each_other(app):
    _mock_account({"tok-a": "user-a", "tok-b": "user-b"})
    async with _client(app) as ac:
        await ac.put("/subtitle-prefs", json={"series_key": "shared", "language": "en"},
                     headers={"X-Plex-Token": "tok-a"})
        await ac.put("/subtitle-prefs", json={"series_key": "shared", "language": "de"},
                     headers={"X-Plex-Token": "tok-b"})
        a = (await ac.get("/subtitle-prefs", headers={"X-Plex-Token": "tok-a"})).json()
        b = (await ac.get("/subtitle-prefs", headers={"X-Plex-Token": "tok-b"})).json()
    # Same series_key, different accounts -> independent rows.
    assert a == {"prefs": {"shared": "en"}}
    assert b == {"prefs": {"shared": "de"}}


# ---- auth failures ----------------------------------------------------------

@respx.mock
async def test_missing_token_rejected_on_all_endpoints(app):
    _mock_account({})  # no token resolves
    async with _client(app) as ac:
        get = await ac.get("/subtitle-prefs")
        put = await ac.put("/subtitle-prefs", json={"series_key": "s1", "language": "en"})
        dele = await ac.delete("/subtitle-prefs", params={"series_key": "s1"})
    for r in (get, put, dele):
        assert r.status_code == 401, r.text


@respx.mock
async def test_bad_token_rejected(app):
    # plex.tv 401s the token -> account unresolved -> 401 from require_plex_account.
    _mock_account({"good-tok": "user-a"})
    async with _client(app) as ac:
        resp = await ac.get("/subtitle-prefs", headers={"X-Plex-Token": "bad-tok"})
    assert resp.status_code == 401
