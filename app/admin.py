"""Admin surface: status, Plex helper lookups, job triggers, and the web UI."""

import asyncio
import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import scheduler as scheduler_mod
from . import section_templates
from .config import (
    CONFIG_KEYS,
    MDBLIST_BASE,
    OPENSUBTITLES_API_BASE,
    OPENSUBTITLES_USER_AGENT,
    SECRET_KEYS,
    SERVICE_VERSION,
    log,
    settings,
)
from .db import get_db, meta_get, meta_set
from .http_client import http_client
from .opensubtitles import opensubtitles_configured
from .plex import plex_configured, plex_get, plex_reachable, reset_reachable_cache
from .plugins import all_plugins, probe_plugin
from .popular import refresh_popular_items
from .ratings import (
    get_ratings_sync_config,
    refresh_library_ratings,
    schedule_library_sync,
)

router = APIRouter()


async def _probe_plex() -> Optional[bool]:
    """None = not configured; True/False = reachable & authorized or not."""
    if not plex_configured():
        return None
    return await plex_reachable()


async def _probe_overseerr() -> Optional[bool]:
    if not (settings.OVERSEERR_URL and settings.OVERSEERR_API_KEY):
        return None
    try:
        # settings/main requires the API key (401/403 without it), unlike the public
        # /api/v1/status health endpoint, so a bad key reads as Unavailable, not OK.
        r = await http_client().get(
            f"{settings.OVERSEERR_URL}/api/v1/settings/main",
            headers={"X-Api-Key": settings.OVERSEERR_API_KEY}, timeout=5
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
    onboarding_dismissed = bool(await meta_get("onboarding_dismissed"))

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
        "ratings": {"configured": bool(settings.MDBLIST_API_KEY), "sync": sync_cfg},
        "jobs": jobs,
        "plugins": plugins_out,
        "overseerr": {"configured": bool(settings.OVERSEERR_URL and settings.OVERSEERR_API_KEY), "healthy": overseerr_health, "url": settings.OVERSEERR_URL},
        "plex": {"configured": plex_configured(), "healthy": plex_health, "url": settings.PLEX_URL},
        "opensubtitles": {"configured": opensubtitles_configured(), "username": settings.OPENSUBTITLES_USERNAME},
        "onboarding_dismissed": onboarding_dismissed,
        "config": settings.as_masked(),
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


# ---------- Live integration config ----------

def _apply_config_effects():
    """Make freshly-saved credentials take effect at once: drop the Plex
    reachability cache so the gate re-probes, and kick the MDbList-gated background
    fetches when a key is now present. Best-effort."""
    reset_reachable_cache()
    if settings.MDBLIST_API_KEY:
        asyncio.create_task(refresh_popular_items())


@router.get("/admin/config")
async def admin_config_get():
    """Current integration config for the admin UI: URLs/usernames in the clear,
    secrets masked with a ``<KEY>_set`` flag."""
    return settings.as_masked()


@router.put("/admin/config")
async def admin_config_put(patch: dict):
    """Apply a partial config update. Only known keys are accepted; a blank secret
    field means 'leave unchanged' (so the UI never has to echo secrets back)."""
    clean = {}
    for k, v in (patch or {}).items():
        if k not in CONFIG_KEYS:
            continue
        if k in SECRET_KEYS and (v is None or v == ""):
            continue  # unchanged secret
        clean[k] = v
    settings.update(clean)
    _apply_config_effects()
    return settings.as_masked()


class ConfigTest(BaseModel):
    service: str
    values: dict = {}


def _test_value(values: dict, key: str) -> str:
    """Candidate value for a test: use the submitted value, else fall back to the
    stored one (so a user can test without re-typing an unchanged secret)."""
    got = (values or {}).get(key)
    if got in (None, ""):
        return settings._values.get(key, "")
    return str(got)


@router.post("/admin/config/test")
async def admin_config_test(body: ConfigTest):
    """Test candidate credentials WITHOUT saving them. Returns {ok, detail}."""
    v = body.values or {}
    svc = (body.service or "").lower()
    try:
        if svc == "plex":
            url, token = _test_value(v, "PLEX_URL").rstrip("/"), _test_value(v, "PLEX_TOKEN")
            if not (url and token):
                return {"ok": False, "detail": "URL and token required"}
            # Probe the server root (requires auth) rather than /identity (which any
            # token can read), so a bogus token is correctly rejected with a 401.
            r = await http_client().get(
                f"{url}/", headers={"X-Plex-Token": token, "Accept": "application/json"}, timeout=6
            )
            if r.status_code == 200:
                name = (r.json().get("MediaContainer", {}) or {}).get("friendlyName")
                return {"ok": True, "detail": f"Connected to {name}" if name else "Connected"}
            if r.status_code == 401:
                return {"ok": False, "detail": "Invalid Plex token"}
            return {"ok": False, "detail": f"HTTP {r.status_code}"}

        if svc == "overseerr":
            url, key = _test_value(v, "OVERSEERR_URL").rstrip("/"), _test_value(v, "OVERSEERR_API_KEY")
            if not (url and key):
                return {"ok": False, "detail": "URL and API key required"}
            # settings/main requires the API key, so a bogus key is rejected (401/403);
            # /api/v1/status is public and would accept anything.
            r = await http_client().get(
                f"{url}/api/v1/settings/main", headers={"X-Api-Key": key}, timeout=6
            )
            if r.status_code == 200:
                name = (r.json() or {}).get("applicationTitle") or "Overseerr"
                return {"ok": True, "detail": f"Connected to {name}"}
            if r.status_code in (401, 403):
                return {"ok": False, "detail": "Invalid API key"}
            return {"ok": False, "detail": f"HTTP {r.status_code}"}

        if svc == "mdblist":
            key = _test_value(v, "MDBLIST_API_KEY")
            if not key:
                return {"ok": False, "detail": "API key required"}
            r = await http_client().get(f"{MDBLIST_BASE}/user", params={"apikey": key}, timeout=8)
            if r.status_code == 200 and not r.json().get("error"):
                return {"ok": True, "detail": "Valid API key"}
            return {"ok": False, "detail": "Invalid API key" if r.status_code in (200, 401) else f"HTTP {r.status_code}"}

        if svc == "opensubtitles":
            key = _test_value(v, "OPENSUBTITLES_API_KEY")
            user = _test_value(v, "OPENSUBTITLES_USERNAME")
            pw = _test_value(v, "OPENSUBTITLES_PASSWORD")
            if not (key and user and pw):
                return {"ok": False, "detail": "API key, username and password required"}
            r = await http_client().post(
                f"{OPENSUBTITLES_API_BASE}/login",
                headers={"Api-Key": key, "User-Agent": OPENSUBTITLES_USER_AGENT, "Content-Type": "application/json"},
                json={"username": user, "password": pw}, timeout=10,
            )
            if r.status_code == 200 and r.json().get("token"):
                return {"ok": True, "detail": "Logged in"}
            return {"ok": False, "detail": f"HTTP {r.status_code}"}

        return {"ok": False, "detail": f"Unknown service '{body.service}'"}
    except Exception as e:
        log.warning("Config test for %s failed: %s", svc, e)
        return {"ok": False, "detail": str(e) or "Connection failed"}


@router.post("/admin/onboarding/dismiss")
async def admin_onboarding_dismiss():
    """Remember that the admin chose 'start from scratch', so the starter-sections
    wizard doesn't reappear on an intentionally-empty board."""
    await meta_set("onboarding_dismissed", "1")
    return {"ok": True}


# ---------- Starter-sections wizard ----------

async def _sections_count() -> int:
    db = await get_db()
    try:
        row = await (await db.execute("SELECT COUNT(*) AS c FROM sections")).fetchone()
        return row["c"]
    finally:
        await db.close()


@router.get("/admin/sections/seed/preview")
async def admin_seed_preview():
    """Dry run: the starter sections that would be created (adapted to this server),
    the ones skipped and why, and the Plex collections available to back the optional
    Recommendations section. The collection choice is applied client-side on confirm."""
    return await section_templates.build_seed_preview()


class SeedRequest(BaseModel):
    # One or more collections to back the Recommendations shelf (its children merge).
    collections: list[dict] = []
    # Back-compat single-collection form.
    collection_key: Optional[str] = None
    collection_title: Optional[str] = None


@router.post("/admin/sections/seed")
async def admin_seed(body: SeedRequest):
    """Create the adapted starter sections. Refuses if any section already exists, so
    it can only run on a genuinely empty board (no accidental duplicates)."""
    if await _sections_count() > 0:
        raise HTTPException(409, "Sections already exist")
    cols = []
    for c in body.collections or []:
        k = c.get("key") or c.get("collection_key")
        if k:
            cols.append({"key": str(k), "title": c.get("title") or c.get("collection_title") or ""})
    if not cols and body.collection_key:
        cols.append({"key": body.collection_key, "title": body.collection_title or ""})
    return await section_templates.seed_starter_sections(cols or None)


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
