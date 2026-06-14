"""OpenSubtitles: search subtitles and download them into Plex as a stream."""

import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .client_auth import plex_user_can_access, require_plex_user, require_plex_user_token
from .config import (
    OPENSUBTITLES_API_BASE,
    OPENSUBTITLES_API_KEY,
    OPENSUBTITLES_PASSWORD,
    OPENSUBTITLES_USER_AGENT,
    OPENSUBTITLES_USERNAME,
    log,
)
from .db import meta_get, meta_set
from .http_client import http_client
from .plex import plex_configured, plex_upload_subtitle

router = APIRouter()


def opensubtitles_configured() -> bool:
    return bool(OPENSUBTITLES_API_KEY and OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD)


def _os_headers(token: Optional[str] = None) -> dict:
    h = {
        "Api-Key": OPENSUBTITLES_API_KEY,
        "User-Agent": OPENSUBTITLES_USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _clean_imdb(v) -> Optional[str]:
    """OpenSubtitles wants a numeric IMDB id (no 'tt' prefix)."""
    if v is None:
        return None
    s = str(v).lower().replace("tt", "").lstrip("0")
    return s if s.isdigit() else None


async def _os_token() -> Optional[tuple[str, str]]:
    """Return (jwt_token, api_base) for OpenSubtitles, logging in (and caching the
    token ~23h) as needed. The login response may redirect us to a per-user base_url."""
    raw = await meta_get("opensubtitles_token")
    if raw:
        try:
            d = json.loads(raw)
            if d.get("token") and d.get("expires_at", 0) > time.time():
                return d["token"], d.get("base_url") or OPENSUBTITLES_API_BASE
        except Exception:
            pass
    if not opensubtitles_configured():
        return None
    try:
        resp = await http_client().post(
            f"{OPENSUBTITLES_API_BASE}/login",
            headers=_os_headers(),
            json={"username": OPENSUBTITLES_USERNAME, "password": OPENSUBTITLES_PASSWORD},
            timeout=20,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("OpenSubtitles login -> %d %s", resp.status_code, resp.text[:200])
            return None
        d = resp.json()
        token = d.get("token")
        if not token:
            return None
        base = d.get("base_url")  # e.g. "vip-api.opensubtitles.com" (host only)
        base_url = f"https://{base}/api/v1" if base else OPENSUBTITLES_API_BASE
        await meta_set("opensubtitles_token", json.dumps({
            "token": token, "base_url": base_url, "expires_at": time.time() + 23 * 3600,
        }))
        return token, base_url
    except Exception as e:
        log.warning("OpenSubtitles login failed: %s", e)
        return None


@router.get("/opensubtitles/status")
async def opensubtitles_status():
    return {"configured": opensubtitles_configured()}


@router.get("/opensubtitles/search", dependencies=[Depends(require_plex_user)])
async def opensubtitles_search(
    query: Optional[str] = None,
    imdb_id: Optional[str] = None,
    tmdb_id: Optional[int] = None,
    parent_imdb_id: Optional[str] = None,
    parent_tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    year: Optional[int] = None,
    languages: str = "en",
    type: Optional[str] = None,
):
    if not opensubtitles_configured():
        raise HTTPException(status_code=503, detail="OpenSubtitles not configured")

    params: dict = {}
    # OpenSubtitles wants languages comma-separated, lowercased and sorted.
    langs = sorted({l.strip().lower() for l in languages.split(",") if l.strip()})
    if langs:
        params["languages"] = ",".join(langs)
    if type in ("movie", "episode", "all"):
        params["type"] = type
    # For episodes the season/episode pair pairs with the *parent* (series) id.
    if season is not None:
        params["season_number"] = season
    if episode is not None:
        params["episode_number"] = episode
    if (pid := _clean_imdb(parent_imdb_id)):
        params["parent_imdb_id"] = pid
    if parent_tmdb_id:
        params["parent_tmdb_id"] = parent_tmdb_id
    if (iid := _clean_imdb(imdb_id)):
        params["imdb_id"] = iid
    if tmdb_id:
        params["tmdb_id"] = tmdb_id
    if year:
        params["year"] = year
    if query:
        params["query"] = query.strip()

    tok = await _os_token()
    token = tok[0] if tok else None
    base = tok[1] if tok else OPENSUBTITLES_API_BASE
    try:
        resp = await http_client().get(
            f"{base}/subtitles", params=params, headers=_os_headers(token), timeout=20,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("OpenSubtitles search -> %d %s", resp.status_code, resp.text[:200])
            raise HTTPException(status_code=502, detail="OpenSubtitles search failed")
        data = resp.json()
    except HTTPException:
        raise
    except Exception as e:
        log.warning("OpenSubtitles search failed: %s", e)
        raise HTTPException(status_code=502, detail="OpenSubtitles search failed")

    results = []
    for item in data.get("data", []) or []:
        a = item.get("attributes", {}) or {}
        files = a.get("files", []) or []
        if not files:
            continue
        f0 = files[0]
        results.append({
            "file_id": f0.get("file_id"),
            "file_name": f0.get("file_name"),
            "language": a.get("language"),
            "release": a.get("release"),
            "downloads": a.get("download_count"),
            "hearing_impaired": bool(a.get("hearing_impaired")),
            "hd": bool(a.get("hd")),
            "fps": a.get("fps"),
            "ai_translated": bool(a.get("ai_translated")),
            "machine_translated": bool(a.get("machine_translated")),
            "ratings": a.get("ratings"),
            "uploader": (a.get("uploader") or {}).get("name"),
            "upload_date": a.get("upload_date"),
        })
    return {"results": results}


class OSDownloadPayload(BaseModel):
    file_id: int
    rating_key: str
    language: Optional[str] = None
    sub_format: Optional[str] = "srt"


@router.post("/opensubtitles/download")
async def opensubtitles_download(
    payload: OSDownloadPayload,
    caller_token: str = Depends(require_plex_user_token),
):
    if not opensubtitles_configured():
        raise HTTPException(status_code=503, detail="OpenSubtitles not configured")
    if not plex_configured():
        raise HTTPException(status_code=503, detail="Plex not configured")

    # The upload below uses the server's (admin) Plex token, so confirm the CALLER
    # actually has access to this item before writing a subtitle onto it — a
    # low-privilege user must not be able to target items they can't see.
    if not await plex_user_can_access(caller_token, payload.rating_key):
        raise HTTPException(status_code=403, detail="You don't have access to that item")

    tok = await _os_token()
    if not tok:
        raise HTTPException(status_code=502, detail="OpenSubtitles login failed")
    token, base = tok

    # 1. Request a (temporary) download link for the file.
    try:
        resp = await http_client().post(
            f"{base}/download",
            headers=_os_headers(token),
            json={"file_id": payload.file_id, "sub_format": payload.sub_format or "srt"},
            timeout=20,
            follow_redirects=True,
        )
    except Exception as e:
        log.warning("OpenSubtitles download request failed: %s", e)
        raise HTTPException(status_code=502, detail="OpenSubtitles download failed")
    if resp.status_code == 406:
        raise HTTPException(status_code=429, detail="OpenSubtitles daily download quota exceeded")
    if resp.status_code != 200:
        log.warning("OpenSubtitles download -> %d %s", resp.status_code, resp.text[:200])
        raise HTTPException(status_code=502, detail="OpenSubtitles download failed")
    d = resp.json()
    link = d.get("link")
    file_name = d.get("file_name") or "subtitle.srt"
    remaining = d.get("remaining")
    if not link:
        raise HTTPException(status_code=502, detail="OpenSubtitles returned no link")

    # 2. Fetch the actual subtitle bytes from the temporary link.
    try:
        sub_resp = await http_client().get(link, timeout=30, follow_redirects=True)
        if sub_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="subtitle fetch failed")
        content = sub_resp.content
    except HTTPException:
        raise
    except Exception as e:
        log.warning("OpenSubtitles file fetch failed: %s", e)
        raise HTTPException(status_code=502, detail="subtitle fetch failed")

    # 3. Upload to Plex so it becomes a selectable subtitle stream on the item.
    ok = await plex_upload_subtitle(
        payload.rating_key, content,
        language=payload.language or "en",
        fmt=payload.sub_format or "srt",
        title=file_name,
    )
    if not ok:
        raise HTTPException(status_code=502, detail="Plex subtitle upload failed")

    return {"ok": True, "file_name": file_name, "remaining": remaining, "language": payload.language}
