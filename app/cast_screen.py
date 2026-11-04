"""The Poptonium Cast receiver's idle screen (`/cast/screen`).

By default the TV follows the app: every enabled section, in the order the app's home shows
them. An admin can switch that off and pick the sections for the TV instead, in order: any
of the library sections (shelves, carousels, "Who's watching", history) or the built-in
top-rated films / series rows. The
receiver fetches them resolved, with the configured rating badge groups, and shows them
as "Spotlight": one title at a time over its backdrop, with a poster rail that steps
through each section in turn. "Who's watching" becomes a live pill.

`/cast/screen` is read-only and unauthenticated like `/sections/resolved`; artwork paths are
Plex-relative and fetched by the receiver with its own token. The layout is edited through
the admin-only `/cast/screen/config`.
"""

import asyncio
import json
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .db import get_db, meta_get, meta_set
from .ratings import get_rating_config
from .section_resolve import _library_type_map, _resolve_filter, resolve_section

router = APIRouter()

CONFIG_KEY = "cast_screen"
MAX_ROWS = 10
# Titles sent per section: the whole shelf as the app shows it, with a safety cap. The
# receiver's rail shows 12; when a shelf holds more, it picks a random 12 per launch (in the
# shelf's order), so the TV doesn't open on the same titles every time.
POOL_ITEMS = 60
# Live sessions are re-resolved at most every 30 s. The other sections stay put for longer:
# templated sections ("{genre} night") pick new tags on every resolve, and a new answer
# makes the receiver rebuild the whole screen.
CACHE_TTL = 30
SECTIONS_TTL = 15 * 60
# The app's home: custom sections anchored around its built-in library shelves.
APP_POSITIONS = ["top", "after_all", "after_movies", "after_shows"]

BUILTINS = {
    "top_movies": {"title": "Top rated films", "media_type": "movie"},
    "top_shows": {"title": "Top rated series", "media_type": "show"},
}
DEFAULT_ROWS = [{"source": "top_movies"}, {"source": "top_shows"}]

_ITEM_FIELDS = (
    "rating_key", "title", "type", "year", "thumb", "art", "clear_logo", "rating", "sources",
    "episode_label", "user_title", "user_thumb", "player", "player_state", "view_offset", "duration",
)

_cache: dict = {"expiry": 0.0, "key": None, "payload": None}
_rows_cache: dict = {}  # json(row) -> (expiry, resolved row or None)


class CastRow(BaseModel):
    source: Literal["top_movies", "top_shows", "section"]
    id: Optional[int] = None


class CastScreenConfig(BaseModel):
    rows: list[CastRow]
    # Show the app's own sections, in the app's order (the default); off = `rows`.
    follow_app: bool = True


async def _section_rows() -> dict[int, dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sections")
        return {r["id"]: dict(r) for r in await cursor.fetchall()}
    finally:
        await db.close()


async def get_settings() -> dict:
    """Stored settings: `follow_app` (default on) and the custom `rows` (kept while off)."""
    raw = await meta_get(CONFIG_KEY)
    out = {"follow_app": True, "rows": list(DEFAULT_ROWS)}
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored.get("rows"), list):
                out["rows"] = stored["rows"]
            if isinstance(stored.get("follow_app"), bool):
                out["follow_app"] = stored["follow_app"]
        except Exception:
            pass
    return out


def app_rows(sections: dict[int, dict]) -> list[dict]:
    """The app's home order: enabled sections by position anchor, then sort order."""
    def order(s: dict):
        pos = s.get("position") or "after_shows"
        return (APP_POSITIONS.index(pos) if pos in APP_POSITIONS else len(APP_POSITIONS), s["sort_order"], s["id"])
    return [{"source": "section", "id": s["id"]} for s in sorted(sections.values(), key=order) if s["enabled"]]


async def get_config() -> list[dict]:
    """The rows the TV shows now."""
    settings = await get_settings()
    if settings["follow_app"]:
        rows = app_rows(await _section_rows())
        return rows or list(DEFAULT_ROWS)
    return settings["rows"] or list(DEFAULT_ROWS)


def _validate(rows: list[CastRow], sections: dict[int, dict]) -> list[dict]:
    if len(rows) > MAX_ROWS:
        raise HTTPException(400, f"At most {MAX_ROWS} sections")
    out = []
    for r in rows:
        if r.source == "section":
            sec = sections.get(r.id or -1)
            if not sec:
                raise HTTPException(400, f"Section {r.id} not found")
            out.append({"source": "section", "id": r.id})
        else:
            out.append({"source": r.source})
    return out


def _trim(item: dict) -> dict:
    out = {k: item.get(k) for k in _ITEM_FIELDS if item.get(k) is not None}
    if item.get("summary"):
        out["summary"] = item["summary"][:280]
    return out


async def _top_rated(media_type: str, limit: int) -> list[dict]:
    types = await _library_type_map()
    libs = [k for k, t in types.items() if t == media_type]
    if not libs:
        return []
    items = await _resolve_filter({"library_sections": libs, "media_type": media_type, "sort": "rating:desc", "limit": limit})
    # Only rated titles make a "top rated" row.
    return [it for it in items if it.get("rating") is not None]


async def _resolve_row(row: dict, sections: dict[int, dict]) -> Optional[dict]:
    if row["source"] in BUILTINS:
        b = BUILTINS[row["source"]]
        items = await _top_rated(b["media_type"], POOL_ITEMS)
        return {"key": row["source"], "title": b["title"], "subtitle": None, "type": "top_rated", "style": "row",
                "items": [_trim(it) for it in items]}
    sec = sections.get(row.get("id") or -1)
    if not sec:
        return None  # deleted since it was picked
    resolved = await resolve_section(sec, client_schema="99.0.0")
    items = resolved["items"][:POOL_ITEMS]
    return {"key": f"section:{sec['id']}", "title": resolved["title"], "subtitle": resolved.get("subtitle"),
            "type": resolved["type"], "style": resolved["style"], "items": [_trim(it) for it in items]}


async def _cached_row(row: dict, sections: dict[int, dict]) -> Optional[dict]:
    live = row.get("source") == "section" and (sections.get(row.get("id") or -1) or {}).get("type") == "sessions"
    key = json.dumps(row, sort_keys=True)
    hit = _rows_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    resolved = await _resolve_row(row, sections)
    _rows_cache[key] = (time.time() + (CACHE_TTL if live else SECTIONS_TTL), resolved)
    return resolved


def _clear_caches() -> None:
    _cache.update(expiry=0.0, key=None, payload=None)
    _rows_cache.clear()


@router.get("/cast/screen")
async def cast_screen():
    rows = await get_config()
    key = json.dumps(rows)
    if _cache["key"] == key and _cache["expiry"] > time.time():
        return _cache["payload"]
    sections = await _section_rows()
    resolved = await asyncio.gather(*[_cached_row(r, sections) for r in rows])
    rcfg = await get_rating_config()
    payload = {
        "rows": [r for r in resolved if r is not None],
        "display_groups": rcfg.get("display_groups", []),
        "display_sources": rcfg.get("display_sources", []),
    }
    _cache.update(expiry=time.time() + CACHE_TTL, key=key, payload=payload)
    return payload


@router.get("/cast/screen/config", dependencies=[Depends(require_admin)])
async def get_cast_screen_config():
    sections = await _section_rows()
    available = [{"source": k, "title": v["title"], "type": "top_rated", "style": "row"} for k, v in BUILTINS.items()]
    available += [{"source": "section", "id": s["id"], "title": s["title"], "type": s["type"], "style": s["style"],
                   "enabled": bool(s["enabled"])} for s in sorted(sections.values(), key=lambda s: (s["sort_order"], s["id"]))]
    settings = await get_settings()
    return {"follow_app": settings["follow_app"], "rows": settings["rows"], "app_rows": app_rows(sections),
            "available": available, "max_rows": MAX_ROWS}


@router.put("/cast/screen/config", dependencies=[Depends(require_admin)])
async def put_cast_screen_config(p: CastScreenConfig):
    rows = _validate(p.rows, await _section_rows())
    await meta_set(CONFIG_KEY, json.dumps({"follow_app": p.follow_app, "rows": rows}))
    _clear_caches()
    return {"follow_app": p.follow_app, "rows": rows}
