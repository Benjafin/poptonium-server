"""Tests for the app wiring: dual-mount routing and the startup/shutdown lifecycle.

httpx's ASGITransport does not run lifespan events, so hitting endpoints on the
real app does NOT fire the startup handler — we drive `startup()`/`shutdown()`
directly with their heavy dependencies stubbed.
"""

import httpx
import respx
from httpx import ASGITransport

import app.db as _db
from app import server
from app import scheduler as scheduler_mod
from app.config import OVERSEERR_URL


class _FakeScheduler:
    """Stand-in for AsyncIOScheduler so no real background thread starts."""
    def __init__(self):
        self.jobs = []
        self.started = False

    def add_job(self, func, *a, **k):
        self.jobs.append(k.get("id"))

    def remove_job(self, job_id):
        pass

    def start(self):
        self.started = True


# ---- dual-mount routing -----------------------------------------------------

async def test_capabilities_mounted_at_both_prefixes(tmp_path, monkeypatch):
    # /capabilities reads the plugins table -> use a temp DB (empty plugin list).
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "t.db"))
    transport = ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        for path in ("/capabilities", "/poptonium/capabilities"):
            resp = await ac.get(path)
            assert resp.status_code == 200, path
            body = resp.json()
            assert body["service"] == "poptonium"
            assert "sections" in body["features"]


# ---- shutdown ---------------------------------------------------------------

async def test_lifespan_runs_startup_then_shutdown(tmp_path, monkeypatch):
    # Drives the lifespan context manager directly (ASGITransport doesn't fire it).
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: False)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "")
    monkeypatch.setattr(server, "OVERSEERR_URL", "")
    monkeypatch.setattr(server, "OVERSEERR_API_KEY", "")

    async with server.lifespan(server.app):
        assert fake.started  # startup ran on enter
    # shutdown ran on exit without raising


async def test_shutdown_closes_http_client():
    from app import http_client as hc
    hc.http_client()  # ensure one exists
    await server.shutdown()
    assert hc._http_client is None or hc._http_client.is_closed


# ---- startup: the branch matrix ---------------------------------------------

def _common_startup_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "startup.db"))
    fake = _FakeScheduler()
    monkeypatch.setattr(server, "AsyncIOScheduler", lambda *a, **k: fake)
    # Neutralize the nightly-sync (re)scheduler so it doesn't need real cron wiring.
    monkeypatch.setattr(server, "schedule_library_sync", lambda cfg: None)
    return fake


async def test_startup_plex_unconfigured_mdblist_off(tmp_path, monkeypatch):
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: False)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "")
    monkeypatch.setattr(server, "OVERSEERR_URL", "")
    monkeypatch.setattr(server, "OVERSEERR_API_KEY", "")

    await server.startup()

    assert fake.started
    assert scheduler_mod.scheduler is fake


async def test_startup_plex_reachable_and_overseerr_connected(tmp_path, monkeypatch):
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: True)

    async def _reachable():
        return True
    monkeypatch.setattr(server, "plex_reachable", _reachable)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "")

    with respx.mock:
        respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(
            return_value=httpx.Response(200, json={"version": "1.33.0"})
        )
        await server.startup()

    assert fake.started


async def test_startup_plex_unreachable_warns(tmp_path, monkeypatch):
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: True)

    async def _unreachable():
        return False
    monkeypatch.setattr(server, "plex_reachable", _unreachable)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "")
    monkeypatch.setattr(server, "OVERSEERR_URL", "")
    monkeypatch.setattr(server, "OVERSEERR_API_KEY", "")

    await server.startup()
    assert fake.started


async def test_startup_overseerr_returns_non_200(tmp_path, monkeypatch):
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: True)

    async def _reachable():
        return True
    monkeypatch.setattr(server, "plex_reachable", _reachable)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "")

    with respx.mock:
        respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(return_value=httpx.Response(503, text="x"))
        await server.startup()
    assert fake.started


async def test_startup_overseerr_connection_error(tmp_path, monkeypatch):
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: True)

    async def _reachable():
        return True
    monkeypatch.setattr(server, "plex_reachable", _reachable)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "")

    with respx.mock:
        respx.get(f"{OVERSEERR_URL}/api/v1/status").mock(side_effect=httpx.ConnectError("down"))
        await server.startup()
    assert fake.started


async def test_startup_mdblist_on_triggers_background_work(tmp_path, monkeypatch):
    fake = _common_startup_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "plex_configured", lambda: True)

    async def _reachable():
        return True
    monkeypatch.setattr(server, "plex_reachable", _reachable)
    monkeypatch.setattr(server, "MDBLIST_API_KEY", "a-key")
    monkeypatch.setattr(server, "OVERSEERR_URL", "")
    monkeypatch.setattr(server, "OVERSEERR_API_KEY", "")

    called = {"popular": False, "library": False}

    async def _popular():
        called["popular"] = True
    async def _library():
        called["library"] = True

    async def _sync_cfg():
        return {"enabled": True, "hour": 3}

    monkeypatch.setattr(server, "refresh_popular_items", _popular)
    monkeypatch.setattr(server, "refresh_library_ratings", _library)
    monkeypatch.setattr(server, "get_ratings_sync_config", _sync_cfg)

    await server.startup()

    # The background tasks are create_task'd; give the loop a tick to run them.
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert called["popular"]
    assert fake.started
