"""Tests for app/sections.py — custom Library sections CRUD + reorder + client
resolution endpoints.

Almost everything here is DB-backed CRUD, so the pattern is: a fresh temp SQLite
DB per test (via monkeypatching ``app.db.DB_PATH``) and a bare FastAPI app that
mounts only the sections router. Admin-gated writes are opened by overriding the
``require_admin`` dependency. The ``resolved``/``preview`` endpoints call
``resolve_section``; for unknown/unhandled section types that helper returns an
empty item list without any outbound HTTP, which keeps these tests offline.
"""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
from app import sections
from app.auth import require_admin


def _app():
    app = FastAPI()
    app.include_router(sections.router)
    # Open the admin gate for the whole app under test.
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _client(app):
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "sections.db"))
    return _app()


# ---- create -----------------------------------------------------------------

async def test_create_returns_row_with_defaults(app):
    async with _client(app) as ac:
        resp = await ac.post("/sections", json={"title": "My Row", "type": "filter"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] >= 1
    assert body["title"] == "My Row"
    assert body["subtitle"] is None
    assert body["type"] == "filter"
    # Payload defaults are persisted.
    assert body["style"] == "row"
    assert body["position"] == "top"
    assert body["sort_order"] == 0
    assert body["enabled"] is True
    assert body["config"] == {}
    # Derived, not stored.
    assert "min_app_version" in body


async def test_create_persists_full_payload_and_config(app):
    payload = {
        "title": "Hero shelf",
        "subtitle": "Featured",
        "type": "plex_collection",
        "style": "hero",
        "position": "after_movies",
        "sort_order": 5,
        "enabled": False,
        "config": {"collection_key": "42"},
    }
    async with _client(app) as ac:
        resp = await ac.post("/sections", json=payload)
        body = resp.json()
        # Round-trip through list to confirm it was actually stored.
        listed = (await ac.get("/sections")).json()
    assert resp.status_code == 200
    assert body["subtitle"] == "Featured"
    assert body["style"] == "hero"
    assert body["position"] == "after_movies"
    assert body["sort_order"] == 5
    assert body["enabled"] is False
    assert body["config"] == {"collection_key": "42"}
    assert listed == [body]


async def test_create_requires_title_and_type(app):
    async with _client(app) as ac:
        # Missing 'type'.
        r1 = await ac.post("/sections", json={"title": "no type"})
        # Missing 'title'.
        r2 = await ac.post("/sections", json={"type": "filter"})
    assert r1.status_code == 422
    assert r2.status_code == 422


# ---- list / ordering --------------------------------------------------------

async def test_list_empty(app):
    async with _client(app) as ac:
        resp = await ac.get("/sections")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_orders_by_sort_order_then_id(app):
    async with _client(app) as ac:
        # Insert out of order; created ids ascend but sort_order does not.
        await ac.post("/sections", json={"title": "C", "type": "filter", "sort_order": 2})
        await ac.post("/sections", json={"title": "A", "type": "filter", "sort_order": 0})
        await ac.post("/sections", json={"title": "B", "type": "filter", "sort_order": 0})
        titles = [s["title"] for s in (await ac.get("/sections")).json()]
    # sort_order asc, then id asc as tiebreak (A inserted before B).
    assert titles == ["A", "B", "C"]


# ---- update -----------------------------------------------------------------

async def test_update_modifies_existing(app):
    async with _client(app) as ac:
        created = (await ac.post("/sections", json={"title": "old", "type": "filter"})).json()
        sid = created["id"]
        resp = await ac.put(
            f"/sections/{sid}",
            json={
                "title": "new",
                "type": "plex_collection",
                "style": "hero",
                "position": "after_shows",
                "sort_order": 3,
                "enabled": False,
                "config": {"x": 1},
            },
        )
        body = resp.json()
    assert resp.status_code == 200
    assert body["id"] == sid
    assert body["title"] == "new"
    assert body["type"] == "plex_collection"
    assert body["style"] == "hero"
    assert body["position"] == "after_shows"
    assert body["enabled"] is False
    assert body["config"] == {"x": 1}


async def test_update_missing_returns_404(app):
    async with _client(app) as ac:
        resp = await ac.put("/sections/9999", json={"title": "x", "type": "filter"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Section not found"


# ---- delete -----------------------------------------------------------------

async def test_delete_removes_row(app):
    async with _client(app) as ac:
        created = (await ac.post("/sections", json={"title": "d", "type": "filter"})).json()
        sid = created["id"]
        resp = await ac.delete(f"/sections/{sid}")
        listed = (await ac.get("/sections")).json()
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    assert listed == []


async def test_delete_nonexistent_is_ok(app):
    # DELETE is idempotent: deleting a missing id still succeeds.
    async with _client(app) as ac:
        resp = await ac.delete("/sections/12345")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}


# ---- reorder ----------------------------------------------------------------

async def test_reorder_updates_sort_order_and_position(app):
    async with _client(app) as ac:
        a = (await ac.post("/sections", json={"title": "A", "type": "filter", "sort_order": 0})).json()
        b = (await ac.post("/sections", json={"title": "B", "type": "filter", "sort_order": 1})).json()
        # Swap order; move B to a different shelf in the same call.
        resp = await ac.post(
            "/sections/reorder",
            json=[
                {"id": a["id"], "sort_order": 1},  # no position -> keep placement
                {"id": b["id"], "sort_order": 0, "position": "after_shows"},
            ],
        )
        listed = (await ac.get("/sections")).json()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    by_id = {s["id"]: s for s in listed}
    assert by_id[a["id"]]["sort_order"] == 1
    assert by_id[a["id"]]["position"] == "top"  # unchanged (position omitted)
    assert by_id[b["id"]]["sort_order"] == 0
    assert by_id[b["id"]]["position"] == "after_shows"
    # Ordering reflects the new sort_order.
    assert [s["title"] for s in listed] == ["B", "A"]


async def test_reorder_empty_list_is_ok(app):
    async with _client(app) as ac:
        resp = await ac.post("/sections/reorder", json=[])
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---- admin gating -----------------------------------------------------------

async def test_write_endpoints_require_admin(tmp_path, monkeypatch):
    # Build an app WITHOUT the require_admin override so the real gate runs.
    # The real require_admin (no admin session) should reject writes.
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "gated.db"))
    gated = FastAPI()
    gated.include_router(sections.router)
    async with _client(gated) as ac:
        create = await ac.post("/sections", json={"title": "x", "type": "filter"})
        update = await ac.put("/sections/1", json={"title": "x", "type": "filter"})
        delete = await ac.delete("/sections/1")
        reorder = await ac.post("/sections/reorder", json=[])
    for r in (create, update, delete, reorder):
        assert r.status_code == 401, r.text
    # Read endpoints stay open.
    async with _client(gated) as ac:
        assert (await ac.get("/sections")).status_code == 200


# ---- resolved / preview (client-facing read endpoints) ----------------------

async def test_resolved_only_returns_enabled(app):
    async with _client(app) as ac:
        # Unknown type -> resolve_section yields items:[] without any HTTP.
        await ac.post("/sections", json={"title": "on", "type": "unknown", "enabled": True})
        await ac.post("/sections", json={"title": "off", "type": "unknown", "enabled": False})
        resp = await ac.get("/sections/resolved")
    assert resp.status_code == 200
    sections_out = resp.json()["sections"]
    assert [s["title"] for s in sections_out] == ["on"]
    assert sections_out[0]["items"] == []


async def test_resolved_empty(app):
    async with _client(app) as ac:
        resp = await ac.get("/sections/resolved")
    assert resp.status_code == 200
    assert resp.json() == {"sections": []}


async def test_preview_returns_resolved_section(app):
    async with _client(app) as ac:
        created = (await ac.post("/sections", json={"title": "P", "type": "unknown"})).json()
        resp = await ac.get(f"/sections/{created['id']}/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["title"] == "P"
    assert body["items"] == []


async def test_preview_missing_returns_404(app):
    async with _client(app) as ac:
        resp = await ac.get("/sections/4242/preview")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Section not found"


# ---- _section_to_dict: config decode fallback -------------------------------

async def test_config_decode_fallback_on_bad_json(app):
    # Write a row with invalid JSON in config directly, then read it back through
    # the list endpoint. _section_to_dict must swallow the decode error -> {}.
    import time

    async with _client(app) as ac:
        db = await _db.get_db()
        try:
            await db.execute(
                "INSERT INTO sections (title, subtitle, type, style, position, sort_order, "
                "enabled, config, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("bad", None, "filter", "row", "top", 0, 1, "{not json", time.time(), time.time()),
            )
            await db.commit()
        finally:
            await db.close()
        listed = (await ac.get("/sections")).json()
    assert listed[0]["config"] == {}
