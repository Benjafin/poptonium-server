"""Tests for the remaining app/client_auth.py surface not covered by
tests/test_client_auth.py (which covers validate_plex_token + plex_user_identity).

Here: plex_account_id, plex_user_can_access, _extract_plex_token, the require_*
dependencies (require_plex_user / _token / _account), require_admin_or_plex_user,
and the per-token caching (a second call within the TTL window skips the Plex
round-trip).
"""

import httpx
import respx
from fastapi import Request

from app import client_auth
from app.config import PLEX_TV_USER_URL, PLEX_URL


def _fake_request(headers=None, query=""):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


# ---- plex_account_id --------------------------------------------------------

async def test_plex_account_id_empty_token():
    assert await client_auth.plex_account_id("") is None


@respx.mock
async def test_plex_account_id_prefers_uuid():
    route = respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, json={"uuid": "abc-uuid", "id": 4242})
    )
    assert await client_auth.plex_account_id("tok") == "abc-uuid"
    assert route.call_count == 1


@respx.mock
async def test_plex_account_id_falls_back_to_numeric_id():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(200, json={"id": 99}))
    assert await client_auth.plex_account_id("tok") == "99"


@respx.mock
async def test_plex_account_id_none_when_no_ids():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(200, json={"name": "x"}))
    assert await client_auth.plex_account_id("tok") is None


@respx.mock
async def test_plex_account_id_non_200():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(401))
    assert await client_auth.plex_account_id("tok") is None


@respx.mock
async def test_plex_account_id_bad_json():
    respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"})
    )
    assert await client_auth.plex_account_id("tok") is None


@respx.mock
async def test_plex_account_id_network_error():
    respx.get(PLEX_TV_USER_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await client_auth.plex_account_id("tok") is None


@respx.mock
async def test_plex_account_id_caches_on_success():
    route = respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, json={"uuid": "u1"})
    )
    assert await client_auth.plex_account_id("tok") == "u1"
    assert await client_auth.plex_account_id("tok") == "u1"
    # Second call served from cache -> only one HTTP round-trip.
    assert route.call_count == 1


# ---- plex_user_identity caching (identity parse itself is in the base file) --

@respx.mock
async def test_plex_user_identity_caches_on_success():
    route = respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, json={"id": 5, "email": "a@b.com"})
    )
    first = await client_auth.plex_user_identity("tok")
    second = await client_auth.plex_user_identity("tok")
    assert first == second
    assert route.call_count == 1


async def test_plex_user_identity_empty_token():
    assert await client_auth.plex_user_identity("") is None


@respx.mock
async def test_plex_user_identity_non_200():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(403))
    assert await client_auth.plex_user_identity("tok") is None


@respx.mock
async def test_plex_user_identity_network_error():
    respx.get(PLEX_TV_USER_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await client_auth.plex_user_identity("tok") is None


@respx.mock
async def test_plex_user_identity_bad_json():
    respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, content=b"nope", headers={"content-type": "application/json"})
    )
    assert await client_auth.plex_user_identity("tok") is None


# ---- plex_user_can_access ---------------------------------------------------

async def test_plex_user_can_access_missing_args():
    assert await client_auth.plex_user_can_access("", "123") is False
    assert await client_auth.plex_user_can_access("tok", "") is False
    # A path that reduces to an empty ratingKey.
    assert await client_auth.plex_user_can_access("tok", "/") is False


@respx.mock
async def test_plex_user_can_access_true_on_200():
    route = respx.get(f"{PLEX_URL}/library/metadata/123").mock(
        return_value=httpx.Response(200, json={})
    )
    assert await client_auth.plex_user_can_access("tok", "123") is True
    assert route.called


@respx.mock
async def test_plex_user_can_access_accepts_metadata_path():
    # A full /library/metadata/<k> path should be reduced to the bare key.
    respx.get(f"{PLEX_URL}/library/metadata/456").mock(return_value=httpx.Response(200, json={}))
    assert await client_auth.plex_user_can_access("tok", "/library/metadata/456") is True


@respx.mock
async def test_plex_user_can_access_false_on_404():
    respx.get(f"{PLEX_URL}/library/metadata/123").mock(return_value=httpx.Response(404))
    assert await client_auth.plex_user_can_access("tok", "123") is False


@respx.mock
async def test_plex_user_can_access_network_error():
    respx.get(f"{PLEX_URL}/library/metadata/123").mock(side_effect=httpx.ConnectError("boom"))
    assert await client_auth.plex_user_can_access("tok", "123") is False


# ---- _extract_plex_token ----------------------------------------------------

def test_extract_plex_token_header():
    req = _fake_request(headers={"X-Plex-Token": "hdr-tok"})
    assert client_auth._extract_plex_token(req) == "hdr-tok"


def test_extract_plex_token_query_uppercase():
    req = _fake_request(query="X-Plex-Token=q-tok")
    assert client_auth._extract_plex_token(req) == "q-tok"


def test_extract_plex_token_query_lowercase():
    req = _fake_request(query="x-plex-token=q-tok")
    assert client_auth._extract_plex_token(req) == "q-tok"


def test_extract_plex_token_none():
    assert client_auth._extract_plex_token(_fake_request()) is None


# ---- require_plex_user ------------------------------------------------------

async def test_require_plex_user_503_when_plex_unconfigured(monkeypatch):
    monkeypatch.setattr(client_auth, "plex_configured", lambda: False)
    try:
        await client_auth.require_plex_user(_fake_request(headers={"X-Plex-Token": "t"}))
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 503


async def test_require_plex_user_401_when_no_token(monkeypatch):
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    try:
        await client_auth.require_plex_user(_fake_request())
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


@respx.mock
async def test_require_plex_user_401_when_token_rejected(monkeypatch):
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    try:
        await client_auth.require_plex_user(_fake_request(headers={"X-Plex-Token": "bad"}))
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


@respx.mock
async def test_require_plex_user_passes_for_valid(monkeypatch):
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    await client_auth.require_plex_user(_fake_request(headers={"X-Plex-Token": "good"}))


# ---- require_plex_user_token ------------------------------------------------

@respx.mock
async def test_require_plex_user_token_returns_token(monkeypatch):
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    tok = await client_auth.require_plex_user_token(_fake_request(headers={"X-Plex-Token": "good"}))
    assert tok == "good"


@respx.mock
async def test_require_plex_user_token_raises_on_bad(monkeypatch):
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    try:
        await client_auth.require_plex_user_token(_fake_request(headers={"X-Plex-Token": "bad"}))
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


# ---- require_plex_account ---------------------------------------------------

@respx.mock
async def test_require_plex_account_returns_id():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(200, json={"uuid": "acct-1"}))
    acct = await client_auth.require_plex_account(_fake_request(headers={"X-Plex-Token": "tok"}))
    assert acct == "acct-1"


async def test_require_plex_account_401_when_no_token():
    try:
        await client_auth.require_plex_account(_fake_request())
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


@respx.mock
async def test_require_plex_account_401_when_unresolvable():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(401))
    try:
        await client_auth.require_plex_account(_fake_request(headers={"X-Plex-Token": "tok"}))
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


# ---- require_admin_or_plex_user ---------------------------------------------

async def test_require_admin_or_plex_user_admin_path(monkeypatch):
    # An admin session short-circuits: no Plex token needed.
    async def _has(_req):
        return True

    csrf_calls = {"n": 0}

    def _csrf(_req):
        csrf_calls["n"] += 1

    monkeypatch.setattr("app.auth.has_admin_session", _has)
    monkeypatch.setattr("app.auth.check_admin_csrf", _csrf)
    await client_auth.require_admin_or_plex_user(_fake_request())
    assert csrf_calls["n"] == 1  # CSRF enforced for the cookie-admin path


@respx.mock
async def test_require_admin_or_plex_user_falls_back_to_plex(monkeypatch):
    async def _has(_req):
        return False

    monkeypatch.setattr("app.auth.has_admin_session", _has)
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    await client_auth.require_admin_or_plex_user(_fake_request(headers={"X-Plex-Token": "good"}))


@respx.mock
async def test_require_admin_or_plex_user_rejects_when_neither(monkeypatch):
    async def _has(_req):
        return False

    monkeypatch.setattr("app.auth.has_admin_session", _has)
    monkeypatch.setattr(client_auth, "plex_configured", lambda: True)
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    try:
        await client_auth.require_admin_or_plex_user(_fake_request(headers={"X-Plex-Token": "bad"}))
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


# ---- validate_plex_token caching (base file covers accept/reject only) ------

@respx.mock
async def test_validate_plex_token_caches_on_success():
    route = respx.get(f"{PLEX_URL}/library/sections").mock(
        return_value=httpx.Response(200, json={})
    )
    assert await client_auth.validate_plex_token("tok") is True
    assert await client_auth.validate_plex_token("tok") is True
    assert route.call_count == 1  # second call cached


@respx.mock
async def test_validate_plex_token_network_error():
    respx.get(f"{PLEX_URL}/library/sections").mock(side_effect=httpx.ConnectError("boom"))
    assert await client_auth.validate_plex_token("tok") is False


# ---- cache-eviction branches (len > _CACHE_MAX -> clear) --------------------

@respx.mock
async def test_validate_plex_token_evicts_when_cache_full():
    # Over-fill the token cache so the next success trips the clear() branch.
    client_auth._token_cache.update({f"k{i}": 1e18 for i in range(client_auth._CACHE_MAX + 1)})
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    assert await client_auth.validate_plex_token("fresh") is True
    # Cache was cleared, then the fresh token cached -> exactly one entry.
    assert len(client_auth._token_cache) == 1


@respx.mock
async def test_plex_account_id_evicts_when_cache_full():
    client_auth._account_cache.update(
        {f"k{i}": (1e18, "x") for i in range(client_auth._CACHE_MAX + 1)}
    )
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(200, json={"uuid": "u1"}))
    assert await client_auth.plex_account_id("fresh") == "u1"
    assert len(client_auth._account_cache) == 1


@respx.mock
async def test_plex_user_identity_evicts_when_cache_full():
    client_auth._identity_cache.update(
        {f"k{i}": (1e18, {}) for i in range(client_auth._CACHE_MAX + 1)}
    )
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(200, json={"id": 1, "email": "a@b.com"}))
    assert await client_auth.plex_user_identity("fresh") is not None
    assert len(client_auth._identity_cache) == 1
