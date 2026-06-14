"""Resolution of custom Library sections into concrete item lists.

Each section type (``plex_collection``, ``filter``, ``sessions``, ``history``)
has a resolver that turns its stored config into the uniform item shape the app
renders, attaching mdblist ratings where relevant. ``resolve_section`` dispatches
on the row's type; the CRUD surface lives in ``sections``.
"""

import asyncio
import json
import random
import re
import time
from typing import Optional
from urllib.parse import urlencode

import httpx

from .config import MDBLIST_API_KEY, PLEX_TOKEN, PLEX_TYPE, log, section_min_version
from .plex import (
    map_plex_item,
    plex_get,
    plex_image,
    SECTION_CACHE_TTL,
    tmdb_from_metadata,
)
from .popular import popular_tmdb_ranks
from .ratings import (
    compute_rating,
    effective_sources,
    fetch_and_store_ratings,
    get_rating_config,
    ratings_for_tmdb,
)


async def map_with_ratings(items: list[dict], rcfg: Optional[dict] = None) -> list[dict]:
    """Attach mdblist sources + canonical rating to each Plex item (by TMDB id).
    Cache-misses are bulk-enriched once on the fly so sections work before the
    nightly sync has run; afterwards everything is a cache hit."""
    if rcfg is None:
        rcfg = await get_rating_config()

    def _pair(m):
        tid = tmdb_from_metadata(m)
        return (tid, "movie" if m.get("type") == "movie" else "show") if tid else None

    pairs = [p for p in (_pair(m) for m in items) if p]
    cache = await ratings_for_tmdb(pairs)

    if MDBLIST_API_KEY:
        miss: dict[str, list[int]] = {}
        for tid, mt in pairs:
            if (tid, mt) not in cache:
                miss.setdefault(mt, []).append(tid)
        if miss:
            for mt, ids in miss.items():
                await fetch_and_store_ratings(mt, ids)
            cache = await ratings_for_tmdb(pairs)

    out = []
    for m in items:
        p = _pair(m)
        row = cache.get(p) if p else None
        sources = effective_sources(row["sources"], rcfg) if row else {}
        rating = compute_rating(sources, rcfg) if sources else None
        out.append(map_plex_item(m, rating, sources))
    return out


# ---------- Collection sections ----------

def _collection_keys(cfg: dict) -> list[str]:
    """Collection ratingKeys for a section: `collection_keys` (multi) with
    back-compat fall-back to the single `collection_key`. Entries may be plain
    keys or `{key,title}` objects (as the editor stores them)."""
    keys = cfg.get("collection_keys")
    if keys is None:
        single = cfg.get("collection_key")
        keys = [single] if single else []
    out: list[str] = []
    for k in keys:
        kk = k.get("key") if isinstance(k, dict) else k
        if kk not in (None, "", "None"):
            out.append(str(kk))
    return out


async def _resolve_collection(cfg: dict) -> list[dict]:
    keys = _collection_keys(cfg)
    if not keys:
        return []
    # Each collection's children are keyed by ratingKey alone (library-agnostic),
    # so collections from different libraries combine into one section.
    metas: list[dict] = []
    for key in keys:
        data = await plex_get(f"/library/metadata/{key}/children", {"includeGuids": 1}, cache_ttl=SECTION_CACHE_TTL)
        if data:
            metas.extend(data.get("MediaContainer", {}).get("Metadata", []))
    seen: set = set()
    items: list[dict] = []
    for m in metas:
        rk = m.get("ratingKey")
        if rk in seen:
            continue
        seen.add(rk)
        items.append(m)
    if cfg.get("randomize"):
        random.shuffle(items)   # random pick across the combined collections
    limit = int(cfg.get("limit") or 0)
    if limit > 0:
        items = items[:limit]
    return await map_with_ratings(items)


# ---------- Filter sections ----------

# Sort keys that rank by the canonical rating (no native Plex equivalent).
_COMBINED_SORTS = {"combined", "combined:desc", "rating", "rating:desc"}


def _section_libraries(cfg: dict) -> list[str]:
    """Library section keys for a filter: `library_sections` (multi) with
    back-compat fall-back to the single `library_section`."""
    libs = cfg.get("library_sections")
    if not libs:
        single = cfg.get("library_section")
        libs = [single] if single else []
    return [str(x) for x in libs if x not in (None, "", "None")]


_TAG_DIMS = ("genres", "directors", "actors", "countries")
# Title/subtitle placeholders → dimension; both plural ({directors}) and singular
# ({director}) forms map to the same picked tag list.
_TEMPLATE_KEYS = {d: d for d in _TAG_DIMS}
_TEMPLATE_KEYS.update({"genre": "genres", "director": "directors",
                       "actor": "actors", "country": "countries"})


def _pick_tags(cfg: dict) -> dict:
    """The tag objects ({id,title}) chosen for each dimension this resolve,
    applying the pool-vs-random mode. `pool` uses every selected tag (OR'd);
    `random` picks a random `<dim>_pick` subset each time the section resolves,
    so the shelf, and any `{director}`-style title placeholder, rotates between
    loads. Computed once so the filter query and the title share the same pick."""
    out: dict = {}
    for dim in _TAG_DIMS:
        objs = []
        for v in cfg.get(dim) or []:
            if isinstance(v, dict):
                obj = {"id": str(v.get("id") if v.get("id") is not None else ""),
                       "title": v.get("title") or ""}
            else:
                obj = {"id": str(v), "title": str(v)}
            if obj["id"] and obj["id"] != "None":
                objs.append(obj)
        if objs and (cfg.get(f"{dim}_mode") or "pool") == "random":
            pick = max(1, int(cfg.get(f"{dim}_pick") or 1))
            if pick < len(objs):
                objs = random.sample(objs, pick)
        out[dim] = objs
    return out


def _apply_template(text: Optional[str], picks: dict) -> Optional[str]:
    """Substitute `{director}`/`{directors}`/`{genre}`/… placeholders in a
    title/subtitle with the comma-joined titles of the picked tags."""
    if not text or "{" not in text:
        return text

    def repl(m):
        dim = _TEMPLATE_KEYS.get(m.group(1))
        if dim is None:
            return m.group(0)
        return ", ".join(o["title"] for o in picks.get(dim, []) if o.get("title"))

    return re.sub(r"\{(\w+)\}", repl, text)


# Plex meta fields we can sort a merged (multi-library) result set by in Python,
# mirroring the native Plex sort so cross-library order stays correct.
_SORT_NUMERIC = {"addedAt", "audienceRating", "rating", "year"}
_SORT_FIELDS = _SORT_NUMERIC | {"originallyAvailableAt", "titleSort"}


def _sort_metas(metas: list[dict], sort: str) -> list[dict]:
    field, _, direction = sort.partition(":")
    if field not in _SORT_FIELDS:
        return metas
    reverse = direction != "asc"
    present = [m for m in metas if m.get(field) not in (None, "")]
    missing = [m for m in metas if m.get(field) in (None, "")]
    if field in _SORT_NUMERIC:
        def keyfn(m):
            try:
                return float(m.get(field))
            except (TypeError, ValueError):
                return 0.0
    else:
        def keyfn(m):
            return str(m.get(field)).lower()
    present.sort(key=keyfn, reverse=reverse)
    return present + missing


async def _resolve_filter(cfg: dict, picks: Optional[dict] = None) -> list[dict]:
    libs = _section_libraries(cfg)
    if not libs:
        return []
    if picks is None:
        picks = _pick_tags(cfg)

    norm: dict = {}
    media_type = cfg.get("media_type")
    if media_type in PLEX_TYPE:
        norm["type"] = PLEX_TYPE[media_type]

    # Equality tag filters: `field=id,id` (OR within a field, AND across fields).
    # Each dimension uses the tags picked for this resolve (pool or random subset).
    for field, key in (("genre", "genres"), ("director", "directors"),
                       ("actor", "actors"), ("country", "countries")):
        chosen = [o["id"] for o in picks.get(key, []) if o.get("id")]
        if chosen:
            norm[field] = ",".join(chosen)

    # Plex `country=` matches ANY of an item's production countries, so a film
    # merely co-produced with (e.g.) the Netherlands, but English/Danish and not
    # actually Dutch, slips into a "Netherlands" shelf. We treat the country
    # filter as PRIMARY origin: keep only items whose first-listed country is one
    # of the picked ones (post-filtered below against the listing's Country[0]).
    country_titles = {(o.get("title") or "").lower() for o in picks.get("countries", []) if o.get("title")}

    # Plex `genre=` matches ANY of a title's genres, so a film whose 6th, least-defining
    # genre happens to be "Comedy" lands in a "Comedy" shelf. Plex lists genres in
    # relevance order, so we treat only the first N as the title's defining genres:
    # keep an item only if a picked genre is among its first `genre_primary_count`
    # (default 3; 0 disables and reverts to plain ANY matching). Post-filtered below
    # against the listing's Genre array.
    genre_titles = {(o.get("title") or "").lower() for o in picks.get("genres", []) if o.get("title")}
    _gpc = cfg.get("genre_primary_count")
    genre_primary = int(_gpc) if _gpc not in (None, "") else 3
    genre_primary_filter = bool(genre_titles) and genre_primary > 0

    # Year / added-date filters are native Plex clauses, passed as raw `field>=value`
    # so httpx encodes only the operator (`%3E`/`%3C`), which Plex decodes.
    ops: list[str] = []
    if cfg.get("released_after_year") not in (None, ""):
        ops.append(f"year>={cfg['released_after_year']}")
    if cfg.get("released_before_year") not in (None, ""):
        ops.append(f"year<={cfg['released_before_year']}")
    if cfg.get("added_within_days") not in (None, "", 0, "0"):
        ops.append(f"addedAt>={int(time.time() - int(cfg['added_within_days']) * 86400)}")

    # Score filtering uses the universal canonical rating (0-100), post-filtered here.
    rating_min = float(cfg["rating_min"]) if cfg.get("rating_min") not in (None, "") else None

    sort = (cfg.get("sort") or "addedAt:desc").strip()
    show_limit = int(cfg.get("limit") or 30)        # how many to display
    randomize = bool(cfg.get("randomize"))
    trending = bool(cfg.get("trending"))            # keep only mdblist-popular titles
    multi = len(libs) > 1
    # When randomizing, fetch a candidate POOL (per sort/filters), shuffle it, then
    # show `limit` of those. Trending must scan the whole library to intersect with
    # the popular set, so it pulls everything. Otherwise the pool is just the show
    # limit (or larger when we re-rank/filter by rating in Python).
    rank_by_rating = sort in _COMBINED_SORTS
    norm["X-Plex-Container-Start"] = 0
    norm["includeGuids"] = 1
    norm["sort"] = "audienceRating:desc" if rank_by_rating else sort
    if trending:
        pool = 10000
    elif randomize:
        pool = int(cfg.get("query_limit") or 100)
    elif rank_by_rating or rating_min is not None or country_titles or genre_primary_filter:
        pool = min(max(show_limit * 5, 100), 500)
    else:
        pool = show_limit
    norm["X-Plex-Container-Size"] = pool

    query = urlencode(norm)
    if ops:
        query += "&" + "&".join(ops)

    # Query each library and merge; lets a single section mix movies and shows.
    metas: list[dict] = []
    for lib in libs:
        data = await plex_get(f"/library/sections/{lib}/all?{query}", cache_ttl=SECTION_CACHE_TTL)
        if data:
            metas.extend(data.get("MediaContainer", {}).get("Metadata", []))
    # De-dupe (a title could appear in more than one library) by ratingKey.
    seen: set = set()
    deduped: list[dict] = []
    for m in metas:
        rk = m.get("ratingKey")
        if rk in seen:
            continue
        seen.add(rk)
        deduped.append(m)
    # Primary-country filter: the listing preserves country order, so Country[0]
    # is the genuine origin (co-production partners follow). Drop items whose
    # first country isn't one of the picked ones.
    if country_titles:
        deduped = [m for m in deduped
                   if (m.get("Country") or [])
                   and (m["Country"][0].get("tag") or "").lower() in country_titles]
    # Primary-genre filter: keep items where a picked genre is among the title's
    # first `genre_primary` genres (Plex lists them in relevance order).
    if genre_primary_filter:
        deduped = [m for m in deduped
                   if genre_titles & {(g.get("tag") or "").lower()
                                      for g in (m.get("Genre") or [])[:genre_primary]}]
    # Plex sorts within each library; re-sort the merged set so order is global.
    if multi and not randomize and not rank_by_rating:
        deduped = _sort_metas(deduped, sort)
    items = await map_with_ratings(deduped)

    popular_rank: dict = {}
    if trending:
        mts = [media_type] if media_type in ("movie", "show") else ["movie", "show"]
        popular_rank = await popular_tmdb_ranks(mts)
        items = [it for it in items if it.get("tmdb_id") in popular_rank]

    if rating_min is not None:
        items = [it for it in items if (it["rating"] if it["rating"] is not None else -1) >= rating_min]
    if randomize:
        random.shuffle(items)               # random pick from the queried pool
    elif rank_by_rating:
        items.sort(key=lambda it: (it["rating"] if it["rating"] is not None else -1), reverse=True)
    elif trending:
        items.sort(key=lambda it: popular_rank.get(it.get("tmdb_id"), 99999))  # by popularity

    return items[:show_limit]


# ---------- Sessions section ("Who's watching") ----------

async def _resolve_sessions(cfg: dict) -> list[dict]:
    """Resolve a "Who's watching" section from Plex's active playback sessions
    (`/status/sessions`), enriching each with the watching user + player state."""
    data = await plex_get("/status/sessions")
    if not data:
        return []
    metas = data.get("MediaContainer", {}).get("Metadata", []) or []
    out: list[dict] = []
    for m in metas:
        user = m.get("User") or {}
        player = m.get("Player") or {}
        is_ep = m.get("type") == "episode"
        ep_label = None
        if is_ep:
            s, e = m.get("parentIndex"), m.get("index")
            se = f"S{s}·E{e}" if s is not None and e is not None else ""
            title = m.get("title")
            ep_label = (f"{se} · {title}" if se and title else (se or title or "")).strip()
        # clearLogo lives on the show/movie, not the episode the session reports, so the
        # lookup key is the grandparent for episodes (enriched below, like Recently Watched).
        logo_rk = str(m.get("grandparentRatingKey") or m.get("ratingKey", "")) if is_ep \
            else str(m.get("ratingKey", ""))
        out.append({
            "rating_key": str(m.get("ratingKey", "")),
            "tmdb_id": tmdb_from_metadata(m),
            "title": (m.get("grandparentTitle") if is_ep else m.get("title")) or m.get("title", ""),
            "type": m.get("type", ""),
            "year": m.get("year"),
            "thumb": (m.get("art") or m.get("grandparentThumb") or m.get("thumb")) if is_ep
                     else (m.get("art") or m.get("thumb")),
            "art": m.get("art") or m.get("grandparentArt"),
            "clear_logo": None,   # enriched below from the show/movie full metadata
            "summary": m.get("summary"),
            "rating": None,
            "sources": {},
            "user_title": user.get("title"),
            "user_thumb": user.get("thumb"),
            "player": player.get("title") or player.get("product"),
            "player_state": player.get("state"),
            "view_offset": m.get("viewOffset"),
            "duration": m.get("duration"),
            "episode_label": ep_label,
            "_logo_rk": logo_rk,
        })
    # Keep every distinct viewer (two people CAN watch the same title); only
    # collapse a genuine exact duplicate (same user + same item reported twice).
    seen: set = set()
    deduped: list[dict] = []
    for it in out:
        key = ((it.get("user_title") or ""), it["rating_key"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    out = deduped
    limit = int(cfg.get("limit", 0) or 0)
    if limit > 0:
        out = out[:limit]
    # The session metadata carries the episode's Image array (no clearLogo), so pull the
    # show/movie clearLogo from full metadata, the same enrichment Recently Watched uses.
    uniq = list({it["_logo_rk"] for it in out if it.get("_logo_rk")})
    fetched = await asyncio.gather(*[_art_logo_for(r) for r in uniq])
    fmap = dict(zip(uniq, fetched))
    for it in out:
        f = fmap.get(it.pop("_logo_rk", "")) or {}
        it["clear_logo"] = f.get("logo")
    return out


# ---------- History section ("Recently watched") ----------

_avatar_cache: dict = {"ts": 0.0, "by_id": {}, "by_name": {}}


async def _plextv_avatars() -> tuple[dict, dict]:
    """plex.tv avatars (the local /accounts has none). Returns (by_id, by_name)
    from home users + friends, cached ~1h. Friend/shared accountIDs match the
    history accountID; the owner is bridged by name."""
    if _avatar_cache["ts"] > time.time() and _avatar_cache["by_id"]:
        return _avatar_cache["by_id"], _avatar_cache["by_name"]
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    if PLEX_TOKEN:
        headers = {"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json",
                   "X-Plex-Client-Identifier": "poptonium-backend"}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                users: list = []
                hu = await c.get("https://plex.tv/api/v2/home/users", headers=headers)
                if hu.status_code == 200:
                    users += (hu.json().get("users") or [])
                fr = await c.get("https://plex.tv/api/v2/friends", headers=headers)
                if fr.status_code == 200:
                    users += (fr.json() or [])
            for u in users:
                th = u.get("thumb")
                if not th:
                    continue
                if u.get("id") is not None:
                    by_id[str(u["id"])] = th
                for k in (u.get("username"), u.get("title")):
                    if k:
                        by_name[k.strip().lower()] = th
        except Exception as e:
            log.warning("plextv avatars failed: %s", e)
    _avatar_cache.update(ts=time.time() + 3600, by_id=by_id, by_name=by_name)
    return by_id, by_name


async def _plex_account_map() -> dict:
    """Map Plex accountID → {title, thumb} (avatar) for labelling watch history."""
    data = await plex_get("/accounts")
    by_id_av, by_name_av = await _plextv_avatars()
    out: dict[str, dict] = {}
    for a in (data or {}).get("MediaContainer", {}).get("Account", []) or []:
        aid = str(a.get("id"))
        name = a.get("name") or ""
        thumb = a.get("thumb") or by_id_av.get(aid) or (by_name_av.get(name.strip().lower()) if name else None)
        out[aid] = {"title": a.get("name"), "thumb": thumb}
    return out


_artlogo_cache: dict = {}  # rk -> (expiry, {"art": .., "logo": ..})
_ARTLOGO_TTL = 6 * 3600


async def _art_logo_for(rk: str) -> dict:
    """Backdrop `art` + clearLogo for a movie/show ratingKey. Watch history gives
    only the poster `thumb` (which has baked-in title text) and no clearLogo, so we
    pull the clean backdrop + logo from full metadata. Cached ~6h; one light GET."""
    if not rk:
        return {"art": None, "logo": None}
    hit = _artlogo_cache.get(rk)
    if hit and hit[0] > time.time():
        return hit[1]
    out = {"art": None, "logo": None}
    data = await plex_get(f"/library/metadata/{rk}")
    metas = (data or {}).get("MediaContainer", {}).get("Metadata", [])
    if metas:
        out["art"] = metas[0].get("art")
        out["logo"] = plex_image(metas[0], "clearLogo")
    if len(_artlogo_cache) > 1024:
        _artlogo_cache.clear()
    _artlogo_cache[rk] = (time.time() + _ARTLOGO_TTL, out)
    return out


async def _resolve_history(cfg: dict) -> list[dict]:
    """Resolve a "Recently Watched" section from Plex watch history
    (`/status/sessions/history/all`), de-duped by item, labelled with the watcher."""
    sort = cfg.get("sort") or "viewedAt:desc"
    limit = int(cfg.get("limit", 20) or 20)
    media_type = (cfg.get("media_type") or "").strip()
    randomize = bool(cfg.get("randomize"))
    type_param = {"movie": "1", "show": "4"}.get(media_type)

    qs = f"sort={sort}&X-Plex-Container-Start=0&X-Plex-Container-Size={max(limit * 4, 80)}"
    if type_param:
        qs += f"&type={type_param}"
    data = await plex_get(f"/status/sessions/history/all?{qs}")
    if not data:
        return []
    metas = data.get("MediaContainer", {}).get("Metadata", []) or []
    accounts = await _plex_account_map()

    # Collapse episodes to their show, and de-dup so a title appears once even if
    # watched repeatedly in the window, keyed by the *content* (show for episodes /
    # movie). Metas are viewedAt:desc, so first seen = most recent watch. All the
    # distinct people who watched it are collected into `watchers` (recent first).
    order: list[str] = []
    by_rk: dict[str, dict] = {}
    for m in metas:
        is_ep = m.get("type") == "episode"
        if is_ep:
            gk = m.get("grandparentKey") or ""
            content_rk = gk.rsplit("/", 1)[-1] if gk else str(m.get("ratingKey", ""))
            title = m.get("grandparentTitle") or m.get("title", "")
            ctype = "show"
            art = m.get("grandparentArt") or m.get("art")
            thumb = m.get("thumb")
        else:
            content_rk = str(m.get("ratingKey", ""))
            title = m.get("title", "")
            ctype = "movie"
            art = m.get("art")
            thumb = m.get("thumb")
        if not content_rk:
            continue
        acct = accounts.get(str(m.get("accountID")), {})
        watcher = {"title": acct.get("title"), "thumb": acct.get("thumb")}
        if content_rk not in by_rk:
            order.append(content_rk)
            by_rk[content_rk] = {
                "rating_key": content_rk,
                "tmdb_id": None,
                "title": title,
                "type": ctype,
                "year": None if is_ep else m.get("year"),
                "thumb": thumb,
                "art": art,
                "clear_logo": None,
                "summary": m.get("summary"),
                "rating": None,
                "sources": {},
                "user_title": watcher["title"],
                "user_thumb": watcher["thumb"],
                "watchers": [],
                "player": None,
                "player_state": None,
                "view_offset": None,
                "duration": None,
                "episode_label": None,
            }
        it = by_rk[content_rk]
        if watcher["title"] and not any(w["title"] == watcher["title"] for w in it["watchers"]):
            it["watchers"].append(watcher)
    built = [by_rk[rk] for rk in order]

    if randomize:
        random.shuffle(built)
    if limit > 0:
        built = built[:limit]

    # Enrich backdrop art + clearLogo concurrently, only for the items shown
    # (the rating_key is the show/movie, which carries the clean art + logo).
    uniq = list({it["rating_key"] for it in built if it["rating_key"]})
    fetched = await asyncio.gather(*[_art_logo_for(r) for r in uniq])
    fmap = dict(zip(uniq, fetched))
    for it in built:
        f = fmap.get(it["rating_key"]) or {}
        if f.get("art"):
            it["art"] = f["art"]          # clean backdrop (history only had the poster)
        it["clear_logo"] = f.get("logo")
    return built


# ---------- Dispatch ----------

async def resolve_section(row) -> dict:
    try:
        cfg = json.loads(row["config"])
    except Exception:
        cfg = {}
    # Pick tags once so the filter query and the title/subtitle templates agree.
    picks = _pick_tags(cfg) if row["type"] == "filter" else {}
    if row["type"] == "plex_collection":
        items = await _resolve_collection(cfg)
    elif row["type"] == "filter":
        items = await _resolve_filter(cfg, picks)
    elif row["type"] == "sessions":
        items = await _resolve_sessions(cfg)
    elif row["type"] == "history":
        items = await _resolve_history(cfg)
    else:
        items = []
    return {
        "id": row["id"],
        "title": _apply_template(row["title"], picks),
        "subtitle": _apply_template(row["subtitle"], picks),
        "type": row["type"],
        "style": row["style"],
        "position": row["position"],
        "sort_order": row["sort_order"],
        # Derived from type/style (not stored): the min app version that can render it.
        "min_app_version": section_min_version(row["type"], row["style"]),
        "items": items,
    }
