"""Idle-screen showcase for the Poptonium Cast receiver (`/showcase`).

The receiver's idle screen shows two scrolling rows: the library's highest-rated
films and highest-rated shows. Ranked with the same canonical rating and
vote-shrunk ordering as a `rating:desc` filter section, across every movie / show
library, and returned with the configured badge groups so the TV renders scores
exactly like the app. Read-only and unauthenticated like `/sections/resolved`;
artwork paths are Plex-relative and fetched by the receiver with its own token.
"""

import asyncio
import time

from fastapi import APIRouter, Query

from .ratings import get_rating_config
from .section_resolve import _library_type_map, _resolve_filter

router = APIRouter()

_CACHE_TTL = 600  # seconds; ratings change nightly, libraries rarely
_cache: dict = {}  # limit -> (expiry, payload)

_FIELDS = ("rating_key", "title", "type", "year", "thumb", "art", "clear_logo", "rating", "sources")


async def _top_rated(libs: list[str], media_type: str, limit: int) -> list[dict]:
    if not libs:
        return []
    items = await _resolve_filter({
        "library_sections": libs,
        "media_type": media_type,
        "sort": "rating:desc",
        "limit": limit,
    })
    # Only rated titles make a "highest rated" row.
    return [{k: it.get(k) for k in _FIELDS} for it in items if it.get("rating") is not None]


@router.get("/showcase")
async def showcase(limit: int = Query(24, ge=1, le=60)):
    hit = _cache.get(limit)
    if hit and hit[0] > time.time():
        return hit[1]
    types = await _library_type_map()
    movie_libs = [k for k, t in types.items() if t == "movie"]
    show_libs = [k for k, t in types.items() if t == "show"]
    rcfg = await get_rating_config()
    movies, shows = await asyncio.gather(
        _top_rated(movie_libs, "movie", limit),
        _top_rated(show_libs, "show", limit),
    )
    payload = {
        "movies": movies,
        "shows": shows,
        "display_groups": rcfg.get("display_groups", []),
        "display_sources": rcfg.get("display_sources", []),
    }
    _cache[limit] = (time.time() + _CACHE_TTL, payload)
    return payload
