"""Tests for app/subtitle_vtt.py — Plex subtitle streams re-served as WebVTT for
Chromecast side-loading.

Auth mirrors client_auth's ``require_plex_user``: the caller presents a Plex
token (query param — the Chromecast can't send headers) and the server
validates it against Plex's ``/library/sections``. The subtitle bytes come from
``/library/streams/{id}`` fetched with the server's own token.
"""

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import subtitle_vtt
from app.subtitle_vtt import _ass_to_vtt, _srt_to_vtt, to_webvtt

PLEX = "http://plex.test"

SRT = """1
00:00:01,000 --> 00:00:02,500
Hello <i>world</i>

2
00:01:00,250 --> 00:01:03,000
{\\an8}Positioned line
second line
"""

ASS = """[Script Info]
Title: t

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\\an8}Hello, world
Dialogue: 0,0:01:00.25,0:01:03.00,Default,,0,0,0,,Line one\\NLine two
"""


def _app():
    app = FastAPI()
    app.include_router(subtitle_vtt.router)
    return app


def _client():
    return httpx.AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")


def _mock_auth(valid: bool = True):
    """require_plex_user validates the presented token via /library/sections."""
    def _responder(request):
        tok = request.headers.get("X-Plex-Token")
        return httpx.Response(200 if (valid and tok == "user-token") else 401)

    respx.get(f"{PLEX}/library/sections").mock(side_effect=_responder)


# --- conversion ---------------------------------------------------------------

def test_srt_to_vtt():
    vtt = _srt_to_vtt(SRT)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in vtt
    # Cue indices dropped, ASS override tags stripped, HTML tags kept.
    assert "\n1\n" not in vtt
    assert "{\\an8}" not in vtt
    assert "Hello <i>world</i>" in vtt
    assert "Positioned line" in vtt
    assert "second line" in vtt


def test_ass_to_vtt():
    vtt = _ass_to_vtt(ASS)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in vtt
    assert "{\\an8}" not in vtt
    # The Text field keeps its embedded comma; \N becomes a line break.
    assert "Hello, world" in vtt
    assert "Line one\nLine two" in vtt


def test_to_webvtt_dispatch():
    assert "Hello <i>world</i>" in to_webvtt(SRT.encode())
    assert "Hello, world" in to_webvtt(ASS.encode())
    # Already-VTT passes through untouched (BOM stripped).
    vtt = "﻿WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
    assert to_webvtt(vtt.encode()) == vtt.lstrip("﻿")


# --- endpoint -----------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_get_vtt_ok():
    _mock_auth()
    route = respx.get(f"{PLEX}/library/streams/42").mock(
        return_value=httpx.Response(200, content=SRT.encode())
    )
    async with _client() as c:
        r = await c.get("/subtitles/42.vtt", params={"X-Plex-Token": "user-token"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/vtt")
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.text.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in r.text
    # Fetched from Plex with the SERVER token, not the caller's.
    assert route.calls.last.request.headers["X-Plex-Token"] == "admin-token"


@pytest.mark.asyncio
@respx.mock
async def test_get_vtt_requires_valid_token():
    _mock_auth()
    async with _client() as c:
        no_token = await c.get("/subtitles/42.vtt")
        bad_token = await c.get("/subtitles/42.vtt", params={"X-Plex-Token": "wrong"})
    assert no_token.status_code == 401
    assert bad_token.status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_get_vtt_embedded_501_maps_to_404():
    _mock_auth()
    respx.get(f"{PLEX}/library/streams/63424").mock(return_value=httpx.Response(501))
    async with _client() as c:
        r = await c.get("/subtitles/63424.vtt", params={"X-Plex-Token": "user-token"})
    assert r.status_code == 404
    assert r.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_preflight_no_auth():
    async with _client() as c:
        r = await c.options("/subtitles/42.vtt")
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
