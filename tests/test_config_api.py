"""Tests for the config + test-connection admin API (app/admin.py).

Same harness as test_admin: mount admin.router on a bare app, neutralise
require_admin, and let respx intercept the outbound probe calls. The settings
singleton is reset per test by the conftest autouse fixture; we point its file at
a temp path so PUT's persist never writes a real config.json.
"""

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.admin as admin
from app.auth import require_admin
from app.config import MDBLIST_BASE, OPENSUBTITLES_API_BASE, settings


def _app():
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_config_get_masks_secrets():
    async with _client(_app()) as ac:
        resp = await ac.get("/admin/config")
    assert resp.status_code == 200
    body = resp.json()
    # env-seeded (conftest): PLEX_URL clear, PLEX_TOKEN present-but-masked.
    assert body["PLEX_URL"] == settings.PLEX_URL
    assert body["PLEX_TOKEN"] != settings.PLEX_TOKEN
    assert body["PLEX_TOKEN_set"] is True


async def test_config_put_updates_and_keeps_unchanged_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_path", str(tmp_path / "config.json"))
    async with _client(_app()) as ac:
        resp = await ac.put("/admin/config", json={
            "OVERSEERR_URL": "http://new-overseerr",
            "OVERSEERR_API_KEY": "",       # blank secret -> unchanged
        })
    assert resp.status_code == 200
    assert settings.OVERSEERR_URL == "http://new-overseerr"
    # The blank secret did NOT clobber the existing key.
    assert settings.OVERSEERR_API_KEY == "test-api-key"


async def test_config_put_sets_secret_and_ignores_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_path", str(tmp_path / "config.json"))
    # Stub the MDbList-added side effect so nothing hits the network.
    async def _noop():
        return None
    monkeypatch.setattr(admin, "refresh_popular_items", _noop)
    async with _client(_app()) as ac:
        resp = await ac.put("/admin/config", json={"MDBLIST_API_KEY": "abc123", "BOGUS": "x"})
    assert resp.status_code == 200
    assert settings.MDBLIST_API_KEY == "abc123"
    assert "BOGUS" not in settings._values


@respx.mock
async def test_test_plex_ok_and_fail():
    respx.get(f"{settings.PLEX_URL}/").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"friendlyName": "Home"}})
    )
    async with _client(_app()) as ac:
        ok = await ac.post("/admin/config/test", json={
            "service": "plex", "values": {"PLEX_URL": settings.PLEX_URL, "PLEX_TOKEN": "t"}})
    assert ok.json()["ok"] is True
    assert "Home" in ok.json()["detail"]

    respx.get(f"{settings.PLEX_URL}/").mock(return_value=httpx.Response(401))
    async with _client(_app()) as ac:
        bad = await ac.post("/admin/config/test", json={
            "service": "plex", "values": {"PLEX_URL": settings.PLEX_URL, "PLEX_TOKEN": "t"}})
    assert bad.json()["ok"] is False


async def test_test_plex_requires_fields():
    async with _client(_app()) as ac:
        resp = await ac.post("/admin/config/test", json={
            "service": "plex", "values": {"PLEX_URL": "", "PLEX_TOKEN": ""}})
    assert resp.json()["ok"] is False


@respx.mock
async def test_test_overseerr_ok_and_bad_key():
    respx.get(f"{settings.OVERSEERR_URL}/api/v1/settings/main").mock(
        return_value=httpx.Response(200, json={"applicationTitle": "My Overseerr"})
    )
    async with _client(_app()) as ac:
        resp = await ac.post("/admin/config/test", json={
            "service": "overseerr",
            "values": {"OVERSEERR_URL": settings.OVERSEERR_URL, "OVERSEERR_API_KEY": "k"}})
    body = resp.json()
    assert body["ok"] is True and "My Overseerr" in body["detail"]

    # A bogus key is rejected (403), not accepted like the public /status endpoint.
    respx.get(f"{settings.OVERSEERR_URL}/api/v1/settings/main").mock(return_value=httpx.Response(403))
    async with _client(_app()) as ac:
        bad = await ac.post("/admin/config/test", json={
            "service": "overseerr",
            "values": {"OVERSEERR_URL": settings.OVERSEERR_URL, "OVERSEERR_API_KEY": "bad"}})
    assert bad.json()["ok"] is False


@respx.mock
async def test_test_mdblist_ok_and_fail():
    respx.get(f"{MDBLIST_BASE}/user").mock(return_value=httpx.Response(200, json={"user_id": 5}))
    async with _client(_app()) as ac:
        ok = await ac.post("/admin/config/test", json={
            "service": "mdblist", "values": {"MDBLIST_API_KEY": "key"}})
    assert ok.json()["ok"] is True

    respx.get(f"{MDBLIST_BASE}/user").mock(return_value=httpx.Response(401))
    async with _client(_app()) as ac:
        bad = await ac.post("/admin/config/test", json={
            "service": "mdblist", "values": {"MDBLIST_API_KEY": "key"}})
    assert bad.json()["ok"] is False


@respx.mock
async def test_test_opensubtitles_ok():
    respx.post(f"{OPENSUBTITLES_API_BASE}/login").mock(
        return_value=httpx.Response(200, json={"token": "jwt"})
    )
    async with _client(_app()) as ac:
        resp = await ac.post("/admin/config/test", json={
            "service": "opensubtitles",
            "values": {"OPENSUBTITLES_API_KEY": "k", "OPENSUBTITLES_USERNAME": "u",
                       "OPENSUBTITLES_PASSWORD": "p"}})
    assert resp.json()["ok"] is True


async def test_test_unknown_service():
    async with _client(_app()) as ac:
        resp = await ac.post("/admin/config/test", json={"service": "nope", "values": {}})
    assert resp.json()["ok"] is False
