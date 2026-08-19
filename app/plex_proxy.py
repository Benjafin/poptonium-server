"""Transparent Plex reverse-proxy with inline rating enrichment.

Forwards /plex/* to the configured Plex server using the *client's* own token and
headers (per-user state preserved), and injects our mdblist ratings into library
and hub JSON listings so the app needs no second round-trip.
"""

import json
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .client_auth import require_plex_user
from .config import log, settings
from .http_client import http_client
from .plex import (
    DROP_REQ_HEADERS,
    DROP_RESP_HEADERS,
    plex_configured,
    tmdb_from_metadata,
)
from .ratings import compute_rating, effective_sources, get_rating_config, ratings_for_tmdb

router = APIRouter()


async def _enrich_media_container(data) -> None:
    """Inject `mdblistRating` + `mdblistSources` into each Metadata item that has
    a cached rating (matched by TMDB id). Mutates `data` in place."""
    mc = data.get("MediaContainer") if isinstance(data, dict) else None
    if not isinstance(mc, dict):
        return
    metas = mc.get("Metadata") if isinstance(mc.get("Metadata"), list) else []
    # Hub endpoints (/hubs/search, /hubs/…) nest their items one level deeper, one
    # list per hub; without this their cards would be the only ones with no ratings.
    for hub in mc.get("Hub") or []:
        if isinstance(hub, dict) and isinstance(hub.get("Metadata"), list):
            metas = metas + hub["Metadata"]
    if not metas:
        return
    cfg = await get_rating_config()
    pairs = []
    for m in metas:
        tid = tmdb_from_metadata(m)
        if tid:
            pairs.append((tid, "movie" if m.get("type") == "movie" else "show"))
    if not pairs:
        return
    cache = await ratings_for_tmdb(pairs)
    for m in metas:
        tid = tmdb_from_metadata(m)
        if not tid:
            continue
        row = cache.get((tid, "movie" if m.get("type") == "movie" else "show"))
        if not row:
            continue
        sources = effective_sources(row["sources"], cfg)
        if not sources:
            continue
        m["mdblistSources"] = sources
        rating = compute_rating(sources, cfg)
        if rating is not None:
            m["mdblistRating"] = rating


# Fields a library listing actually needs. The app renders shelf/grid cards from these
# and sorts on addedAt plus the injected mdblist rating; everything else in a Plex
# listing is only ever read on the detail page, which refetches /library/metadata/{key}
# anyway. Measured on a 645-item movie section: 1537 KB -> 349 KB (4.4x).
LIST_FIELDS = frozenset("""
    ratingKey key parentRatingKey grandparentRatingKey guid type title parentTitle
    grandparentTitle year thumb art parentThumb grandparentThumb grandparentArt
    duration viewOffset index parentIndex leafCount viewedLeafCount viewCount
    addedAt lastViewedAt rating Guid Genre Image mdblistRating mdblistSources
""".split())

# Listings only. /library/metadata/{key} (the detail fetch) must stay complete, so this
# deliberately does not match it; /children and /all carry the same card-shaped rows.
LIST_PATHS = re.compile(r"^library/(sections/[^/]+/(all|recentlyAdded|newest|onDeck)|"
                        r"metadata/[^/]+/children|onDeck)/?$")


def _trim_listing(data) -> None:
    """Drop fields no card renders from a listing response, in place.

    The heavy ones are Media (with its Part/Stream trees), summary, and the three
    Image variants that aren't clearLogo — together about two thirds of the payload.
    Anything that isn't the shape we expect is left alone.
    """
    mc = data.get("MediaContainer") if isinstance(data, dict) else None
    if not isinstance(mc, dict):
        return
    metas = mc.get("Metadata")
    if not isinstance(metas, list):
        return
    for m in metas:
        if not isinstance(m, dict):
            continue
        for key in [k for k in m if k not in LIST_FIELDS]:
            del m[key]
        # clearLogo is the only Image the app looks up (PlexItem.clearLogoPath); the
        # other three variants it ships are a fifth of the payload on their own.
        images = m.get("Image")
        if isinstance(images, list):
            logos = [i for i in images
                     if isinstance(i, dict) and i.get("type") == "clearLogo"]
            if logos:
                m["Image"] = logos
            else:
                del m["Image"]


@router.api_route("/plex/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                  dependencies=[Depends(require_plex_user)])
async def plex_proxy(path: str, request: Request):
    """Transparent Plex reverse-proxy. Forwards to PLEX_URL with the CLIENT's own
    token/headers (per-user state preserved), and enriches library/hub JSON
    listings with our mdblist ratings inline so the app needs no second call."""
    if not plex_configured():
        raise HTTPException(503, "Plex not configured")

    params = dict(request.query_params)
    if request.method == "GET" and re.match(r"^(library|hubs|search)", path):
        params.setdefault("includeGuids", "1")
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ_HEADERS}
    body = await request.body()

    try:
        up = await http_client().request(
            request.method, f"{settings.PLEX_URL}/{path}",
            params=params, headers=fwd_headers, content=body, timeout=30,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Plex proxy error: {e}")

    ctype = up.headers.get("content-type", "")
    if request.method == "GET" and "application/json" in ctype:
        try:
            data = up.json()
            await _enrich_media_container(data)
            if LIST_PATHS.match(path):
                _trim_listing(data)
                body_out = json.dumps(data, separators=(",", ":")).encode()
                log.info("plex listing %s: %d -> %d bytes (%.0f%% saved)", path,
                         len(up.content), len(body_out),
                         100 * (1 - len(body_out) / max(len(up.content), 1)))
                return Response(content=body_out, status_code=up.status_code,
                                media_type="application/json")
            return JSONResponse(data, status_code=up.status_code)
        except Exception:
            pass  # fall through to passthrough on any parse/enrich issue

    resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in DROP_RESP_HEADERS}
    return Response(content=up.content, status_code=up.status_code,
                    media_type=ctype or None, headers=resp_headers)
