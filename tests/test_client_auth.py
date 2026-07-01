"""Tests for the Plex-token client auth gate and identity resolution."""

import httpx
import respx

from app import client_auth
from app.config import PLEX_TV_USER_URL, PLEX_URL


@respx.mock
async def test_validate_plex_token_accepts_200():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(200, json={}))
    assert await client_auth.validate_plex_token("good-token") is True


@respx.mock
async def test_validate_plex_token_rejects_401():
    respx.get(f"{PLEX_URL}/library/sections").mock(return_value=httpx.Response(401))
    assert await client_auth.validate_plex_token("bad-token") is False


async def test_validate_plex_token_empty_is_false():
    assert await client_auth.validate_plex_token("") is False


@respx.mock
async def test_plex_user_identity_parses_id_and_email():
    respx.get(PLEX_TV_USER_URL).mock(
        return_value=httpx.Response(200, json={"id": 4242, "email": "Ada@Example.com", "uuid": "u1"})
    )
    identity = await client_auth.plex_user_identity("tok")
    assert identity == {"plex_id": 4242, "email": "ada@example.com"}  # email lowercased


@respx.mock
async def test_plex_user_identity_missing_email_is_none():
    respx.get(PLEX_TV_USER_URL).mock(return_value=httpx.Response(200, json={"id": 5, "email": ""}))
    identity = await client_auth.plex_user_identity("tok")
    assert identity == {"plex_id": 5, "email": None}
