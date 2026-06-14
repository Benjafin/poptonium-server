"""Client capability discovery (`/capabilities`).

The app probes a Plex connection for this endpoint to learn which backend
features are available (sections, ratings, proxy, plugins) before building UI.
"""

import asyncio

from fastapi import APIRouter

from .config import (
    MDBLIST_API_KEY,
    OVERSEERR_API_KEY,
    OVERSEERR_URL,
    SECTION_SCHEMA_VERSION,
    SERVICE_VERSION,
)
from .opensubtitles import opensubtitles_configured
from .plex import plex_configured
from .plugins import all_plugins, probe_plugin

router = APIRouter()


@router.get("/capabilities")
async def capabilities():
    # Advertise registered plugins so clients can gate plugin-specific UI on the
    # plugin being present + enabled + reachable (healthy).
    plugins = await all_plugins()

    async def status_for(p):
        if not p["enabled"]:
            return None
        return await probe_plugin(p["base_url"])

    statuses = await asyncio.gather(*[status_for(p) for p in plugins]) if plugins else []
    plugin_caps = [
        {"id": p["id"], "name": p["name"], "enabled": p["enabled"],
         "healthy": bool(st and st.get("healthy")),
         # From the cached manifest, so the client can build chips without a round-trip.
         "interface": p["manifest"].get("interface"),
         "chip": p["manifest"].get("chip")}
        for p, st in zip(plugins, statuses)
    ]
    return {
        "service": "poptonium",
        "version": SERVICE_VERSION,
        # Section-rendering contract version. Clients compare each section's
        # `min_app_version` against their own schema version and skip newer ones.
        "section_schema_version": SECTION_SCHEMA_VERSION,
        "features": ["sections", "ratings", "popular", "overseerr", "opensubtitles", "plugins", "subtitle_prefs"],
        "sections": True,
        "ratings": bool(MDBLIST_API_KEY),
        # Per-series subtitle preference store (Plex has no per-show subtitle setting).
        "subtitle_prefs": True,
        # The Plex reverse-proxy (backend mode) is only usable if we can reach Plex.
        "plex_proxy": plex_configured(),
        "overseerr_configured": bool(OVERSEERR_URL and OVERSEERR_API_KEY),
        "plex_configured": plex_configured(),
        # Online subtitle search needs both OpenSubtitles creds and Plex (to upload).
        "opensubtitles_configured": opensubtitles_configured() and plex_configured(),
        "plugins": plugin_caps,
    }
