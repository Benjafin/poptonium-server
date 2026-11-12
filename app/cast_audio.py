"""Audio-only stream for split playback (`/cast/audio`): video on the TV, sound on the phone.

While the Chromecast shows the picture muted, the app plays the item's audio on the phone
(headphones), kept in sync with the TV by the receiver's clock. Plex can't serve an audio
track on its own, and the original file would make the phone download the whole video
too, which doesn't fit a remote connection. This service reads the file from the same
read-only media mounts as `/cast/subtitles` and streams one audio track: the original track
untouched, or stereo Opus (headphones are stereo) at a bitrate that fits the connection.

Access works as for subtitles: the caller's own Plex token looks the item up on Plex, and
the file path and stream index come from Plex, never from the client.

Quality `auto` (the default) follows Plex's own rules for this server: a local client
(Plex's LAN networks, or the server's own public IP when "treat WAN IP as local" is on)
gets the original track; a remote one gets the original if it fits Plex's per-stream
remote limit, else the best Opus bitrate that does.

Timestamps are the file's own (absolute content time): an Opus stream starts at
`offset`, a copied track at the packet just before it. It isn't seekable: the client
seeks within what it has buffered and asks for a new offset otherwise. ffmpeg runs only as fast as the client
reads, so a paused phone costs nothing but an idle process.
"""

import asyncio
import ipaddress
import os
import time
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .cast_subtitles import ffmpeg_path, find_stream, item_for_user, local_path, server_machine_id
from .client_auth import require_plex_user_token
from .config import log, settings
from .http_client import http_client
from .plex import plex_get

router = APIRouter()

MAX_STREAMS = 6
_slots = asyncio.Semaphore(MAX_STREAMS)
# Opus bitrates (kbit/s), best first.
OPUS_KBPS = {"high": 256, "normal": 128, "low": 64}
# Codecs Matroska carries as they are, for `original`.
COPYABLE = {"aac", "ac3", "eac3", "truehd", "dts", "dca", "flac", "opus", "vorbis", "mp3", "mp2", "alac"}
# Leave room under the remote limit for bitrate peaks (lossless audio is variable).
LIMIT_HEADROOM = 0.75

_net: dict = {"expiry": 0.0}


async def plex_network() -> dict:
    """Plex's view of its network: LAN ranges, the remote per-stream limit (kbit/s, 0 =
    none), whether its public IP counts as local, and that public IP. Cached 10 min."""
    if _net["expiry"] > time.time():
        return _net
    prefs = {}
    data = await plex_get("/:/prefs") or {}
    for s in data.get("MediaContainer", {}).get("Setting", []):
        prefs[s.get("id")] = s.get("value")
    lans = []
    for part in str(prefs.get("LanNetworksBandwidth") or "").split(","):
        part = part.strip()
        if part:
            try:
                lans.append(ipaddress.ip_network(part.replace(" ", ""), strict=False))
            except ValueError:
                pass
    public = None
    if prefs.get("TreatWanIpAsLocal") in (True, "1", "true", 1):
        public = await server_public_ip()
    try:
        limit = int(prefs.get("WanPerStreamMaxUploadRate") or 0)
    except (TypeError, ValueError):
        limit = 0
    _net.update(expiry=time.time() + 600, lans=lans, limit=limit, public=public)
    return _net


async def server_public_ip() -> Optional[str]:
    """This server's public address, as plex.tv knows it."""
    machine = await server_machine_id()
    try:
        r = await http_client().get(
            "https://plex.tv/api/v2/resources",
            headers={"X-Plex-Token": settings.PLEX_TOKEN, "Accept": "application/json",
                     "X-Plex-Client-Identifier": "poptonium-server"},
            timeout=8,
        )
        for d in r.json():
            if d.get("clientIdentifier") == machine:
                return d.get("publicAddress")
    except Exception as e:
        log.warning("plex.tv resources lookup failed: %s", e)
    return None


def client_ip(request: Request) -> Optional[str]:
    """The caller's address. Behind a reverse proxy on a private network, the proxy's
    X-Real-IP / X-Forwarded-For; anything else is taken as the peer itself."""
    peer = request.client.host if request.client else None
    try:
        proxied = peer is not None and ipaddress.ip_address(peer).is_private
    except ValueError:
        proxied = False
    if proxied:
        fwd = request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
    return peer


def is_local(ip: Optional[str], net: dict) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or (net.get("public") and ip == net["public"]):
        return True
    lans = net.get("lans") or []
    if lans:
        return any(addr in n for n in lans if n.version == addr.version)
    return addr.is_private


def pick_quality(stream: dict, local: bool, limit_kbps: int) -> str:
    """`auto`: the original track when it fits, else the best Opus that does."""
    codec = (stream.get("codec") or "").lower()
    budget = limit_kbps * LIMIT_HEADROOM if limit_kbps > 0 and not local else None
    source_kbps = stream.get("bitrate")
    if codec in COPYABLE and (budget is None or (source_kbps and source_kbps <= budget)):
        return "original"
    for name, kbps in OPUS_KBPS.items():
        if budget is None or kbps <= budget:
            return name
    return "low"


def default_audio_stream(item: dict) -> dict:
    """The audio stream Plex has selected for the caller, else the first one."""
    first = None
    for media in item.get("Media") or []:
        for part in media.get("Part") or []:
            for s in part.get("Stream") or []:
                if s.get("streamType") != 2:
                    continue
                if s.get("selected"):
                    return s
                first = first or s
    if not first:
        raise HTTPException(404, "This item has no audio")
    return first


def ffmpeg_args(binary: str, file: str, index: int, offset: float, quality: str) -> list[str]:
    args = [binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if offset > 0:
        args += ["-ss", f"{offset:.3f}"]
    # -copyts: the file's own timestamps, so positions are content time in every mode.
    args += ["-copyts", "-i", file, "-map", f"0:{index}", "-vn", "-sn", "-dn"]
    if quality == "original":
        args += ["-c:a", "copy"]
    else:
        # Stereo downmix (ffmpeg's matrix keeps the centre channel, so dialogue stays clear).
        args += ["-ac", "2", "-c:a", "libopus", "-b:a", f"{OPUS_KBPS[quality]}k", "-application", "audio"]
    # Send each packet as soon as it's muxed, so playback starts without waiting for a buffer.
    args += ["-flush_packets", "1", "-f", "matroska", "-live", "1"]
    return args + ["pipe:1"]


async def run_stream(args: list[str]) -> AsyncIterator[bytes]:
    async with _slots:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            while True:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    break
                yield chunk
            rc = await proc.wait()
            if rc != 0:
                err = (await proc.stderr.read()).decode(errors="replace")[-300:]
                log.warning("audio stream failed (%s): %s", rc, err)
        finally:
            # Client gone (seek, stop, phone asleep): don't leave ffmpeg behind.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


@router.get("/cast/audio")
async def cast_audio(
    request: Request,
    ratingKey: str = Query(..., min_length=1, max_length=20),
    streamId: Optional[int] = Query(None),
    offset: float = Query(0, ge=0, le=86400),
    quality: str = Query("auto", pattern="^(auto|original|high|normal|low)$"),
    machineIdentifier: Optional[str] = Query(None, max_length=64),
    token: str = Depends(require_plex_user_token),
):
    binary = ffmpeg_path()
    if not binary:
        raise HTTPException(501, "ffmpeg is not installed")
    item = await item_for_user(ratingKey, token, machineIdentifier)
    if streamId is not None:
        part, stream = find_stream(item, streamId)
        if stream.get("streamType") != 2:
            raise HTTPException(415, "Not an audio stream")
    else:
        stream = default_audio_stream(item)
        part, _ = find_stream(item, stream["id"])
    if stream.get("key") or stream.get("index") is None:
        raise HTTPException(415, "Not an embedded stream")
    file = local_path(part.get("file") or "")
    if not os.path.isfile(file):
        raise HTTPException(404, "Media file is not mounted in this container")
    chosen = quality
    if quality == "auto":
        net = await plex_network()
        chosen = pick_quality(stream, is_local(client_ip(request), net), net.get("limit") or 0)
    elif quality == "original" and (stream.get("codec") or "").lower() not in COPYABLE:
        chosen = "high"
    args = ffmpeg_args(binary, file, int(stream["index"]), offset, chosen)
    return StreamingResponse(
        run_stream(args),
        media_type="audio/x-matroska",
        headers={
            "Cache-Control": "no-store",
            "X-Audio-Offset": f"{offset:.3f}",
            "X-Audio-Stream": str(stream.get("id")),
            "X-Audio-Quality": chosen,
        },
    )
