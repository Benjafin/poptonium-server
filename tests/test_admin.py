"""Tests for the admin surface (app/admin.py).

Covers admin status (Plex/Overseerr reachable vs not), Plex helper lookups
(sections / collections / tags), on-demand job triggers, cache clearing,
the ratings-sync config read/write, and the /admin HTML shell.

Pattern (per conftest + the assignment notes):
- Mount only admin.router / admin.ui_router on a fresh FastAPI(); drive via
  ASGITransport so the app's real startup is skipped. ASGITransport is NOT
  intercepted by respx, so the inner Plex/Overseerr httpx calls ARE mocked.
- require_admin is per-endpoint on admin.router; override it to a no-op.
- DB isolation: point app.db.DB_PATH (and app.admin.get_db closes over the
  module-level get_db, which reads app.db.DB_PATH at call time) at a temp file.
- Config values are value-imported into app.admin; monkeypatch them there.
"""

import asyncio
import json

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.admin as admin
import app.db as _db
from app.auth import require_admin
from app.config import OVERSEERR_URL, PLEX_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _isolate_db(tmp_path, monkeypatch):
    """Point every get_db()/meta_* call at a fresh temp SQLite file."""
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "admin.db"))


def _api_app():
    app = FastAPI()
    app.include_router(admin.router)
    # require_admin is attached per-endpoint via Depends; neutralise it.
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _ui_app():
    app = FastAPI()
    app.include_router(admin.ui_router)
    return app


def _client(app):
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _mock_plex_reachable(ok: bool):
    """The reachability probe hits {PLEX_URL}/identity."""
    respx.get(f"{PLEX_URL}/identity").mock(
        return_value=httpx.Response(200 if ok else 401, json={})
    )


def _reset_plex_reachable_cache():
    import app.plex as plex
    plex._reachable_cache = None


# ===========================================================================
# _mask (pure helper)
# ===========================================================================

def test_mask_empty():
    assert admin._mask("") == ""
    assert admin._mask(None) == ""


def test_mask_short_value_fully_masked():
    assert admin._mask("abcd") == "••••"
    assert admin._mask("12345678") == "•" * 8


def test_mask_long_value_shows_ends():
    assert admin._mask("abcdefghij") == "abcd…ghij"


# ===========================================================================
# _probe_plex / _probe_overseerr (unit)
# ===========================================================================

async def test_probe_plex_not_configured(monkeypatch):
    monkeypatch.setattr(admin, "plex_configured", lambda: False)
    assert await admin._probe_plex() is None


@respx.mock
async def test_probe_plex_reachable(monkeypatch):
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    _mock_plex_reachable(True)
    assert await admin._probe_plex() is True


@respx.mock
async def test_probe_plex_unreachable(monkeypatch):
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    _mock_plex_reachable(False)
    assert await admin._probe_plex() is False


async def test_probe_overseerr_not_configured(monkeypatch):
    monkeypatch.setattr(admin, "OVERSEERR_URL", "")
    monkeypatch.setattr(admin, "OVERSEERR_API_KEY", "")
    assert await admin._probe_overseerr() is None


@respx.mock
async def test_probe_overseerr_ok():
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"version": "1"})
    )
    assert await admin._probe_overseerr() is True


@respx.mock
async def test_probe_overseerr_bad_status():
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        return_value=httpx.Response(500)
    )
    assert await admin._probe_overseerr() is False


@respx.mock
async def test_probe_overseerr_exception():
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        side_effect=httpx.ConnectError("boom")
    )
    assert await admin._probe_overseerr() is False


# ===========================================================================
# GET /admin/status
# ===========================================================================

@respx.mock
async def test_status_happy_path_plex_and_overseerr_reachable(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    monkeypatch.setattr(admin, "MDBLIST_API_KEY", "secretkey123456")
    _mock_plex_reachable(True)
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        return_value=httpx.Response(200, json={})
    )
    # No scheduler running (module global stays None) and no plugins in the DB.

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "poptonium"
    assert body["version"] == admin.SERVICE_VERSION
    # Fresh DB → all caches empty.
    assert body["caches"] == {
        "mdblist_ratings": 0,
        "ratings_last_sync": None,
        "popular_items": 0,
        "popular_last_refresh": None,
        "sections": 0,
    }
    assert body["ratings"]["configured"] is True
    assert body["ratings"]["sync"] == {"enabled": True, "hour": 3}
    assert body["jobs"] == []
    assert body["plugins"] == []
    assert body["plex"] == {"configured": True, "healthy": True, "url": PLEX_URL}
    assert body["overseerr"]["configured"] is True
    assert body["overseerr"]["healthy"] is True
    # Config secrets are masked.
    assert body["config"]["MDBLIST_API_KEY"] == "secr…3456"
    assert "…" in body["config"]["PLEX_TOKEN"]


@respx.mock
async def test_status_plex_unreachable_overseerr_down(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    _mock_plex_reachable(False)
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        side_effect=httpx.ConnectError("down")
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/status")

    body = resp.json()
    assert body["plex"]["healthy"] is False
    assert body["overseerr"]["healthy"] is False


@respx.mock
async def test_status_plex_not_configured(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "plex_configured", lambda: False)
    monkeypatch.setattr(admin, "OVERSEERR_URL", "")
    monkeypatch.setattr(admin, "OVERSEERR_API_KEY", "")

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/status")

    body = resp.json()
    # None = not configured (no probe attempted).
    assert body["plex"] == {"configured": False, "healthy": None, "url": PLEX_URL}
    assert body["overseerr"]["configured"] is False
    assert body["overseerr"]["healthy"] is None
    assert body["ratings"]["configured"] is False  # MDBLIST default env is empty


@respx.mock
async def test_status_reports_cache_counts_and_last_times(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    _mock_plex_reachable(True)
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        return_value=httpx.Response(200, json={})
    )

    # Seed the DB with a rating row, a popular row, a section, and last-* metas.
    import time
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, ratings_json, fetched_at) VALUES (?,?,?,?)",
            (1, "movie", "{}", time.time()),
        )
        await db.execute(
            "INSERT INTO popular_items (imdb_id, title, media_type, fetched_at) VALUES (?,?,?,?)",
            ("tt1", "A", "movie", time.time()),
        )
        await db.execute(
            "INSERT INTO sections (title, type, created_at, updated_at) VALUES (?,?,?,?)",
            ("Row", "filter", time.time(), time.time()),
        )
        await db.commit()
    finally:
        await db.close()
    await _db.meta_set("popular_last_refresh", "123.5")
    await _db.meta_set("library_ratings_last_sync", "999.0")

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/status")

    caches = resp.json()["caches"]
    assert caches["mdblist_ratings"] == 1
    assert caches["popular_items"] == 1
    assert caches["sections"] == 1
    assert caches["popular_last_refresh"] == 123.5
    assert caches["ratings_last_sync"] == 999.0


@respx.mock
async def test_status_includes_scheduler_jobs(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    _mock_plex_reachable(True)
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        return_value=httpx.Response(200, json={})
    )

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import app.scheduler as sched_mod

    sched = AsyncIOScheduler()

    async def _noop():
        return None

    sched.add_job(_noop, "interval", hours=1, id="job1", name="Job One")
    # Starting the scheduler populates each job's next_run_time (which the status
    # endpoint reads). paused=True so nothing actually fires during the test.
    sched.start(paused=True)
    monkeypatch.setattr(sched_mod, "scheduler", sched)

    try:
        async with _client(_api_app()) as ac:
            resp = await ac.get("/admin/status")
    finally:
        sched.shutdown(wait=False)

    jobs = resp.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == "job1"
    assert jobs[0]["name"] == "Job One"
    assert jobs[0]["next_run"] is not None
    assert "trigger" in jobs[0]


@respx.mock
async def test_status_includes_plugins_with_live_status(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _reset_plex_reachable_cache()
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    _mock_plex_reachable(True)
    respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
        return_value=httpx.Response(200, json={})
    )

    import time
    db = await _db.get_db()
    try:
        # One enabled plugin (probed), one disabled (not probed).
        await db.execute(
            """INSERT INTO plugins (id, name, base_url, enabled, manifest_json, added_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            ("p_enabled", "Enabled", "http://plugin.enabled", 1, "{}", time.time(), time.time()),
        )
        await db.execute(
            """INSERT INTO plugins (id, name, base_url, enabled, manifest_json, added_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            ("p_disabled", "Disabled", "http://plugin.disabled", 0, "{}", time.time(), time.time()),
        )
        await db.commit()
    finally:
        await db.close()

    respx.get("http://plugin.enabled/plugin/status").mock(
        return_value=httpx.Response(200, json={"configured": True, "healthy": True, "detail": "ok"})
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/status")

    plugins = resp.json()["plugins"]
    by_id = {p["id"]: p for p in plugins}
    assert by_id["p_enabled"]["status"] == {"configured": True, "healthy": True, "detail": "ok"}
    assert by_id["p_disabled"]["status"] == {"configured": False, "healthy": None, "detail": "disabled"}


# ===========================================================================
# GET /admin/plex/sections
# ===========================================================================

@respx.mock
async def test_plex_sections_returns_mapped_dirs(monkeypatch):
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "2", "title": "TV", "type": "show"},
            ]}
        })
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/sections")

    assert resp.status_code == 200
    assert resp.json() == {"sections": [
        {"key": "1", "title": "Movies", "type": "movie"},
        {"key": "2", "title": "TV", "type": "show"},
    ]}


@respx.mock
async def test_plex_sections_empty_when_plex_returns_nothing(monkeypatch):
    # plex_get returns None (non-200) → endpoint returns empty list.
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(500)
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/sections")

    assert resp.json() == {"sections": []}


# ===========================================================================
# GET /admin/plex/collections
# ===========================================================================

@respx.mock
async def test_plex_collections_returns_mapped(monkeypatch):
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections/1/collections").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [
                {"ratingKey": 55, "title": "Marvel", "childCount": 30},
            ]}
        })
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/collections", params={"section": "1"})

    assert resp.json() == {"collections": [
        {"key": "55", "title": "Marvel", "count": 30},
    ]}


@respx.mock
async def test_plex_collections_empty(monkeypatch):
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections/9/collections").mock(
        return_value=httpx.Response(404)
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/collections", params={"section": "9"})

    assert resp.json() == {"collections": []}


async def test_plex_collections_requires_section_param():
    # `section` is a required query param → 422 without it.
    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/collections")
    assert resp.status_code == 422


# ===========================================================================
# GET /admin/plex/tags
# ===========================================================================

@respx.mock
async def test_plex_tags_tallies_and_sorts(monkeypatch):
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    # Full-library scan: two items carry Genre "Action", one "Drama".
    respx.get(f"{PLEX_URL}/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Metadata": [
                {"Genre": [{"tag": "Action"}, {"tag": "Drama"}]},
                {"Genre": [{"tag": "Action"}]},
                {"Director": [{"tag": "Nolan"}]},
            ]}
        })
    )
    # Per-dimension directory listings.
    respx.get(f"{PLEX_URL}/library/sections/1/genre").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Directory": [
                {"key": "10", "title": "Drama"},
                {"key": "11", "title": "Action"},
            ]}
        })
    )
    respx.get(f"{PLEX_URL}/library/sections/1/director").mock(
        return_value=httpx.Response(200, json={
            "MediaContainer": {"Directory": [{"key": "20", "title": "Nolan"}]}
        })
    )
    respx.get(f"{PLEX_URL}/library/sections/1/actor").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Directory": []}})
    )
    respx.get(f"{PLEX_URL}/library/sections/1/country").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Directory": []}})
    )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/tags", params={"section": "1"})

    body = resp.json()
    # Genres sorted high→low: Action (2) before Drama (1).
    assert body["genres"] == [
        {"id": "11", "title": "Action", "count": 2},
        {"id": "10", "title": "Drama", "count": 1},
    ]
    assert body["directors"] == [{"id": "20", "title": "Nolan", "count": 1}]
    assert body["actors"] == []
    assert body["countries"] == []


@respx.mock
async def test_plex_tags_empty_library(monkeypatch):
    monkeypatch.setattr(admin, "plex_configured", lambda: True)
    # Full scan returns nothing; each directory listing also empty.
    respx.get(f"{PLEX_URL}/library/sections/2/all").mock(
        return_value=httpx.Response(500)
    )
    for dirname in ("genre", "director", "actor", "country"):
        respx.get(f"{PLEX_URL}/library/sections/2/{dirname}").mock(
            return_value=httpx.Response(500)
        )

    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/plex/tags", params={"section": "2"})

    body = resp.json()
    assert body == {"genres": [], "directors": [], "actors": [], "countries": []}


# ===========================================================================
# POST /admin/cron/{job_id}/run
# ===========================================================================

async def test_run_cron_known_job(monkeypatch):
    ran = asyncio.Event()

    async def fake_job():
        ran.set()

    monkeypatch.setattr(admin, "_JOB_FUNCS", {"popular_refresh": fake_job})

    async with _client(_api_app()) as ac:
        resp = await ac.post("/admin/cron/popular_refresh/run")

    assert resp.status_code == 200
    assert resp.json() == {"status": "started", "job": "popular_refresh"}
    # The task is fire-and-forget; give it a tick to run.
    await asyncio.wait_for(ran.wait(), timeout=1)


async def test_run_cron_unknown_job():
    async with _client(_api_app()) as ac:
        resp = await ac.post("/admin/cron/nope/run")
    assert resp.status_code == 404
    assert "Unknown job" in resp.json()["detail"]


# ===========================================================================
# POST /admin/cache/clear
# ===========================================================================

async def test_cache_clear_ratings(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    import time
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, ratings_json, fetched_at) VALUES (?,?,?,?)",
            (1, "movie", "{}", time.time()),
        )
        await db.commit()
    finally:
        await db.close()

    async with _client(_api_app()) as ac:
        resp = await ac.post("/admin/cache/clear", params={"which": "ratings"})

    assert resp.json() == {"status": "cleared", "which": "ratings"}
    db = await _db.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) c FROM mdblist_ratings")
        assert (await cur.fetchone())["c"] == 0
    finally:
        await db.close()


async def test_cache_clear_popular(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    import time
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO popular_items (imdb_id, title, media_type, fetched_at) VALUES (?,?,?,?)",
            ("tt1", "A", "movie", time.time()),
        )
        await db.commit()
    finally:
        await db.close()

    async with _client(_api_app()) as ac:
        resp = await ac.post("/admin/cache/clear", params={"which": "popular"})

    assert resp.json() == {"status": "cleared", "which": "popular"}
    db = await _db.get_db()
    try:
        cur = await db.execute("SELECT COUNT(*) c FROM popular_items")
        assert (await cur.fetchone())["c"] == 0
    finally:
        await db.close()


async def test_cache_clear_rejects_bad_which(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    async with _client(_api_app()) as ac:
        resp = await ac.post("/admin/cache/clear", params={"which": "bogus"})
    assert resp.status_code == 422  # fails the regex pattern constraint


# ===========================================================================
# POST /admin/ratings/refresh
# ===========================================================================

async def test_ratings_refresh_triggers_job(monkeypatch):
    ran = asyncio.Event()

    async def fake_refresh():
        ran.set()

    monkeypatch.setattr(admin, "refresh_library_ratings", fake_refresh)

    async with _client(_api_app()) as ac:
        resp = await ac.post("/admin/ratings/refresh")

    assert resp.status_code == 200
    assert resp.json() == {"status": "started", "job": "library_ratings"}
    await asyncio.wait_for(ran.wait(), timeout=1)


# ===========================================================================
# GET / PUT /admin/ratings/sync
# ===========================================================================

async def test_ratings_sync_get_default(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    async with _client(_api_app()) as ac:
        resp = await ac.get("/admin/ratings/sync")
    assert resp.json() == {"enabled": True, "hour": 3}


async def test_ratings_sync_put_updates_and_persists(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    # schedule_library_sync no-ops when scheduler is None, but stub it to be safe.
    called = {}

    def fake_schedule(cfg):
        called["cfg"] = cfg

    monkeypatch.setattr(admin, "schedule_library_sync", fake_schedule)

    async with _client(_api_app()) as ac:
        resp = await ac.put("/admin/ratings/sync", json={"enabled": False, "hour": 5})

    assert resp.json() == {"enabled": False, "hour": 5}
    assert called["cfg"] == {"enabled": False, "hour": 5}
    # Persisted to the meta store.
    raw = await _db.meta_get("ratings_sync")
    assert json.loads(raw) == {"enabled": False, "hour": 5}


async def test_ratings_sync_put_clamps_hour(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "schedule_library_sync", lambda cfg: None)

    async with _client(_api_app()) as ac:
        resp = await ac.put("/admin/ratings/sync", json={"hour": 99})

    # Clamped into 0..23.
    assert resp.json()["hour"] == 23

    async with _client(_api_app()) as ac:
        resp2 = await ac.put("/admin/ratings/sync", json={"hour": -5})
    assert resp2.json()["hour"] == 0


async def test_ratings_sync_put_partial_keeps_other_field(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    monkeypatch.setattr(admin, "schedule_library_sync", lambda cfg: None)

    async with _client(_api_app()) as ac:
        # Only toggle enabled; hour keeps its default (3).
        resp = await ac.put("/admin/ratings/sync", json={"enabled": False})

    assert resp.json() == {"enabled": False, "hour": 3}


# ===========================================================================
# Admin web UI shell (ui_router): GET /admin and /admin/
# ===========================================================================

async def test_admin_ui_serves_html():
    async with _client(_ui_app()) as ac:
        resp = await ac.get("/admin")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Poptonium" in resp.text
    assert "<!DOCTYPE html>" in resp.text


async def test_admin_ui_trailing_slash():
    async with _client(_ui_app()) as ac:
        resp = await ac.get("/admin/")
    assert resp.status_code == 200
    assert "Poptonium" in resp.text


async def test_admin_ui_missing_file_returns_404(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_HTML_PATH", "/nonexistent/admin.html")
    async with _client(_ui_app()) as ac:
        resp = await ac.get("/admin")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Admin UI not found"
