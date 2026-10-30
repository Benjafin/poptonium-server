"""The Poptonium Cast receiver's idle screen (`/cast/screen`).

An admin picks up to three rows for the TV, in order: any of the library sections (shelves,
a hero carousel, "Who's watching", history) or the built-in top-rated films / series rows.
The receiver fetches them resolved, with the configured rating badge groups, and renders
shelves drifting in alternating directions, one hero carousel and live sessions.

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
MAX_ROWS = 3
SHELF_ITEMS = 24
HERO_ITEMS = 8
# Sessions change by the second; everything else is cached briefly server-side anyway.
CACHE_TTL = 30

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


class CastRow(BaseModel):
    source: Literal["top_movies", "top_shows", "section"]
    id: Optional[int] = None


class CastScreenConfig(BaseModel):
    rows: list[CastRow]


async def _section_rows() -> dict[int, dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sections")
        return {r["id"]: dict(r) for r in await cursor.fetchall()}
    finally:
        await db.close()


async def get_config() -> list[dict]:
    raw = await meta_get(CONFIG_KEY)
    if raw:
        try:
            rows = json.loads(raw).get("rows")
            if isinstance(rows, list):
                return rows
        except Exception:
            pass
    return list(DEFAULT_ROWS)


def _validate(rows: list[CastRow], sections: dict[int, dict]) -> list[dict]:
    if len(rows) > MAX_ROWS:
        raise HTTPException(400, f"At most {MAX_ROWS} rows fit on the TV")
    heroes = 0
    out = []
    for r in rows:
        if r.source == "section":
            sec = sections.get(r.id or -1)
            if not sec:
                raise HTTPException(400, f"Section {r.id} not found")
            heroes += sec["style"] == "hero"
            out.append({"source": "section", "id": r.id})
        else:
            out.append({"source": r.source})
    if heroes > 1:
        raise HTTPException(400, "Only one carousel fits on the TV")
    return out


def _trim(item: dict, hero: bool) -> dict:
    out = {k: item.get(k) for k in _ITEM_FIELDS if item.get(k) is not None}
    if hero and item.get("summary"):
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
        items = await _top_rated(b["media_type"], SHELF_ITEMS)
        return {"key": row["source"], "title": b["title"], "subtitle": None, "type": "top_rated", "style": "row",
                "items": [_trim(it, False) for it in items]}
    sec = sections.get(row.get("id") or -1)
    if not sec:
        return None  # deleted since it was picked
    resolved = await resolve_section(sec, client_schema="99.0.0")
    hero = resolved["style"] == "hero"
    items = resolved["items"][: HERO_ITEMS if hero else SHELF_ITEMS]
    return {"key": f"section:{sec['id']}", "title": resolved["title"], "subtitle": resolved.get("subtitle"),
            "type": resolved["type"], "style": resolved["style"], "items": [_trim(it, hero) for it in items]}


@router.get("/cast/screen")
async def cast_screen():
    rows = await get_config()
    key = json.dumps(rows)
    if _cache["key"] == key and _cache["expiry"] > time.time():
        return _cache["payload"]
    sections = await _section_rows()
    resolved = await asyncio.gather(*[_resolve_row(r, sections) for r in rows])
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
    return {"rows": await get_config(), "available": available, "max_rows": MAX_ROWS}


@router.put("/cast/screen/config", dependencies=[Depends(require_admin)])
async def put_cast_screen_config(p: CastScreenConfig):
    rows = _validate(p.rows, await _section_rows())
    await meta_set(CONFIG_KEY, json.dumps({"rows": rows}))
    _cache.update(expiry=0.0, key=None, payload=None)
    return {"rows": rows}
