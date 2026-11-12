"""Tests for app/cast_audio.py: the audio-only stream for split playback.

Plex is mocked with respx; ffmpeg is a fake script that records its arguments and writes
a few bytes, so the tests check access, stream selection and the ffmpeg command line.
"""

import stat
import sys

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app import cast_audio, cast_subtitles
from app.config import settings

PLEX = "http://plex.test"
BODY = b"\x1a\x45\xdf\xa3opus"


def _item(file, *, selected=None):
    audio = [
        {"id": 11, "streamType": 2, "codec": "truehd", "index": 1, "channels": 8, "bitrate": 4500},
        {"id": 12, "streamType": 2, "codec": "aac", "index": 2, "channels": 2, "bitrate": 160},
    ]
    if selected:
        next(s for s in audio if s["id"] == selected)["selected"] = True
    return {"ratingKey": "147", "Media": [{"Part": [{"file": file, "Stream": [
        {"id": 1, "streamType": 1, "codec": "hevc", "index": 0}, *audio,
        {"id": 13, "streamType": 3, "codec": "srt", "index": 3}]}]}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(settings._values, "PLEX_URL", PLEX)
    monkeypatch.setitem(settings._values, "PLEX_TOKEN", "admin-token")
    cast_subtitles._identity.update(expiry=0.0, machine=None)
    cast_audio._net.clear()
    cast_audio._net["expiry"] = 0.0
    media = tmp_path / "Dune.mkv"
    media.write_bytes(b"\x1a\x45\xdf\xa3")
    argfile = tmp_path / "args.txt"
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(argfile)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.buffer.write({BODY!r})\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FFMPEG_PATH", str(fake))
    return {"media": str(media), "args": argfile}


def _mock_plex(item, readable_by=("user-token",), limit=12000, lan="192.168.1.0/255.255.255.0", wan_local=True):
    respx.get(f"{PLEX}/library/sections").mock(
        side_effect=lambda r: httpx.Response(200 if r.headers.get("X-Plex-Token") in readable_by else 401))

    def item_resp(r):
        if r.headers.get("X-Plex-Token") not in readable_by:
            return httpx.Response(401)
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [item]}})

    respx.get(url__regex=rf"{PLEX}/library/metadata/\d+$").mock(side_effect=item_resp)
    respx.get(f"{PLEX}/identity").mock(return_value=httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "mid-1"}}))
    respx.get(f"{PLEX}/:/prefs").mock(return_value=httpx.Response(200, json={"MediaContainer": {"Setting": [
        {"id": "WanPerStreamMaxUploadRate", "value": limit},
        {"id": "LanNetworksBandwidth", "value": lan},
        {"id": "TreatWanIpAsLocal", "value": wan_local},
    ]}}))
    respx.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(200, json=[
        {"clientIdentifier": "mid-1", "provides": "server", "publicAddress": "85.1.2.3"}]))


async def _get(params, token="user-token", client=("127.0.0.1", 5000), headers=None):
    app = FastAPI()
    app.include_router(cast_audio.router)
    transport = ASGITransport(app=app, client=client)
    h = {"X-Plex-Token": token} if token else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/cast/audio", params=params, headers={**h, **(headers or {})})


@respx.mock
async def test_streams_the_requested_track_from_offset(env):
    _mock_plex(_item(env["media"]))
    r = await _get({"ratingKey": "147", "streamId": 12, "offset": 1293.5, "quality": "high", "machineIdentifier": "mid-1"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/x-matroska"
    assert r.headers["x-audio-offset"] == "1293.500" and r.headers["x-audio-stream"] == "12"
    assert r.headers["x-audio-quality"] == "high"
    assert r.content == BODY
    args = env["args"].read_text()
    # Absolute timestamps in every mode: positions are content time.
    assert "-ss 1293.500 -copyts -i" in args and "-map 0:2" in args
    assert "-ac 2 -c:a libopus -b:a 256k" in args and "-f matroska" in args


@respx.mock
async def test_original_copies_the_track(env):
    _mock_plex(_item(env["media"]))
    r = await _get({"ratingKey": "147", "streamId": 11, "quality": "original"})
    assert r.headers["x-audio-quality"] == "original"
    args = env["args"].read_text()
    assert "-map 0:1" in args and "-c:a copy" in args and "libopus" not in args


@respx.mock
@pytest.mark.parametrize("selected,index", [(None, "0:1"), (12, "0:2")])
async def test_default_track_is_plex_selection(env, selected, index):
    _mock_plex(_item(env["media"], selected=selected))
    r = await _get({"ratingKey": "147", "quality": "low"})
    assert r.status_code == 200
    args = env["args"].read_text()
    assert f"-map {index}" in args and "-b:a 64k" in args and "-ss" not in args


PROXY = ("172.21.0.5", 40000)  # the reverse proxy on the Docker network


@respx.mock
@pytest.mark.parametrize("client,headers,limit,stream,expected", [
    (("127.0.0.1", 1), None, 12000, 11, "original"),                      # local
    (PROXY, {"X-Real-IP": "192.168.1.40"}, 12000, 11, "original"),         # Plex LAN via the proxy
    (PROXY, {"X-Real-IP": "85.1.2.3"}, 2000, 11, "original"),              # own public IP counts as local
    (PROXY, {"X-Real-IP": "31.2.3.4"}, 12000, 11, "original"),             # remote, 4.5 Mbit fits 12 Mbit
    (PROXY, {"X-Real-IP": "31.2.3.4"}, 4000, 11, "high"),                  # remote, doesn't fit 4 Mbit
    (PROXY, {"X-Forwarded-For": "31.2.3.4, 172.21.0.1"}, 300, 11, "normal"),
    (PROXY, {"X-Real-IP": "31.2.3.4"}, 0, 11, "original"),                 # no remote limit
    (("31.2.3.4", 1), {"X-Real-IP": "192.168.1.40"}, 4000, 11, "high"),    # untrusted header ignored
])
async def test_auto_follows_plex_remote_rules(env, client, headers, limit, stream, expected):
    _mock_plex(_item(env["media"]), limit=limit)
    r = await _get({"ratingKey": "147", "streamId": stream}, client=client, headers=headers)
    assert r.status_code == 200 and r.headers["x-audio-quality"] == expected


@respx.mock
async def test_auto_without_lan_setting_uses_private_ranges(env):
    _mock_plex(_item(env["media"]), limit=1000, lan="", wan_local=False)
    r = await _get({"ratingKey": "147", "streamId": 11}, client=PROXY, headers={"X-Real-IP": "10.0.0.7"})
    assert r.headers["x-audio-quality"] == "original"
    cast_audio._net["expiry"] = 0.0
    r = await _get({"ratingKey": "147", "streamId": 11}, client=PROXY, headers={"X-Real-IP": "85.1.2.3"})
    assert r.headers["x-audio-quality"] == "high"  # TreatWanIpAsLocal off: public IP is remote


@respx.mock
@pytest.mark.parametrize("params,status", [
    ({"streamId": 13}, 415),                 # a subtitle stream
    ({"streamId": 99}, 404),                 # not on this item
    ({"machineIdentifier": "other"}, 409),   # another server
    ({"quality": "lossless"}, 422),
])
async def test_refusals(env, params, status):
    _mock_plex(_item(env["media"]))
    assert (await _get({"ratingKey": "147", **params})).status_code == status


@respx.mock
async def test_needs_a_token_plex_accepts(env):
    _mock_plex(_item(env["media"]))
    assert (await _get({"ratingKey": "147"}, token=None)).status_code == 401
    assert (await _get({"ratingKey": "147"}, token="stranger")).status_code == 401


@respx.mock
async def test_unmounted_media_and_missing_ffmpeg(env, monkeypatch):
    _mock_plex(_item("/movies/not-here.mkv"))
    assert (await _get({"ratingKey": "147"})).status_code == 404
    monkeypatch.setenv("FFMPEG_PATH", "")
    monkeypatch.setattr(cast_subtitles.shutil, "which", lambda _: None)
    assert (await _get({"ratingKey": "147"})).status_code == 501
