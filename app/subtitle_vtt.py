"""Serve Plex subtitle streams as WebVTT (`/subtitles/{stream_id}.vtt`).

Chromecast-bound clients side-load subtitles as a native WebVTT text track
instead of selecting them inside the Plex transcode session (PMS 1.43.2+ never
throttles a video-copy session that carries a subtitle transcode, so the
transcoder races to EOF and floods the transcode directory). The app clears the
server-side subtitle selection, attaches this endpoint's URL as a Cast TEXT
track, and the receiver renders it itself — no subtitle ever enters the
transcode session.

The subtitle bytes come from Plex's static ``/library/streams/{id}`` endpoint
(external sidecars, plus embedded tracks Plex has blobbed during analysis; it
501s for embedded tracks with no blob). SRT/ASS/SSA are converted to WebVTT
here; the receiver can't render ASS styling anyway, so override tags are
stripped.

Auth mirrors ``client_auth``: the caller must present a Plex token this server
accepts (query param, since the Chromecast fetches the track URL directly and
can't send custom headers). CORS is wide open — CAF receivers require CORS on
side-loaded text tracks, and the response is just subtitle text the caller's
token already grants access to.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Response

from .client_auth import require_plex_user
from .config import log, settings
from .http_client import http_client
from .plex import plex_configured

router = APIRouter()

# CAF loads side-loaded tracks straight from the receiver, so every response
# (including errors, so failures are debuggable from the receiver console) must
# carry CORS headers.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

_SRT_TIME = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")
# ASS/SSA inline override blocks, e.g. {\an8}{\i1} — meaningless to a Cast receiver.
_ASS_TAGS = re.compile(r"\{\\[^}]*\}")
_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.:](\d{2})$")


def _srt_to_vtt(text: str) -> str:
    """SRT -> WebVTT: header, comma->dot timestamps, drop cue indices, strip ASS tags.

    Basic HTML-style tags (<i>, <b>, <u>) are valid WebVTT and pass through.
    """
    out = ["WEBVTT", ""]
    for block in re.split(r"\r?\n\r?\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        # Drop the numeric cue index line if present.
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        cue_time = _SRT_TIME.sub(r"\1.\2", lines[0])
        cue_text = [_ASS_TAGS.sub("", l) for l in lines[1:]]
        cue_text = [l for l in cue_text if l.strip()]
        if not cue_text:
            continue
        out.append(cue_time)
        out.extend(cue_text)
        out.append("")
    return "\n".join(out)


def _ass_time_to_vtt(t: str) -> str | None:
    m = _ASS_TIME.match(t.strip())
    if not m:
        return None
    h, mm, ss, cs = m.groups()
    return f"{int(h):02d}:{mm}:{ss}.{cs}0"


def _ass_to_vtt(text: str) -> str:
    """ASS/SSA -> WebVTT: keep Dialogue cue times + text, strip all styling."""
    out = ["WEBVTT", ""]
    fmt: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("format:"):
            fmt = [f.strip().lower() for f in stripped.split(":", 1)[1].split(",")]
            continue
        if not stripped.lower().startswith("dialogue:"):
            continue
        fields = fmt or ["layer", "start", "end", "style", "name",
                         "marginl", "marginr", "marginv", "effect", "text"]
        # Text is the last field and may itself contain commas.
        parts = stripped.split(":", 1)[1].split(",", len(fields) - 1)
        if len(parts) < len(fields):
            continue
        row = dict(zip(fields, parts))
        start = _ass_time_to_vtt(row.get("start", ""))
        end = _ass_time_to_vtt(row.get("end", ""))
        if not start or not end:
            continue
        cue = _ASS_TAGS.sub("", row.get("text", "")).replace("\\N", "\n").replace("\\n", "\n")
        cue = "\n".join(l for l in cue.splitlines() if l.strip())
        if not cue:
            continue
        out.append(f"{start} --> {end}")
        out.append(cue)
        out.append("")
    return "\n".join(out)


def to_webvtt(raw: bytes) -> str:
    """Convert subtitle bytes (SRT / ASS / SSA / VTT) to a WebVTT string."""
    text = raw.decode("utf-8-sig", errors="replace")
    head = text.lstrip()[:200].lower()
    if head.startswith("webvtt"):
        return text.lstrip()
    if "[script info]" in head or head.startswith("dialogue:"):
        return _ass_to_vtt(text)
    return _srt_to_vtt(text)


@router.options("/subtitles/{stream_id}.vtt")
async def subtitle_vtt_preflight(stream_id: int):
    return Response(status_code=204, headers=_CORS_HEADERS)


@router.get("/subtitles/{stream_id}.vtt", dependencies=[Depends(require_plex_user)])
async def subtitle_vtt(stream_id: int):
    """The Plex subtitle stream `stream_id`, converted to WebVTT."""
    if not plex_configured():
        raise HTTPException(503, "Plex not configured", headers=_CORS_HEADERS)
    try:
        resp = await http_client().get(
            f"{settings.PLEX_URL}/library/streams/{stream_id}",
            headers={"X-Plex-Token": settings.PLEX_TOKEN},
            timeout=15,
        )
    except Exception as e:
        log.warning("Subtitle stream %s fetch failed: %s", stream_id, e)
        raise HTTPException(502, "Plex unreachable", headers=_CORS_HEADERS)
    if resp.status_code == 501:
        # Embedded track Plex hasn't blobbed: not statically extractable. The app
        # falls back to its normal in-session subtitle path for these.
        raise HTTPException(
            404, "Subtitle not extractable (embedded, no blob)", headers=_CORS_HEADERS
        )
    if resp.status_code != 200:
        log.warning("Subtitle stream %s -> %d", stream_id, resp.status_code)
        raise HTTPException(404, "Subtitle stream not found", headers=_CORS_HEADERS)

    vtt = to_webvtt(resp.content)
    return Response(
        content=vtt,
        media_type="text/vtt",
        headers={**_CORS_HEADERS, "Cache-Control": "private, max-age=3600"},
    )
