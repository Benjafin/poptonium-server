"""Tests for the generic integration-plugin host (app/plugins.py).

Covers the plugin CRUD + manifest cache + reverse-proxy: register/list/update/
delete/refresh plugins, fetch+cache the manifest, and proxy /plugins/{id}/* to a
plugin's base_url. Both happy paths and the error/edge branches are exercised.

Patterns follow the canonical tests: respx (bare @respx.mock) mocks the outbound
httpx calls to plugin base_urls, ASGITransport drives the router, and each test
gets an isolated temp SQLite DB via monkeypatching app.db.DB_PATH.
"""

import json

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
from app import plugins
from app.auth import require_admin
from app.config import PLEX_URL

PLUGIN_URL = "http://plugin.test"


# --- helpers -----------------------------------------------------------------

def _use_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "plugins.db"))


def _admin_app():
    """Router mounted with require_admin overridden (admin-authenticated)."""
    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[require_admin] = lambda: None
    return app


def _client(app):
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _mock_manifest(base_url=PLUGIN_URL, manifest=None, status=200, json_body=None):
    body = json_body if json_body is not None else (
        manifest if manifest is not None else {"id": "acme", "name": "Acme"}
    )
    return respx.get(base_url.rstrip("/") + "/plugin/manifest").mock(
        return_value=httpx.Response(status, json=body)
    )


async def _seed_plugin(tmp_path, monkeypatch, pid="acme", base_url=PLUGIN_URL,
                       name="Acme", enabled=True, manifest=None):
    """Insert a plugin row directly so proxy/update/refresh tests have a target."""
    _use_temp_db(tmp_path, monkeypatch)
    if manifest is None:
        manifest = {"id": pid, "name": name}
    db = await _db.get_db()
    try:
        await db.execute(
            """INSERT INTO plugins (id, name, base_url, enabled, manifest_json, added_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1.0, 1.0)""",
            (pid, name, base_url, 1 if enabled else 0, json.dumps(manifest)),
        )
        await db.commit()
    finally:
        await db.close()


# ============================================================================
# Unit-level helpers
# ============================================================================

def test_plugin_to_dict_valid_manifest():
    row = {"id": "x", "name": "X", "base_url": PLUGIN_URL, "enabled": 1,
           "manifest_json": json.dumps({"id": "x", "name": "X"})}
    d = plugins._plugin_to_dict(row)
    assert d == {"id": "x", "name": "X", "base_url": PLUGIN_URL,
                 "enabled": True, "manifest": {"id": "x", "name": "X"}}


def test_plugin_to_dict_bad_json_falls_back_to_empty():
    row = {"id": "x", "name": "X", "base_url": PLUGIN_URL, "enabled": 0,
           "manifest_json": "{not json"}
    d = plugins._plugin_to_dict(row)
    assert d["manifest"] == {}
    assert d["enabled"] is False


def test_plugin_to_dict_empty_manifest_json():
    row = {"id": "x", "name": "X", "base_url": PLUGIN_URL, "enabled": 1,
           "manifest_json": ""}
    assert plugins._plugin_to_dict(row)["manifest"] == {}


async def test_get_plugin_missing_returns_none(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert await plugins.get_plugin("nope") is None


async def test_get_plugin_found(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme")
    p = await plugins.get_plugin("acme")
    assert p["id"] == "acme"
    assert p["base_url"] == PLUGIN_URL
    assert p["enabled"] is True


async def test_all_plugins_ordered_by_name(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    db = await _db.get_db()
    try:
        for pid, name in [("z", "Zeta"), ("a", "Alpha")]:
            await db.execute(
                """INSERT INTO plugins (id, name, base_url, enabled, manifest_json, added_at, updated_at)
                   VALUES (?, ?, ?, 1, '{}', 1.0, 1.0)""",
                (pid, name, PLUGIN_URL),
            )
        await db.commit()
    finally:
        await db.close()
    result = await plugins.all_plugins()
    assert [p["name"] for p in result] == ["Alpha", "Zeta"]


# ---- _fetch_plugin_manifest -------------------------------------------------

@respx.mock
async def test_fetch_manifest_success():
    _mock_manifest(manifest={"id": "acme", "name": "Acme", "extra": 1})
    m = await plugins._fetch_plugin_manifest(PLUGIN_URL)
    assert m["id"] == "acme"
    assert m["extra"] == 1


@respx.mock
async def test_fetch_manifest_strips_trailing_slash():
    route = _mock_manifest(base_url=PLUGIN_URL)
    await plugins._fetch_plugin_manifest(PLUGIN_URL + "/")
    assert route.called
    assert str(route.calls.last.request.url) == PLUGIN_URL + "/plugin/manifest"


@respx.mock
async def test_fetch_manifest_connect_error_502():
    respx.get(PLUGIN_URL + "/plugin/manifest").mock(
        side_effect=httpx.ConnectError("boom")
    )
    try:
        await plugins._fetch_plugin_manifest(PLUGIN_URL)
        assert False, "expected HTTPException"
    except httpx.HTTPError:
        assert False, "should have been converted to HTTPException"
    except Exception as e:
        from fastapi import HTTPException
        assert isinstance(e, HTTPException)
        assert e.status_code == 502


@respx.mock
async def test_fetch_manifest_non_200_502():
    from fastapi import HTTPException
    _mock_manifest(status=500, json_body={})
    try:
        await plugins._fetch_plugin_manifest(PLUGIN_URL)
        assert False
    except HTTPException as e:
        assert e.status_code == 502
        assert "HTTP 500" in e.detail


@respx.mock
async def test_fetch_manifest_bad_json_502():
    from fastapi import HTTPException
    respx.get(PLUGIN_URL + "/plugin/manifest").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})
    )
    try:
        await plugins._fetch_plugin_manifest(PLUGIN_URL)
        assert False
    except HTTPException as e:
        assert e.status_code == 502
        assert "not valid JSON" in e.detail


@respx.mock
async def test_fetch_manifest_missing_id_422():
    from fastapi import HTTPException
    _mock_manifest(manifest={"name": "no id here"})
    try:
        await plugins._fetch_plugin_manifest(PLUGIN_URL)
        assert False
    except HTTPException as e:
        assert e.status_code == 422


@respx.mock
async def test_fetch_manifest_non_dict_422():
    from fastapi import HTTPException
    _mock_manifest(json_body=["not", "a", "dict"])
    try:
        await plugins._fetch_plugin_manifest(PLUGIN_URL)
        assert False
    except HTTPException as e:
        assert e.status_code == 422


# ---- probe_plugin -----------------------------------------------------------

@respx.mock
async def test_probe_plugin_healthy():
    respx.get(PLUGIN_URL + "/plugin/status").mock(
        return_value=httpx.Response(200, json={"configured": True, "healthy": True, "detail": "ok"})
    )
    assert await plugins.probe_plugin(PLUGIN_URL) == {
        "configured": True, "healthy": True, "detail": "ok"}


@respx.mock
async def test_probe_plugin_non_200_unreachable():
    respx.get(PLUGIN_URL + "/plugin/status").mock(return_value=httpx.Response(503))
    assert await plugins.probe_plugin(PLUGIN_URL) == {
        "configured": False, "healthy": None, "detail": "unreachable"}


@respx.mock
async def test_probe_plugin_connect_error_unreachable():
    respx.get(PLUGIN_URL + "/plugin/status").mock(side_effect=httpx.ConnectError("x"))
    result = await plugins.probe_plugin(PLUGIN_URL)
    assert result["healthy"] is None


# ============================================================================
# Endpoint: POST /plugins (register)
# ============================================================================

@respx.mock
async def test_register_plugin_success(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _mock_manifest(manifest={"id": "acme", "name": "Acme Media"})
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins", json={"base_url": PLUGIN_URL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "acme"
    assert body["name"] == "Acme Media"
    assert body["base_url"] == PLUGIN_URL
    assert body["enabled"] is True
    # It was actually persisted.
    assert (await plugins.get_plugin("acme"))["name"] == "Acme Media"


@respx.mock
async def test_register_plugin_defaults_name_to_id(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _mock_manifest(manifest={"id": "acme"})  # no name field
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins", json={"base_url": PLUGIN_URL})
    assert resp.json()["name"] == "acme"


@respx.mock
async def test_register_plugin_trims_and_strips_slash(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _mock_manifest(base_url=PLUGIN_URL, manifest={"id": "acme"})
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins", json={"base_url": f"  {PLUGIN_URL}/  "})
    assert resp.status_code == 200
    assert resp.json()["base_url"] == PLUGIN_URL


async def test_register_plugin_empty_base_url_400(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins", json={"base_url": "   "})
    assert resp.status_code == 400


@respx.mock
async def test_register_plugin_manifest_unreachable_502(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    respx.get(PLUGIN_URL + "/plugin/manifest").mock(side_effect=httpx.ConnectError("no"))
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins", json={"base_url": PLUGIN_URL})
    assert resp.status_code == 502


@respx.mock
async def test_register_plugin_upsert_on_conflict(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    _mock_manifest(manifest={"id": "acme", "name": "First"})
    async with _client(_admin_app()) as ac:
        await ac.post("/plugins", json={"base_url": PLUGIN_URL})
        # Re-register same id with a new name/url -> ON CONFLICT updates.
        _mock_manifest(base_url="http://plugin2.test", manifest={"id": "acme", "name": "Second"})
        resp = await ac.post("/plugins", json={"base_url": "http://plugin2.test"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Second"
    all_p = await plugins.all_plugins()
    assert len(all_p) == 1  # still one row
    assert all_p[0]["base_url"] == "http://plugin2.test"


# ============================================================================
# Endpoint: GET /plugins (list)
# ============================================================================

@respx.mock
async def test_list_plugins_probes_enabled(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    respx.get(PLUGIN_URL + "/plugin/status").mock(
        return_value=httpx.Response(200, json={"configured": True, "healthy": True, "detail": "ok"})
    )
    async with _client(_admin_app()) as ac:
        resp = await ac.get("/plugins")
    assert resp.status_code == 200
    plist = resp.json()["plugins"]
    assert len(plist) == 1
    assert plist[0]["status"] == {"configured": True, "healthy": True, "detail": "ok"}


async def test_list_plugins_disabled_not_probed(tmp_path, monkeypatch):
    # Disabled plugin: no status route mocked; if it probed, respx would error.
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=False)
    async with _client(_admin_app()) as ac:
        resp = await ac.get("/plugins")
    assert resp.status_code == 200
    st = resp.json()["plugins"][0]["status"]
    assert st == {"configured": False, "healthy": None, "detail": "disabled"}


async def test_list_plugins_empty(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    async with _client(_admin_app()) as ac:
        resp = await ac.get("/plugins")
    assert resp.status_code == 200
    assert resp.json() == {"plugins": []}


# ============================================================================
# Endpoint: PUT /plugins/{id} (update)
# ============================================================================

async def test_update_plugin_toggle_enabled(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    async with _client(_admin_app()) as ac:
        resp = await ac.put("/plugins/acme", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert (await plugins.get_plugin("acme"))["enabled"] is False


@respx.mock
async def test_update_plugin_change_base_url_refetches(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", base_url=PLUGIN_URL)
    new_url = "http://newhost.test"
    _mock_manifest(base_url=new_url, manifest={"id": "acme", "name": "Renamed"})
    async with _client(_admin_app()) as ac:
        resp = await ac.put("/plugins/acme", json={"base_url": new_url + "/"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["base_url"] == new_url
    assert body["name"] == "Renamed"


async def test_update_plugin_no_fields_keeps_enabled(tmp_path, monkeypatch):
    # enabled None + base_url None -> unchanged enabled, no manifest refetch.
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    async with _client(_admin_app()) as ac:
        resp = await ac.put("/plugins/acme", json={})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


async def test_update_plugin_unknown_404(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    async with _client(_admin_app()) as ac:
        resp = await ac.put("/plugins/ghost", json={"enabled": True})
    assert resp.status_code == 404


@respx.mock
async def test_update_plugin_new_url_unreachable_502(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme")
    respx.get("http://bad.test/plugin/manifest").mock(side_effect=httpx.ConnectError("x"))
    async with _client(_admin_app()) as ac:
        resp = await ac.put("/plugins/acme", json={"base_url": "http://bad.test"})
    assert resp.status_code == 502


# ============================================================================
# Endpoint: DELETE /plugins/{id}
# ============================================================================

async def test_delete_plugin(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme")
    async with _client(_admin_app()) as ac:
        resp = await ac.delete("/plugins/acme")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    assert await plugins.get_plugin("acme") is None


async def test_delete_plugin_unknown_is_noop(tmp_path, monkeypatch):
    # DELETE is idempotent: deleting a nonexistent id still returns deleted.
    _use_temp_db(tmp_path, monkeypatch)
    async with _client(_admin_app()) as ac:
        resp = await ac.delete("/plugins/ghost")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}


# ============================================================================
# Endpoint: POST /plugins/{id}/refresh
# ============================================================================

@respx.mock
async def test_refresh_plugin_success(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", name="Old")
    _mock_manifest(manifest={"id": "acme", "name": "New Name", "v": 2})
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins/acme/refresh")
    assert resp.status_code == 200
    assert resp.json()["manifest"]["v"] == 2
    # Cached name updated in DB.
    assert (await plugins.get_plugin("acme"))["name"] == "New Name"


async def test_refresh_plugin_unknown_404(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins/ghost/refresh")
    assert resp.status_code == 404


@respx.mock
async def test_refresh_plugin_upstream_error_502(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme")
    _mock_manifest(status=500, json_body={})
    async with _client(_admin_app()) as ac:
        resp = await ac.post("/plugins/acme/refresh")
    assert resp.status_code == 502


# ============================================================================
# Endpoint: proxy /plugins/{id}/{path} (require_admin_or_plex_user)
# ============================================================================

def _proxy_app():
    """Router with NO dependency override -> exercises require_admin_or_plex_user
    (the plex-user path validates the token against Plex/library/sections)."""
    app = FastAPI()
    app.include_router(plugins.router)
    return app


@respx.mock
async def test_proxy_get_success(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    # Auth gate (plex-user path).
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    upstream = respx.get(PLUGIN_URL + "/plugin/settings").mock(
        return_value=httpx.Response(200, json={"api_key": "secret"},
                                    headers={"content-type": "application/json"})
    )
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/acme/plugin/settings", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    assert resp.json() == {"api_key": "secret"}
    assert upstream.called


@respx.mock
async def test_proxy_post_forwards_body_and_query(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    upstream = respx.post(PLUGIN_URL + "/plugin/action").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )
    async with _client(_proxy_app()) as ac:
        resp = await ac.post(
            "/plugins/acme/plugin/action?foo=bar",
            headers={"X-Plex-Token": "tok"},
            json={"hello": "world"},
        )
    assert resp.status_code == 201
    assert upstream.called
    req = upstream.calls.last.request
    assert json.loads(req.content) == {"hello": "world"}
    assert req.url.params.get("foo") == "bar"


@respx.mock
async def test_proxy_upstream_error_502(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    respx.get(PLUGIN_URL + "/plugin/settings").mock(side_effect=httpx.ConnectError("down"))
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/acme/plugin/settings", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 502


@respx.mock
async def test_proxy_passes_through_upstream_status(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    respx.get(PLUGIN_URL + "/plugin/missing").mock(
        return_value=httpx.Response(404, json={"error": "nope"})
    )
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/acme/plugin/missing", headers={"X-Plex-Token": "tok"})
    # Upstream 404 is faithfully relayed (not turned into a proxy error).
    assert resp.status_code == 404
    assert resp.json() == {"error": "nope"}


@respx.mock
async def test_proxy_unknown_plugin_404(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/ghost/plugin/settings", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 404


@respx.mock
async def test_proxy_disabled_plugin_503(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=False)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/acme/plugin/settings", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 503


@respx.mock
async def test_proxy_rejects_bad_plex_token(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/acme/plugin/settings", headers={"X-Plex-Token": "bad"})
    assert resp.status_code == 401


@respx.mock
async def test_proxy_admin_override_bypasses_plex(tmp_path, monkeypatch):
    # Admin path via dependency override: no Plex token needed.
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    from app.client_auth import require_admin_or_plex_user
    app = FastAPI()
    app.include_router(plugins.router)
    app.dependency_overrides[require_admin_or_plex_user] = lambda: None
    upstream = respx.get(PLUGIN_URL + "/plugin/settings").mock(
        return_value=httpx.Response(200, json={"ok": 1})
    )
    async with _client(app) as ac:
        resp = await ac.get("/plugins/acme/plugin/settings")
    assert resp.status_code == 200
    assert upstream.called


@respx.mock
async def test_proxy_drops_hop_headers(tmp_path, monkeypatch):
    await _seed_plugin(tmp_path, monkeypatch, pid="acme", enabled=True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    upstream = respx.get(PLUGIN_URL + "/plugin/settings").mock(
        return_value=httpx.Response(
            200, json={"ok": 1},
            headers={"content-type": "application/json", "connection": "keep-alive"},
        )
    )
    async with _client(_proxy_app()) as ac:
        resp = await ac.get("/plugins/acme/plugin/settings", headers={"X-Plex-Token": "tok"})
    assert resp.status_code == 200
    # The inbound Host ("test", from base_url) is dropped so httpx sets the
    # upstream host itself rather than forwarding the caller's Host header.
    fwd = upstream.calls.last.request
    assert fwd.headers["host"] == "plugin.test"
