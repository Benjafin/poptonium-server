"""Tests for the mdblist fetch/cache layer, the /ratings + /health API, and the
library-sync helpers in app/ratings.py.

The rating FORMULA (compute_rating, effective_sources, _parse_sources, display
groups) is covered separately in tests/test_ratings.py and is NOT duplicated here.

mdblist bulk calls go to {MDBLIST_BASE}/tmdb/{movie|show} and are mocked via
respx. DB access is isolated to a temp file. Endpoint tests mount only the
ratings router and drive it via ASGITransport (which respx does not intercept, so
the inner httpx calls are still mocked).
"""

import json
import time

import httpx
import respx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
import app.ratings as ratings
import app.scheduler as scheduler_mod
from app.config import MDBLIST_BASE


def _use_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "t.db"))


def _enable_key(monkeypatch):
    monkeypatch.setattr(ratings, "MDBLIST_API_KEY", "testkey")


def _no_sleep(monkeypatch):
    async def _sleep(*_a, **_k):
        return None
    monkeypatch.setattr(ratings.asyncio, "sleep", _sleep)


def _app():
    app = FastAPI()
    app.include_router(ratings.router)
    return app


# ---- mdblist_bulk -----------------------------------------------------------

async def test_mdblist_bulk_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(ratings, "MDBLIST_API_KEY", "")
    assert await ratings.mdblist_bulk("movie", [1, 2, 3]) == {}


async def test_mdblist_bulk_empty_ids_returns_empty(monkeypatch):
    _enable_key(monkeypatch)
    assert await ratings.mdblist_bulk("movie", []) == {}


@respx.mock
async def test_mdblist_bulk_happy_path_movie(monkeypatch):
    _enable_key(monkeypatch)
    route = respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(
            200,
            headers={"x-ratelimit-remaining": "500"},
            json=[
                {"ids": {"tmdb": 603, "imdb": "tt0133093"}, "score": 88},
                {"id": 604, "score": 70},  # tmdb id via top-level "id"
            ],
        )
    )
    out = await ratings.mdblist_bulk("movie", [603, 604])
    assert route.called
    assert set(out) == {603, 604}
    assert out[603]["score"] == 88
    # It dedups ids and sends them in the request body.
    sent = json.loads(route.calls.last.request.content)
    assert sorted(sent["ids"]) == [603, 604]


@respx.mock
async def test_mdblist_bulk_show_path(monkeypatch):
    _enable_key(monkeypatch)
    route = respx.post(f"{MDBLIST_BASE}/tmdb/show").mock(
        return_value=httpx.Response(200, json=[{"ids": {"tmdb": 1396}, "score": 95}])
    )
    out = await ratings.mdblist_bulk("show", [1396])
    assert route.called
    assert out[1396]["score"] == 95


@respx.mock
async def test_mdblist_bulk_dedups_ids(monkeypatch):
    _enable_key(monkeypatch)
    route = respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(200, json=[])
    )
    await ratings.mdblist_bulk("movie", [1, 1, 2, 0, None])
    sent = json.loads(route.calls.last.request.content)
    # Zero/None filtered, duplicates removed.
    assert sent["ids"] == [1, 2]


@respx.mock
async def test_mdblist_bulk_chunks_over_200(monkeypatch):
    _enable_key(monkeypatch)
    ids = list(range(1, 251))  # 250 ids -> 2 chunks (200 + 50)
    route = respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(200, json=[])
    )
    await ratings.mdblist_bulk("movie", ids)
    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert len(first["ids"]) == 200
    assert len(second["ids"]) == 50


@respx.mock
async def test_mdblist_bulk_429_then_200(monkeypatch):
    _enable_key(monkeypatch)
    _no_sleep(monkeypatch)
    route = respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=[{"ids": {"tmdb": 1}, "score": 50}]),
        ]
    )
    out = await ratings.mdblist_bulk("movie", [1])
    assert route.call_count == 2
    assert out[1]["score"] == 50


@respx.mock
async def test_mdblist_bulk_non_200_breaks(monkeypatch):
    _enable_key(monkeypatch)
    respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(500, text="server error")
    )
    assert await ratings.mdblist_bulk("movie", [1]) == {}


@respx.mock
async def test_mdblist_bulk_http_error_breaks(monkeypatch):
    _enable_key(monkeypatch)
    respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        side_effect=httpx.ConnectError("boom")
    )
    assert await ratings.mdblist_bulk("movie", [1]) == {}


@respx.mock
async def test_mdblist_bulk_low_ratelimit_warns_still_returns(monkeypatch):
    _enable_key(monkeypatch)
    respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(
            200, headers={"x-ratelimit-remaining": "5"},
            json=[{"ids": {"tmdb": 1}, "score": 42}],
        )
    )
    out = await ratings.mdblist_bulk("movie", [1])
    assert out[1]["score"] == 42


# ---- store_ratings / ratings_for_tmdb ---------------------------------------

async def test_store_ratings_empty_noops(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await ratings.store_ratings("movie", {})
    assert await ratings.ratings_for_tmdb([(1, "movie")]) == {}


async def test_store_and_read_roundtrip(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    items = {
        603: {"ids": {"tmdb": 603, "imdb": "tt0133093"}, "score": 88,
              "ratings": [{"source": "imdb", "score": 8.7, "votes": 100}]},
        1: {"imdb_id": "tt9", "score": None},  # null aggregate -> stored as NULL
    }
    await ratings.store_ratings("movie", items)
    out = await ratings.ratings_for_tmdb([(603, "movie"), (1, "movie")])
    assert out[(603, "movie")]["mdblist_score"] == 88
    assert out[(603, "movie")]["imdb_id"] == "tt0133093"
    assert out[(603, "movie")]["sources"]["imdb"]["score"] == 8.7
    assert out[(1, "movie")]["mdblist_score"] is None
    assert out[(1, "movie")]["imdb_id"] == "tt9"


async def test_ratings_for_tmdb_empty_pairs(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert await ratings.ratings_for_tmdb([]) == {}


async def test_ratings_for_tmdb_bad_json_falls_back_to_empty(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    now = time.time()
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, imdb_id, mdblist_score, "
            "ratings_json, fetched_at) VALUES (?,?,?,?,?,?)",
            (7, "movie", "tt7", 50, "not-json", now))
        await db.commit()
    finally:
        await db.close()
    out = await ratings.ratings_for_tmdb([(7, "movie")])
    assert out[(7, "movie")]["sources"] == {}


async def test_ratings_for_tmdb_chunks_over_500(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    now = time.time()
    db = await _db.get_db()
    try:
        for tid in range(1, 601):  # 600 rows -> exercises the 500-id chunk loop
            await db.execute(
                "INSERT INTO mdblist_ratings (tmdb_id, media_type, imdb_id, "
                "mdblist_score, ratings_json, fetched_at) VALUES (?,?,?,?,?,?)",
                (tid, "movie", f"tt{tid}", tid, "{}", now))
        await db.commit()
    finally:
        await db.close()
    out = await ratings.ratings_for_tmdb([(t, "movie") for t in range(1, 601)])
    assert len(out) == 600


# ---- fetch_and_store_ratings ------------------------------------------------

@respx.mock
async def test_fetch_and_store_ratings_end_to_end(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _enable_key(monkeypatch)
    respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(200, json=[
            {"ids": {"tmdb": 603, "imdb": "tt0133093"}, "score": 88,
             "ratings": [{"source": "imdb", "score": 8.7, "votes": 100}]},
        ])
    )
    items = await ratings.fetch_and_store_ratings("movie", [603])
    assert 603 in items
    cached = await ratings.ratings_for_tmdb([(603, "movie")])
    assert cached[(603, "movie")]["mdblist_score"] == 88


# ---- GET /ratings/config + PUT /ratings/config ------------------------------

async def test_ratings_config_get(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/ratings/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "display_sources" in body and "formula" in body and "display_groups" in body


async def test_ratings_config_put_persists(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    from app.auth import require_admin
    app = _app()
    app.dependency_overrides[require_admin] = lambda: None

    new_cfg = {
        "display_groups": [{"visibility": "always", "sources": ["imdb", "tmdb"]}],
        "formula": {"preset": "custom", "missing_mdblist": "zero"},
    }
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.put("/ratings/config", json=new_cfg)
    assert resp.status_code == 200
    body = resp.json()
    # Normalized: display_sources flattened from the group.
    assert body["display_sources"] == ["imdb", "tmdb"]
    assert body["formula"]["preset"] == "custom"

    # Persisted: reading it back (unauthenticated GET) reflects the change.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        got = await ac.get("/ratings/config")
    assert got.json()["display_sources"] == ["imdb", "tmdb"]


async def test_ratings_config_legacy_flat_display_sources(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # Legacy config: a flat display_sources list (no display_groups) is upgraded
    # into a single "always" group.
    await _db.meta_set("rating_config", json.dumps(
        {"display_sources": ["imdb", "bogus", "tmdb"]}))
    cfg = await ratings.get_rating_config()
    assert cfg["display_sources"] == ["imdb", "tmdb"]  # bogus filtered out
    assert cfg["display_groups"] == [{"visibility": "always", "sources": ["imdb", "tmdb"]}]


async def test_ratings_config_legacy_empty_display_sources(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await _db.meta_set("rating_config", json.dumps({"display_sources": ["bogus"]}))
    cfg = await ratings.get_rating_config()
    assert cfg["display_sources"] == []
    assert cfg["display_groups"] == []


async def test_ratings_config_malformed_json_falls_back_to_default(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # Non-JSON stored value hits the except branch and returns the default cfg.
    await _db.meta_set("rating_config", "}{not json")
    cfg = await ratings.get_rating_config()
    from app.config import DEFAULT_RATING_CONFIG
    assert cfg["display_sources"] == DEFAULT_RATING_CONFIG["display_sources"]


async def test_ratings_config_put_requires_admin(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # No dependency override -> require_admin runs; account not configured -> 401.
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.put("/ratings/config", json={})
    assert resp.status_code == 401


# ---- POST /ratings/batch ----------------------------------------------------

async def test_ratings_batch_returns_cached(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    now = time.time()
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, imdb_id, mdblist_score, "
            "ratings_json, fetched_at) VALUES (?,?,?,?,?,?)",
            (603, "movie", "tt1", 88,
             json.dumps({"mdblist": {"score": 88, "votes": None},
                         "imdb": {"score": 8.7, "votes": 100}}), now))
        await db.commit()
    finally:
        await db.close()

    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/ratings/batch", json={"items": [
            {"tmdb_id": 603, "media_type": "movie"},
            {"tmdb_id": 999, "media_type": "movie"},  # not cached -> skipped
        ]})
    assert resp.status_code == 200
    body = resp.json()
    assert "603" in body["results"]
    assert "999" not in body["results"]
    assert body["results"]["603"]["rating"] == 88.0
    assert "display_sources" in body


async def test_ratings_batch_normalizes_media_type(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    now = time.time()
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, imdb_id, mdblist_score, "
            "ratings_json, fetched_at) VALUES (?,?,?,?,?,?)",
            (5, "show", "tt5", 70, json.dumps({"mdblist": {"score": 70}}), now))
        await db.commit()
    finally:
        await db.close()
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # Anything not "movie" maps to "show".
        resp = await ac.post("/ratings/batch", json={"items": [
            {"tmdb_id": 5, "media_type": "tv"},
        ]})
    body = resp.json()
    assert body["results"]["5"]["media_type"] == "show"


# ---- GET /health ------------------------------------------------------------

async def test_health_counts(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    now = time.time()
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, imdb_id, mdblist_score, "
            "ratings_json, fetched_at) VALUES (?,?,?,?,?,?)",
            (1, "movie", "tt1", 50, "{}", now))
        await db.execute(
            "INSERT INTO popular_items (imdb_id, tmdb_id, title, media_type, rank, "
            "fetched_at) VALUES (?,?,?,?,?,?)", ("tt1", 1, "A", "movie", 1, now))
        await db.commit()
    finally:
        await db.close()

    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "rated_items": 1, "popular_items": 1}


# ---- get_ratings_sync_config ------------------------------------------------

async def test_get_ratings_sync_config_defaults(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    cfg = await ratings.get_ratings_sync_config()
    assert cfg == {"enabled": True, "hour": 3}


async def test_get_ratings_sync_config_override(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await _db.meta_set("ratings_sync", json.dumps(
        {"enabled": False, "hour": 5, "ignored": "x"}))
    cfg = await ratings.get_ratings_sync_config()
    assert cfg == {"enabled": False, "hour": 5}


async def test_get_ratings_sync_config_bad_json(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await _db.meta_set("ratings_sync", "not-json")
    cfg = await ratings.get_ratings_sync_config()
    assert cfg == {"enabled": True, "hour": 3}


# ---- refresh_library_ratings ------------------------------------------------

async def test_refresh_library_ratings_no_key(monkeypatch):
    monkeypatch.setattr(ratings, "MDBLIST_API_KEY", "")
    # Should early-return without touching Plex.
    called = {"plex": False}

    async def _pg(*a, **k):
        called["plex"] = True
        return None
    monkeypatch.setattr(ratings, "plex_get", _pg)
    await ratings.refresh_library_ratings()
    assert called["plex"] is False


async def test_refresh_library_ratings_plex_not_configured(monkeypatch):
    _enable_key(monkeypatch)
    monkeypatch.setattr(ratings, "plex_configured", lambda: False)
    called = {"plex": False}

    async def _pg(*a, **k):
        called["plex"] = True
        return None
    monkeypatch.setattr(ratings, "plex_get", _pg)
    await ratings.refresh_library_ratings()
    assert called["plex"] is False


@respx.mock
async def test_refresh_library_ratings_happy_path(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _enable_key(monkeypatch)
    monkeypatch.setattr(ratings, "plex_configured", lambda: True)

    # Fake Plex responses driven off the request path.
    async def _plex_get(path, params=None, cache_ttl=0):
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": [
                {"key": "1", "type": "movie", "title": "Movies"},
                {"key": "2", "type": "show", "title": "TV"},
                {"key": "3", "type": "artist", "title": "Music"},  # skipped
            ]}}
        if path == "/library/sections/1/all":
            return {"MediaContainer": {"Metadata": [
                {"Guid": [{"id": "tmdb://603"}]},
                {"Guid": [{"id": "imdb://tt0"}]},  # no tmdb -> filtered
            ]}}
        if path == "/library/sections/2/all":
            return {"MediaContainer": {"Metadata": [
                {"Guid": [{"id": "tmdb://1396"}]},
            ]}}
        return None
    monkeypatch.setattr(ratings, "plex_get", _plex_get)

    respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(200, json=[
            {"ids": {"tmdb": 603, "imdb": "tt0133093"}, "score": 88}]))
    respx.post(f"{MDBLIST_BASE}/tmdb/show").mock(
        return_value=httpx.Response(200, json=[
            {"ids": {"tmdb": 1396, "imdb": "tt0903747"}, "score": 95}]))

    await ratings.refresh_library_ratings()

    cached = await ratings.ratings_for_tmdb([(603, "movie"), (1396, "show")])
    assert cached[(603, "movie")]["mdblist_score"] == 88
    assert cached[(1396, "show")]["mdblist_score"] == 95
    assert await _db.meta_get("library_ratings_last_sync") is not None


# ---- schedule_library_sync --------------------------------------------------

def test_schedule_library_sync_no_scheduler(monkeypatch):
    monkeypatch.setattr(scheduler_mod, "scheduler", None)
    # Should return without error when there's no scheduler.
    ratings.schedule_library_sync({"enabled": True, "hour": 3})


def test_schedule_library_sync_adds_job(monkeypatch):
    _enable_key(monkeypatch)
    sched = AsyncIOScheduler()  # not started
    monkeypatch.setattr(scheduler_mod, "scheduler", sched)
    ratings.schedule_library_sync({"enabled": True, "hour": 4})
    job = sched.get_job("library_ratings")
    assert job is not None
    assert job.name == "Library ratings sync"


def test_schedule_library_sync_disabled_removes_job(monkeypatch):
    _enable_key(monkeypatch)
    sched = AsyncIOScheduler()
    monkeypatch.setattr(scheduler_mod, "scheduler", sched)
    # First add it, then disable -> job removed and not re-added.
    ratings.schedule_library_sync({"enabled": True, "hour": 3})
    assert sched.get_job("library_ratings") is not None
    ratings.schedule_library_sync({"enabled": False, "hour": 3})
    assert sched.get_job("library_ratings") is None


def test_schedule_library_sync_no_job_when_key_blank(monkeypatch):
    monkeypatch.setattr(ratings, "MDBLIST_API_KEY", "")
    sched = AsyncIOScheduler()
    monkeypatch.setattr(scheduler_mod, "scheduler", sched)
    ratings.schedule_library_sync({"enabled": True, "hour": 3})
    assert sched.get_job("library_ratings") is None
