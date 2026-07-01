"""Admin surface: status, Plex helper lookups, job triggers, and the web UI."""

import asyncio
import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from . import scheduler as scheduler_mod
from .config import (
    MDBLIST_API_KEY,
    OPENSUBTITLES_PASSWORD,
    OPENSUBTITLES_USERNAME,
    OPENSUBTITLES_API_KEY,
    OVERSEERR_API_KEY,
    OVERSEERR_URL,
    PLEX_TOKEN,
    PLEX_URL,
    SERVICE_VERSION,
)
from .db import get_db, meta_get, meta_set
from .http_client import http_client
from .opensubtitles import opensubtitles_configured
from .plex import plex_configured, plex_get, plex_reachable
from .plugins import all_plugins, probe_plugin
from .popular import refresh_popular_items
from .ratings import (
    get_ratings_sync_config,
    refresh_library_ratings,
    schedule_library_sync,
)

router = APIRouter()


def _mask(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 8:
        return "•" * len(val)
    return f"{val[:4]}…{val[-4:]}"


async def _probe_plex() -> Optional[bool]:
    """None = not configured; True/False = reachable & authorized or not."""
    if not plex_configured():
        return None
    return await plex_reachable()


async def _probe_overseerr() -> Optional[bool]:
    if not (OVERSEERR_URL and OVERSEERR_API_KEY):
        return None
    try:
        r = await http_client().get(
            f"{OVERSEERR_URL}/api/v1/status", headers={"X-Api-Key": OVERSEERR_API_KEY}, timeout=5
        )
        return r.status_code == 200
    except Exception:
        return False


@router.get("/admin/status")
async def admin_status():
    db = await get_db()
    try:
        c1 = await db.execute("SELECT COUNT(*) AS c FROM mdblist_ratings")
        rated_count = (await c1.fetchone())["c"]
        c2 = await db.execute("SELECT COUNT(*) AS c FROM popular_items")
        popular_count = (await c2.fetchone())["c"]
        c3 = await db.execute("SELECT COUNT(*) AS c FROM sections")
        sections_count = (await c3.fetchone())["c"]
    finally:
        await db.close()

    last_refresh = await meta_get("popular_last_refresh")
    last_sync = await meta_get("library_ratings_last_sync")
    sync_cfg = await get_ratings_sync_config()

    # Live health probes (concurrent, short timeout). None = not configured.
    plex_health, overseerr_health = await asyncio.gather(
        _probe_plex(), _probe_overseerr()
    )
    plugins = await all_plugins()

    async def _plugin_status(p):
        if not p["enabled"]:
            return {"configured": False, "healthy": None, "detail": "disabled"}
        return await probe_plugin(p["base_url"])

    plugin_statuses = await asyncio.gather(*[_plugin_status(p) for p in plugins]) if plugins else []
    plugins_out = [{**p, "status": st} for p, st in zip(plugins, plugin_statuses)]

    jobs = []
    if scheduler_mod.scheduler:
        for job in scheduler_mod.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            })

    return {
        "service": "poptonium",
        "version": SERVICE_VERSION,
        "caches": {
            "mdblist_ratings": rated_count,
            "ratings_last_sync": float(last_sync) if last_sync else None,
            "popular_items": popular_count,
            "popular_last_refresh": float(last_refresh) if last_refresh else None,
            "sections": sections_count,
        },
        "ratings": {"configured": bool(MDBLIST_API_KEY), "sync": sync_cfg},
        "jobs": jobs,
        "plugins": plugins_out,
        "overseerr": {"configured": bool(OVERSEERR_URL and OVERSEERR_API_KEY), "healthy": overseerr_health, "url": OVERSEERR_URL},
        "plex": {"configured": plex_configured(), "healthy": plex_health, "url": PLEX_URL},
        "opensubtitles": {"configured": opensubtitles_configured(), "username": OPENSUBTITLES_USERNAME},
        "config": {
            "MDBLIST_API_KEY": _mask(MDBLIST_API_KEY),
            "OVERSEERR_URL": OVERSEERR_URL,
            "OVERSEERR_API_KEY": _mask(OVERSEERR_API_KEY),
            "PLEX_URL": PLEX_URL,
            "PLEX_TOKEN": _mask(PLEX_TOKEN),
            "OPENSUBTITLES_API_KEY": _mask(OPENSUBTITLES_API_KEY),
            "OPENSUBTITLES_USERNAME": OPENSUBTITLES_USERNAME,
            "OPENSUBTITLES_PASSWORD": _mask(OPENSUBTITLES_PASSWORD),
        },
    }


@router.get("/admin/plex/sections")
async def admin_plex_sections():
    data = await plex_get("/library/sections")
    if not data:
        return {"sections": []}
    dirs = data.get("MediaContainer", {}).get("Directory", [])
    return {"sections": [
        {"key": d.get("key"), "title": d.get("title"), "type": d.get("type")}
        for d in dirs
    ]}


@router.get("/admin/plex/collections")
async def admin_plex_collections(section: str = Query(...)):
    data = await plex_get(f"/library/sections/{section}/collections")
    if not data:
        return {"collections": []}
    items = data.get("MediaContainer", {}).get("Metadata", [])
    return {"collections": [
        {"key": str(m.get("ratingKey")), "title": m.get("title"), "count": m.get("childCount")}
        for m in items
    ]}


# Plex secondary-directory name → the per-item tag array it tallies against.
_TAG_KINDS = {"genres": ("genre", "Genre"), "directors": ("director", "Director"),
              "actors": ("actor", "Role"), "countries": ("country", "Country")}


@router.get("/admin/plex/tags")
async def admin_plex_tags(section: str = Query(...)):
    """Genres / directors / actors / countries for a library, each with a per-item
    count (how many titles carry the tag), ordered high → low so the most-used
    options surface first in the editor's dropdowns."""
    # One full-library scan tallies every dimension at once (cheaper than 4 scans).
    all_data = await plex_get(f"/library/sections/{section}/all", {"X-Plex-Container-Size": 10000})
    metas = (all_data or {}).get("MediaContainer", {}).get("Metadata", [])
    counts: dict = {k: {} for k in _TAG_KINDS}
    for m in metas:
        for key, (_, arr) in _TAG_KINDS.items():
            c = counts[key]
            for t in m.get(arr, []) or []:
                tag = t.get("tag")
                if tag:
                    c[tag] = c.get(tag, 0) + 1

    out: dict = {}
    for key, (dirname, _) in _TAG_KINDS.items():
        data = await plex_get(f"/library/sections/{section}/{dirname}")
        items = (data or {}).get("MediaContainer", {}).get("Directory", [])
        rows = [{"id": str(d.get("key")), "title": d.get("title"),
                 "count": counts[key].get(d.get("title"), 0)} for d in items]
        rows.sort(key=lambda r: (-r["count"], (r["title"] or "").lower()))
        out[key] = rows
    return out


_JOB_FUNCS = {
    "popular_refresh": refresh_popular_items,
    "library_ratings": refresh_library_ratings,
}


@router.post("/admin/cron/{job_id}/run")
async def admin_run_cron(job_id: str):
    func = _JOB_FUNCS.get(job_id)
    if not func:
        raise HTTPException(404, f"Unknown job {job_id}")
    asyncio.create_task(func())
    return {"status": "started", "job": job_id}


@router.post("/admin/cache/clear")
async def admin_clear_cache(which: str = Query(..., pattern="^(ratings|popular)$")):
    db = await get_db()
    try:
        await db.execute("DELETE FROM mdblist_ratings" if which == "ratings" else "DELETE FROM popular_items")
        await db.commit()
    finally:
        await db.close()
    return {"status": "cleared", "which": which}


@router.post("/admin/ratings/refresh")
async def admin_ratings_refresh():
    asyncio.create_task(refresh_library_ratings())
    return {"status": "started", "job": "library_ratings"}


@router.get("/admin/ratings/sync")
async def admin_ratings_sync_get():
    return await get_ratings_sync_config()


@router.put("/admin/ratings/sync")
async def admin_ratings_sync_put(cfg: dict):
    sync = await get_ratings_sync_config()
    if "enabled" in cfg:
        sync["enabled"] = bool(cfg["enabled"])
    if "hour" in cfg:
        sync["hour"] = max(0, min(23, int(cfg["hour"])))
    await meta_set("ratings_sync", json.dumps(sync))
    schedule_library_sync(sync)
    return sync


# ---------- Admin web UI ----------

ADMIN_HTML_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "static", "admin.html")

# The HTML shell is public (it contains no privileged data); its JS calls
# /admin/auth/state and renders the setup form, the login form, or the dashboard.
# Every data/action route on `router` above is guarded by require_admin (wired in
# server.py), so the shell is useless without a valid session.
ui_router = APIRouter()


@ui_router.get("/admin", response_class=HTMLResponse)
@ui_router.get("/admin/", response_class=HTMLResponse)
async def admin_ui():
    try:
        with open(ADMIN_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "Admin UI not found")
