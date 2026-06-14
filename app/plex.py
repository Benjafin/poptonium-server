"""Plex HTTP helpers and Metadata mapping.

Low-level access to the Plex Media Server: a cached GET helper, image/GUID
extraction, mapping a Plex Metadata object to the uniform shape the app renders,
and uploading a subtitle. No rating logic lives here (see ``ratings``); the
rating-enriched mapping (``map_with_ratings``) lives in ``sections``.
"""

import time
from typing import Optional
from urllib.parse import urlencode

from .config import PLEX_TOKEN, PLEX_URL, log
from .http_client import http_client


def plex_configured() -> bool:
    return bool(PLEX_URL and PLEX_TOKEN)


# Short-lived cache of the reachability probe so the boot gate / status polls
# don't hit Plex on every request.
_reachable_cache: tuple[float, bool] | None = None
_REACHABLE_TTL = 15  # seconds


async def plex_reachable() -> bool:
    """True only if Plex is configured AND answers an authorized /identity probe."""
    global _reachable_cache
    if not plex_configured():
        return False
    if _reachable_cache and _reachable_cache[0] > time.time():
        return _reachable_cache[1]
    ok = False
    try:
        r = await http_client().get(
            f"{PLEX_URL}/identity", headers={"X-Plex-Token": PLEX_TOKEN}, timeout=5
        )
        ok = r.status_code == 200
    except Exception:
        ok = False
    _reachable_cache = (time.time() + _REACHABLE_TTL, ok)
    return ok


# Hop-by-hop / encoding headers we must not copy verbatim between proxy hops
# (shared by the Plex reverse-proxy and the plugin proxy).
DROP_REQ_HEADERS = {"host", "content-length", "connection", "accept-encoding"}
DROP_RESP_HEADERS = {"content-encoding", "transfer-encoding", "content-length", "connection"}


# Short-TTL cache of Plex GET listing responses. Section resolution re-queries the
# same library pages on every app open / pull-to-refresh; caching the slow Plex
# round-trip for a few seconds collapses those. Randomize/sort/{director} run in
# Python on TOP of the cached fetch, so they still vary per request.
_plex_cache: dict[str, tuple[float, dict]] = {}
_PLEX_CACHE_MAX = 256
SECTION_CACHE_TTL = 30   # seconds; how long a section's Plex listing is reused


async def plex_get(path: str, params: dict | None = None, cache_ttl: float = 0) -> dict | None:
    if not plex_configured():
        return None
    key = None
    if cache_ttl > 0:
        qs = urlencode(sorted(params.items(), key=lambda kv: kv[0])) if params else ""
        key = f"{path}::{qs}"
        hit = _plex_cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    try:
        # NOTE: pass params through as-is (None, not {}). An empty dict makes
        # httpx rebuild the query and DROP any query string already in `path`
        # (which is how filter operator clauses like `addedAt>=` are passed).
        resp = await http_client().get(
            f"{PLEX_URL}{path}",
            params=params,
            headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Plex GET %s -> %d", path, resp.status_code)
            return None
        data = resp.json()
        if key is not None:
            if len(_plex_cache) > _PLEX_CACHE_MAX:
                _plex_cache.clear()
            _plex_cache[key] = (time.time() + cache_ttl, data)
        return data
    except Exception as e:
        log.warning("Plex GET %s failed: %s", path, e)
        return None


def plex_image(m: dict, image_type: str) -> Optional[str]:
    """Pull a Plex-relative image URL of the given type from the Metadata's Image array."""
    for img in m.get("Image", []) or []:
        if img.get("type") == image_type:
            return img.get("url")
    return None


def tmdb_from_metadata(m: dict) -> Optional[int]:
    """Extract the TMDB id from a Plex Metadata's Guid array (needs includeGuids=1)."""
    for g in m.get("Guid", []) or []:
        gid = g.get("id", "")
        if gid.startswith("tmdb://"):
            try:
                return int(gid[len("tmdb://"):])
            except ValueError:
                return None
    return None


def map_plex_item(m: dict, rating: Optional[float] = None, sources: Optional[dict] = None) -> dict:
    """Map a Plex Metadata object to the uniform shape the app renders.

    thumb/art/clear_logo are Plex-relative paths the client fetches via its own
    connection. `rating` is the canonical 0-100 score (configured formula);
    `sources` is the per-source {score,votes} the client renders as badges."""
    return {
        "rating_key": str(m.get("ratingKey", "")),
        "tmdb_id": tmdb_from_metadata(m),
        "title": m.get("title", ""),
        "type": m.get("type", ""),
        "year": m.get("year"),
        "thumb": m.get("thumb"),
        "art": m.get("art"),
        "clear_logo": plex_image(m, "clearLogo"),
        "summary": m.get("summary"),
        "content_rating": m.get("contentRating"),
        "added_at": m.get("addedAt"),
        "duration": m.get("duration"),
        "child_count": m.get("childCount") or m.get("leafCount"),
        "rating": round(rating, 1) if rating is not None else None,
        "sources": sources or {},
    }


async def plex_upload_subtitle(rating_key: str, content: bytes, language: str, fmt: str, title: str) -> bool:
    """Attach a subtitle to a Plex item via POST /library/metadata/{rk}/subtitles.
    Plex stores it as an external (sidecar-style) stream, no media-folder mount needed."""
    if not plex_configured():
        return False
    params = {"language": language, "format": fmt, "title": title}
    try:
        resp = await http_client().post(
            f"{PLEX_URL}/library/metadata/{rating_key}/subtitles",
            params=params,
            content=content,
            headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "text/plain, */*"},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            log.warning("Plex subtitle upload -> %d %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("Plex subtitle upload failed: %s", e)
        return False
