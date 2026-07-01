"""FastAPI app wiring: router aggregation, startup/shutdown, scheduler boot.

Every feature module exposes an ``APIRouter``; they're combined into one router
that is mounted at ``/`` and ``/poptonium`` so the service works regardless of
reverse-proxy prefix. ``/poptonium`` is canonical (exposed via the Plex SWAG domain).
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, Depends, FastAPI

from . import scheduler as scheduler_mod
from .auth import require_admin
from .config import MDBLIST_API_KEY, OVERSEERR_API_KEY, OVERSEERR_URL, PLEX_URL, log
from .db import get_db
from .http_client import aclose_http_client, http_client
from .plex import plex_configured, plex_reachable
from .popular import refresh_popular_items
from .ratings import (
    get_ratings_sync_config,
    refresh_library_ratings,
    schedule_library_sync,
)

# Feature routers.
from . import (
    admin,
    auth,
    capabilities,
    opensubtitles,
    overseerr,
    plex_proxy,
    plugins,
    popular,
    ratings,
    sections,
    subtitle_prefs,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


app = FastAPI(title="poptonium", lifespan=lifespan)

# Aggregate every feature router into one, then mount it under each prefix.
# Per-endpoint config mutations carry their own require_admin dependency
# (ratings/config PUT, sections write, plugin management); the whole admin data
# surface is guarded here at include time. The admin HTML shell and the auth
# endpoints stay public so the login/setup screen can load.
api = APIRouter()
for _module in (
    ratings,
    popular,
    overseerr,
    plex_proxy,
    plugins,
    sections,
    subtitle_prefs,
    opensubtitles,
    capabilities,
    auth,
):
    api.include_router(_module.router)

api.include_router(admin.router, dependencies=[Depends(require_admin)])
api.include_router(admin.ui_router)


async def startup():
    # Warm the shared keep-alive HTTP client.
    http_client()
    # Create tables
    db = await get_db()
    await db.close()

    # Plex is mandatory. Probe it on boot and log loudly if it's missing or
    # unreachable; the admin UI blocks on its setup screen until this is fixed.
    if not plex_configured():
        log.warning("Plex not configured: set PLEX_URL and PLEX_TOKEN. The admin "
                    "UI stays blocked until Plex is reachable.")
    elif await plex_reachable():
        log.info("Plex connected: %s", PLEX_URL)
    else:
        log.warning("Plex configured but unreachable at %s: check PLEX_URL/PLEX_TOKEN. "
                    "The admin UI stays blocked until Plex is reachable.", PLEX_URL)

    # Check Overseerr connectivity
    if OVERSEERR_URL and OVERSEERR_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    f"{OVERSEERR_URL}/api/v1/status",
                    headers={"X-Api-Key": OVERSEERR_API_KEY},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    log.info("Overseerr connected: %s (v%s)", OVERSEERR_URL, data.get("version", "?"))
                else:
                    log.warning("Overseerr returned HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.error("Overseerr connection failed: %s", e)
    else:
        log.info("Overseerr not configured (OVERSEERR_URL or OVERSEERR_API_KEY missing)")

    # Run initial fetches
    if MDBLIST_API_KEY:
        asyncio.create_task(refresh_popular_items())
        sync_cfg = await get_ratings_sync_config()
        if sync_cfg.get("enabled") and plex_configured():
            asyncio.create_task(refresh_library_ratings())
    else:
        log.info("MDBLIST_API_KEY not set, ratings + popular disabled")

    # Schedule nightly jobs
    scheduler_mod.scheduler = AsyncIOScheduler()
    scheduler_mod.scheduler.add_job(refresh_popular_items, "cron", hour=3, minute=0,
                                    id="popular_refresh", name="Popular items refresh")
    scheduler_mod.scheduler.start()
    # Nightly library-ratings sync per config.
    schedule_library_sync(await get_ratings_sync_config())
    log.info("Scheduler started: popular items refresh at 3:00 AM daily")


async def shutdown():
    await aclose_http_client()


# Mount at / and /poptonium so it works regardless of reverse proxy config.
app.include_router(api)
app.include_router(api, prefix="/poptonium")
