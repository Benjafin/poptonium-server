"""Audio-only stream for split playback (`/cast/audio`): video on the TV, sound on the phone.

While the Chromecast shows the picture muted, the app plays the item's audio on the phone
(headphones), kept in sync with the TV by the receiver's clock. Plex can't serve an audio
track on its own, and the original file would make the phone download the whole video
too, which doesn't fit a remote connection. This service reads the file from the same
read-only media mounts as `/cast/subtitles` and streams one audio track, downmixed to
stereo Opus (headphones are stereo, and ~128 kbit/s works over any connection).

Access works as for subtitles: the caller's own Plex token looks the item up on Plex, and
the file path and stream index come from Plex, never from the client.

The stream starts exactly at `offset` (ffmpeg decodes from the previous keyframe and
trims to the sample), with timestamps from 0, so the client's position is
`offset + its playback time`. It isn't seekable: the client seeks within what it has
buffered and asks for a new offset otherwise. ffmpeg runs only as fast as the client
reads, so a paused phone costs nothing but an idle process.
"""

import asyncio
import os
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from .cast_subtitles import ffmpeg_path, find_stream, item_for_user, local_path
from .client_auth import require_plex_user_token
from .config import log

router = APIRouter()

MAX_STREAMS = 6
_slots = asyncio.Semaphore(MAX_STREAMS)
BITRATES = {"low": "64k", "normal": "128k", "high": "192k"}


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


def ffmpeg_args(binary: str, file: str, index: int, offset: float, bitrate: str) -> list[str]:
    args = [binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if offset > 0:
        args += ["-ss", f"{offset:.3f}"]
    args += ["-i", file, "-map", f"0:{index}", "-vn", "-sn", "-dn"]
    # Stereo downmix (ffmpeg's matrix keeps the centre channel, so dialogue stays clear).
    args += ["-ac", "2", "-c:a", "libopus", "-b:a", bitrate, "-application", "audio"]
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
    ratingKey: str = Query(..., min_length=1, max_length=20),
    streamId: Optional[int] = Query(None),
    offset: float = Query(0, ge=0, le=86400),
    quality: str = Query("normal", pattern="^(low|normal|high)$"),
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
    args = ffmpeg_args(binary, file, int(stream["index"]), offset, BITRATES[quality])
    return StreamingResponse(
        run_stream(args),
        media_type="audio/x-matroska",
        headers={
            "Cache-Control": "no-store",
            "X-Audio-Offset": f"{offset:.3f}",
            "X-Audio-Stream": str(stream.get("id")),
        },
    )
