"""Tests for app/cast_subtitles.py: embedded subtitle extraction for the Cast receiver.

Plex is mocked with respx; ffmpeg is a small fake script that records its arguments and
prints a WebVTT document, so the tests check access rules, stream selection, arguments,
caching and the fallback errors the receiver relies on.
"""

import os
import stat
import sys

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import cast_subtitles
from app.config import settings

PLEX = "http://plex.test"
VTT = "WEBVTT\n\n29:48.997 --> 29:52.626\nHow about, for the next few hours,\n"


def _item(file, *, codec="subrip", key=None, index=2):
    stream = {"id": 2799, "streamType": 3, "codec": codec, "index": index}
    if key:
        stream["key"] = key
    return {"ratingKey": "147", "Media": [{"Part": [{"file": file, "Stream": [
        {"id": 1, "streamType": 1, "codec": "hevc", "index": 0}, stream]}]}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(settings._values, "PLEX_URL", PLEX)
    monkeypatch.setitem(settings._values, "PLEX_TOKEN", "admin-token")
    monkeypatch.setattr(cast_subtitles, "CACHE_DIR", str(tmp_path / "cache"))
    cast_subtitles._identity.update(expiry=0.0, machine=None)
    media = tmp_path / "Ted Lasso S03E08.mkv"
    media.write_bytes(b"\x1a\x45\xdf\xa3")
    argfile = tmp_path / "args.txt"
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(argfile)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({VTT!r})\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FFMPEG_PATH", str(fake))
    return {"media": str(media), "args": argfile}


def _client():
    app = FastAPI()
    app.include_router(cast_subtitles.router)
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _mock_plex(item, readable_by=("user-token",)):
    respx.get(f"{PLEX}/library/sections").mock(
        side_effect=lambda r: httpx.Response(200 if r.headers.get("X-Plex-Token") in readable_by else 401))

    def item_resp(r):
        if r.headers.get("X-Plex-Token") not in readable_by:
            return httpx.Response(401)
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [item]}})

    respx.get(url__regex=rf"{PLEX}/library/metadata/\d+$").mock(side_effect=item_resp)
    respx.get(f"{PLEX}/identity").mock(return_value=httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "mid-1"}}))


async def _get(params, token="user-token"):
    async with _client() as c:
        return await c.get("/cast/subtitles", params=params, headers={"X-Plex-Token": token} if token else {})


@respx.mock
async def test_extracts_from_offset_with_absolute_timestamps(env):
    _mock_plex(_item(env["media"]))
    r = await _get({"ratingKey": "147", "streamId": 2799, "offset": 1780, "machineIdentifier": "mid-1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/vtt")
    assert r.text == VTT
    args = env["args"].read_text()
    assert "-ss 1780.000 -copyts -i" in args and "-map 0:2" in args and "-c:s webvtt" in args
    # Only a run from the start is complete enough to cache.
    assert not os.listdir(cast_subtitles.CACHE_DIR)


@respx.mock
async def test_full_run_is_cached(env):
    _mock_plex(_item(env["media"]))
    params = {"ratingKey": "147", "streamId": 2799}
    assert (await _get(params)).text == VTT
    r = await _get({**params, "offset": 600})
    assert r.headers["x-subtitle-source"] == "cache" and r.text == VTT
    assert len(env["args"].read_text().splitlines()) == 1


@respx.mock
async def test_ass_keeps_styling(env):
    _mock_plex(_item(env["media"], codec="ass"))
    r = await _get({"ratingKey": "147", "streamId": 2799, "format": "ass"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-ssa")
    assert "-c:s copy -f ass" in env["args"].read_text()


@respx.mock
@pytest.mark.parametrize("item_kw,params,status", [
    ({"codec": "pgs"}, {}, 415),                       # picture subtitles: Plex burns those
    ({"key": "/library/streams/2799"}, {}, 415),        # sidecar files come from Plex directly
    ({"codec": "subrip"}, {"format": "ass"}, 415),      # no ASS from an SRT source
    ({}, {"streamId": 5}, 404),                         # not a stream of this item
    ({}, {"machineIdentifier": "other"}, 409),          # receiver plays from another server
])
async def test_refusals_let_the_receiver_fall_back(env, item_kw, params, status):
    _mock_plex(_item(env["media"], **item_kw))
    r = await _get({"ratingKey": "147", "streamId": 2799, **params})
    assert r.status_code == status


@respx.mock
async def test_needs_a_token_plex_accepts(env):
    _mock_plex(_item(env["media"]))
    assert (await _get({"ratingKey": "147", "streamId": 2799}, token=None)).status_code == 401
    assert (await _get({"ratingKey": "147", "streamId": 2799}, token="stranger")).status_code == 401


@respx.mock
async def test_unmounted_media_and_missing_ffmpeg(env, monkeypatch):
    _mock_plex(_item("/movies/not-here.mkv"))
    assert (await _get({"ratingKey": "147", "streamId": 2799})).status_code == 404
    monkeypatch.setenv("FFMPEG_PATH", "")
    monkeypatch.setattr(cast_subtitles.shutil, "which", lambda _: None)
    assert (await _get({"ratingKey": "147", "streamId": 2799})).status_code == 501


def test_path_map(monkeypatch):
    monkeypatch.setenv("SUBTITLE_PATH_MAP", "/data/tv=/tv; /films=/movies")
    assert cast_subtitles.local_path("/data/tv/Show/S01E01.mkv") == "/tv/Show/S01E01.mkv"
    assert cast_subtitles.local_path("/films/A.mkv") == "/movies/A.mkv"
    assert cast_subtitles.local_path("/npo/x.mp4") == "/npo/x.mp4"


@respx.mock
async def test_media_status_reports_unmounted_library_folders(env, tmp_path):
    (tmp_path / "tv").mkdir()
    respx.get(f"{PLEX}/library/sections").mock(return_value=httpx.Response(200, json={"MediaContainer": {"Directory": [
        {"title": "TV Shows", "type": "show", "Location": [{"path": str(tmp_path / "tv")}]},
        {"title": "Films", "type": "movie", "Location": [{"path": "/movies"}]},
        {"title": "Music", "type": "artist", "Location": [{"path": "/music"}]},
    ]}}))
    s = await cast_subtitles.media_status()
    assert s["configured"] is True and s["healthy"] is False and s["mounted"] == 1
    assert [(l["title"], l["mounted"]) for l in s["libraries"]] == [("TV Shows", True), ("Films", False)]
