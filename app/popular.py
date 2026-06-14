"""The Discover/popular feed.

Rebuilds the Discover feed from mdblist's official popular list (nightly + on
demand), and serves it joined with cached ratings. ``popular_tmdb_ranks`` is
reused by trending sections to keep only currently-popular library titles.
"""

import asyncio
import time

import httpx
from fastapi import APIRouter, Depends, Query

from .auth import require_admin
from .config import MDBLIST_API_KEY, log
from .db import get_db, meta_set
from .ratings import (
    compute_rating,
    effective_sources,
    fetch_and_store_ratings,
    get_rating_config,
    ratings_for_tmdb,
)

router = APIRouter()


async def fetch_mdblist_official_list(slug_media: str) -> list[dict]:
    """Fetch mdblist's official popular list (slug_media is 'movies' or 'shows').

    As of 2026-06 mdblist merged the per-media official lists into a single
    combined "popular" list at /lists/official/popular/items, which returns
    {"movies": [...], "shows": [...], "pagination": {...}}. We fetch it once
    and return the requested media subset."""
    url = "https://api.mdblist.com/lists/official/popular/items"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(
            url,
            params={
                "apikey": MDBLIST_API_KEY,
                "append_to_response": "poster,description,ratings",
            },
        )
        if resp.status_code != 200:
            log.error("mdblist official %s list failed: %s %s",
                      slug_media, resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        # Combined response keyed by media: {"movies": [...], "shows": [...]}
        return data.get(slug_media, [])


async def refresh_popular_items():
    """Rebuild the Discover feed from mdblist's official popular list and
    bulk-cache the ratings for those titles (≈1-2 mdblist calls total)."""
    if not MDBLIST_API_KEY:
        log.warning("MDBLIST_API_KEY not set, skipping popular items fetch")
        return

    log.info("Starting popular items refresh (official list + batched ratings)...")
    # Phase 1: fetch lists and cache their ratings (each uses its own short-lived
    # connection. Do NOT hold an open write txn across these, or we self-deadlock).
    pages = []
    for slug_media in ("movies", "shows"):
        media_type = "movie" if slug_media == "movies" else "show"
        list_items = await fetch_mdblist_official_list(slug_media)
        log.info("Fetched %d items from mdblist official %s popular list", len(list_items), slug_media)
        tmdb_ids = [(it.get("tmdb_id") or (it.get("ids") or {}).get("tmdb")) for it in list_items]
        await fetch_and_store_ratings(media_type, [int(t) for t in tmdb_ids if t])
        pages.append((media_type, list_items))

    # Phase 2: rebuild popular_items in a single transaction.
    now = time.time()
    db = await get_db()
    try:
        total = 0
        await db.execute("DELETE FROM popular_items")
        for media_type, list_items in pages:
            for rank, it in enumerate(list_items, 1):
                ids = it.get("ids") or {}
                imdb = it.get("imdb_id") or ids.get("imdb")
                if not imdb:
                    continue
                poster = it.get("poster")
                if poster and "/w200/" in poster:
                    poster = poster.replace("/w200/", "/w500/")
                await db.execute(
                    """INSERT OR REPLACE INTO popular_items
                       (imdb_id, tmdb_id, title, year, media_type, poster_url,
                        description, certification, rank, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (imdb, it.get("tmdb_id") or ids.get("tmdb"), it.get("title") or "",
                     it.get("release_year") or it.get("year"), media_type, poster,
                     it.get("description") or it.get("plot"), it.get("certification"), rank, now),
                )
                total += 1
        await db.commit()
        log.info("Popular items refresh complete: %d items stored", total)
    except Exception:
        log.exception("Error refreshing popular items")
    finally:
        await db.close()
    await meta_set("popular_last_refresh", str(time.time()))


async def popular_tmdb_ranks(media_types: list[str]) -> dict[int, int]:
    """{tmdb_id: rank} of the currently-popular titles (from the mdblist Discover
    feed); used to keep only 'trending' library items, ordered by popularity."""
    if not media_types:
        return {}
    out: dict[int, int] = {}
    db = await get_db()
    try:
        ph = ",".join("?" * len(media_types))
        cursor = await db.execute(
            f"SELECT tmdb_id, rank FROM popular_items WHERE tmdb_id IS NOT NULL AND media_type IN ({ph})",
            media_types,
        )
        for row in await cursor.fetchall():
            tid = int(row["tmdb_id"])
            rank = row["rank"] if row["rank"] is not None else 99999
            if tid not in out or rank < out[tid]:
                out[tid] = rank
    finally:
        await db.close()
    return out


@router.get("/popular")
async def get_popular(
    media_type: str = Query(..., regex="^(movie|show)$"),
    limit: int = Query(200, ge=1, le=1000),
):
    cfg = await get_rating_config()
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT imdb_id, tmdb_id, title, year, media_type, poster_url,
                      description, certification, rank
               FROM popular_items WHERE media_type = ? ORDER BY rank ASC LIMIT ?""",
            (media_type, limit),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    ratings = await ratings_for_tmdb([(r["tmdb_id"], media_type) for r in rows if r["tmdb_id"]])
    items = []
    for r in rows:
        rt = ratings.get((r["tmdb_id"], media_type)) if r["tmdb_id"] else None
        sources = effective_sources(rt["sources"], cfg) if rt else {}
        items.append({
            "imdb_id": r["imdb_id"],
            "tmdb_id": r["tmdb_id"],
            "title": r["title"],
            "year": r["year"],
            "media_type": r["media_type"],
            "poster_url": r["poster_url"],
            "description": r["description"],
            "certification": r["certification"],
            "rank": r["rank"],
            "sources": sources,
            "rating": compute_rating(sources, cfg) if sources else None,
        })
    return {"items": items, "count": len(items), "display_sources": cfg["display_sources"]}


@router.post("/popular/refresh", dependencies=[Depends(require_admin)])
async def trigger_refresh():
    asyncio.create_task(refresh_popular_items())
    return {"status": "refresh started"}
