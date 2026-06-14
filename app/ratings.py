"""Ratings: mdblist fetching, the canonical rating formula, and library sync.

This module owns everything about *scores*: pulling per-source ratings from
mdblist, caching them, the configurable formula that collapses them into one
0-100 number, and the nightly job that warms the cache for the whole Plex
library. The universal ratings API (``/ratings/*``, ``/health``) is exposed via
``router``.
"""

import asyncio
import json
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import scheduler as scheduler_mod
from .auth import require_admin
from .config import (
    DEFAULT_RATING_CONFIG,
    MDBLIST_API_KEY,
    MDBLIST_BASE,
    PLEX_TYPE,
    SUPPORTED_SOURCES,
    log,
)
from .db import get_db, meta_get, meta_set
from .plex import plex_configured, plex_get, tmdb_from_metadata

router = APIRouter()

_mdblist_sem = asyncio.Semaphore(3)  # limit concurrent mdblist requests


# ---------- mdblist fetch + cache ----------

def _parse_sources(item: dict) -> dict:
    """Extract {source: {score, votes}} for SUPPORTED_SOURCES from an mdblist item.
    `tomatoes`=RT critic, `popcorn`=RT audience; `mdblist` is the aggregate score."""
    out: dict = {}
    for r in item.get("ratings") or []:
        src = r.get("source")
        if src in SUPPORTED_SOURCES and src != "mdblist":
            score = r.get("score")
            if score is not None:
                out[src] = {"score": round(float(score), 1), "votes": r.get("votes") or 0}
    mscore = item.get("score")
    if mscore is not None:
        out["mdblist"] = {"score": round(float(mscore), 1), "votes": None}
    return out


async def mdblist_bulk(media_type: str, tmdb_ids: list[int]) -> dict[int, dict]:
    """Bulk-fetch mdblist data for TMDB ids: 1 request per ≤200 ids.
    Returns {tmdb_id: raw_item}. media_type is 'movie' or 'show'."""
    if not MDBLIST_API_KEY or not tmdb_ids:
        return {}
    path = "movie" if media_type == "movie" else "show"
    ids = list(dict.fromkeys(int(i) for i in tmdb_ids if i))
    out: dict[int, dict] = {}
    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        backoff = 1.0
        async with _mdblist_sem:
            async with httpx.AsyncClient(timeout=60) as client:
                for _ in range(5):
                    try:
                        resp = await client.post(
                            f"{MDBLIST_BASE}/tmdb/{path}",
                            params={"apikey": MDBLIST_API_KEY},
                            json={"ids": chunk},
                        )
                    except httpx.HTTPError as e:
                        log.warning("mdblist bulk %s failed: %s", path, e)
                        break
                    if resp.status_code == 429:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 16.0)
                        continue
                    if resp.status_code != 200:
                        log.warning("mdblist bulk %s -> %s %s", path, resp.status_code, resp.text[:200])
                        break
                    rem = resp.headers.get("x-ratelimit-remaining")
                    if rem is not None and int(rem) < 20:
                        log.warning("mdblist rate limit low: %s remaining", rem)
                    data = resp.json()
                    for it in data if isinstance(data, list) else []:
                        tid = (it.get("ids") or {}).get("tmdb") or it.get("id")
                        if tid:
                            out[int(tid)] = it
                    break
    return out


async def store_ratings(media_type: str, items_by_tmdb: dict[int, dict]):
    """Upsert mdblist items into the ratings cache."""
    if not items_by_tmdb:
        return
    now = time.time()
    db = await get_db()
    try:
        for tid, it in items_by_tmdb.items():
            ids = it.get("ids") or {}
            imdb = it.get("imdb_id") or ids.get("imdb")
            mscore = it.get("score")
            await db.execute(
                """INSERT OR REPLACE INTO mdblist_ratings
                   (tmdb_id, media_type, imdb_id, mdblist_score, ratings_json, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (int(tid), media_type, imdb, int(mscore) if mscore is not None else None,
                 json.dumps(_parse_sources(it)), now),
            )
        await db.commit()
    finally:
        await db.close()


async def fetch_and_store_ratings(media_type: str, tmdb_ids: list[int]) -> dict[int, dict]:
    """Bulk-fetch from mdblist and persist; returns {tmdb_id: raw_item}."""
    items = await mdblist_bulk(media_type, tmdb_ids)
    await store_ratings(media_type, items)
    return items


# ---------- Rating config + formula ----------

def _norm_display_groups(groups: list) -> list:
    """Validate badge groups: keep only supported sources, valid visibility,
    drop empties, and ensure a source appears in at most one group (first wins)."""
    out: list = []
    used: set = set()
    for g in groups:
        if not isinstance(g, dict):
            continue
        vis = g.get("visibility")
        if vis not in ("always", "fallback"):
            vis = "always"
        srcs: list = []
        for s in g.get("sources") or []:
            if s in SUPPORTED_SOURCES and s not in used:
                srcs.append(s)
                used.add(s)
        if srcs:
            out.append({"visibility": vis, "sources": srcs})
    return out


def _flatten_groups(groups: list) -> list:
    """Flatten groups to the ordered unique source list (for older app clients)."""
    out: list = []
    for g in groups:
        for s in g["sources"]:
            if s not in out:
                out.append(s)
    return out


async def get_rating_config() -> dict:
    raw = await meta_get("rating_config")
    merged = json.loads(json.dumps(DEFAULT_RATING_CONFIG))  # deep copy
    if raw:
        try:
            cfg = json.loads(raw)
            groups = None
            if isinstance(cfg.get("display_groups"), list):
                groups = _norm_display_groups(cfg["display_groups"])
            if groups:
                merged["display_groups"] = groups
                merged["display_sources"] = _flatten_groups(groups)
            elif isinstance(cfg.get("display_sources"), list):
                # Legacy config (flat list) → one "always" group.
                ds = [s for s in cfg["display_sources"] if s in SUPPORTED_SOURCES]
                merged["display_sources"] = ds
                merged["display_groups"] = [{"visibility": "always", "sources": ds}] if ds else []
            if isinstance(cfg.get("formula"), dict):
                merged["formula"].update(cfg["formula"])
        except Exception:
            pass
    return merged


def effective_sources(sources: dict, cfg: dict) -> dict:
    """Apply the missing-MDbList-score policy: when mdblist gives no aggregate
    score and the policy is 'average', synthesize one as the mean of the
    available per-source scores. Idempotent. mdblist returns 0 (not null) for
    low-provider titles, so 0 is treated the same as missing."""
    md = sources.get("mdblist")
    if md and (md.get("score") or 0) > 0:
        return sources
    if cfg.get("formula", {}).get("missing_mdblist", "average") != "average":
        return sources
    vals = [v["score"] for k, v in sources.items()
            if k != "mdblist" and isinstance(v, dict) and v.get("score") is not None]
    if not vals:
        return sources
    out = dict(sources)
    out["mdblist"] = {"score": round(sum(vals) / len(vals), 1), "votes": None}
    return out


def compute_rating(sources: dict, cfg: dict) -> Optional[float]:
    """Canonical 0-100 rating from a sources dict per the configured formula."""
    sources = effective_sources(sources, cfg)
    formula = cfg.get("formula", {})
    md = sources.get("mdblist")
    md_score = float(md["score"]) if md and md.get("score") is not None else None

    if formula.get("preset", "mdblist") == "mdblist":
        return md_score

    weights = formula.get("weights", {})
    vote_aware = formula.get("vote_aware", False)
    min_votes = formula.get("min_votes", {})
    num = den = 0.0
    for src, data in sources.items():
        if src == "mdblist" or data.get("score") is None:
            continue
        w = float(weights.get(src, 0) or 0)
        if w <= 0:
            continue
        conf = 1.0
        if vote_aware:
            v = float(data.get("votes") or 0)
            m = float(min_votes.get(src, 0) or 0)
            conf = v / (v + m) if (v + m) > 0 else 1.0
        num += w * conf * float(data["score"])
        den += w * conf
    if den <= 0:
        return md_score  # nothing usable → fall back to the mdblist aggregate
    return round(num / den, 1)


async def ratings_for_tmdb(pairs: list[tuple]) -> dict[tuple, dict]:
    """Cached ratings rows for (tmdb_id, media_type) pairs →
    {(tmdb_id, media_type): {sources, mdblist_score, imdb_id}}."""
    if not pairs:
        return {}
    out: dict[tuple, dict] = {}
    by_type: dict[str, list[int]] = {}
    for tid, mt in pairs:
        by_type.setdefault(mt, []).append(int(tid))
    db = await get_db()
    try:
        for mt, ids in by_type.items():
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                ph = ",".join("?" * len(chunk))
                cursor = await db.execute(
                    f"SELECT * FROM mdblist_ratings WHERE media_type=? AND tmdb_id IN ({ph})",
                    [mt, *chunk],
                )
                for row in await cursor.fetchall():
                    try:
                        sources = json.loads(row["ratings_json"])
                    except Exception:
                        sources = {}
                    out[(row["tmdb_id"], mt)] = {
                        "sources": sources,
                        "mdblist_score": row["mdblist_score"],
                        "imdb_id": row["imdb_id"],
                    }
    finally:
        await db.close()
    return out


# ---------- Universal ratings API ----------

class RatingItem(BaseModel):
    tmdb_id: int
    media_type: str  # 'movie' | 'show'


class RatingBatchRequest(BaseModel):
    items: list[RatingItem]


@router.get("/ratings/config")
async def ratings_config_get():
    return await get_rating_config()


@router.put("/ratings/config", dependencies=[Depends(require_admin)])
async def ratings_config_put(cfg: dict):
    await meta_set("rating_config", json.dumps(cfg))
    return await get_rating_config()


@router.post("/ratings/batch")
async def ratings_batch(req: RatingBatchRequest):
    cfg = await get_rating_config()
    pairs = [(it.tmdb_id, "movie" if it.media_type == "movie" else "show") for it in req.items]
    cached = await ratings_for_tmdb(pairs)
    results = {}
    for tid, mt in pairs:
        row = cached.get((tid, mt))
        if not row:
            continue
        sources = effective_sources(row["sources"], cfg)
        results[str(tid)] = {
            "tmdb_id": tid,
            "media_type": mt,
            "sources": sources,
            "mdblist_score": row["mdblist_score"],
            "rating": compute_rating(sources, cfg),
        }
    return {"results": results, "display_sources": cfg["display_sources"]}


@router.get("/health")
async def health():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as count FROM mdblist_ratings")
        row = await cursor.fetchone()
        cursor2 = await db.execute("SELECT COUNT(*) as count FROM popular_items")
        row2 = await cursor2.fetchone()
        return {"status": "ok", "rated_items": row["count"], "popular_items": row2["count"]}
    finally:
        await db.close()


# ---------- Library ratings sync (nightly + on-demand) ----------

async def get_ratings_sync_config() -> dict:
    # Configured in the dashboard; defaults to nightly on at 03:00.
    cfg = {"enabled": True, "hour": 3}
    raw = await meta_get("ratings_sync")
    if raw:
        try:
            cfg.update({k: v for k, v in json.loads(raw).items() if k in ("enabled", "hour")})
        except Exception:
            pass
    return cfg


async def refresh_library_ratings():
    """Bulk-cache mdblist ratings for every movie/show in the Plex library."""
    if not MDBLIST_API_KEY:
        log.warning("MDBLIST_API_KEY not set, skipping library ratings sync")
        return
    if not plex_configured():
        log.warning("Plex not configured, skipping library ratings sync")
        return
    log.info("Library ratings sync: starting")
    data = await plex_get("/library/sections")
    dirs = (data or {}).get("MediaContainer", {}).get("Directory", [])
    total = 0
    for d in dirs:
        if d.get("type") not in ("movie", "show"):
            continue
        media_type = "movie" if d.get("type") == "movie" else "show"
        page = await plex_get(
            f"/library/sections/{d.get('key')}/all",
            {"type": PLEX_TYPE[media_type], "includeGuids": 1, "X-Plex-Container-Size": 10000},
        )
        metas = (page or {}).get("MediaContainer", {}).get("Metadata", [])
        tmdb_ids = [t for t in (tmdb_from_metadata(m) for m in metas) if t]
        log.info("Library ratings sync: %s '%s' -> %d titles", media_type, d.get("title"), len(tmdb_ids))
        items = await fetch_and_store_ratings(media_type, tmdb_ids)
        total += len(items)
    await meta_set("library_ratings_last_sync", str(time.time()))
    log.info("Library ratings sync: cached %d titles", total)


def schedule_library_sync(sync_cfg: dict):
    """(Re)register the nightly library-ratings cron job per config."""
    scheduler = scheduler_mod.scheduler
    if scheduler is None:
        return
    try:
        scheduler.remove_job("library_ratings")
    except Exception:
        pass
    if sync_cfg.get("enabled") and MDBLIST_API_KEY:
        scheduler.add_job(refresh_library_ratings, "cron", hour=int(sync_cfg.get("hour", 3)),
                          minute=15, id="library_ratings", name="Library ratings sync")
        log.info("Library ratings sync scheduled at %02d:15 daily", int(sync_cfg.get("hour", 3)))
