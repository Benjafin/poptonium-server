"""Generic integration-plugin host.

A plugin is a separate HTTP service implementing the manifest contract:
  GET /plugin/manifest      -> identity + a generic settings schema
  GET /plugin/status        -> {configured, healthy, detail} for the LED
  GET|PUT /plugin/settings  -> the plugin persists its own config
plus its own functional endpoints. We register it by base URL, cache its
manifest, proxy /plugins/{id}/* to it, and render its settings generically.
"""

import asyncio
import json
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .auth import require_admin
from .client_auth import require_admin_or_plex_user
from .config import log
from .db import get_db
from .http_client import http_client
from .plex import DROP_REQ_HEADERS, DROP_RESP_HEADERS

router = APIRouter()


class PluginRegister(BaseModel):
    base_url: str


class PluginUpdate(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None


def _plugin_to_dict(row) -> dict:
    try:
        manifest = json.loads(row["manifest_json"]) if row["manifest_json"] else {}
    except Exception:
        manifest = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "enabled": bool(row["enabled"]),
        "manifest": manifest,
    }


async def get_plugin(plugin_id: str) -> Optional[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM plugins WHERE id = ?", (plugin_id,))
        row = await cursor.fetchone()
    finally:
        await db.close()
    return _plugin_to_dict(row) if row else None


async def _fetch_plugin_manifest(base_url: str) -> dict:
    url = base_url.rstrip("/") + "/plugin/manifest"
    try:
        resp = await http_client().get(url, timeout=10)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach plugin at {base_url}: {e}")
    if resp.status_code != 200:
        raise HTTPException(502, f"Plugin manifest fetch failed: HTTP {resp.status_code}")
    try:
        manifest = resp.json()
    except Exception:
        raise HTTPException(502, "Plugin manifest is not valid JSON")
    if not isinstance(manifest, dict) or not manifest.get("id"):
        raise HTTPException(422, "Plugin manifest missing 'id'")
    return manifest


async def probe_plugin(base_url: str) -> dict:
    """Live status for a plugin. healthy=None means unreachable/unknown."""
    url = base_url.rstrip("/") + "/plugin/status"
    try:
        resp = await http_client().get(url, timeout=6)
        if resp.status_code == 200:
            d = resp.json()
            return {"configured": bool(d.get("configured")),
                    "healthy": d.get("healthy"),
                    "detail": d.get("detail")}
    except Exception:
        pass
    return {"configured": False, "healthy": None, "detail": "unreachable"}


async def all_plugins() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM plugins ORDER BY name")
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [_plugin_to_dict(r) for r in rows]


@router.get("/plugins", dependencies=[Depends(require_admin)])
async def list_plugins():
    plugins = await all_plugins()

    async def status_for(p):
        if not p["enabled"]:
            return {"configured": False, "healthy": None, "detail": "disabled"}
        return await probe_plugin(p["base_url"])

    statuses = await asyncio.gather(*[status_for(p) for p in plugins]) if plugins else []
    for p, st in zip(plugins, statuses):
        p["status"] = st
    return {"plugins": plugins}


@router.post("/plugins", dependencies=[Depends(require_admin)])
async def register_plugin(body: PluginRegister):
    base_url = body.base_url.strip().rstrip("/")
    if not base_url:
        raise HTTPException(400, "base_url required")
    manifest = await _fetch_plugin_manifest(base_url)
    pid = str(manifest["id"])
    name = str(manifest.get("name", pid))
    now = time.time()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO plugins (id, name, base_url, enabled, manifest_json, added_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=?, base_url=?, manifest_json=?, updated_at=?""",
            (pid, name, base_url, json.dumps(manifest), now, now,
             name, base_url, json.dumps(manifest), now),
        )
        await db.commit()
    finally:
        await db.close()
    log.info("Registered plugin %s at %s", pid, base_url)
    return {"id": pid, "name": name, "base_url": base_url, "enabled": True, "manifest": manifest}


@router.put("/plugins/{plugin_id}", dependencies=[Depends(require_admin)])
async def update_plugin(plugin_id: str, body: PluginUpdate):
    p = await get_plugin(plugin_id)
    if not p:
        raise HTTPException(404, f"Unknown plugin {plugin_id}")
    base_url = p["base_url"]
    manifest = p["manifest"]
    if body.base_url is not None:
        base_url = body.base_url.strip().rstrip("/")
        manifest = await _fetch_plugin_manifest(base_url)
    enabled = p["enabled"] if body.enabled is None else bool(body.enabled)
    now = time.time()
    db = await get_db()
    try:
        await db.execute(
            "UPDATE plugins SET base_url=?, enabled=?, manifest_json=?, updated_at=? WHERE id=?",
            (base_url, 1 if enabled else 0, json.dumps(manifest), now, plugin_id),
        )
        await db.commit()
    finally:
        await db.close()
    return {"id": plugin_id, "name": str(manifest.get("name", p["name"])),
            "base_url": base_url, "enabled": enabled, "manifest": manifest}


@router.delete("/plugins/{plugin_id}", dependencies=[Depends(require_admin)])
async def delete_plugin(plugin_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted"}


@router.post("/plugins/{plugin_id}/refresh", dependencies=[Depends(require_admin)])
async def refresh_plugin(plugin_id: str):
    p = await get_plugin(plugin_id)
    if not p:
        raise HTTPException(404, f"Unknown plugin {plugin_id}")
    manifest = await _fetch_plugin_manifest(p["base_url"])
    now = time.time()
    db = await get_db()
    try:
        await db.execute(
            "UPDATE plugins SET name=?, manifest_json=?, updated_at=? WHERE id=?",
            (str(manifest.get("name", p["name"])), json.dumps(manifest), now, plugin_id),
        )
        await db.commit()
    finally:
        await db.close()
    return {"id": plugin_id, "manifest": manifest}


@router.api_route("/plugins/{plugin_id}/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                  dependencies=[Depends(require_admin_or_plex_user)])
async def plugin_proxy(plugin_id: str, path: str, request: Request):
    """Forward /plugins/{id}/<path> to the registered plugin's base URL, passing
    method/query/body/headers through (same shape as the Plex reverse-proxy)."""
    p = await get_plugin(plugin_id)
    if not p:
        raise HTTPException(404, f"Unknown plugin {plugin_id}")
    if not p["enabled"]:
        raise HTTPException(503, f"Plugin {plugin_id} is disabled")
    target = p["base_url"].rstrip("/") + "/" + path
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ_HEADERS}
    body = await request.body()
    try:
        up = await http_client().request(
            request.method, target,
            params=dict(request.query_params), headers=fwd_headers,
            content=body, timeout=120,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Plugin proxy error: {e}")
    ctype = up.headers.get("content-type", "")
    resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in DROP_RESP_HEADERS}
    return Response(content=up.content, status_code=up.status_code,
                    media_type=ctype or None, headers=resp_headers)
