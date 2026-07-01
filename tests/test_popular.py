"""Tests for the Discover/popular feed (app/popular.py).

Covers building the feed from mdblist's official popular list, the storage
round-trip, popular_tmdb_ranks, and the /popular endpoint (happy path, missing
key, upstream error, empty, and pattern-validated media_type).

mdblist bulk-ratings calls (via fetch_and_store_ratings) go to
{MDBLIST_BASE}/tmdb/{movie|show}; the official-list call goes to
api.mdblist.com/lists/official/popular/items. Both are mocked via respx.
"""

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
import app.popular as popular
import app.ratings as ratings
from app.config import MDBLIST_BASE

OFFICIAL_URL = "https://api.mdblist.com/lists/official/popular/items"


def _use_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "t.db"))


def _enable_key(monkeypatch):
    monkeypatch.setattr(popular, "MDBLIST_API_KEY", "testkey")
    monkeypatch.setattr(ratings, "MDBLIST_API_KEY", "testkey")


def _app():
    app = FastAPI()
    app.include_router(popular.router)
    return app


# ---- fetch_mdblist_official_list --------------------------------------------

@respx.mock
async def test_fetch_official_list_combined_response(monkeypatch):
    _enable_key(monkeypatch)
    route = respx.get(OFFICIAL_URL).mock(
        return_value=httpx.Response(200, json={
            "movies": [{"tmdb_id": 1, "imdb_id": "tt1"}],
            "shows": [{"tmdb_id": 2, "imdb_id": "tt2"}],
        })
    )
    movies = await popular.fetch_mdblist_official_list("movies")
    shows = await popular.fetch_mdblist_official_list("shows")
    assert movies == [{"tmdb_id": 1, "imdb_id": "tt1"}]
    assert shows == [{"tmdb_id": 2, "imdb_id": "tt2"}]
    assert route.called


@respx.mock
async def test_fetch_official_list_bare_list_response(monkeypatch):
    _enable_key(monkeypatch)
    respx.get(OFFICIAL_URL).mock(
        return_value=httpx.Response(200, json=[{"tmdb_id": 5, "imdb_id": "tt5"}])
    )
    out = await popular.fetch_mdblist_official_list("movies")
    assert out == [{"tmdb_id": 5, "imdb_id": "tt5"}]


@respx.mock
async def test_fetch_official_list_upstream_error_returns_empty(monkeypatch):
    _enable_key(monkeypatch)
    respx.get(OFFICIAL_URL).mock(return_value=httpx.Response(500, text="boom"))
    out = await popular.fetch_mdblist_official_list("movies")
    assert out == []


# ---- refresh_popular_items --------------------------------------------------

@respx.mock
async def test_refresh_popular_items_missing_key_noops(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # MDBLIST_API_KEY is blank by default (conftest doesn't set it).
    monkeypatch.setattr(popular, "MDBLIST_API_KEY", "")
    await popular.refresh_popular_items()
    # Nothing stored.
    ranks = await popular.popular_tmdb_ranks(["movie", "show"])
    assert ranks == {}


@respx.mock
async def test_refresh_popular_items_happy_path(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _enable_key(monkeypatch)

    respx.get(OFFICIAL_URL).mock(
        return_value=httpx.Response(200, json={
            "movies": [
                {"tmdb_id": 603, "imdb_id": "tt0133093", "title": "The Matrix",
                 "release_year": 1999, "poster": "http://img/w200/x.jpg",
                 "description": "Neo", "certification": "R"},
                {"tmdb_id": 604, "ids": {"imdb": "tt0234215"}, "title": "Reloaded",
                 "year": 2003},
                # No imdb id at all -> skipped in storage.
                {"tmdb_id": 999, "title": "No IMDb"},
            ],
            "shows": [
                {"tmdb_id": 1396, "imdb_id": "tt0903747", "title": "Breaking Bad"},
            ],
        })
    )
    # Ratings bulk-fetch for movies and shows.
    movie_route = respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(
        return_value=httpx.Response(200, json=[
            {"ids": {"tmdb": 603, "imdb": "tt0133093"}, "score": 88,
             "ratings": [{"source": "imdb", "score": 8.7, "votes": 100}]},
        ])
    )
    show_route = respx.post(f"{MDBLIST_BASE}/tmdb/show").mock(
        return_value=httpx.Response(200, json=[
            {"ids": {"tmdb": 1396, "imdb": "tt0903747"}, "score": 95,
             "ratings": [{"source": "imdb", "score": 9.5, "votes": 200}]},
        ])
    )

    await popular.refresh_popular_items()

    assert movie_route.called and show_route.called

    # Poster w200 -> w500 rewrite; skipped item not stored.
    ranks = await popular.popular_tmdb_ranks(["movie"])
    assert set(ranks) == {603, 604}
    assert ranks[603] == 1  # first movie -> rank 1

    show_ranks = await popular.popular_tmdb_ranks(["show"])
    assert set(show_ranks) == {1396}

    # Verify stored fields via the endpoint below indirectly; check poster here.
    db = await _db.get_db()
    try:
        cur = await db.execute("SELECT poster_url FROM popular_items WHERE tmdb_id=603")
        row = await cur.fetchone()
        assert row["poster_url"] == "http://img/w500/x.jpg"
    finally:
        await db.close()

    # last-refresh meta was written.
    assert await _db.meta_get("popular_last_refresh") is not None


@respx.mock
async def test_refresh_popular_items_empty_lists(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _enable_key(monkeypatch)
    respx.get(OFFICIAL_URL).mock(
        return_value=httpx.Response(200, json={"movies": [], "shows": []})
    )
    await popular.refresh_popular_items()
    assert await popular.popular_tmdb_ranks(["movie", "show"]) == {}
    assert await _db.meta_get("popular_last_refresh") is not None


@respx.mock
async def test_refresh_popular_items_db_error_is_swallowed(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _enable_key(monkeypatch)
    respx.get(OFFICIAL_URL).mock(
        return_value=httpx.Response(200, json={
            "movies": [{"tmdb_id": 1, "imdb_id": "tt1", "title": "X"}], "shows": []})
    )
    respx.post(f"{MDBLIST_BASE}/tmdb/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{MDBLIST_BASE}/tmdb/show").mock(return_value=httpx.Response(200, json=[]))

    # Wrap get_db so the phase-2 write connection raises on its first execute
    # (the DELETE), exercising the except/log.exception branch. Ratings fetch in
    # phase 1 already used its own connections, so this only hits the rebuild.
    real_get_db = popular.get_db
    state = {"calls": 0}

    async def flaky_get_db():
        state["calls"] += 1
        db = await real_get_db()
        if state["calls"] == 1:  # first get_db() in phase 2 rebuild
            orig_execute = db.execute

            async def boom(*a, **k):
                raise RuntimeError("db kaboom")
            db.execute = boom
            db._orig_execute = orig_execute  # keep ref
        return db

    # Phase 1 (fetch_and_store_ratings) calls get_db inside app.ratings, not
    # app.popular, so patching popular.get_db only affects the phase-2 rebuild.
    monkeypatch.setattr(popular, "get_db", flaky_get_db)

    # Should not raise; the exception is logged and swallowed.
    await popular.refresh_popular_items()
    # meta_set still runs after the finally.
    assert await _db.meta_get("popular_last_refresh") is not None


# ---- popular_tmdb_ranks -----------------------------------------------------

async def test_popular_tmdb_ranks_empty_media_types(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert await popular.popular_tmdb_ranks([]) == {}


async def test_popular_tmdb_ranks_dedups_to_lowest_rank(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    db = await _db.get_db()
    try:
        import time as _t
        now = _t.time()
        # Same tmdb_id appears under both movie (rank 5) and show (rank 2): keep 2.
        await db.execute(
            "INSERT INTO popular_items (imdb_id, tmdb_id, title, media_type, rank, fetched_at) "
            "VALUES (?,?,?,?,?,?)", ("tt1", 42, "A", "movie", 5, now))
        await db.execute(
            "INSERT INTO popular_items (imdb_id, tmdb_id, title, media_type, rank, fetched_at) "
            "VALUES (?,?,?,?,?,?)", ("tt2", 42, "A", "show", 2, now))
        # A row with NULL rank -> treated as 99999.
        await db.execute(
            "INSERT INTO popular_items (imdb_id, tmdb_id, title, media_type, rank, fetched_at) "
            "VALUES (?,?,?,?,?,?)", ("tt3", 7, "B", "movie", None, now))
        await db.commit()
    finally:
        await db.close()

    out = await popular.popular_tmdb_ranks(["movie", "show"])
    assert out[42] == 2
    assert out[7] == 99999


# ---- GET /popular endpoint --------------------------------------------------

@respx.mock
async def test_popular_endpoint_returns_items_with_ratings(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # Seed popular_items + a cached rating row directly.
    import time as _t
    import json as _json
    now = _t.time()
    db = await _db.get_db()
    try:
        await db.execute(
            "INSERT INTO popular_items (imdb_id, tmdb_id, title, year, media_type, "
            "poster_url, description, certification, rank, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("tt1", 603, "The Matrix", 1999, "movie", "p.jpg", "Neo", "R", 1, now))
        await db.execute(
            "INSERT INTO popular_items (imdb_id, tmdb_id, title, year, media_type, "
            "poster_url, description, certification, rank, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("tt2", None, "No TMDB", 2000, "movie", None, None, None, 2, now))
        await db.execute(
            "INSERT INTO mdblist_ratings (tmdb_id, media_type, imdb_id, mdblist_score, "
            "ratings_json, fetched_at) VALUES (?,?,?,?,?,?)",
            (603, "movie", "tt1", 88, _json.dumps({"mdblist": {"score": 88, "votes": None},
                                                   "imdb": {"score": 8.7, "votes": 100}}), now))
        await db.commit()
    finally:
        await db.close()

    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/popular", params={"media_type": "movie"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    first = body["items"][0]
    assert first["tmdb_id"] == 603
    assert first["rating"] == 88.0
    assert first["sources"]["imdb"]["score"] == 8.7
    # The item without a tmdb_id has no ratings.
    second = body["items"][1]
    assert second["tmdb_id"] is None
    assert second["sources"] == {}
    assert second["rating"] is None
    assert "display_sources" in body


async def test_popular_endpoint_empty(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/popular", params={"media_type": "show"})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "count": 0, "display_sources":
                           resp.json()["display_sources"]}


async def test_popular_endpoint_invalid_media_type_422(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/popular", params={"media_type": "banana"})
    assert resp.status_code == 422


async def test_popular_endpoint_limit_out_of_range_422(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/popular", params={"media_type": "movie", "limit": 0})
    assert resp.status_code == 422


# ---- POST /popular/refresh (admin-gated trigger) ----------------------------

async def test_trigger_refresh_schedules_task(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # Key blank so the scheduled refresh_popular_items early-returns harmlessly.
    monkeypatch.setattr(popular, "MDBLIST_API_KEY", "")

    from app.auth import require_admin
    app = _app()
    app.dependency_overrides[require_admin] = lambda: None

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/popular/refresh")
    assert resp.status_code == 200
    assert resp.json() == {"status": "refresh started"}
