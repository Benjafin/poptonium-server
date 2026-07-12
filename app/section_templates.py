"""Starter-sections template and its per-server adaptation.

A brand-new instance has an empty Library board. The setup wizard offers to seed a
curated set of sections modeled on a real, well-liked layout. The template can't be
copied verbatim — its library keys and genre/director/actor tag IDs are specific to
the server it came from — so each section is re-resolved against *this* server:

* libraries are expressed as role tokens (``movie`` / ``show``) and mapped to the
  actual library keys of that type; a section with no usable library is skipped;
* tag dimensions are stored by **title** and matched to this server's tag IDs; a
  section that can't match enough tags is skipped;
* MDbList-dependent sections are skipped (Trending) or degraded (``rating_min``
  dropped) when no MDbList key is configured.

``build_starter_plan`` produces the (planned, skipped) preview; ``seed_starter_sections``
inserts the planned sections. The HTTP surface lives in ``admin``.
"""

import json
import time

from .config import settings
from .db import get_db
from .plex import plex_get
from .sections import _section_to_dict

# Default config for a filter section — the template overrides only what differs,
# so the stored config matches exactly what the section editor would produce.
_FILTER_DEFAULTS = {
    "randomize": False,
    "library_sections": [],
    "media_type": None,
    "sort": "addedAt:desc",
    "limit": 30,
    "query_limit": 100,
    "rating_min": None,
    "added_within_days": None,
    "released_after_year": None,
    "released_before_year": None,
    "genres": [], "genres_mode": "pool", "genres_pick": 1,
    "directors": [], "directors_mode": "pool", "directors_pick": 1,
    "actors": [], "actors_mode": "pool", "actors_pick": 1,
    "countries": [], "countries_mode": "pool", "countries_pick": 1,
    "trending": False,
}

# Plex secondary-directory name for each tag dimension (for title→id resolution).
_DIM_DIR = {"genres": "genre", "directors": "director",
            "actors": "actor", "countries": "country"}

# The starter layout, in display order. `libs` are role tokens; tag dimensions
# hold plain titles (resolved to this server's IDs at build time).
STARTER_TEMPLATE = [
    {
        "title": "Who's Watching", "subtitle": None,
        "type": "sessions", "style": "row", "position": "after_all",
        "libs": [], "config": {"randomize": False, "limit": 0},
    },
    {
        "title": "Movie Spotlight", "subtitle": "Recently added, highly rated",
        "type": "filter", "style": "hero", "position": "after_all",
        "libs": ["movie"],
        "drop_rating_min_without_mdblist": True,
        "config": {"sort": "addedAt:desc", "limit": 30, "rating_min": 75,
                   "added_within_days": 21},
    },
    {
        "title": "Trending", "subtitle": None,
        "type": "filter", "style": "row", "position": "after_all",
        "libs": ["movie", "show"], "requires_mdblist": True,
        "config": {"sort": "addedAt:desc", "limit": 20, "trending": True},
    },
    {
        "title": "Recently Watched", "subtitle": None,
        "type": "history", "style": "row", "position": "after_all",
        "libs": [], "config": {"media_type": None, "sort": "viewedAt:desc", "limit": 20},
    },
    {
        "title": "Director Spotlight", "subtitle": "{director}",
        "type": "filter", "style": "hero", "position": "after_movies",
        "libs": ["movie"], "min_tags": 2,
        "config": {"randomize": True, "sort": "addedAt:desc", "limit": 5,
                   "directors_mode": "random", "directors_pick": 1,
                   "directors": ["Wes Anderson", "Steven Spielberg", "Stanley Kubrick",
                                 "Yorgos Lanthimos", "Christopher Nolan", "Martin Scorsese",
                                 "Quentin Tarantino", "Ari Aster", "Ethan Coen", "Joel Coen"]},
    },
    {
        "title": "Recently Added {library}", "subtitle": None,
        "type": "filter", "style": "row", "position": "after_shows",
        "libs": ["show"],
        "config": {"sort": "episodeAddedAt:desc", "limit": 20, "episode_items": True},
    },
    {
        "title": "TV Spotlight", "subtitle": "Recently added, highly rated",
        "type": "filter", "style": "hero", "position": "after_shows",
        "libs": ["show"], "drop_rating_min_without_mdblist": True,
        "config": {"media_type": "show", "sort": "addedAt:desc", "limit": 5,
                   "rating_min": 72, "added_within_days": 21},
    },
    {
        "title": "{genre} night", "subtitle": None,
        "type": "filter", "style": "bento", "position": "after_shows",
        "libs": ["movie", "show"], "min_tags": 2,
        "config": {"randomize": True, "sort": "rating:desc", "limit": 5, "query_limit": 101,
                   "genres_mode": "random", "genres_pick": 1,
                   "genres": ["Comedy", "Crime", "Fantasy", "Documentary", "Horror", "War"]},
    },
    {
        "title": "Recommendations", "subtitle": None,
        "type": "plex_collection", "style": "hero", "position": "after_shows",
        "libs": [], "collection_picker": True,
        "config": {"randomize": True, "limit": 5},
    },
    {
        "title": "{actor}'s in this", "subtitle": None,
        "type": "filter", "style": "hero", "position": "after_shows",
        "libs": ["movie", "show"], "min_tags": 2,
        "config": {"randomize": True, "sort": "rating:desc", "limit": 5, "query_limit": 101,
                   "actors_mode": "random", "actors_pick": 1,
                   "actors": ["Leonardo DiCaprio", "Colin Farrell", "Robert De Niro",
                              "Ryan Gosling", "Bill Murray", "Rebecca Ferguson", "Christian Bale",
                              "Michael Cera", "Nicolas Cage", "Steve Carell", "Sam Rockwell",
                              "Bill Hader", "Joaquin Phoenix", "Florence Pugh", "Jesse Plemons"]},
    },
    {
        "title": "{genre}", "subtitle": None,
        "type": "filter", "style": "bento", "position": "after_shows",
        "libs": ["movie", "show"], "min_tags": 2,
        "config": {"randomize": True, "sort": "rating:desc", "limit": 5, "query_limit": 101,
                   "genres_mode": "random", "genres_pick": 1,
                   "genres": ["Action", "Thriller", "Documentary", "Family", "Science Fiction"]},
    },
]

_ROLE_LABEL = {"movie": "movie", "show": "TV"}


def _role_label(roles: list[str]) -> str:
    return " or ".join(_ROLE_LABEL.get(r, r) for r in roles) or "media"


async def _libraries_by_role() -> dict:
    """This server's library keys bucketed by kind: {"movie": [...], "show": [...]}."""
    data = await plex_get("/library/sections")
    out = {"movie": [], "show": []}
    for d in (data or {}).get("MediaContainer", {}).get("Directory", []):
        t, key = d.get("type"), d.get("key")
        if key is not None and t in out:
            out[t].append(str(key))
    return out


async def _resolve_tags(dim: str, titles: list, lib_keys: list[str], tag_cache: dict) -> list:
    """Match template tag titles to this server's tag IDs for a dimension, across the
    section's libraries. Returns [{id, title}] for the ones that exist (display title
    kept from the template); unmatched titles are dropped. Per-(library,dim) directory
    listings are memoized in ``tag_cache`` so a build scans each at most once."""
    dirname = _DIM_DIR[dim]
    title_to_id: dict = {}
    for lib in lib_keys:
        ck = (lib, dim)
        if ck not in tag_cache:
            data = await plex_get(f"/library/sections/{lib}/{dirname}")
            m = {}
            for d in (data or {}).get("MediaContainer", {}).get("Directory", []):
                t = (d.get("title") or "").strip().lower()
                if t and d.get("key") is not None:
                    m[t] = str(d.get("key"))
            tag_cache[ck] = m
        for t, i in tag_cache[ck].items():
            title_to_id.setdefault(t, i)
    out = []
    for title in titles:
        i = title_to_id.get(str(title).strip().lower())
        if i:
            out.append({"id": i, "title": str(title)})
    return out


async def _all_collections() -> list:
    """Every Plex collection across this server's movie + show libraries, each tagged
    with its library name/type so identically-named collections (e.g. a Films and a TV
    'Recommendations') are distinguishable in the picker."""
    data = await plex_get("/library/sections")
    out, seen = [], set()
    for d in (data or {}).get("MediaContainer", {}).get("Directory", []):
        key, ltype = d.get("key"), d.get("type")
        if key is None or ltype not in ("movie", "show"):
            continue
        lib_title = d.get("title") or ""
        cdata = await plex_get(f"/library/sections/{key}/collections")
        for m in (cdata or {}).get("MediaContainer", {}).get("Metadata", []):
            k = str(m.get("ratingKey"))
            if k in seen:
                continue
            seen.add(k)
            out.append({"key": k, "title": m.get("title"), "count": m.get("childCount"),
                        "library": lib_title, "library_type": ltype})
    return out


def _payload(tpl: dict, cfg: dict) -> dict:
    return {
        "title": tpl["title"],
        "subtitle": tpl.get("subtitle"),
        "type": tpl["type"],
        "style": tpl["style"],
        "position": tpl["position"],
        "enabled": True,
        "config": cfg,
    }


def _norm_collections(collections) -> list:
    """Normalise the picked collections to [{key, title}]; accepts a single dict or a
    list. One or many collections can back a Recommendations shelf (the resolver
    merges their children), which is how a 'Films + TV' recommendations row works."""
    if not collections:
        return []
    if isinstance(collections, dict):
        collections = [collections]
    out = []
    for c in collections:
        k = c.get("key")
        if k not in (None, ""):
            out.append({"key": str(k), "title": c.get("title") or ""})
    return out


async def _resolve_template_section(tpl: dict, libs_by_role: dict,
                                    tag_cache: dict, collections) -> dict:
    """Adapt one template section to this server. Returns {"payload": ...} to keep it
    or {"skip": True, "reason": ...} to drop it."""
    ttype = tpl["type"]

    # Session/history sections read global Plex endpoints — always portable.
    if ttype in ("sessions", "history"):
        return {"payload": _payload(tpl, dict(tpl["config"]))}

    # Collection section — only if the admin picked one or more collections to back it.
    if ttype == "plex_collection":
        cols = _norm_collections(collections)
        if not cols:
            return {"skip": True, "reason": "no collection chosen"}
        cfg = dict(tpl["config"])
        cfg["collection_keys"] = cols
        return {"payload": _payload(tpl, cfg)}

    # Filter section.
    cfg = {**_FILTER_DEFAULTS, **tpl["config"]}

    lib_keys: list[str] = []
    for role in tpl["libs"]:
        lib_keys.extend(libs_by_role.get(role, []))
    lib_keys = list(dict.fromkeys(lib_keys))  # dedupe, keep order
    if not lib_keys:
        return {"skip": True, "reason": f"no {_role_label(tpl['libs'])} library"}
    cfg["library_sections"] = lib_keys

    has_mdblist = bool(settings.MDBLIST_API_KEY)
    if tpl.get("requires_mdblist") and not has_mdblist:
        return {"skip": True, "reason": "requires an MDbList API key"}
    if tpl.get("drop_rating_min_without_mdblist") and not has_mdblist:
        cfg["rating_min"] = None

    min_tags = tpl.get("min_tags", 1)
    for dim in ("genres", "directors", "actors", "countries"):
        titles = tpl["config"].get(dim)
        if not titles:
            continue
        resolved = await _resolve_tags(dim, titles, lib_keys, tag_cache)
        if len(resolved) < min_tags:
            return {"skip": True,
                    "reason": f"fewer than {min_tags} {dim} matched your library"}
        cfg[dim] = resolved

    return {"payload": _payload(tpl, cfg)}


async def build_starter_plan(collections=None) -> dict:
    """Dry run: which starter sections would be created (adapted to this server) and
    which are skipped and why. ``collections`` = a {key,title} (or list of them) to
    back the Recommendations shelf."""
    libs_by_role = await _libraries_by_role()
    tag_cache: dict = {}
    planned, skipped = [], []
    order = 0
    for tpl in STARTER_TEMPLATE:
        res = await _resolve_template_section(tpl, libs_by_role, tag_cache, collections)
        if res.get("skip"):
            skipped.append({"title": tpl["title"], "reason": res["reason"]})
            continue
        payload = res["payload"]
        payload["sort_order"] = order
        order += 1
        planned.append({"title": payload["title"], "type": payload["type"],
                        "style": payload["style"], "position": payload["position"],
                        "payload": payload})
    return {"planned": planned, "skipped": skipped}


async def build_seed_preview(collections=None) -> dict:
    """The wizard preview: the plan plus the collection choices for the picker."""
    plan = await build_starter_plan(collections)
    return {**plan, "collections": await _all_collections()}


async def seed_starter_sections(collections=None) -> dict:
    """Insert the planned starter sections. Returns {created, skipped}."""
    plan = await build_starter_plan(collections)
    now = time.time()
    created = []
    db = await get_db()
    try:
        for item in plan["planned"]:
            p = item["payload"]
            cur = await db.execute(
                """INSERT INTO sections (title, subtitle, type, style, position, sort_order, enabled, config, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p["title"], p["subtitle"], p["type"], p["style"], p["position"],
                 p["sort_order"], int(p["enabled"]), json.dumps(p["config"]), now, now),
            )
            sid = cur.lastrowid
            row = await (await db.execute("SELECT * FROM sections WHERE id = ?", (sid,))).fetchone()
            created.append(_section_to_dict(row))
        await db.commit()
    finally:
        await db.close()
    return {"created": created, "skipped": plan["skipped"]}
