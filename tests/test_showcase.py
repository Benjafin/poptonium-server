"""Tests for the Cast receiver idle showcase (``app/showcase.py``) and the CORS
policy that lets the receiver page read it from the browser."""

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app import showcase


def _app():
    app = FastAPI()
    app.include_router(showcase.router)
    return app


def _item(rk, rating, mt="movie"):
    return {"rating_key": rk, "title": f"T{rk}", "type": mt, "year": 2001, "thumb": f"/t/{rk}",
            "art": None, "clear_logo": None, "rating": rating, "sources": {"imdb": {"score": 80, "votes": 10}},
            "summary": "dropped from the payload"}


def _stub(monkeypatch, calls):
    async def types():
        return {"1": "movie", "2": "show", "3": "movie", "9": "artist"}

    async def resolve(cfg, picks=None):
        calls.append(cfg)
        if cfg["media_type"] == "movie":
            return [_item("10", 91.0), _item("11", None), _item("12", 80.5)]
        return [_item("20", 88.0, "show")]

    async def rcfg():
        return {"display_groups": [{"visibility": "always", "sources": ["imdb"]}], "display_sources": ["imdb"]}

    monkeypatch.setattr(showcase, "_library_type_map", types)
    monkeypatch.setattr(showcase, "_resolve_filter", resolve)
    monkeypatch.setattr(showcase, "get_rating_config", rcfg)
    showcase._cache.clear()


async def _get(path):
    async with httpx.AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as ac:
        return await ac.get(path)


async def test_top_rated_rows_per_media_type(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    resp = await _get("/showcase?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert [m["rating_key"] for m in body["movies"]] == ["10", "12"]  # unrated dropped
    assert [s["rating_key"] for s in body["shows"]] == ["20"]
    assert "summary" not in body["movies"][0]
    assert body["display_groups"] == [{"visibility": "always", "sources": ["imdb"]}]
    by_type = {c["media_type"]: c for c in calls}
    assert by_type["movie"] == {"library_sections": ["1", "3"], "media_type": "movie", "sort": "rating:desc", "limit": 5}
    assert by_type["show"]["library_sections"] == ["2"]


async def test_cached_between_calls(monkeypatch):
    calls = []
    _stub(monkeypatch, calls)
    await _get("/showcase")
    await _get("/showcase")
    assert len(calls) == 2  # one movie + one show resolve, second request served from cache


async def test_limit_is_bounded(monkeypatch):
    _stub(monkeypatch, [])
    assert (await _get("/showcase?limit=500")).status_code == 422


async def test_cors_allows_the_receiver_origin_only():
    from app.server import app

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ok = await ac.options("/showcase", headers={"Origin": "https://poptonium.app", "Access-Control-Request-Method": "GET"})
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == "https://poptonium.app"
        dev = await ac.options("/poptonium/showcase", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
        assert dev.headers["access-control-allow-origin"] == "http://localhost:5173"
        bad = await ac.options("/showcase", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in bad.headers
        put = await ac.options("/sections/1", headers={"Origin": "https://poptonium.app", "Access-Control-Request-Method": "PUT"})
        assert put.status_code == 400
