"""Overseerr request proxy: request media, list requested ids, and search."""

import time
from typing import Optional

from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .client_auth import plex_user_identity, require_plex_user, require_plex_user_token
from .config import OVERSEERR_API_KEY, OVERSEERR_URL, log

router = APIRouter()

# Overseerr's user list keyed for lookup (plexId -> id, lowercased email -> id),
# cached briefly so we don't page the whole list on every request.
_USER_CACHE_TTL = 300  # seconds
_user_cache: dict = {"expiry": 0.0, "by_plex_id": {}, "by_email": {}}


async def _overseerr_user_maps() -> tuple[dict[int, int], dict[str, int]]:
    """Return (plexId -> Overseerr user id, lowercased email -> Overseerr user id).

    Cached for a short window on success; on failure we return the previously
    cached maps (possibly empty) so a transient Overseerr blip just falls back to
    owner attribution rather than erroring the request."""
    now = time.time()
    if _user_cache["expiry"] > now:
        return _user_cache["by_plex_id"], _user_cache["by_email"]

    by_plex_id: dict[int, int] = {}
    by_email: dict[str, int] = {}
    ok = False
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            skip, take = 0, 100
            while True:
                resp = await client.get(
                    f"{OVERSEERR_URL}/api/v1/user",
                    params={"take": take, "skip": skip},
                    headers={"X-Api-Key": OVERSEERR_API_KEY},
                )
                if resp.status_code != 200:
                    log.error("Overseerr user list failed: %s", resp.status_code)
                    break
                data = resp.json()
                results = data.get("results", [])
                ok = True  # at least one page came back cleanly
                for u in results:
                    uid = u.get("id")
                    if uid is None:
                        continue
                    pid = u.get("plexId")
                    if pid is not None:
                        try:
                            by_plex_id[int(pid)] = uid
                        except (TypeError, ValueError):
                            pass
                    email = u.get("email")
                    if email:
                        by_email[email.strip().lower()] = uid
                total = data.get("pageInfo", {}).get("results", 0)
                skip += take
                if not results or skip >= total:
                    break
    except Exception as e:
        log.error("Overseerr user fetch error: %s", e)

    if ok:
        _user_cache.update(expiry=now + _USER_CACHE_TTL, by_plex_id=by_plex_id, by_email=by_email)
        return by_plex_id, by_email
    return _user_cache["by_plex_id"], _user_cache["by_email"]


def _match_user(
    by_plex_id: dict[int, int], by_email: dict[str, int],
    plex_id, email: Optional[str],
) -> Optional[int]:
    """Match a Plex identity to an Overseerr user id — plexId first, email fallback."""
    if plex_id is not None:
        try:
            uid = by_plex_id.get(int(plex_id))
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            return uid
    if email:
        return by_email.get(email)
    return None


async def _import_overseerr_user(plex_id: int) -> Optional[int]:
    """Ask Overseerr to import this Plex user (only works if they're shared to the
    configured Plex server), returning their new Overseerr user id. None if the
    import created no matching user (e.g. the user isn't shared to the server)."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.post(
                f"{OVERSEERR_URL}/api/v1/user/import-from-plex",
                json={"plexIds": [str(plex_id)]},
                headers={"X-Api-Key": OVERSEERR_API_KEY, "Content-Type": "application/json"},
            )
        if resp.status_code not in (200, 201):
            log.error("Overseerr user import failed: %s %s", resp.status_code, resp.text[:200])
            return None
        created = resp.json()
        for u in (created if isinstance(created, list) else []):
            pid = u.get("plexId")
            if pid is None:
                continue
            try:
                if int(pid) == int(plex_id):
                    return u.get("id")
            except (TypeError, ValueError):
                pass
    except Exception as e:
        log.error("Overseerr user import error: %s", e)
    return None


async def _overseerr_user_id_for(token: str) -> Optional[int]:
    """Resolve the requesting caller's Overseerr user id from their Plex token, so
    the request is attributed to them rather than to the API-key owner. Imports the
    user into Overseerr on first sight when possible. Returns None when the caller
    can't be matched or imported (caller falls back to owner attribution)."""
    identity = await plex_user_identity(token)
    if not identity:
        return None
    plex_id = identity.get("plex_id")
    email = identity.get("email")

    by_plex_id, by_email = await _overseerr_user_maps()
    uid = _match_user(by_plex_id, by_email, plex_id, email)
    if uid is not None:
        return uid

    # Not in Overseerr yet — import them from the Plex server, then re-resolve.
    if plex_id is not None:
        imported = await _import_overseerr_user(plex_id)
        if imported is not None:
            _user_cache["expiry"] = 0.0  # bust cache so later lookups see the new user
            log.info("Imported Plex user %s into Overseerr as user %s", plex_id, imported)
            return imported
    return None


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


@router.post("/overseerr/request")
async def overseerr_request(
    req: OverseerrRequest,
    token: str = Depends(require_plex_user_token),
):
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

    # Attribute the request to the actual requesting Plex user when we can map
    # them to an Overseerr account; otherwise it falls back to the API-key owner.
    user_id = await _overseerr_user_id_for(token)
    if user_id is not None:
        body["userId"] = user_id
    else:
        log.info("Overseerr request: no matching Overseerr user, attributing to API-key owner")

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
