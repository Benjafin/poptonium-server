"""Tests for the capability-discovery endpoint (``app/capabilities.py``).

Mounts only the capabilities router and stubs the integration probes
(``all_plugins`` / ``probe_plugin`` / ``plex_configured`` /
``opensubtitles_configured``) and the config flags it reads, so we can assert
the shape of the discovery document across configured/unconfigured toggles.
"""

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app import capabilities


def _app():
    app = FastAPI()
    app.include_router(capabilities.router)
    return app


async def _get(monkeypatch, *, plugins=None, statuses=None):
    """Drive /capabilities with a stubbed plugin registry. `statuses` maps a
    plugin base_url → its probe result (or None)."""
    plugins = plugins if plugins is not None else []
    statuses = statuses or {}

    async def _all():
        return plugins

    async def _probe(base_url):
        return statuses.get(base_url)

    monkeypatch.setattr(capabilities, "all_plugins", _all)
    monkeypatch.setattr(capabilities, "probe_plugin", _probe)

    async with httpx.AsyncClient(transport=ASGITransport(app=_app()),
                                 base_url="http://test") as ac:
        return await ac.get("/capabilities")


# ---- static / always-present keys -------------------------------------------

async def test_returns_core_document(monkeypatch):
    resp = await _get(monkeypatch)
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "poptonium"
    assert "version" in body
    assert "section_schema_version" in body
    assert body["sections"] is True
    assert body["subtitle_prefs"] is True
    assert body["plugins"] == []
    assert set(["sections", "ratings", "popular", "overseerr",
                "opensubtitles", "plugins", "subtitle_prefs"]).issubset(body["features"])


# ---- integration toggles ----------------------------------------------------

async def test_ratings_true_when_mdblist_key_set(monkeypatch):
    monkeypatch.setattr(capabilities, "MDBLIST_API_KEY", "key")
    assert (await _get(monkeypatch)).json()["ratings"] is True


async def test_ratings_false_without_mdblist_key(monkeypatch):
    monkeypatch.setattr(capabilities, "MDBLIST_API_KEY", "")
    assert (await _get(monkeypatch)).json()["ratings"] is False


async def test_plex_flags_true_when_configured(monkeypatch):
    monkeypatch.setattr(capabilities, "plex_configured", lambda: True)
    body = (await _get(monkeypatch)).json()
    assert body["plex_configured"] is True
    assert body["plex_proxy"] is True


async def test_plex_flags_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(capabilities, "plex_configured", lambda: False)
    body = (await _get(monkeypatch)).json()
    assert body["plex_configured"] is False
    assert body["plex_proxy"] is False


async def test_overseerr_configured_requires_both(monkeypatch):
    monkeypatch.setattr(capabilities, "OVERSEERR_URL", "http://o")
    monkeypatch.setattr(capabilities, "OVERSEERR_API_KEY", "k")
    assert (await _get(monkeypatch)).json()["overseerr_configured"] is True


async def test_overseerr_unconfigured_when_key_missing(monkeypatch):
    monkeypatch.setattr(capabilities, "OVERSEERR_URL", "http://o")
    monkeypatch.setattr(capabilities, "OVERSEERR_API_KEY", "")
    assert (await _get(monkeypatch)).json()["overseerr_configured"] is False


async def test_opensubtitles_requires_creds_and_plex(monkeypatch):
    monkeypatch.setattr(capabilities, "opensubtitles_configured", lambda: True)
    monkeypatch.setattr(capabilities, "plex_configured", lambda: True)
    assert (await _get(monkeypatch)).json()["opensubtitles_configured"] is True


async def test_opensubtitles_false_when_plex_missing(monkeypatch):
    monkeypatch.setattr(capabilities, "opensubtitles_configured", lambda: True)
    monkeypatch.setattr(capabilities, "plex_configured", lambda: False)
    assert (await _get(monkeypatch)).json()["opensubtitles_configured"] is False


async def test_opensubtitles_false_when_creds_missing(monkeypatch):
    monkeypatch.setattr(capabilities, "opensubtitles_configured", lambda: False)
    monkeypatch.setattr(capabilities, "plex_configured", lambda: True)
    assert (await _get(monkeypatch)).json()["opensubtitles_configured"] is False


# ---- plugin advertisement ---------------------------------------------------

async def test_healthy_enabled_plugin(monkeypatch):
    plugins = [{
        "id": "npo", "name": "NPO", "enabled": True, "base_url": "http://npo",
        "manifest": {"interface": "video", "chip": {"label": "NPO"}},
    }]
    statuses = {"http://npo": {"healthy": True}}
    body = (await _get(monkeypatch, plugins=plugins, statuses=statuses)).json()
    assert body["plugins"] == [{
        "id": "npo", "name": "NPO", "enabled": True, "healthy": True,
        "interface": "video", "chip": {"label": "NPO"},
    }]


async def test_disabled_plugin_not_probed_and_unhealthy(monkeypatch):
    plugins = [{
        "id": "x", "name": "X", "enabled": False, "base_url": "http://x",
        "manifest": {"interface": "video", "chip": None},
    }]
    # No status provided; disabled plugins are never probed → healthy False.
    body = (await _get(monkeypatch, plugins=plugins, statuses={})).json()
    cap = body["plugins"][0]
    assert cap["enabled"] is False
    assert cap["healthy"] is False


async def test_enabled_but_unreachable_plugin_unhealthy(monkeypatch):
    plugins = [{
        "id": "y", "name": "Y", "enabled": True, "base_url": "http://y",
        "manifest": {"interface": "video", "chip": {}},
    }]
    # Probe returns a non-healthy status (unreachable → healthy None).
    statuses = {"http://y": {"healthy": None}}
    body = (await _get(monkeypatch, plugins=plugins, statuses=statuses)).json()
    assert body["plugins"][0]["healthy"] is False
