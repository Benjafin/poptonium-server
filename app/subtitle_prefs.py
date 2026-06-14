"""Per-series subtitle preferences (per user).

Plex has no per-series subtitle setting, so we keep one here, keyed by the Plex
account id + the show's ratingKey. The client applies the remembered language to
each episode of the show, falling back to Plex's own default selection when
there's no preference (or no matching track on the episode).

The owning account id is ALWAYS resolved server-side from the caller's Plex token
(`require_plex_account`), never trusted from the request body/query — otherwise
any caller could read or overwrite another user's prefs by changing the id.
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from .client_auth import require_plex_account
from .db import get_db

router = APIRouter()


class SubtitlePrefPayload(BaseModel):
    # Identity is taken from the authenticated token, not this field; it is kept
    # for backward compatibility with existing clients and ignored.
    user_id: Optional[str] = None
    series_key: str
    language: Optional[str] = None  # ISO-639 code; null = subtitles off


@router.get("/subtitle-prefs")
async def get_subtitle_prefs(user_id: str = Depends(require_plex_account)):
    """All of a user's per-series subtitle preferences, as {series_key: language|null}."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT series_key, language FROM subtitle_prefs WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        # null language = subtitles explicitly off; an absent key = no preference.
        return {"prefs": {r["series_key"]: r["language"] for r in rows}}
    finally:
        await db.close()


@router.put("/subtitle-prefs")
async def set_subtitle_pref(p: SubtitlePrefPayload, user_id: str = Depends(require_plex_account)):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO subtitle_prefs (user_id, series_key, language, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, p.series_key, p.language, time.time()),
        )
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()


@router.delete("/subtitle-prefs")
async def delete_subtitle_pref(series_key: str = Query(...), user_id: str = Depends(require_plex_account)):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM subtitle_prefs WHERE user_id = ? AND series_key = ?",
            (user_id, series_key),
        )
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()
