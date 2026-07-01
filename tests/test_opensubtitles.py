"""Tests for app/opensubtitles.py: search + download-to-Plex.

Covers configuration gating, the login/token cache flow, search happy/error
paths, the full download pipeline (link -> fetch bytes -> upload to Plex), and
every error branch (quota, upstream non-200, missing link, upload failure).

HTTP is mocked with respx (bare ``@respx.mock``). The DB used by the token
cache is pointed at a temp SQLite file per test. Config values are value-imported
into the module, so we monkeypatch them on ``app.opensubtitles`` directly.
"""

import json
import time

import httpx
import respx
from fastapi import FastAPI
from httpx import ASGITransport

import app.db as _db
from app import opensubtitles
from app.config import OPENSUBTITLES_API_BASE, PLEX_URL


# --- helpers -----------------------------------------------------------------

def _configure(monkeypatch):
    """Make ``opensubtitles_configured()`` return True (username/password are
    blank by default in conftest; the API key is set in env but re-assert it)."""
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_API_KEY", "os-key")
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_USERNAME", "user")
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_PASSWORD", "pass")


def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "os.db"))


def _mock_login(token="jwt-tok", base=None, status=200):
    body = {"token": token}
    if base is not None:
        body["base_url"] = base
    return respx.post(f"{OPENSUBTITLES_API_BASE}/login").mock(
        return_value=httpx.Response(status, json=body)
    )


def _app():
    app = FastAPI()
    app.include_router(opensubtitles.router)
    return app


def _mock_plex_gate():
    """Auth gate for require_plex_user: /library/sections -> 200."""
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))


# --- opensubtitles_configured / _clean_imdb / _os_headers --------------------

def test_configured_true(monkeypatch):
    _configure(monkeypatch)
    assert opensubtitles.opensubtitles_configured() is True


def test_configured_false_when_key_blank(monkeypatch):
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_API_KEY", "")
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_USERNAME", "user")
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_PASSWORD", "pass")
    assert opensubtitles.opensubtitles_configured() is False


def test_clean_imdb_variants():
    assert opensubtitles._clean_imdb(None) is None
    assert opensubtitles._clean_imdb("tt0133093") == "133093"
    assert opensubtitles._clean_imdb("0000111") == "111"
    assert opensubtitles._clean_imdb(133093) == "133093"
    assert opensubtitles._clean_imdb("notanid") is None
    # All zeros strip to empty -> not a digit -> None.
    assert opensubtitles._clean_imdb("tt0000000") is None


def test_os_headers_with_and_without_token(monkeypatch):
    _configure(monkeypatch)
    h = opensubtitles._os_headers()
    assert h["Api-Key"] == "os-key"
    assert h["User-Agent"] == "Poptonium"
    assert "Authorization" not in h
    h2 = opensubtitles._os_headers("abc")
    assert h2["Authorization"] == "Bearer abc"


# --- _os_token ---------------------------------------------------------------

@respx.mock
async def test_os_token_uses_cached_valid_token(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    await _db.meta_set("opensubtitles_token", json.dumps({
        "token": "cached-tok", "base_url": "https://vip/api/v1",
        "expires_at": time.time() + 1000,
    }))
    login = _mock_login()
    tok = await opensubtitles._os_token()
    assert tok == ("cached-tok", "https://vip/api/v1")
    # No login call needed when cache is fresh.
    assert not login.called


@respx.mock
async def test_os_token_cached_without_base_url_falls_back(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    await _db.meta_set("opensubtitles_token", json.dumps({
        "token": "cached-tok", "expires_at": time.time() + 1000,
    }))
    tok = await opensubtitles._os_token()
    assert tok == ("cached-tok", OPENSUBTITLES_API_BASE)


@respx.mock
async def test_os_token_logs_in_and_caches(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_login(token="fresh-tok", base="vip-api.opensubtitles.com")
    tok = await opensubtitles._os_token()
    assert tok == ("fresh-tok", "https://vip-api.opensubtitles.com/api/v1")
    # Cached to the DB.
    raw = await _db.meta_get("opensubtitles_token")
    assert json.loads(raw)["token"] == "fresh-tok"


@respx.mock
async def test_os_token_login_no_base_url_uses_default(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_login(token="fresh-tok")  # no base_url in response
    tok = await opensubtitles._os_token()
    assert tok == ("fresh-tok", OPENSUBTITLES_API_BASE)


@respx.mock
async def test_os_token_expired_cache_triggers_login(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    await _db.meta_set("opensubtitles_token", json.dumps({
        "token": "old", "expires_at": time.time() - 10,
    }))
    login = _mock_login(token="new-tok")
    tok = await opensubtitles._os_token()
    assert tok[0] == "new-tok"
    assert login.called


@respx.mock
async def test_os_token_corrupt_cache_triggers_login(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    await _db.meta_set("opensubtitles_token", "not-json{{{")
    _mock_login(token="new-tok")
    tok = await opensubtitles._os_token()
    assert tok[0] == "new-tok"


async def test_os_token_returns_none_when_not_configured(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_API_KEY", "")
    assert await opensubtitles._os_token() is None


@respx.mock
async def test_os_token_login_non_200_returns_none(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_login(status=401)
    assert await opensubtitles._os_token() is None


@respx.mock
async def test_os_token_login_missing_token_returns_none(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    respx.post(f"{OPENSUBTITLES_API_BASE}/login").mock(
        return_value=httpx.Response(200, json={"base_url": "x"})  # no token
    )
    assert await opensubtitles._os_token() is None


@respx.mock
async def test_os_token_login_raises_returns_none(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    respx.post(f"{OPENSUBTITLES_API_BASE}/login").mock(
        side_effect=httpx.ConnectError("boom")
    )
    assert await opensubtitles._os_token() is None


# --- GET /opensubtitles/status -----------------------------------------------

@respx.mock
async def test_status_endpoint(monkeypatch):
    _configure(monkeypatch)
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/opensubtitles/status")
    assert resp.status_code == 200
    assert resp.json() == {"configured": True}


# --- GET /opensubtitles/search -----------------------------------------------

async def _get_search(params, headers=None):
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(
            "/opensubtitles/search",
            params=params,
            headers=headers or {"X-Plex-Token": "tok"},
        )


@respx.mock
async def test_search_not_configured_503(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _mock_plex_gate()
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_API_KEY", "")
    resp = await _get_search({"query": "matrix"})
    assert resp.status_code == 503


@respx.mock
async def test_search_requires_plex_user(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    resp = await _get_search({"query": "matrix"}, headers={"X-Plex-Token": "bad"})
    assert resp.status_code == 401


@respx.mock
async def test_search_happy_path_builds_all_params(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_login(token="jwt-tok")
    search = respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": [
            {
                "attributes": {
                    "language": "en",
                    "release": "BluRay",
                    "download_count": 42,
                    "hearing_impaired": True,
                    "hd": True,
                    "fps": 23.976,
                    "ai_translated": False,
                    "machine_translated": False,
                    "ratings": 8.5,
                    "uploader": {"name": "bob"},
                    "upload_date": "2020-01-01",
                    "files": [{"file_id": 111, "file_name": "sub.srt"}],
                }
            },
            # Item with no files is skipped.
            {"attributes": {"language": "fr", "files": []}},
        ]})
    )
    resp = await _get_search({
        "query": "  The Matrix  ",
        "imdb_id": "tt0133093",
        "tmdb_id": 603,
        "parent_imdb_id": "tt0111161",
        "parent_tmdb_id": 1396,
        "season": 2,
        "episode": 5,
        "year": 1999,
        "languages": "EN, es,en",
        "type": "movie",
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    r = results[0]
    assert r["file_id"] == 111
    assert r["file_name"] == "sub.srt"
    assert r["uploader"] == "bob"
    assert r["hearing_impaired"] is True

    # Verify outbound params were assembled + auth header carried the jwt.
    req = search.calls.last.request
    q = dict(req.url.params)
    assert q["languages"] == "en,es"  # deduped + sorted + lowercased
    assert q["type"] == "movie"
    assert q["season_number"] == "2"
    assert q["episode_number"] == "5"
    assert q["parent_imdb_id"] == "111161"
    assert q["parent_tmdb_id"] == "1396"
    assert q["imdb_id"] == "133093"
    assert q["tmdb_id"] == "603"
    assert q["year"] == "1999"
    assert q["query"] == "The Matrix"
    assert req.headers["Authorization"] == "Bearer jwt-tok"


@respx.mock
async def test_search_empty_results(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_login()
    respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    resp = await _get_search({"query": "matrix"})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


@respx.mock
async def test_search_null_data_key(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_login()
    respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": None})
    )
    resp = await _get_search({"query": "matrix"})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


@respx.mock
async def test_search_no_token_uses_default_base_no_auth(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    # Login fails -> _os_token returns None -> search proceeds with no token,
    # against the default base.
    _mock_login(status=500)
    search = respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    resp = await _get_search({"query": "matrix"})
    assert resp.status_code == 200
    assert "Authorization" not in search.calls.last.request.headers


@respx.mock
async def test_search_upstream_non_200_returns_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_login()
    respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        return_value=httpx.Response(500, text="upstream boom")
    )
    resp = await _get_search({"query": "matrix"})
    assert resp.status_code == 502


@respx.mock
async def test_search_request_raises_returns_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_login()
    respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        side_effect=httpx.ConnectError("down")
    )
    resp = await _get_search({"query": "matrix"})
    assert resp.status_code == 502


@respx.mock
async def test_search_no_optional_params(tmp_path, monkeypatch):
    """No query/ids at all: params stay minimal (just languages default 'en')."""
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_login()
    search = respx.get(f"{OPENSUBTITLES_API_BASE}/subtitles").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    resp = await _get_search({})
    assert resp.status_code == 200
    q = dict(search.calls.last.request.url.params)
    assert q == {"languages": "en"}


# --- POST /opensubtitles/download --------------------------------------------

async def _post_download(payload, headers=None):
    transport = ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.post(
            "/opensubtitles/download",
            json=payload,
            headers=headers or {"X-Plex-Token": "tok"},
        )


def _mock_item_access(ok=True):
    """plex_user_can_access -> GET /library/metadata/{rk}."""
    respx.get(url__regex=rf"{PLEX_URL}/library/metadata/.*").mock(
        return_value=httpx.Response(200 if ok else 403, json={})
    )


def _valid_payload(**over):
    p = {"file_id": 111, "rating_key": "555", "language": "en", "sub_format": "srt"}
    p.update(over)
    return p


@respx.mock
async def test_download_not_configured_503(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _mock_plex_gate()
    monkeypatch.setattr(opensubtitles, "OPENSUBTITLES_API_KEY", "")
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 503


@respx.mock
async def test_download_plex_not_configured_503(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    monkeypatch.setattr(opensubtitles, "plex_configured", lambda: False)
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 503


@respx.mock
async def test_download_requires_plex_user(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    resp = await _post_download(_valid_payload(), headers={"X-Plex-Token": "bad"})
    assert resp.status_code == 401


@respx.mock
async def test_download_no_item_access_403(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=False)
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 403


@respx.mock
async def test_download_login_failed_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login(status=401)  # token resolution fails
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OpenSubtitles login failed"


@respx.mock
async def test_download_happy_path(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login(token="jwt-tok")
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(200, json={
            "link": "https://dl.opensubtitles.com/tmp/sub.srt",
            "file_name": "the.matrix.srt",
            "remaining": 19,
        })
    )
    fetch = respx.get("https://dl.opensubtitles.com/tmp/sub.srt").mock(
        return_value=httpx.Response(200, content=b"1\n00:00:01 --> 00:00:02\nHi\n")
    )
    upload = respx.post(f"{PLEX_URL}/library/metadata/555/subtitles").mock(
        return_value=httpx.Response(201, text="OK")
    )
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "file_name": "the.matrix.srt", "remaining": 19, "language": "en"}
    assert fetch.called
    assert upload.called
    # The subtitle bytes were forwarded to Plex.
    assert upload.calls.last.request.content == b"1\n00:00:01 --> 00:00:02\nHi\n"


@respx.mock
async def test_download_quota_exceeded_429(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(406, text="quota")
    )
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 429
    assert "quota" in resp.json()["detail"].lower()


@respx.mock
async def test_download_link_request_non_200_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(500, text="boom")
    )
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OpenSubtitles download failed"


@respx.mock
async def test_download_link_request_raises_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        side_effect=httpx.ConnectError("down")
    )
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OpenSubtitles download failed"


@respx.mock
async def test_download_no_link_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(200, json={"file_name": "x.srt"})  # no link
    )
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OpenSubtitles returned no link"


@respx.mock
async def test_download_subtitle_fetch_non_200_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.test/s.srt"})
    )
    respx.get("https://dl.test/s.srt").mock(return_value=httpx.Response(404))
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "subtitle fetch failed"


@respx.mock
async def test_download_subtitle_fetch_raises_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.test/s.srt"})
    )
    respx.get("https://dl.test/s.srt").mock(side_effect=httpx.ConnectError("down"))
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "subtitle fetch failed"


@respx.mock
async def test_download_plex_upload_fails_502(tmp_path, monkeypatch):
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.test/s.srt", "file_name": "s.srt"})
    )
    respx.get("https://dl.test/s.srt").mock(return_value=httpx.Response(200, content=b"data"))
    respx.post(f"{PLEX_URL}/library/metadata/555/subtitles").mock(
        return_value=httpx.Response(500, text="plex boom")
    )
    resp = await _post_download(_valid_payload())
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Plex subtitle upload failed"


@respx.mock
async def test_download_defaults_sub_format_and_language(tmp_path, monkeypatch):
    """Payload with null sub_format/language: defaults 'srt'/'en' are applied."""
    _isolate_db(tmp_path, monkeypatch)
    _configure(monkeypatch)
    _mock_plex_gate()
    _mock_item_access(ok=True)
    _mock_login()
    link_req = respx.post(f"{OPENSUBTITLES_API_BASE}/download").mock(
        return_value=httpx.Response(200, json={"link": "https://dl.test/s.srt"})
    )
    respx.get("https://dl.test/s.srt").mock(return_value=httpx.Response(200, content=b"data"))
    upload = respx.post(f"{PLEX_URL}/library/metadata/555/subtitles").mock(
        return_value=httpx.Response(200, text="OK")
    )
    resp = await _post_download({"file_id": 111, "rating_key": "555",
                                 "language": None, "sub_format": None})
    assert resp.status_code == 200
    # Default file_name applied since response omitted it.
    assert resp.json()["file_name"] == "subtitle.srt"
    # download request used default sub_format 'srt'.
    sent = json.loads(link_req.calls.last.request.content)
    assert sent == {"file_id": 111, "sub_format": "srt"}
    # Plex upload got default language 'en' and format 'srt'.
    up_params = dict(upload.calls.last.request.url.params)
    assert up_params["language"] == "en"
    assert up_params["format"] == "srt"
