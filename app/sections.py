"""Custom Library sections: CRUD, reorder, and the client resolution endpoints.

Sections drive the app's Library page. This module owns persistence and the HTTP
surface; the per-type resolution logic lives in ``section_resolve``.
"""

import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_admin
from .config import section_min_version
from .db import get_db
from .section_resolve import resolve_section

router = APIRouter()


class SectionPayload(BaseModel):
    title: str
    subtitle: Optional[str] = None
    type: str  # 'plex_collection' | 'filter'
    style: str = "row"  # 'row' | 'hero'
    # Placement relative to the built-in shelves:
    # 'top' | 'after_all' | 'after_movies' | 'after_shows'.
    position: str = "top"
    sort_order: int = 0
    enabled: bool = True
    config: dict = {}


class ReorderItem(BaseModel):
    id: int
    sort_order: int
    # The WYSIWYG board persists placement together with order on drop, so a
    # cross-shelf move lands atomically in one reorder call.
    position: Optional[str] = None


def _section_to_dict(row, include_config: bool = True) -> dict:
    d = {
        "id": row["id"],
        "title": row["title"],
        "subtitle": row["subtitle"],
        "type": row["type"],
        "style": row["style"],
        "position": row["position"],
        "sort_order": row["sort_order"],
        "enabled": bool(row["enabled"]),
        # Derived from type/style (not stored): the min app version that can render it.
        "min_app_version": section_min_version(row["type"], row["style"]),
    }
    if include_config:
        try:
            d["config"] = json.loads(row["config"])
        except Exception:
            d["config"] = {}
    return d


@router.get("/sections")
async def list_sections():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM sections ORDER BY sort_order ASC, id ASC"
        )
        rows = await cursor.fetchall()
        return [_section_to_dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/sections/resolved")
async def resolved_sections():
    """Client endpoint: enabled sections, each with items resolved from Plex."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM sections WHERE enabled = 1 ORDER BY sort_order ASC, id ASC"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    sections = await asyncio.gather(*[resolve_section(r) for r in rows])
    return {"sections": list(sections)}


@router.post("/sections/reorder", dependencies=[Depends(require_admin)])
async def reorder_sections(items: list[ReorderItem]):
    now = time.time()
    db = await get_db()
    try:
        for it in items:
            if it.position is not None:
                await db.execute(
                    "UPDATE sections SET sort_order = ?, position = ?, updated_at = ? WHERE id = ?",
                    (it.sort_order, it.position, now, it.id),
                )
            else:
                await db.execute(
                    "UPDATE sections SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (it.sort_order, now, it.id),
                )
        await db.commit()
    finally:
        await db.close()
    return {"status": "ok"}


@router.post("/sections", dependencies=[Depends(require_admin)])
async def create_section(p: SectionPayload):
    now = time.time()
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO sections (title, subtitle, type, style, position, sort_order, enabled, config, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p.title, p.subtitle, p.type, p.style, p.position, p.sort_order,
             int(p.enabled), json.dumps(p.config), now, now),
        )
        await db.commit()
        sid = cursor.lastrowid
        cursor = await db.execute("SELECT * FROM sections WHERE id = ?", (sid,))
        return _section_to_dict(await cursor.fetchone())
    finally:
        await db.close()


@router.put("/sections/{section_id}", dependencies=[Depends(require_admin)])
async def update_section(section_id: int, p: SectionPayload):
    now = time.time()
    db = await get_db()
    try:
        await db.execute(
            """UPDATE sections SET title=?, subtitle=?, type=?, style=?, position=?, sort_order=?, enabled=?, config=?, updated_at=?
               WHERE id=?""",
            (p.title, p.subtitle, p.type, p.style, p.position, p.sort_order,
             int(p.enabled), json.dumps(p.config), now, section_id),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM sections WHERE id = ?", (section_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Section not found")
        return _section_to_dict(row)
    finally:
        await db.close()


@router.delete("/sections/{section_id}", dependencies=[Depends(require_admin)])
async def delete_section(section_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM sections WHERE id = ?", (section_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted"}


@router.get("/sections/{section_id}/preview")
async def preview_section(section_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM sections WHERE id = ?", (section_id,))
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(404, "Section not found")
    return await resolve_section(row)
