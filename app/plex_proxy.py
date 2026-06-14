"""Transparent Plex reverse-proxy with inline rating enrichment.

Forwards /plex/* to the configured Plex server using the *client's* own token and
headers (per-user state preserved), and injects our mdblist ratings into library
and hub JSON listings so the app needs no second round-trip.
"""

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .client_auth import require_plex_user
from .config import PLEX_URL
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
    metas = mc.get("Metadata") if isinstance(mc, dict) else None
    if not isinstance(metas, list) or not metas:
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


@router.api_route("/plex/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                  dependencies=[Depends(require_plex_user)])
async def plex_proxy(path: str, request: Request):
    """Transparent Plex reverse-proxy. Forwards to PLEX_URL with the CLIENT's own
    token/headers (per-user state preserved), and enriches library/hub JSON
    listings with our mdblist ratings inline so the app needs no second call."""
    if not plex_configured():
        raise HTTPException(503, "Plex not configured")

    params = dict(request.query_params)
    if request.method == "GET" and re.match(r"^(library|hubs)/", path):
        params.setdefault("includeGuids", "1")
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ_HEADERS}
    body = await request.body()

    try:
        up = await http_client().request(
            request.method, f"{PLEX_URL}/{path}",
            params=params, headers=fwd_headers, content=body, timeout=30,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Plex proxy error: {e}")

    ctype = up.headers.get("content-type", "")
    if request.method == "GET" and "application/json" in ctype:
        try:
            data = up.json()
            await _enrich_media_container(data)
            return JSONResponse(data, status_code=up.status_code)
        except Exception:
            pass  # fall through to passthrough on any parse/enrich issue

    resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in DROP_RESP_HEADERS}
    return Response(content=up.content, status_code=up.status_code,
                    media_type=ctype or None, headers=resp_headers)
