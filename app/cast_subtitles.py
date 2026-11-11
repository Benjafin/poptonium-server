"""Embedded text subtitles for the Poptonium Cast receiver (`/cast/subtitles`).

A Chromecast can't read subtitle tracks embedded in a file it direct plays, and Plex only
extracts them inside a full video conversion session, where its remote bandwidth limit
applies to video nobody watches (and can force a pointless re-encode). This service runs
next to Plex with the same media mounted read-only, so it reads the track straight from
the file with ffmpeg: no video work, no Plex session, first cue in well under a second.

Access: the caller presents their own Plex token. The item is looked up on Plex *with that
token*, so a user only gets subtitles for items Plex lets them see, and the file path and
stream index come from Plex, never from the client. The video itself is untouched: Plex
still decides and serves it, so its bandwidth limits stay in charge.

Output is WebVTT (any text source) or ASS (ASS/SSA sources, keeping their styling), with
absolute timestamps from `offset` on, matching the receiver's timeline in every playback
mode. A complete extraction from the start is cached on /data, so rewatches and seeks are
instant.
"""

import asyncio
import hashlib
import os
import shutil
import time
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from .client_auth import require_plex_user_token
from .config import DB_PATH, log, settings
from .http_client import http_client
from .plex import plex_configured, plex_get

router = APIRouter()

TEXT_CODECS = {"srt", "subrip", "ass", "ssa", "mov_text", "webvtt", "vtt", "text", "tx3g"}
ASS_CODECS = {"ass", "ssa"}
MAX_PROCESSES = 4
_slots = asyncio.Semaphore(MAX_PROCESSES)
_identity: dict = {"expiry": 0.0, "machine": None}

CACHE_DIR = os.environ.get("SUBTITLE_CACHE_DIR") or os.path.join(os.path.dirname(DB_PATH) or "/data", "subtitle-cache")
CACHE_MAX_BYTES = 200 * 1024 * 1024


def ffmpeg_path() -> Optional[str]:
    return os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")


def path_map() -> list[tuple[str, str]]:
    """`SUBTITLE_PATH_MAP="/plex/prefix=/local/prefix;..."`; identity by default (the
    container mounts media at the same paths as Plex)."""
    out = []
    for pair in (os.environ.get("SUBTITLE_PATH_MAP") or "").split(";"):
        src, sep, dst = pair.partition("=")
        if sep and src.strip():
            out.append((src.strip(), dst.strip()))
    return out


def local_path(plex_file: str) -> str:
    for src, dst in path_map():
        if plex_file.startswith(src):
            return dst + plex_file[len(src):]
    return plex_file


def find_stream(metadata: dict, stream_id: int) -> tuple[dict, dict]:
    """(part, stream) for `stream_id` in any part of any media of the item."""
    for media in metadata.get("Media") or []:
        for part in media.get("Part") or []:
            for s in part.get("Stream") or []:
                if str(s.get("id")) == str(stream_id):
                    return part, s
    raise HTTPException(404, "No such stream on this item")


def ffmpeg_args(binary: str, file: str, index: int, fmt: str, offset: float) -> list[str]:
    args = [binary, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if offset > 0:
        # Input seek is fast (index-based); -copyts keeps timestamps absolute.
        args += ["-ss", f"{offset:.3f}"]
    args += ["-copyts", "-i", file, "-map", f"0:{index}", "-vn", "-an", "-dn"]
    args += ["-c:s", "copy", "-f", "ass"] if fmt == "ass" else ["-c:s", "webvtt", "-f", "webvtt"]
    return args + ["pipe:1"]


def cache_key(file: str, st: os.stat_result, index: int, fmt: str) -> str:
    raw = f"{file}|{st.st_size}|{int(st.st_mtime)}|{index}|{fmt}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32] + (".ass" if fmt == "ass" else ".vtt")


def prune_cache() -> None:
    try:
        files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)]
        files = [(os.stat(f), f) for f in files if os.path.isfile(f)]
    except FileNotFoundError:
        return
    total = sum(s.st_size for s, _ in files)
    for s, f in sorted(files, key=lambda x: x[0].st_atime):
        if total <= CACHE_MAX_BYTES:
            break
        try:
            os.remove(f)
            total -= s.st_size
        except OSError:
            pass


async def server_machine_id() -> Optional[str]:
    if _identity["expiry"] > time.time():
        return _identity["machine"]
    try:
        r = await http_client().get(f"{settings.PLEX_URL}/identity", headers={"Accept": "application/json"}, timeout=8)
        _identity.update(machine=r.json().get("MediaContainer", {}).get("machineIdentifier"), expiry=time.time() + 600)
    except Exception as e:
        log.warning("Plex identity lookup failed: %s", e)
        return None
    return _identity["machine"]


async def item_for_user(rating_key: str, token: str, machine_identifier: Optional[str]) -> dict:
    """The item's metadata as Plex shows it to the caller's own token, so nobody reads a
    file Plex wouldn't serve them. Raises the HTTP errors the receiver/app fall back on."""
    if not rating_key.isdigit():
        raise HTTPException(400, "Bad ratingKey")
    if machine_identifier and machine_identifier != await server_machine_id():
        # The client plays from another server than the one this backend serves.
        raise HTTPException(409, "Different Plex server")
    try:
        r = await http_client().get(
            f"{settings.PLEX_URL}/library/metadata/{rating_key}",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=10,
        )
    except Exception as e:
        raise HTTPException(502, f"Plex unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(404 if r.status_code in (401, 403, 404) else 502, "Item not available")
    items = r.json().get("MediaContainer", {}).get("Metadata") or []
    if not items:
        raise HTTPException(404, "Item not available")
    return items[0]


async def run_extraction(args: list[str], cache_file: Optional[str]) -> AsyncIterator[bytes]:
    """Streams ffmpeg's output; writes a complete run to `cache_file`."""
    async with _slots:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        tmp = f"{cache_file}.{os.getpid()}.part" if cache_file else None
        out = open(tmp, "wb") if tmp else None
        complete = False
        try:
            while True:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    break
                if out:
                    out.write(chunk)
                yield chunk
            rc = await proc.wait()
            complete = rc == 0
            if rc != 0:
                err = (await proc.stderr.read()).decode(errors="replace")[-300:]
                log.warning("subtitle extraction failed (%s): %s", rc, err)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            if out:
                out.close()
                if complete:
                    os.replace(tmp, cache_file)
                    prune_cache()
                else:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass


async def media_status() -> dict:
    """For the admin dashboard: ffmpeg present, and which of Plex's library folders this
    container can see (they must be mounted at the same path, or mapped)."""
    libraries = []
    if plex_configured():
        data = await plex_get("/library/sections") or {}
        for d in data.get("MediaContainer", {}).get("Directory", []):
            if d.get("type") not in ("movie", "show"):
                continue
            for loc in d.get("Location") or []:
                path = loc.get("path") or ""
                libraries.append({"title": d.get("title"), "path": path, "mounted": bool(path) and os.path.isdir(local_path(path))})
    return {
        "configured": bool(ffmpeg_path()),
        "healthy": bool(libraries) and all(l["mounted"] for l in libraries),
        "mounted": sum(l["mounted"] for l in libraries),
        "libraries": libraries,
    }


@router.get("/cast/subtitles")
async def cast_subtitles(
    request: Request,
    ratingKey: str = Query(..., min_length=1, max_length=20),
    streamId: int = Query(...),
    offset: float = Query(0, ge=0, le=86400),
    format: str = Query("webvtt", pattern="^(webvtt|ass)$"),
    machineIdentifier: Optional[str] = Query(None, max_length=64),
    token: str = Depends(require_plex_user_token),
):
    binary = ffmpeg_path()
    if not binary:
        raise HTTPException(501, "ffmpeg is not installed")
    item = await item_for_user(ratingKey, token, machineIdentifier)
    part, stream = find_stream(item, streamId)
    codec = (stream.get("codec") or "").lower()
    if stream.get("streamType") != 3 or codec not in TEXT_CODECS:
        raise HTTPException(415, "Not a text subtitle stream")
    if stream.get("key") or stream.get("index") is None:
        raise HTTPException(415, "Not an embedded stream")  # sidecar files come from Plex directly
    if format == "ass" and codec not in ASS_CODECS:
        raise HTTPException(415, "ASS output needs an ASS/SSA source")
    file = local_path(part.get("file") or "")
    try:
        st = os.stat(file)
    except OSError:
        raise HTTPException(404, "Media file is not mounted in this container")

    media_type = "text/x-ssa; charset=utf-8" if format == "ass" else "text/vtt; charset=utf-8"
    os.makedirs(CACHE_DIR, exist_ok=True)
    cached = os.path.join(CACHE_DIR, cache_key(file, st, int(stream["index"]), format))
    if os.path.exists(cached):
        os.utime(cached)  # LRU by atime; some mounts are noatime
        return FileResponse(cached, media_type=media_type, headers={"X-Subtitle-Source": "cache"})
    args = ffmpeg_args(binary, file, int(stream["index"]), format, offset)
    return StreamingResponse(
        run_extraction(args, cached if offset <= 1 else None),
        media_type=media_type,
        headers={"X-Subtitle-Source": "file", "Cache-Control": "no-store"},
    )
