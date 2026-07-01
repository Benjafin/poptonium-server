"""Tests for the section-resolution *resolvers* (the non-pure parts).

Covers the async resolvers that hit Plex / plex.tv / the ratings cache:
``map_with_ratings``, ``_resolve_collection``, ``_resolve_filter``,
``_resolve_sessions``, ``_resolve_history``, the plex.tv avatar/account helpers
(``_plextv_avatars``, ``_plex_account_map``), ``_art_logo_for``, and the
top-level ``resolve_section`` dispatcher.

HTTP is either mocked with respx (bare ``@respx.mock``) or the module-level
plex helpers are monkeypatched directly, whichever is cleaner per test. The
ratings cache is isolated by pointing ``app.db.DB_PATH`` at a temp file.
"""

import json
import time

import httpx
import pytest
import respx

import app.db as _db
from app import section_resolve as sr
from app.config import PLEX_URL


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _container(metas):
    """Wrap a Metadata list in the MediaContainer shape plex_get returns."""
    return {"MediaContainer": {"Metadata": list(metas)}}


def _meta(rating_key, title="T", mtype="movie", tmdb=None, **extra):
    m = {"ratingKey": rating_key, "title": title, "type": mtype}
    if tmdb is not None:
        m["Guid"] = [{"id": f"tmdb://{tmdb}"}]
    m.update(extra)
    return m


async def _seed_rating(tmdb_id, media_type, sources):
    """Insert one row into the mdblist_ratings cache."""
    db = await _db.get_db()
    try:
        await db.execute(
            """INSERT OR REPLACE INTO mdblist_ratings
               (tmdb_id, media_type, imdb_id, mdblist_score, ratings_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (int(tmdb_id), media_type, None, None, json.dumps(sources), time.time()),
        )
        await db.commit()
    finally:
        await db.close()


class _Fake:
    """Async stand-in for plex_get that dispatches on the requested path.

    ``routes`` maps a substring -> response dict (or callable(path)->dict).
    Records every path it was called with in ``.calls``.
    """

    def __init__(self, routes, default=None):
        self.routes = routes
        self.default = default
        self.calls = []

    async def __call__(self, path, params=None, cache_ttl=0):
        self.calls.append(path)
        for needle, resp in self.routes.items():
            if needle in path:
                return resp(path) if callable(resp) else resp
        return self.default


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Reset the module-level caches so avatar/art-logo results don't leak."""
    sr._avatar_cache.update(ts=0.0, by_id={}, by_name={})
    sr._artlogo_cache.clear()
    yield
    sr._avatar_cache.update(ts=0.0, by_id={}, by_name={})
    sr._artlogo_cache.clear()


@pytest.fixture
def isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "sr.db"))


# --------------------------------------------------------------------------- #
# map_with_ratings                                                            #
# --------------------------------------------------------------------------- #

async def test_map_with_ratings_attaches_cached_rating(isolate_db, monkeypatch):
    await _seed_rating(100, "movie", {"mdblist": {"score": 82, "votes": None}})
    items = [_meta("1", "Movie", "movie", tmdb=100),
             _meta("2", "Show", "show", tmdb=200)]  # 200 not cached

    out = await sr.map_with_ratings(items)

    assert out[0]["tmdb_id"] == 100
    assert out[0]["rating"] == 82.0
    assert out[0]["sources"]["mdblist"]["score"] == 82
    # Uncached title still maps, just without a rating.
    assert out[1]["tmdb_id"] == 200
    assert out[1]["rating"] is None
    assert out[1]["sources"] == {}


async def test_map_with_ratings_bulk_enriches_cache_misses(isolate_db, monkeypatch):
    # With an API key set, a cache miss is bulk-enriched on the fly, then the
    # cache is re-read so the section works before the nightly sync has run.
    monkeypatch.setattr(sr, "MDBLIST_API_KEY", "key")

    async def fake_fetch(mt, ids):
        # Simulate the fetch populating the cache for the missing title.
        for tid in ids:
            await _seed_rating(tid, mt, {"mdblist": {"score": 77, "votes": None}})

    called = {}

    async def spy(mt, ids):
        called[mt] = list(ids)
        await fake_fetch(mt, ids)

    monkeypatch.setattr(sr, "fetch_and_store_ratings", spy)

    out = await sr.map_with_ratings([_meta("1", "Movie", "movie", tmdb=500)])
    assert called == {"movie": [500]}
    assert out[0]["rating"] == 77.0


async def test_map_with_ratings_no_tmdb_id_maps_without_rating(isolate_db):
    items = [_meta("9", "NoGuid", "movie")]  # no Guid -> no tmdb id
    out = await sr.map_with_ratings(items)
    assert out[0]["rating"] is None
    assert out[0]["sources"] == {}


async def test_map_with_ratings_accepts_explicit_rcfg(isolate_db):
    # Passing rcfg avoids the get_rating_config DB read path; empty items -> [].
    out = await sr.map_with_ratings([], rcfg={"formula": {"preset": "mdblist"}})
    assert out == []


# --------------------------------------------------------------------------- #
# _resolve_collection                                                         #
# --------------------------------------------------------------------------- #

async def test_resolve_collection_empty_keys_returns_empty():
    assert await sr._resolve_collection({}) == []


async def test_resolve_collection_merges_dedupes_and_limits(isolate_db, monkeypatch):
    def route(path):
        if "/metadata/10/children" in path:
            return _container([_meta("a", "A", tmdb=1), _meta("b", "B", tmdb=2)])
        if "/metadata/20/children" in path:
            # 'b' is a duplicate across collections -> deduped by ratingKey.
            return _container([_meta("b", "B", tmdb=2), _meta("c", "C", tmdb=3)])
        return None

    fake = _Fake({"/children": route})
    monkeypatch.setattr(sr, "plex_get", fake)

    out = await sr._resolve_collection({"collection_keys": ["10", "20"], "limit": 2})
    assert [it["rating_key"] for it in out] == ["a", "b"]  # deduped, limited to 2
    # Both collections were queried.
    assert any("/metadata/10/children" in c for c in fake.calls)
    assert any("/metadata/20/children" in c for c in fake.calls)


async def test_resolve_collection_randomize_keeps_all(isolate_db, monkeypatch):
    metas = [_meta(str(i), tmdb=i) for i in range(5)]
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/children": _container(metas)}))
    monkeypatch.setattr(sr.random, "shuffle", lambda x: None)  # deterministic

    out = await sr._resolve_collection({"collection_key": "10", "randomize": True})
    assert {it["rating_key"] for it in out} == {str(i) for i in range(5)}


# --------------------------------------------------------------------------- #
# _resolve_filter                                                             #
# --------------------------------------------------------------------------- #

async def test_resolve_filter_no_libraries_returns_empty():
    assert await sr._resolve_filter({}) == []


async def test_resolve_filter_basic_query_and_limit(isolate_db, monkeypatch):
    metas = [_meta(str(i), f"M{i}", tmdb=i, addedAt=i) for i in range(5)]
    fake = _Fake({"/library/sections/1/all": _container(metas)})
    monkeypatch.setattr(sr, "plex_get", fake)

    out = await sr._resolve_filter({"library_section": "1", "limit": 3,
                                    "media_type": "movie"})
    assert len(out) == 3
    # The query encodes the media type and a plain limit pool (no re-rank).
    q = fake.calls[0]
    assert "type=1" in q
    assert "X-Plex-Container-Size=3" in q


async def test_resolve_filter_tag_and_year_ops_in_query(isolate_db, monkeypatch):
    fake = _Fake({"/library/sections/1/all": _container([])})
    monkeypatch.setattr(sr, "plex_get", fake)

    cfg = {
        "library_section": "1",
        "genres": [{"id": "5", "title": "Action"}],
        "genres_mode": "pool",
        "genre_primary_count": 0,   # disable the primary-genre post-filter
        "released_after_year": 2000,
        "released_before_year": 2020,
        "added_within_days": 7,
    }
    await sr._resolve_filter(cfg)

    q = fake.calls[0]
    assert "genre=5" in q
    assert "year>=2000" in q
    assert "year<=2020" in q
    assert "addedAt>=" in q


async def test_resolve_filter_rating_min_postfilters(isolate_db, monkeypatch):
    await _seed_rating(1, "movie", {"mdblist": {"score": 90, "votes": None}})
    await _seed_rating(2, "movie", {"mdblist": {"score": 40, "votes": None}})
    metas = [_meta("1", "High", tmdb=1), _meta("2", "Low", tmdb=2),
             _meta("3", "None", tmdb=3)]  # no rating -> treated as -1, dropped
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/sections/1/all": _container(metas)}))

    out = await sr._resolve_filter({"library_section": "1", "rating_min": 50})
    assert [it["rating_key"] for it in out] == ["1"]


async def test_resolve_filter_rank_by_rating_sorts_desc(isolate_db, monkeypatch):
    await _seed_rating(1, "movie", {"mdblist": {"score": 50, "votes": None}})
    await _seed_rating(2, "movie", {"mdblist": {"score": 95, "votes": None}})
    await _seed_rating(3, "movie", {"mdblist": {"score": 70, "votes": None}})
    metas = [_meta("1", tmdb=1), _meta("2", tmdb=2), _meta("3", tmdb=3)]
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/sections/1/all": _container(metas)}))

    out = await sr._resolve_filter({"library_section": "1", "sort": "combined:desc"})
    assert [it["rating_key"] for it in out] == ["2", "3", "1"]


async def test_resolve_filter_randomize_shuffles_pool(isolate_db, monkeypatch):
    metas = [_meta(str(i), tmdb=i) for i in range(4)]
    fake = _Fake({"/library/sections/1/all": _container(metas)})
    monkeypatch.setattr(sr, "plex_get", fake)
    # Reverse instead of random so the result is deterministic but "shuffled".
    monkeypatch.setattr(sr.random, "shuffle", lambda x: x.reverse())

    out = await sr._resolve_filter({"library_section": "1", "randomize": True,
                                    "limit": 2, "query_limit": 50})
    assert [it["rating_key"] for it in out] == ["3", "2"]
    assert "X-Plex-Container-Size=50" in fake.calls[0]  # randomize pool


async def test_resolve_filter_trending_intersects_and_orders(isolate_db, monkeypatch):
    metas = [_meta("1", tmdb=10), _meta("2", tmdb=20), _meta("3", tmdb=30)]
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/sections/1/all": _container(metas)}))

    async def fake_ranks(mts):
        # tmdb 20 is more popular (lower rank) than 30; 10 is not popular at all.
        return {20: 2, 30: 5}

    monkeypatch.setattr(sr, "popular_tmdb_ranks", fake_ranks)

    out = await sr._resolve_filter({"library_section": "1", "trending": True})
    # Only the popular titles, ordered by rank.
    assert [it["tmdb_id"] for it in out] == [20, 30]


async def test_resolve_filter_primary_country_postfilter(isolate_db, monkeypatch):
    keep = _meta("1", "Dutch", tmdb=1,
                 Country=[{"tag": "Netherlands"}, {"tag": "Belgium"}])
    drop = _meta("2", "CoProd", tmdb=2,
                 Country=[{"tag": "USA"}, {"tag": "Netherlands"}])  # NL not first
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/sections/1/all": _container([keep, drop])}))

    cfg = {"library_section": "1",
           "countries": [{"id": "9", "title": "Netherlands"}],
           "countries_mode": "pool"}
    out = await sr._resolve_filter(cfg)
    assert [it["rating_key"] for it in out] == ["1"]


async def test_resolve_filter_primary_genre_postfilter(isolate_db, monkeypatch):
    keep = _meta("1", "ComedyFirst", tmdb=1,
                 Genre=[{"tag": "Comedy"}, {"tag": "Drama"}])
    drop = _meta("2", "ComedyLate", tmdb=2,
                 Genre=[{"tag": "Action"}, {"tag": "Thriller"},
                        {"tag": "Sci-Fi"}, {"tag": "Comedy"}])  # 4th genre
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/sections/1/all": _container([keep, drop])}))

    cfg = {"library_section": "1",
           "genres": [{"id": "3", "title": "Comedy"}],
           "genres_mode": "pool",
           "genre_primary_count": 3}
    out = await sr._resolve_filter(cfg)
    assert [it["rating_key"] for it in out] == ["1"]


async def test_resolve_filter_multi_library_sort_nonnumeric_value(isolate_db, monkeypatch):
    # A non-numeric value in a numeric sort field hits the parse-fallback (->0.0)
    # in _sort_metas, exercised via the multi-library merge re-sort.
    def route(path):
        if "/sections/1/all" in path:
            return _container([_meta("a", "A", tmdb=1, year="notayear")])
        if "/sections/2/all" in path:
            return _container([_meta("b", "B", tmdb=2, year=2000)])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"/all": route}))
    out = await sr._resolve_filter({"library_sections": ["1", "2"],
                                    "sort": "year:desc"})
    # 'b' (2000) ranks above 'a' (bad value coerced to 0.0).
    assert [it["rating_key"] for it in out] == ["b", "a"]


async def test_resolve_filter_multi_library_merge_dedupe_sort(isolate_db, monkeypatch):
    def route(path):
        if "/sections/1/all" in path:
            return _container([_meta("a", "A", tmdb=1, addedAt=10),
                               _meta("dup", "Dup", tmdb=2, addedAt=30)])
        if "/sections/2/all" in path:
            return _container([_meta("dup", "Dup", tmdb=2, addedAt=30),
                               _meta("c", "C", tmdb=3, addedAt=20)])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"/all": route}))

    out = await sr._resolve_filter({"library_sections": ["1", "2"],
                                    "sort": "addedAt:desc"})
    # Deduped by ratingKey, then re-sorted across libraries by addedAt desc.
    assert [it["rating_key"] for it in out] == ["dup", "c", "a"]


# --------------------------------------------------------------------------- #
# _resolve_sessions                                                           #
# --------------------------------------------------------------------------- #

async def test_resolve_sessions_empty(monkeypatch):
    monkeypatch.setattr(sr, "plex_get", _Fake({}, default=None))
    assert await sr._resolve_sessions({}) == []


async def test_resolve_sessions_populated_with_episode_and_logo(monkeypatch):
    session_movie = _meta("100", "A Movie", "movie", year=2001,
                          art="/art/movie", thumb="/thumb/movie",
                          User={"title": "Ada", "thumb": "/u/ada"},
                          Player={"title": "TV", "state": "playing"},
                          viewOffset=1000, duration=5000)
    session_ep = {
        "ratingKey": "201", "type": "episode",
        "grandparentRatingKey": "50", "grandparentTitle": "The Show",
        "parentIndex": 2, "index": 5, "title": "Some Episode",
        "grandparentThumb": "/gp/thumb", "grandparentArt": "/gp/art",
        "User": {"title": "Bob", "thumb": "/u/bob"},
        "Player": {"product": "Plex Web", "state": "paused"},
    }

    def route(path):
        if "/status/sessions" in path:
            return _container([session_movie, session_ep])
        if "/library/metadata/100" in path:
            return _container([{"art": "/full/art/100",
                                "Image": [{"type": "clearLogo", "url": "/logo/100"}]}])
        if "/library/metadata/50" in path:
            return _container([{"art": "/full/art/50",
                                "Image": [{"type": "clearLogo", "url": "/logo/50"}]}])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"": route}))

    out = await sr._resolve_sessions({})
    assert len(out) == 2
    mv = next(it for it in out if it["rating_key"] == "100")
    assert mv["title"] == "A Movie"
    assert mv["user_title"] == "Ada"
    assert mv["player"] == "TV"
    assert mv["clear_logo"] == "/logo/100"

    ep = next(it for it in out if it["rating_key"] == "201")
    assert ep["title"] == "The Show"          # grandparent title
    assert ep["episode_label"] == "S2·E5 · Some Episode"
    assert ep["player"] == "Plex Web"          # falls back to product
    assert ep["clear_logo"] == "/logo/50"      # grandparent logo


async def test_resolve_sessions_dedupes_same_user_and_item_and_limits(monkeypatch):
    a = _meta("1", "X", "movie", User={"title": "Ada"}, Player={"title": "P"})
    a_dup = _meta("1", "X", "movie", User={"title": "Ada"}, Player={"title": "P"})
    b = _meta("2", "Y", "movie", User={"title": "Bob"}, Player={"title": "P"})

    def route(path):
        if "/status/sessions" in path:
            return _container([a, a_dup, b])
        if "/library/metadata/" in path:
            return _container([{"art": None, "Image": []}])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"": route}))

    out = await sr._resolve_sessions({"limit": 1})
    assert len(out) == 1                        # limit applied after dedupe
    assert out[0]["rating_key"] == "1"


# --------------------------------------------------------------------------- #
# _plextv_avatars / _plex_account_map                                         #
# --------------------------------------------------------------------------- #

@respx.mock
async def test_plextv_avatars_from_home_and_friends():
    respx.get("https://plex.tv/api/v2/home/users").mock(
        return_value=httpx.Response(200, json={"users": [
            {"id": 1, "username": "owner", "title": "Owner", "thumb": "/av/1"},
            {"id": 2, "title": "NoThumb"},  # no thumb -> skipped
        ]}))
    respx.get("https://plex.tv/api/v2/friends").mock(
        return_value=httpx.Response(200, json=[
            {"id": 3, "username": "friend", "thumb": "/av/3"},
        ]))

    by_id, by_name = await sr._plextv_avatars()
    assert by_id == {"1": "/av/1", "3": "/av/3"}
    assert by_name["owner"] == "/av/1"
    assert by_name["friend"] == "/av/3"


@respx.mock
async def test_plextv_avatars_cached_after_first_call():
    home = respx.get("https://plex.tv/api/v2/home/users").mock(
        return_value=httpx.Response(200, json={"users": [
            {"id": 1, "title": "A", "thumb": "/av/1"}]}))
    respx.get("https://plex.tv/api/v2/friends").mock(
        return_value=httpx.Response(200, json=[]))

    await sr._plextv_avatars()
    await sr._plextv_avatars()  # served from cache, no second HTTP call
    assert home.call_count == 1


@respx.mock
async def test_plextv_avatars_handles_http_error():
    respx.get("https://plex.tv/api/v2/home/users").mock(side_effect=httpx.ConnectError("boom"))
    # Exception is caught; returns empty maps.
    by_id, by_name = await sr._plextv_avatars()
    assert by_id == {} and by_name == {}


async def test_plex_account_map_bridges_avatars(monkeypatch):
    monkeypatch.setattr(sr, "plex_get", _Fake({"/accounts": {
        "MediaContainer": {"Account": [
            {"id": 1, "name": "Ada", "thumb": "/local/ada"},   # local thumb wins
            {"id": 2, "name": "Bob"},                           # by name avatar
            {"id": 3, "name": "Cy"},                            # by id avatar
        ]}}}))

    async def fake_avatars():
        return {"3": "/av/cy"}, {"bob": "/av/bob"}

    monkeypatch.setattr(sr, "_plextv_avatars", fake_avatars)

    out = await sr._plex_account_map()
    assert out["1"]["thumb"] == "/local/ada"
    assert out["2"]["thumb"] == "/av/bob"
    assert out["3"]["thumb"] == "/av/cy"


# --------------------------------------------------------------------------- #
# _art_logo_for                                                               #
# --------------------------------------------------------------------------- #

async def test_art_logo_for_empty_rk():
    assert await sr._art_logo_for("") == {"art": None, "logo": None}


async def test_art_logo_for_clears_cache_when_full(monkeypatch):
    # Pre-fill the cache past its 1024 cap; the next store triggers a full clear.
    sr._artlogo_cache.update({str(i): (time.time() + 999, {"art": None, "logo": None})
                              for i in range(1100)})
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/metadata/new": _container(
                            [{"art": "/a", "Image": []}])}))
    out = await sr._art_logo_for("new")
    assert out["art"] == "/a"
    assert sr._artlogo_cache == {"new": sr._artlogo_cache["new"]}  # cleared, only 'new'


async def test_art_logo_for_fetches_and_caches(monkeypatch):
    fake = _Fake({"/library/metadata/77": _container([
        {"art": "/art/77", "Image": [{"type": "clearLogo", "url": "/logo/77"}]}])})
    monkeypatch.setattr(sr, "plex_get", fake)

    out = await sr._art_logo_for("77")
    assert out == {"art": "/art/77", "logo": "/logo/77"}

    # Second call served from cache (no extra plex_get).
    out2 = await sr._art_logo_for("77")
    assert out2 == out
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- #
# _resolve_history                                                            #
# --------------------------------------------------------------------------- #

async def test_resolve_history_empty(monkeypatch):
    monkeypatch.setattr(sr, "plex_get", _Fake({}, default=None))
    assert await sr._resolve_history({}) == []


async def test_resolve_history_collapses_episodes_and_labels_watchers(monkeypatch):
    ep1 = {"type": "episode", "ratingKey": "e1", "grandparentKey": "/library/metadata/50",
           "grandparentTitle": "The Show", "grandparentArt": "/gp/art",
           "thumb": "/ep1/thumb", "accountID": 1, "summary": "s"}
    ep2 = {"type": "episode", "ratingKey": "e2", "grandparentKey": "/library/metadata/50",
           "grandparentTitle": "The Show", "thumb": "/ep2/thumb", "accountID": 2}
    movie = {"type": "movie", "ratingKey": "900", "title": "A Film",
             "year": 2010, "art": "/m/art", "thumb": "/m/thumb", "accountID": 1}

    def route(path):
        if "/status/sessions/history/all" in path:
            return _container([ep1, ep2, movie])
        if "/accounts" in path:
            return {"MediaContainer": {"Account": [
                {"id": 1, "name": "Ada", "thumb": "/av/ada"},
                {"id": 2, "name": "Bob", "thumb": "/av/bob"}]}}
        if "/library/metadata/50" in path:
            return _container([{"art": "/full/art/50",
                                "Image": [{"type": "clearLogo", "url": "/logo/50"}]}])
        if "/library/metadata/900" in path:
            return _container([{"art": "/full/art/900",
                                "Image": [{"type": "clearLogo", "url": "/logo/900"}]}])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"": route}))
    monkeypatch.setattr(sr, "_plextv_avatars", _no_avatars)

    out = await sr._resolve_history({"limit": 10})
    show = next(it for it in out if it["rating_key"] == "50")
    assert show["title"] == "The Show"
    assert show["type"] == "show"
    # Both accounts that watched an episode are collected as watchers.
    assert {w["title"] for w in show["watchers"]} == {"Ada", "Bob"}
    assert show["art"] == "/full/art/50"      # enriched clean backdrop
    assert show["clear_logo"] == "/logo/50"

    film = next(it for it in out if it["rating_key"] == "900")
    assert film["type"] == "movie"
    assert film["year"] == 2010
    assert film["clear_logo"] == "/logo/900"


async def test_resolve_history_skips_items_without_content_rk(monkeypatch):
    # An item with no ratingKey (and, being a movie, no grandparentKey) yields an
    # empty content_rk and is skipped.
    def route(path):
        if "/status/sessions/history/all" in path:
            return _container([{"type": "movie", "title": "Ghost", "accountID": 1},
                               {"type": "movie", "ratingKey": "5", "title": "Real",
                                "accountID": 1}])
        if "/accounts" in path:
            return {"MediaContainer": {"Account": []}}
        if "/library/metadata/" in path:
            return _container([{"art": None, "Image": []}])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"": route}))
    monkeypatch.setattr(sr, "_plextv_avatars", _no_avatars)
    out = await sr._resolve_history({})
    assert [it["rating_key"] for it in out] == ["5"]


async def test_resolve_history_type_filter_and_randomize(monkeypatch):
    movie = {"type": "movie", "ratingKey": "1", "title": "M", "accountID": 1}
    fake = _Fake({}, default=None)

    def route(path):
        if "/status/sessions/history/all" in path:
            fake.last_history = path
            return _container([movie])
        if "/accounts" in path:
            return {"MediaContainer": {"Account": []}}
        if "/library/metadata/" in path:
            return _container([{"art": None, "Image": []}])
        return None

    fake.routes = {"": route}
    monkeypatch.setattr(sr, "plex_get", fake)
    monkeypatch.setattr(sr, "_plextv_avatars", _no_avatars)
    monkeypatch.setattr(sr.random, "shuffle", lambda x: None)

    out = await sr._resolve_history({"media_type": "movie", "randomize": True})
    assert "type=1" in fake.last_history       # movie -> Plex type 1
    assert out[0]["rating_key"] == "1"


async def _no_avatars():
    return {}, {}


# --------------------------------------------------------------------------- #
# resolve_section dispatcher                                                  #
# --------------------------------------------------------------------------- #

def _row(**over):
    row = {"id": 1, "title": "T", "subtitle": None, "type": "filter",
           "style": "row", "position": "top", "sort_order": 0, "config": "{}"}
    row.update(over)
    return row


async def test_resolve_section_filter_applies_title_template(isolate_db, monkeypatch):
    metas = [_meta("1", tmdb=1)]
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/library/sections/1/all": _container(metas)}))

    cfg = {"library_section": "1",
           "genres": [{"id": "9", "title": "Sci-Fi"}], "genres_mode": "pool",
           "genre_primary_count": 0}
    row = _row(type="filter", title="Best {genre}", subtitle="{genre} picks",
               config=json.dumps(cfg))

    out = await sr.resolve_section(row)
    assert out["title"] == "Best Sci-Fi"
    assert out["subtitle"] == "Sci-Fi picks"
    assert out["type"] == "filter"
    assert out["min_app_version"] == "1.0.0"
    assert [it["rating_key"] for it in out["items"]] == ["1"]


async def test_resolve_section_plex_collection(isolate_db, monkeypatch):
    monkeypatch.setattr(sr, "plex_get",
                        _Fake({"/children": _container([_meta("a", tmdb=1)])}))
    row = _row(type="plex_collection",
               config=json.dumps({"collection_key": "10"}))
    out = await sr.resolve_section(row)
    assert out["type"] == "plex_collection"
    assert [it["rating_key"] for it in out["items"]] == ["a"]


async def test_resolve_section_sessions(monkeypatch):
    def route(path):
        if "/status/sessions" in path:
            return _container([_meta("1", "X", "movie",
                                     User={"title": "Ada"}, Player={"title": "P"})])
        if "/library/metadata/" in path:
            return _container([{"art": None, "Image": []}])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"": route}))
    out = await sr.resolve_section(_row(type="sessions"))
    assert out["type"] == "sessions"
    assert out["items"][0]["user_title"] == "Ada"


async def test_resolve_section_history(monkeypatch):
    def route(path):
        if "/status/sessions/history/all" in path:
            return _container([{"type": "movie", "ratingKey": "1",
                                "title": "M", "accountID": 1}])
        if "/accounts" in path:
            return {"MediaContainer": {"Account": []}}
        if "/library/metadata/" in path:
            return _container([{"art": None, "Image": []}])
        return None

    monkeypatch.setattr(sr, "plex_get", _Fake({"": route}))
    monkeypatch.setattr(sr, "_plextv_avatars", _no_avatars)
    out = await sr.resolve_section(_row(type="history"))
    assert out["type"] == "history"
    assert out["items"][0]["rating_key"] == "1"


async def test_resolve_section_unknown_type_and_bad_config():
    # Bad JSON config -> {} ; unknown type -> no items.
    row = _row(type="mystery", config="not json{")
    out = await sr.resolve_section(row)
    assert out["items"] == []
    assert out["type"] == "mystery"
