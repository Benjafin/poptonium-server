"""Overseerr request proxy: request media, list requested ids, and search."""

from typing import Optional

from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .client_auth import require_plex_user
from .config import OVERSEERR_API_KEY, OVERSEERR_URL, log

router = APIRouter()


class OverseerrRequest(BaseModel):
    tmdb_id: int
    media_type: str  # "movie" or "show"
    # For TV: explicit season numbers to request. None/empty = all seasons.
    seasons: Optional[list[int]] = None


def _season_statuses(media_info: dict) -> dict[int, int]:
    """Map seasonNumber -> Overseerr media status (1 none, 2 pending, 3 processing,
    4 partial, 5 available) from a show's mediaInfo. Seasons absent here are not
    requested yet (treated as status 1 by callers)."""
    statuses: dict[int, int] = {}
    for s in (media_info.get("seasons") or []):
        n = s.get("seasonNumber")
        st = s.get("status")
        if n is not None and st is not None:
            statuses[n] = st
    return statuses


@router.post("/overseerr/request", dependencies=[Depends(require_plex_user)])
async def overseerr_request(req: OverseerrRequest):
    if not OVERSEERR_URL or not OVERSEERR_API_KEY:
        raise HTTPException(503, "Overseerr not configured")

    # Map our media_type to Overseerr's mediaType
    overseerr_type = "movie" if req.media_type == "movie" else "tv"

    body = {
        "mediaType": overseerr_type,
        "mediaId": req.tmdb_id,
    }
    # For TV shows, request the chosen seasons (or all when none specified).
    if overseerr_type == "tv":
        body["seasons"] = req.seasons if req.seasons else "all"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OVERSEERR_URL}/api/v1/request",
            json=body,
            headers={
                "X-Api-Key": OVERSEERR_API_KEY,
                "Content-Type": "application/json",
            },
        )

    if resp.status_code in (200, 201):
        return {"status": "requested", "detail": resp.json()}
    else:
        log.error("Overseerr request failed: %s %s", resp.status_code, resp.text[:500])
        raise HTTPException(resp.status_code, f"Overseerr error: {resp.text[:200]}")


@router.get("/overseerr/requested", dependencies=[Depends(require_plex_user)])
async def overseerr_requested():
    """Return all TMDB IDs that have been requested or are available in Overseerr."""
    if not OVERSEERR_URL or not OVERSEERR_API_KEY:
        return {"movie": [], "tv": []}

    requested_movies = []
    requested_tv = []

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Fetch all media with any status (requested, processing, available, etc.)
            # Overseerr /api/v1/media returns all known media entries
            page = 0
            page_size = 50
            while True:
                resp = await client.get(
                    f"{OVERSEERR_URL}/api/v1/media",
                    params={"take": page_size, "skip": page * page_size},
                    headers={"X-Api-Key": OVERSEERR_API_KEY},
                )
                if resp.status_code != 200:
                    log.error("Overseerr media list failed: %s", resp.status_code)
                    break

                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break

                for item in results:
                    tmdb_id = item.get("tmdbId")
                    media_type = item.get("mediaType")
                    status = item.get("status")
                    # status: 1=unknown, 2=pending, 3=processing, 4=partially_available, 5=available
                    if tmdb_id and status and status >= 2:
                        if media_type == "movie":
                            requested_movies.append(tmdb_id)
                        elif media_type == "tv":
                            requested_tv.append(tmdb_id)

                total = data.get("pageInfo", {}).get("results", 0)
                if (page + 1) * page_size >= total:
                    break
                page += 1

    except Exception as e:
        log.error("Overseerr requested check failed: %s", e)

    return {"movie": requested_movies, "tv": requested_tv}


@router.get("/overseerr/search", dependencies=[Depends(require_plex_user)])
async def overseerr_search(query: str = Query(..., min_length=1)):
    """Search Overseerr and return results in DiscoverItem format."""
    if not OVERSEERR_URL or not OVERSEERR_API_KEY:
        raise HTTPException(503, "Overseerr not configured")

    try:
        encoded_query = quote(query.strip())
        search_url = f"{OVERSEERR_URL}/api/v1/search?query={encoded_query}&page=1&language=en"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                search_url,
                headers={"X-Api-Key": OVERSEERR_API_KEY},
            )
            if resp.status_code != 200:
                log.error("Overseerr search failed: %s %s", resp.status_code, resp.text[:200])
                raise HTTPException(resp.status_code, "Overseerr search failed")

            data = resp.json()
            results = data.get("results", [])

            items = []
            for r in results:
                media_type = r.get("mediaType")
                if media_type not in ("movie", "tv"):
                    continue

                tmdb_id = r.get("id")
                title = r.get("title") or r.get("name") or ""
                poster_path = r.get("posterPath")
                poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
                overview = r.get("overview")

                # Extract year from releaseDate or firstAirDate
                date_str = r.get("releaseDate") or r.get("firstAirDate") or ""
                year = int(date_str[:4]) if len(date_str) >= 4 else None

                # Media status from mediaInfo
                media_info = r.get("mediaInfo") or {}
                status = media_info.get("status", 0)
                # status: 2=pending, 3=processing, 4=partial, 5=available

                items.append({
                    "tmdb_id": tmdb_id,
                    "title": title,
                    "year": year,
                    "media_type": "movie" if media_type == "movie" else "show",
                    "poster_url": poster_url,
                    "description": overview,
                    "media_status": status,
                })

            return {"items": items, "count": len(items)}

    except HTTPException:
        raise
    except Exception as e:
        log.error("Overseerr search error: %s", e)
        raise HTTPException(500, str(e))


@router.get("/overseerr/details", dependencies=[Depends(require_plex_user)])
async def overseerr_details(
    tmdb_id: int = Query(...),
    media_type: str = Query(..., pattern="^(movie|show)$"),
):
    """Full TMDB-grade detail for an arbitrary tmdb_id (in library or not), via Overseerr.

    Returns overview, backdrop, runtime, genres and cast so the Discover detail page can look
    like the library one for items the user doesn't own yet.
    """
    if not OVERSEERR_URL or not OVERSEERR_API_KEY:
        raise HTTPException(503, "Overseerr not configured")

    overseerr_type = "movie" if media_type == "movie" else "tv"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{OVERSEERR_URL}/api/v1/{overseerr_type}/{tmdb_id}",
                headers={"X-Api-Key": OVERSEERR_API_KEY},
            )
        if resp.status_code != 200:
            log.error("Overseerr details failed: %s %s", resp.status_code, resp.text[:200])
            raise HTTPException(resp.status_code, "Overseerr details failed")
        d = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Overseerr details error: %s", e)
        raise HTTPException(500, str(e))

    def tmdb_image(path, size):
        return f"https://image.tmdb.org/t/p/{size}{path}" if path else None

    # Runtime: movies carry a single `runtime`; shows carry `episodeRunTime` list.
    runtime = d.get("runtime")
    if not runtime:
        ep_runtimes = d.get("episodeRunTime") or []
        runtime = ep_runtimes[0] if ep_runtimes else None

    date_str = d.get("releaseDate") or d.get("firstAirDate") or ""
    year = int(date_str[:4]) if len(date_str) >= 4 else None

    cast = []
    for c in (d.get("credits") or {}).get("cast", [])[:20]:
        cast.append({
            "name": c.get("name") or "",
            "character": c.get("character") or None,
            "profile_url": tmdb_image(c.get("profilePath"), "w185"),
        })

    media_info = d.get("mediaInfo") or {}

    # Per-season list (shows only): name, episode count, and current Overseerr status so
    # the client can show which seasons are already requested/available and pick the rest.
    seasons = None
    if media_type == "show":
        season_status = _season_statuses(media_info)
        seasons = []
        for s in (d.get("seasons") or []):
            n = s.get("seasonNumber")
            if n is None or n < 1:  # skip specials (season 0)
                continue
            seasons.append({
                "season_number": n,
                "name": s.get("name") or f"Season {n}",
                "episode_count": s.get("episodeCount"),
                "status": season_status.get(n, 1),
            })
        seasons.sort(key=lambda x: x["season_number"])

    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": d.get("title") or d.get("name") or "",
        "year": year,
        "overview": d.get("overview") or None,
        "tagline": d.get("tagline") or None,
        "runtime": runtime,
        "backdrop_url": tmdb_image(d.get("backdropPath"), "w1280"),
        "poster_url": tmdb_image(d.get("posterPath"), "w500"),
        "genres": [g.get("name") for g in (d.get("genres") or []) if g.get("name")],
        "cast": cast,
        "number_of_seasons": d.get("numberOfSeasons"),
        "media_status": media_info.get("status"),
        "seasons": seasons,
    }


@router.get("/overseerr/status")
async def overseerr_status():
    return {"configured": bool(OVERSEERR_URL and OVERSEERR_API_KEY)}
