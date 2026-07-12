"""Tests for the starter-sections template + seed API (app/section_templates.py,
app/admin.py seed endpoints).

respx mocks the Plex library/section + tag-directory listings; the template is then
adapted to that fake server. We assert which sections survive, how their libraries
and tags resolve, the MDbList gating, and the seed endpoint's create/409 behaviour.
"""

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.admin as admin
import app.db as _db
from app import section_templates
from app.auth import require_admin
from app.config import settings

PLEX = "http://plex.test"  # == settings.PLEX_URL (conftest env)


def _dir(*pairs):
    return {"MediaContainer": {"Directory": [{"key": k, "title": t} for k, t in pairs]}}


def _mock_server(movie=True, show=True):
    """Register a fake Plex server: a movie library (key 1) and/or a show library
    (key 2), each with tag directories rich enough to match the template."""
    libs = []
    if movie:
        libs.append({"key": "1", "title": "Movies", "type": "movie"})
    if show:
        libs.append({"key": "2", "title": "TV", "type": "show"})
    respx.get(f"{PLEX}/library/sections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Directory": libs}}))

    genres = _dir(("g1", "Comedy"), ("g2", "Crime"), ("g3", "Action"), ("g4", "Thriller"),
                  ("g5", "Documentary"), ("g6", "Family"), ("g7", "Science Fiction"),
                  ("g8", "Fantasy"), ("g9", "Horror"), ("g10", "War"))
    directors = _dir(("20", "Christopher Nolan"), ("21", "Steven Spielberg"),
                     ("22", "Quentin Tarantino"))
    actors = _dir(("30", "Leonardo DiCaprio"), ("31", "Robert De Niro"), ("32", "Bill Murray"))
    for key in ("1", "2"):
        respx.get(f"{PLEX}/library/sections/{key}/genre").mock(return_value=httpx.Response(200, json=genres))
        respx.get(f"{PLEX}/library/sections/{key}/director").mock(return_value=httpx.Response(200, json=directors))
        respx.get(f"{PLEX}/library/sections/{key}/actor").mock(return_value=httpx.Response(200, json=actors))


def _by_title(items):
    return {i["title"]: i for i in items}


# ---------------------------------------------------------------------------
# build_starter_plan
# ---------------------------------------------------------------------------

@respx.mock
async def test_full_server_plan():
    _mock_server(movie=True, show=True)   # MDbList unset (conftest) -> Trending skips
    plan = await section_templates.build_starter_plan()
    planned = _by_title(plan["planned"])
    skipped = _by_title(plan["skipped"])

    # Universal + adaptable sections survive; Trending needs MDbList; Recommendations
    # needs a chosen collection.
    assert "Who's Watching" in planned
    assert "Movie Spotlight" in planned
    assert "Director Spotlight" in planned
    assert "Trending" in skipped and "MDbList" in skipped["Trending"]["reason"]
    assert "Recommendations" in skipped

    # sort_order is sequential over the surviving sections, in template order.
    orders = [p["payload"]["sort_order"] for p in plan["planned"]]
    assert orders == list(range(len(plan["planned"])))

    # Positions are preserved from the template.
    assert planned["Movie Spotlight"]["position"] == "after_all"
    assert planned["Director Spotlight"]["position"] == "after_movies"


@respx.mock
async def test_libraries_and_tags_resolve():
    _mock_server(movie=True, show=True)
    plan = await section_templates.build_starter_plan()
    planned = _by_title(plan["planned"])

    # A movie-only section resolves to the movie library key.
    assert planned["Movie Spotlight"]["payload"]["config"]["library_sections"] == ["1"]
    # A movie+show section spans both.
    assert planned["{genre} night"]["payload"]["config"]["library_sections"] == ["1", "2"]

    # Director titles resolve to this server's tag IDs (unmatched template names dropped).
    directors = planned["Director Spotlight"]["payload"]["config"]["directors"]
    assert len(directors) >= 2
    assert all(set(d) == {"id", "title"} for d in directors)
    assert {"id": "20", "title": "Christopher Nolan"} in directors


@respx.mock
async def test_show_only_skips_movie_sections():
    _mock_server(movie=False, show=True)
    plan = await section_templates.build_starter_plan()
    skipped = _by_title(plan["skipped"])
    assert "Movie Spotlight" in skipped and "movie" in skipped["Movie Spotlight"]["reason"]
    assert "Director Spotlight" in skipped
    # A show section still lands.
    assert "TV Spotlight" in _by_title(plan["planned"])


@respx.mock
async def test_movie_only_skips_tv_sections():
    _mock_server(movie=True, show=False)
    plan = await section_templates.build_starter_plan()
    skipped = _by_title(plan["skipped"])
    assert "TV Spotlight" in skipped and "TV" in skipped["TV Spotlight"]["reason"]
    assert "Recently Added {library}" in skipped
    assert "Movie Spotlight" in _by_title(plan["planned"])


@respx.mock
async def test_insufficient_directors_skips_section():
    _mock_server(movie=True, show=True)
    # Override the movie director directory with a single template match.
    respx.get(f"{PLEX}/library/sections/1/director").mock(
        return_value=httpx.Response(200, json=_dir(("20", "Christopher Nolan"))))
    plan = await section_templates.build_starter_plan()
    skipped = _by_title(plan["skipped"])
    assert "Director Spotlight" in skipped
    assert "directors" in skipped["Director Spotlight"]["reason"]


@respx.mock
async def test_mdblist_gating(monkeypatch):
    _mock_server(movie=True, show=True)
    # Without MDbList: Trending skipped, rating_min dropped from spotlights.
    plan = await section_templates.build_starter_plan()
    planned = _by_title(plan["planned"])
    assert "Trending" in _by_title(plan["skipped"])
    assert planned["Movie Spotlight"]["payload"]["config"]["rating_min"] is None

    # With MDbList: Trending survives and rating_min is kept.
    monkeypatch.setitem(settings._values, "MDBLIST_API_KEY", "key")
    plan2 = await section_templates.build_starter_plan()
    planned2 = _by_title(plan2["planned"])
    assert "Trending" in planned2
    assert planned2["Movie Spotlight"]["payload"]["config"]["rating_min"] == 75


@respx.mock
async def test_collection_picker_single_and_multi():
    _mock_server(movie=True, show=True)
    # No collection -> Recommendations skipped.
    plan = await section_templates.build_starter_plan()
    assert "Recommendations" in _by_title(plan["skipped"])
    # A single collection (dict) -> Recommendations planned, key carried.
    plan2 = await section_templates.build_starter_plan(collections={"key": "71", "title": "Faves"})
    rec = _by_title(plan2["planned"]).get("Recommendations")
    assert rec is not None
    assert rec["payload"]["config"]["collection_keys"] == [{"key": "71", "title": "Faves"}]
    # Multiple collections merge into one shelf's collection_keys.
    plan3 = await section_templates.build_starter_plan(
        collections=[{"key": "71", "title": "Films faves"}, {"key": "61", "title": "TV faves"}])
    rec3 = _by_title(plan3["planned"])["Recommendations"]
    assert rec3["payload"]["config"]["collection_keys"] == [
        {"key": "71", "title": "Films faves"}, {"key": "61", "title": "TV faves"}]


@respx.mock
async def test_preview_collections_labelled_by_library():
    _mock_server(movie=True, show=True)
    respx.get(f"{PLEX}/library/sections/1/collections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": 71, "title": "Recommendations", "childCount": 40}]}}))
    respx.get(f"{PLEX}/library/sections/2/collections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": 61, "title": "Recommendations", "childCount": 31}]}}))
    preview = await section_templates.build_seed_preview()
    cols = {c["key"]: c for c in preview["collections"]}
    # Same-named collections are kept distinct and tagged with their library.
    assert cols["71"]["library"] == "Movies" and cols["71"]["library_type"] == "movie"
    assert cols["61"]["library"] == "TV" and cols["61"]["library_type"] == "show"


@respx.mock
async def test_preview_includes_collections():
    _mock_server(movie=True, show=True)
    respx.get(f"{PLEX}/library/sections/1/collections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": 71, "title": "Ben's Faves", "childCount": 12}]}}))
    respx.get(f"{PLEX}/library/sections/2/collections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": []}}))
    preview = await section_templates.build_seed_preview()
    assert any(c["title"] == "Ben's Faves" for c in preview["collections"])
    assert "planned" in preview and "skipped" in preview


# ---------------------------------------------------------------------------
# seed endpoint (create + 409)
# ---------------------------------------------------------------------------

def _app():
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@respx.mock
async def test_seed_creates_then_409(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "seed.db"))
    _mock_server(movie=True, show=True)

    async with _client(_app()) as ac:
        first = await ac.post("/admin/sections/seed", json={})
        assert first.status_code == 200
        body = first.json()
        assert len(body["created"]) > 0
        # Every created row round-trips through the section dict shape.
        assert all("min_app_version" in s for s in body["created"])

        second = await ac.post("/admin/sections/seed", json={})
        assert second.status_code == 409
