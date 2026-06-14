"""Admin authentication: first-run account setup, login, and the guard.

A single admin account is stored in the ``meta`` KV store (username + a PBKDF2
password hash + a per-install signing secret). Login issues an HMAC-signed,
expiring session token, returned both as an ``HttpOnly`` cookie (for the admin
web UI) and in the response body (for API clients / curl).

``require_admin`` is a FastAPI dependency placed on every config-mutating and
admin-only endpoint. The admin HTML shell itself stays public: it loads, then
asks ``/admin/auth/state`` whether to render the setup form, the login form, or
the dashboard.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from .db import meta_get, meta_set
from .plex import plex_configured, plex_reachable

router = APIRouter()

_META_KEY = "admin_account"
COOKIE_NAME = "bf_admin_session"
CSRF_COOKIE_NAME = "bf_admin_csrf"
CSRF_HEADER = "X-CSRF-Token"
TOKEN_TTL = 30 * 86400          # sessions last 30 days
_PBKDF2_ITERS = 200_000
_MIN_PASSWORD_LEN = 8
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


# ---------- Account storage + password hashing ----------

async def _get_account() -> Optional[dict]:
    raw = await meta_get(_META_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def is_configured() -> bool:
    return await _get_account() is not None


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return dk.hex()


async def create_account(username: str, password: str) -> dict:
    """Create the single admin account. Fails if one already exists."""
    if await is_configured():
        raise HTTPException(409, "Admin account already configured")
    username = (username or "").strip()
    if not username:
        raise HTTPException(422, "Username required")
    if len(password or "") < _MIN_PASSWORD_LEN:
        raise HTTPException(422, f"Password must be at least {_MIN_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(16)
    account = {
        "username": username,
        "salt": salt.hex(),
        "hash": _hash_password(password, salt),
        # Per-install secret used to sign session tokens. Rotating it (e.g. on a
        # password change) invalidates every outstanding session.
        "secret": secrets.token_hex(32),
        "created_at": time.time(),
    }
    await meta_set(_META_KEY, json.dumps(account))
    return account


def _verify_password(account: dict, password: str) -> bool:
    try:
        salt = bytes.fromhex(account["salt"])
    except Exception:
        return False
    expected = account.get("hash", "")
    return hmac.compare_digest(_hash_password(password, salt), expected)


async def verify_credentials(username: str, password: str) -> Optional[dict]:
    account = await _get_account()
    if not account:
        return None
    if (username or "").strip() != account.get("username"):
        return None
    return account if _verify_password(account, password) else None


# ---------- Session tokens (HMAC-signed, expiring) ----------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64(sig)


def make_token(account: dict) -> str:
    payload = {"u": account["username"], "exp": int(time.time()) + TOKEN_TTL}
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, account['secret'])}"


async def _token_valid(token: str) -> bool:
    if not token or "." not in token:
        return False
    account = await _get_account()
    if not account:
        return False
    payload_b64, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(payload_b64, account["secret"])):
        return False
    try:
        payload = json.loads(_unb64(payload_b64))
    except Exception:
        return False
    if payload.get("u") != account.get("username"):
        return False
    return int(payload.get("exp", 0)) > time.time()


def _extract_token(request: Request) -> Optional[str]:
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def has_admin_session(request: Request) -> bool:
    """True if the request carries a valid admin session (cookie or bearer).

    Non-raising counterpart to ``require_admin`` — for guards that accept an
    admin session as *one* of several acceptable credentials.
    """
    return await is_configured() and await _token_valid(_extract_token(request))


def check_admin_csrf(request: Request) -> None:
    """Double-submit CSRF guard for cookie-authenticated, state-changing requests.

    Browsers attach the session cookie automatically (the CSRF risk); we require
    them to also echo the non-HttpOnly CSRF cookie in a header, which a cross-site
    page cannot read. Bearer-token API callers (curl) don't carry the cookie and
    are not subject to CSRF, so they're skipped.
    """
    if request.method in _SAFE_METHODS:
        return
    if not request.cookies.get(COOKIE_NAME):
        return  # not cookie-authenticated → CSRF not applicable
    sent = request.headers.get(CSRF_HEADER, "")
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_csrf or not sent or not hmac.compare_digest(sent, cookie_csrf):
        raise HTTPException(403, "CSRF validation failed")


async def require_admin(request: Request) -> None:
    """Dependency: allow only requests carrying a valid admin session."""
    if not await is_configured():
        # Until the account is set up nothing is protected — but the setup
        # endpoint is the only mutation reachable, and it self-guards.
        raise HTTPException(401, "Admin account not configured")
    if not await _token_valid(_extract_token(request)):
        raise HTTPException(401, "Authentication required")
    check_admin_csrf(request)


def _is_https(request: Request) -> bool:
    """Whether the original client connection was HTTPS (honouring the reverse
    proxy's X-Forwarded-Proto). Used to set the cookie `Secure` flag without
    breaking plain-HTTP LAN access to the admin UI."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto.split(",")[0].strip().lower() == "https"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    secure = _is_https(request)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=TOKEN_TTL, httponly=True, secure=secure, samesite="lax", path="/",
    )
    # Companion CSRF token: readable by the admin JS (not HttpOnly) so it can echo
    # it back in the X-CSRF-Token header on mutating calls (double-submit).
    response.set_cookie(
        CSRF_COOKIE_NAME, secrets.token_urlsafe(32),
        max_age=TOKEN_TTL, httponly=False, secure=secure, samesite="lax", path="/",
    )


# ---------- Routes ----------

class Credentials(BaseModel):
    username: str
    password: str


@router.get("/admin/auth/state")
async def auth_state(request: Request):
    """Public: tells the UI whether to show setup, login, or the dashboard.

    A working Plex connection is mandatory, so we also report whether Plex is
    configured and reachable. The UI blocks on the setup screen until it is.
    """
    configured = await is_configured()
    authed = configured and await _token_valid(_extract_token(request))
    account = await _get_account() if configured else None
    plex_set = plex_configured()
    plex_ok = await plex_reachable() if plex_set else False
    return {
        "configured": configured,
        "authenticated": authed,
        "username": account.get("username") if (account and authed) else None,
        "plex_configured": plex_set,
        "plex_ready": plex_ok,
    }


@router.post("/admin/auth/setup")
async def auth_setup(creds: Credentials, request: Request, response: Response):
    """Public ONLY on first run: create the admin account, then log in."""
    account = await create_account(creds.username, creds.password)
    token = make_token(account)
    _set_session_cookie(response, request, token)
    return {"ok": True, "username": account["username"], "token": token}


@router.post("/admin/auth/login")
async def auth_login(creds: Credentials, request: Request, response: Response):
    account = await verify_credentials(creds.username, creds.password)
    if not account:
        raise HTTPException(401, "Invalid username or password")
    token = make_token(account)
    _set_session_cookie(response, request, token)
    return {"ok": True, "username": account["username"], "token": token}


@router.post("/admin/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return {"ok": True}
