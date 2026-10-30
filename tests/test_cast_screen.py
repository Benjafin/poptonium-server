"""Tests for the Cast receiver idle screen (``app/cast_screen.py``) and the CORS policy
that lets the receiver page read it from the browser."""

import json

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app import cast_screen
from app.auth import require_admin

SECTIONS = {
    7: {"id": 7, "title": "Recently added", "subtitle": None, "type": "filter", "style": "row", "enabled": 1, "sort_order": 1, "config": "{}"},
    8: {"id": 8, "title": "Spotlight", "subtitle": None, "type": "plex_collection", "style": "hero", "enabled": 1, "sort_order": 2, "config": "{}"},
    9: {"id": 9, "title": "Featured", "subtitle": None, "type": "plex_collection", "style": "hero", "enabled": 0, "sort_order": 3, "config": "{}"},
    10: {"id": 10, "title": "Who's watching", "subtitle": None, "type": "sessions", "style": "row", "enabled": 1, "sort_order": 4, "config": "{}"},
}


def _app():
    app = FastAPI()
    app.include_router(cast_screen.router)
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _item(rk, rating=80.0, **extra):
    return {"rating_key": rk, "title": f"T{rk}", "type": "movie", "thumb": f"/t/{rk}", "rating": rating,
            "sources": {"imdb": {"score": 80}}, "summary": "x" * 400, "guid": "dropped", **extra}


def _stub(monkeypatch, stored=None):
    store = {"value": json.dumps({"rows": stored}) if stored is not None else None}
    calls = []

    async def meta_get(key):
        return store["value"]

    async def meta_set(key, value):
        store["value"] = value

    async def rows():
        return SECTIONS

    async def resolve(row, client_schema="1.0.0"):
        calls.append(row["id"])
        items = [_item(str(row["id"] * 100 + i), user_title="Abi" if row["type"] == "sessions" else None) for i in range(30)]
        return {"id": row["id"], "title": row["title"], "subtitle": None, "type": row["type"], "style": row["style"], "items": items}

    async def top(media_type, limit):
        return [_item(f"{media_type}{i}") for i in range(3)]

    async def rcfg():
        return {"display_groups": [{"visibility": "always", "sources": ["imdb"]}], "display_sources": ["imdb"]}

    for name, fn in [("meta_get", meta_get), ("meta_set", meta_set), ("_section_rows", rows), ("resolve_section", resolve),
                     ("_top_rated", top), ("get_rating_config", rcfg)]:
        monkeypatch.setattr(cast_screen, name, fn)
    cast_screen._cache.update(expiry=0.0, key=None, payload=None)
    return store, calls


async def _req(method, path, **kw):
    async with httpx.AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as ac:
        return await ac.request(method, path, **kw)


async def test_defaults_to_top_rated_rows(monkeypatch):
    _stub(monkeypatch)
    body = (await _req("GET", "/cast/screen")).json()
    assert [r["key"] for r in body["rows"]] == ["top_movies", "top_shows"]
    assert body["rows"][0]["title"] == "Top rated films"
    assert body["display_groups"][0]["sources"] == ["imdb"]
    assert "summary" not in body["rows"][0]["items"][0]  # shelves don't need it


async def test_configured_rows_resolve_in_order(monkeypatch):
    _stub(monkeypatch, [{"source": "section", "id": 8}, {"source": "section", "id": 10}, {"source": "top_shows"}])
    rows = (await _req("GET", "/cast/screen")).json()["rows"]
    assert [r["key"] for r in rows] == ["section:8", "section:10", "top_shows"]
    hero, sessions = rows[0], rows[1]
    assert hero["style"] == "hero" and len(hero["items"]) == cast_screen.HERO_ITEMS
    assert len(hero["items"][0]["summary"]) == 280 and "guid" not in hero["items"][0]
    assert sessions["type"] == "sessions" and sessions["items"][0]["user_title"] == "Abi"
    assert len(sessions["items"]) == cast_screen.SHELF_ITEMS


async def test_deleted_section_is_skipped(monkeypatch):
    _stub(monkeypatch, [{"source": "section", "id": 99}, {"source": "top_movies"}])
    rows = (await _req("GET", "/cast/screen")).json()["rows"]
    assert [r["key"] for r in rows] == ["top_movies"]


async def test_cached_briefly(monkeypatch):
    _, calls = _stub(monkeypatch, [{"source": "section", "id": 7}])
    await _req("GET", "/cast/screen")
    await _req("GET", "/cast/screen")
    assert calls == [7]


async def test_config_lists_choices_and_saves(monkeypatch):
    store, _ = _stub(monkeypatch)
    cfg = (await _req("GET", "/cast/screen/config")).json()
    assert cfg["rows"] == cast_screen.DEFAULT_ROWS and cfg["max_rows"] == 3
    assert [a.get("id", a["source"]) for a in cfg["available"]] == ["top_movies", "top_shows", 7, 8, 9, 10]
    assert cfg["available"][4]["enabled"] is False  # cast-only sections may be disabled in the app
    r = await _req("PUT", "/cast/screen/config", json={"rows": [{"source": "section", "id": 8}, {"source": "top_movies"}]})
    assert r.status_code == 200
    assert json.loads(store["value"]) == {"rows": [{"source": "section", "id": 8}, {"source": "top_movies"}]}


async def test_config_rejects_what_does_not_fit(monkeypatch):
    _stub(monkeypatch)
    four = [{"source": "top_movies"}, {"source": "top_shows"}, {"source": "section", "id": 7}, {"source": "section", "id": 10}]
    assert (await _req("PUT", "/cast/screen/config", json={"rows": four})).status_code == 400
    two_heroes = [{"source": "section", "id": 8}, {"source": "section", "id": 9}]
    assert (await _req("PUT", "/cast/screen/config", json={"rows": two_heroes})).status_code == 400
    assert (await _req("PUT", "/cast/screen/config", json={"rows": [{"source": "section", "id": 42}]})).status_code == 400


async def test_config_needs_admin():
    app = FastAPI()
    app.include_router(cast_screen.router)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/cast/screen/config")).status_code in (401, 403)


async def test_cors_allows_the_receiver_origin_only():
    from app.server import app

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ok = await ac.options("/cast/screen", headers={"Origin": "https://poptonium.app", "Access-Control-Request-Method": "GET"})
        assert ok.headers["access-control-allow-origin"] == "https://poptonium.app"
        bad = await ac.options("/cast/screen", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in bad.headers
