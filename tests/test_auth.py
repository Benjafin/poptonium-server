"""Tests for admin authentication (app/auth.py).

Covers first-run account setup, login/logout, session cookies (issue + validate),
the double-submit CSRF guard, the require_admin dependency, has_admin_session, the
setup-needed vs configured states, and password hashing/verification.

Every test points app.db.DB_PATH at a fresh temp SQLite file so the admin account
lives in an isolated DB. Endpoints are driven via a fresh FastAPI() mounting only
the auth router over an ASGITransport (skips real startup); no network is used
except the Plex probes in /admin/auth/state, which we monkeypatch out.
"""

import json
import time

import httpx
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport

import app.auth as auth
import app.db as _db


def _use_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "auth.db"))


def _auth_app():
    app = FastAPI()
    app.include_router(auth.router)

    # A tiny protected route to exercise require_admin end-to-end.
    @app.post("/admin/protected", dependencies=[Depends(auth.require_admin)])
    async def _protected():
        return {"ok": True}

    return app


# ---- password hashing / verification (pure) ---------------------------------

def test_hash_password_is_deterministic_and_salted():
    salt = b"0123456789abcdef"
    h1 = auth._hash_password("hunter22", salt)
    h2 = auth._hash_password("hunter22", salt)
    assert h1 == h2
    # Different salt -> different hash for the same password.
    assert auth._hash_password("hunter22", b"fedcba9876543210") != h1
    # It's a hex digest, not the plaintext.
    assert "hunter22" not in h1
    bytes.fromhex(h1)  # valid hex


def test_verify_password_true_and_false():
    salt = b"0123456789abcdef"
    account = {"salt": salt.hex(), "hash": auth._hash_password("correct-horse", salt)}
    assert auth._verify_password(account, "correct-horse") is True
    assert auth._verify_password(account, "wrong") is False


def test_verify_password_bad_salt_is_false():
    assert auth._verify_password({"salt": "not-hex-zz", "hash": "x"}, "pw") is False


# ---- account storage / create_account ---------------------------------------

async def test_is_configured_false_then_true(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert await auth.is_configured() is False
    await auth.create_account("admin", "supersecret")
    assert await auth.is_configured() is True


async def test_get_account_returns_none_on_corrupt_json(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await _db.meta_set(auth._META_KEY, "{not valid json")
    assert await auth._get_account() is None
    assert await auth.is_configured() is False


async def test_create_account_stores_hash_not_plaintext(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("Ada", "longenough")
    assert account["username"] == "Ada"
    assert "longenough" not in account["hash"]
    assert "secret" in account and len(account["secret"]) == 64
    # Round-trips through the meta store.
    stored = json.loads(await _db.meta_get(auth._META_KEY))
    assert stored["username"] == "Ada"


async def test_create_account_rejects_duplicate(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await auth.create_account("admin", "longenough")
    try:
        await auth.create_account("admin2", "longenough2")
        assert False, "expected HTTPException"
    except Exception as e:
        assert getattr(e, "status_code", None) == 409


async def test_create_account_rejects_blank_username(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    try:
        await auth.create_account("   ", "longenough")
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 422


async def test_create_account_rejects_short_password(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    try:
        await auth.create_account("admin", "short")
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 422


# ---- verify_credentials -----------------------------------------------------

async def test_verify_credentials_none_when_unconfigured(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert await auth.verify_credentials("admin", "pw") is None


async def test_verify_credentials_ok_and_bad(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await auth.create_account("admin", "longenough")
    assert await auth.verify_credentials("admin", "longenough") is not None
    assert await auth.verify_credentials("admin", "wrongpass") is None
    assert await auth.verify_credentials("nobody", "longenough") is None


# ---- token issue / validation ----------------------------------------------

async def test_make_token_valid_and_tamper_rejected(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    token = auth.make_token(account)
    assert await auth._token_valid(token) is True
    # Tampered signature.
    payload_b64, _, _sig = token.partition(".")
    assert await auth._token_valid(f"{payload_b64}.deadbeef") is False
    # Malformed (no dot) / empty.
    assert await auth._token_valid("nodot") is False
    assert await auth._token_valid("") is False


async def test_token_valid_false_when_unconfigured(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    # Craft a token against an account that isn't stored.
    fake = {"username": "admin", "secret": "s" * 64}
    token = auth.make_token(fake)
    assert await auth._token_valid(token) is False


async def test_token_valid_rejects_expired(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    # Build an already-expired token signed with the real secret.
    payload = {"u": account["username"], "exp": int(time.time()) - 10}
    payload_b64 = auth._b64(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{payload_b64}.{auth._sign(payload_b64, account['secret'])}"
    assert await auth._token_valid(token) is False


async def test_token_valid_rejects_wrong_username(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    payload = {"u": "someone-else", "exp": int(time.time()) + 1000}
    payload_b64 = auth._b64(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{payload_b64}.{auth._sign(payload_b64, account['secret'])}"
    assert await auth._token_valid(token) is False


async def test_token_valid_rejects_bad_payload_encoding(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    # Payload that signs correctly but isn't valid base64-json.
    payload_b64 = "!!!notbase64!!!"
    token = f"{payload_b64}.{auth._sign(payload_b64, account['secret'])}"
    assert await auth._token_valid(token) is False


# ---- _extract_token ---------------------------------------------------------

def _fake_request(cookies=None, headers=None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "scheme": "http",
        "headers": [],
    }
    hdrs = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        hdrs.append((b"cookie", cookie_str.encode()))
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    scope["headers"] = hdrs
    return Request(scope)


def test_extract_token_from_cookie():
    req = _fake_request(cookies={auth.COOKIE_NAME: "cookie-tok"})
    assert auth._extract_token(req) == "cookie-tok"


def test_extract_token_from_bearer():
    req = _fake_request(headers={"Authorization": "Bearer bearer-tok"})
    assert auth._extract_token(req) == "bearer-tok"


def test_extract_token_cookie_wins_over_bearer():
    req = _fake_request(
        cookies={auth.COOKIE_NAME: "cookie-tok"},
        headers={"Authorization": "Bearer bearer-tok"},
    )
    assert auth._extract_token(req) == "cookie-tok"


def test_extract_token_none():
    assert auth._extract_token(_fake_request()) is None
    # Non-bearer auth header -> None.
    assert auth._extract_token(_fake_request(headers={"Authorization": "Basic abc"})) is None


# ---- _is_https --------------------------------------------------------------

def test_is_https_from_forwarded_proto():
    req = _fake_request(headers={"X-Forwarded-Proto": "https, http"})
    assert auth._is_https(req) is True


def test_is_https_defaults_to_scheme():
    # No forwarded header -> falls back to url.scheme ("http" for our fake).
    assert auth._is_https(_fake_request()) is False


# ---- has_admin_session ------------------------------------------------------

async def test_has_admin_session_true_with_cookie(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    token = auth.make_token(account)
    req = _fake_request(cookies={auth.COOKIE_NAME: token})
    assert await auth.has_admin_session(req) is True


async def test_has_admin_session_false_when_unconfigured(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    req = _fake_request(cookies={auth.COOKIE_NAME: "whatever"})
    assert await auth.has_admin_session(req) is False


async def test_has_admin_session_false_without_token(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await auth.create_account("admin", "longenough")
    assert await auth.has_admin_session(_fake_request()) is False


# ---- check_admin_csrf -------------------------------------------------------

def _csrf_request(method, cookies=None, csrf_header=None):
    scope = {"type": "http", "method": method, "headers": []}
    hdrs = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        hdrs.append((b"cookie", cookie_str.encode()))
    if csrf_header is not None:
        hdrs.append((auth.CSRF_HEADER.lower().encode(), csrf_header.encode()))
    scope["headers"] = hdrs
    return Request(scope)


def test_csrf_safe_method_skipped():
    # GET is safe -> passes even with no CSRF material.
    auth.check_admin_csrf(_csrf_request("GET"))


def test_csrf_no_session_cookie_skipped():
    # Bearer/API caller (no session cookie) -> CSRF not applicable.
    auth.check_admin_csrf(_csrf_request("POST"))


def test_csrf_valid_double_submit_passes():
    req = _csrf_request(
        "POST",
        cookies={auth.COOKIE_NAME: "sess", auth.CSRF_COOKIE_NAME: "tok123"},
        csrf_header="tok123",
    )
    auth.check_admin_csrf(req)  # no raise


def test_csrf_missing_header_raises():
    req = _csrf_request(
        "POST",
        cookies={auth.COOKIE_NAME: "sess", auth.CSRF_COOKIE_NAME: "tok123"},
    )
    try:
        auth.check_admin_csrf(req)
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 403


def test_csrf_mismatch_raises():
    req = _csrf_request(
        "POST",
        cookies={auth.COOKIE_NAME: "sess", auth.CSRF_COOKIE_NAME: "tok123"},
        csrf_header="different",
    )
    try:
        auth.check_admin_csrf(req)
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 403


# ---- require_admin (unit) ---------------------------------------------------

async def test_require_admin_unconfigured_raises(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    try:
        await auth.require_admin(_fake_request())
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


async def test_require_admin_bad_token_raises(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await auth.create_account("admin", "longenough")
    try:
        await auth.require_admin(_fake_request(headers={"Authorization": "Bearer nope"}))
        assert False
    except Exception as e:
        assert getattr(e, "status_code", None) == 401


async def test_require_admin_bearer_ok(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    token = auth.make_token(account)
    # Bearer path -> no session cookie -> CSRF not applicable -> passes.
    await auth.require_admin(_fake_request(headers={"Authorization": f"Bearer {token}"}))


# ---- endpoint flows ---------------------------------------------------------

async def test_auth_state_setup_needed(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "plex_configured", lambda: True)

    async def _reachable():
        return True

    monkeypatch.setattr(auth, "plex_reachable", _reachable)

    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/admin/auth/state")
    body = resp.json()
    assert body["configured"] is False
    assert body["authenticated"] is False
    assert body["username"] is None
    assert body["plex_configured"] is True
    assert body["plex_ready"] is True


async def test_auth_state_plex_not_configured_skips_probe(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "plex_configured", lambda: False)

    called = {"probe": False}

    async def _reachable():
        called["probe"] = True
        return True

    monkeypatch.setattr(auth, "plex_reachable", _reachable)

    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/admin/auth/state")
    body = resp.json()
    assert body["plex_configured"] is False
    assert body["plex_ready"] is False
    assert called["probe"] is False  # probe short-circuited


async def test_setup_then_state_authenticated(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(auth, "plex_configured", lambda: True)

    async def _reachable():
        return True

    monkeypatch.setattr(auth, "plex_reachable", _reachable)

    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        setup = await ac.post(
            "/admin/auth/setup", json={"username": "admin", "password": "longenough"}
        )
        assert setup.status_code == 200
        assert setup.json()["ok"] is True
        assert setup.json()["username"] == "admin"
        # A session cookie + CSRF cookie were set.
        assert auth.COOKIE_NAME in ac.cookies
        assert auth.CSRF_COOKIE_NAME in ac.cookies

        # State now reflects an authenticated session (cookie carried by the client).
        state = await ac.get("/admin/auth/state")
        sbody = state.json()
        assert sbody["configured"] is True
        assert sbody["authenticated"] is True
        assert sbody["username"] == "admin"


async def test_setup_twice_conflicts(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        first = await ac.post(
            "/admin/auth/setup", json={"username": "admin", "password": "longenough"}
        )
        assert first.status_code == 200
        second = await ac.post(
            "/admin/auth/setup", json={"username": "admin2", "password": "longenough2"}
        )
        assert second.status_code == 409


async def test_login_success_and_failure(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    await auth.create_account("admin", "longenough")

    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        bad = await ac.post(
            "/admin/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert bad.status_code == 401

        ok = await ac.post(
            "/admin/auth/login", json={"username": "admin", "password": "longenough"}
        )
        assert ok.status_code == 200
        assert ok.json()["token"]
        assert auth.COOKIE_NAME in ac.cookies


async def test_protected_route_requires_session_and_csrf(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    account = await auth.create_account("admin", "longenough")
    token = auth.make_token(account)

    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # No credentials at all -> 401.
        r0 = await ac.post("/admin/protected")
        assert r0.status_code == 401

        # Bearer token, no cookie -> passes (CSRF not applicable to API callers).
        r1 = await ac.post(
            "/admin/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert r1.status_code == 200

        # Cookie session WITHOUT the CSRF header -> 403.
        ac.cookies.set(auth.COOKIE_NAME, token)
        ac.cookies.set(auth.CSRF_COOKIE_NAME, "csrf-val")
        r2 = await ac.post("/admin/protected")
        assert r2.status_code == 403

        # Cookie session WITH matching CSRF header -> 200.
        r3 = await ac.post("/admin/protected", headers={auth.CSRF_HEADER: "csrf-val"})
        assert r3.status_code == 200


async def test_logout_clears_cookies(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    transport = ASGITransport(app=_auth_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/admin/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # The response instructs the browser to delete both cookies.
        set_cookie = resp.headers.get_list("set-cookie")
        joined = " ".join(set_cookie)
        assert auth.COOKIE_NAME in joined
        assert auth.CSRF_COOKIE_NAME in joined
