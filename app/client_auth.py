"""Client authentication for abuse-sensitive endpoints.

The iOS app has no account with this service, but every user already holds a Plex
token that the server authorizes. For endpoints that mutate state or spend a
shared quota (Overseerr requests, OpenSubtitles downloads, the plugin proxy) we
require the caller to present that Plex token (``X-Plex-Token`` header or query
param) and validate it against the configured Plex server. This stops anonymous
internet callers from abusing those endpoints when the service is exposed
alongside Plex, without the app needing a separate login.

The service always runs alongside Plex, so if Plex isn't configured we can't
validate and the gate fails CLOSED (returns 503).
"""

import hashlib
import time
from typing import Optional

from fastapi import HTTPException, Request

from .config import PLEX_TV_USER_URL, PLEX_URL, log
from .http_client import http_client
from .plex import plex_configured

# Positive validations are cached briefly so a burst of client calls (a library
# refresh fires many) costs at most one Plex round-trip per token per window.
_VALID_TTL = 300  # seconds
_token_cache: dict[str, float] = {}            # sha256(token) -> expiry
_account_cache: dict[str, tuple[float, str]] = {}  # sha256(token) -> (expiry, account_id)
_CACHE_MAX = 4096


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def validate_plex_token(token: str) -> bool:
    """True if `token` is accepted by the Plex server (i.e. a real authorized
    user of this server). Cached on success for a short window."""
    if not token:
        return False
    key = _digest(token)
    hit = _token_cache.get(key)
    if hit and hit > time.time():
        return True
    try:
        # /library/sections returns 200 only for a token with library access,
        # 401 otherwise. Cheap and works for owner + shared users alike.
        resp = await http_client().get(
            f"{PLEX_URL}/library/sections",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Plex token validation failed: %s", e)
        return False
    if resp.status_code == 200:
        if len(_token_cache) > _CACHE_MAX:
            _token_cache.clear()
        _token_cache[key] = time.time() + _VALID_TTL
        return True
    return False


async def plex_account_id(token: str) -> Optional[str]:
    """Resolve the Plex account id (uuid) that owns `token`, via plex.tv. Only a
    real account token resolves, so this both authenticates and identifies the
    caller. Mirrors the app's own account-id derivation (uuid, falling back to the
    numeric id). Cached briefly per token."""
    if not token:
        return None
    key = _digest(token)
    hit = _account_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        resp = await http_client().get(
            PLEX_TV_USER_URL,
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Plex account resolution failed: %s", e)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    account_id = data.get("uuid") or (str(data["id"]) if data.get("id") is not None else None)
    if account_id:
        if len(_account_cache) > _CACHE_MAX:
            _account_cache.clear()
        _account_cache[key] = (time.time() + _VALID_TTL, account_id)
    return account_id


async def plex_user_can_access(token: str, rating_key: str) -> bool:
    """True if `token` (the caller's own Plex token) can read the given library
    item — i.e. the user actually has access to it. Used to gate server-side
    actions performed with the admin token against a client-supplied ratingKey."""
    if not token or not rating_key:
        return False
    # Accept either a bare key or a /library/metadata/<k> path.
    rk = str(rating_key).strip().strip("/").split("/")[-1]
    if not rk:
        return False
    try:
        resp = await http_client().get(
            f"{PLEX_URL}/library/metadata/{rk}",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Plex item access check failed: %s", e)
        return False
    return resp.status_code == 200


def _extract_plex_token(request: Request) -> Optional[str]:
    return (
        request.headers.get("X-Plex-Token")
        or request.query_params.get("X-Plex-Token")
        or request.query_params.get("x-plex-token")
    )


async def require_plex_user(request: Request) -> None:
    """Dependency: allow only callers presenting a Plex token this server accepts."""
    if not plex_configured():
        raise HTTPException(503, "Client authentication unavailable: Plex not configured")
    token = _extract_plex_token(request)
    if not token or not await validate_plex_token(token):
        raise HTTPException(401, "A valid Plex token is required")


async def require_plex_user_token(request: Request) -> str:
    """Like `require_plex_user`, but returns the validated token for handlers that
    must then act on the caller's behalf (e.g. verify item access)."""
    await require_plex_user(request)
    return _extract_plex_token(request)  # guaranteed present once the gate passes


async def require_plex_account(request: Request) -> str:
    """Dependency: resolve the caller's Plex account id from their token. The
    returned id is authoritative — endpoints MUST key per-user data on it and
    never on a client-supplied id (this is what closes the IDOR)."""
    token = _extract_plex_token(request)
    account_id = await plex_account_id(token) if token else None
    if not account_id:
        raise HTTPException(401, "A valid Plex account token is required")
    return account_id


async def require_admin_or_plex_user(request: Request) -> None:
    """Dependency: allow either an authenticated admin session or a Plex user.

    The plugin proxy is reached two ways: the iOS app calls functional endpoints
    carrying a Plex token, while the admin web UI reads/writes plugin config
    (e.g. ``plugin/settings``) carrying only its admin session cookie. The admin
    is strictly more privileged, so accept it without demanding a Plex token —
    otherwise admin config calls 401 and the UI mistakes it for an expired login.
    """
    # Imported lazily to keep auth.py free of a client_auth dependency.
    from .auth import check_admin_csrf, has_admin_session
    if await has_admin_session(request):
        check_admin_csrf(request)  # admin via cookie → CSRF-protect unsafe methods
        return
    await require_plex_user(request)
