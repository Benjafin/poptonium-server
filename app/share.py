"""Public share links for library items (movies, shows, episodes).

The app asks for a link with ``POST /share {"rating_key": ...}``; we give the item a
readable slug (``the-matrix-1999``, ``breaking-bad``, ``breaking-bad-s01e03``) and
return ``<public base>/s/<slug>``. That page is public: it carries Open Graph and
Twitter Card tags so the link unfurls with artwork, title and synopsis in chat apps
and social media, and offers to open the item in Poptonium.

Only items someone explicitly shared resolve, and only their title, synopsis and
artwork are exposed. Artwork is served through ``/s/<slug>/<kind>.jpg`` so crawlers
never see a Plex token. Creating a link requires a Plex token that can read the item,
so a shared user can't publish titles from libraries they don't have.
"""

import html
import re
import time
import unicodedata
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from .client_auth import require_plex_user_token
from .config import log, settings
from .db import get_db
from .http_client import http_client
from .plex import plex_configured, plex_get

router = APIRouter()

APP_STORE_URL = "https://apps.apple.com/app/poptonium-for-plex/id6779742766"
APP_STORE_ID = "6779742766"
PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=games.benja.poptonium"
SITE_URL = "https://poptonium.app"

# App identity for iOS Universal Links / Android App Links: a share link tapped on a
# phone with Poptonium installed opens the app instead of this page. The apps list
# the domains they claim; these files are the domain's side of that handshake.
IOS_APP_ID = "RGYYV9YYGV.games.benja.poptonium"
ANDROID_PACKAGE = "games.benja.poptonium"
# Play app-signing key (what Play-installed builds carry), then the upload and debug
# keys. The ANDROID_CERT_SHA256 setting can add more, e.g. after a key rotation.
ANDROID_CERT_SHA256 = [
    "0A:1A:85:61:DC:A6:BE:8E:14:36:D6:25:25:6C:42:8D:48:E4:6E:17:77:2F:28:5E:31:3C:7C:ED:47:99:B0:DC",
    "AD:1A:D0:8B:88:DD:0F:3C:93:ED:C1:79:7A:FC:9E:9F:2E:0D:FA:42:40:47:66:D7:10:29:26:F5:F4:0C:3E:02",
    "11:23:FC:F9:0A:B7:58:6B:68:BD:CD:18:37:EB:19:11:22:01:13:7B:61:BA:74:53:A7:70:3B:62:9E:52:5A:0E",
]

# Plex metadata for a shared page is reused briefly: a link posted to a group chat
# gets hit by several unfurlers at once.
_META_TTL = 300
# Artwork sizes. 1200x630 is the Open Graph / Twitter large-card ratio.
_IMAGE_SIZES = {"image": (1200, 630), "poster": (600, 900)}


class ShareRequest(BaseModel):
    rating_key: str


# ---------- slugs ----------

def slugify(text: str) -> str:
    """Lowercase ASCII words joined by hyphens ("Amélie: Part 2" -> "amelie-part-2")."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:80].strip("-")


def base_slug(m: dict) -> str:
    """The readable slug for an item, before collision handling."""
    kind = m.get("type")
    if kind == "episode":
        show = slugify(m.get("grandparentTitle", ""))
        season, episode = m.get("parentIndex"), m.get("index")
        code = f"s{int(season):02d}e{int(episode):02d}" if season is not None and episode is not None else ""
        slug = "-".join(p for p in (show, code) if p)
    else:
        title = slugify(m.get("title", ""))
        year = m.get("year") if kind == "movie" else None
        slug = "-".join(str(p) for p in (title, year) if p)
    # Titles with no Latin letters (e.g. Japanese) slug to nothing: fall back to the key.
    return slug or f"{kind or 'item'}-{m.get('ratingKey', '')}"


# ---------- Plex ----------

async def _item_metadata(rating_key: str) -> Optional[dict]:
    data = await plex_get(f"/library/metadata/{rating_key}", cache_ttl=_META_TTL)
    items = ((data or {}).get("MediaContainer") or {}).get("Metadata") or []
    return items[0] if items else None


async def _caller_can_read(rating_key: str, token: str) -> bool:
    """True when the caller's own token can see the item (owner or a shared user with
    access to that library)."""
    try:
        resp = await http_client().get(
            f"{settings.PLEX_URL}/library/metadata/{rating_key}",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=10,
        )
    except Exception as e:
        log.warning("Share access check for %s failed: %s", rating_key, e)
        return False
    return resp.status_code == 200


async def plex_machine_id() -> Optional[str]:
    """This Plex server's machine identifier, which Plex's own links address it by."""
    data = await plex_get("/identity", cache_ttl=3600)
    return ((data or {}).get("MediaContainer") or {}).get("machineIdentifier")


_PLEX_METADATA_TYPES = {"movie": 1, "show": 2, "episode": 4}


def plex_links(rating_key: str, kind: str, machine_id: str) -> dict:
    """The item in Plex per platform: the app's plex://preplay link on iOS, the same
    wrapped in a Chrome intent for the Plex package on Android, Plex Web elsewhere."""
    key = quote(f"/library/metadata/{rating_key}", safe="")
    query = f"metadataKey={key}&metadataType={_PLEX_METADATA_TYPES.get(kind, 1)}&server={machine_id}"
    return {
        "ios": f"plex://preplay/?{query}",
        "android": f"intent://preplay/?{query}#Intent;scheme=plex;package=com.plexapp.android;end",
        "desktop": f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key={key}",
    }


def public_base(request: Request) -> str:
    """Internet-facing base of this service: the configured PUBLIC_URL, else what the
    request came in on (honouring a reverse proxy's forwarded host/proto and prefix)."""
    if settings.PUBLIC_URL:
        return settings.PUBLIC_URL.rstrip("/")
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    prefix = "/poptonium" if request.url.path.startswith("/poptonium/") else ""
    return f"{proto}://{host}{prefix}"


# ---------- create ----------

@router.post("/share")
async def create_share(body: ShareRequest, request: Request, token: str = Depends(require_plex_user_token)):
    rating_key = body.rating_key.strip()
    if not rating_key.isdigit():
        raise HTTPException(400, "rating_key must be a Plex ratingKey")
    if not await _caller_can_read(rating_key, token):
        raise HTTPException(404, "Item not found")
    m = await _item_metadata(rating_key)
    if not m or m.get("type") not in ("movie", "show", "episode"):
        raise HTTPException(404, "Only movies, shows and episodes can be shared")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT slug FROM shares WHERE rating_key = ?", (rating_key,))
        row = await cursor.fetchone()
        if row:
            slug = row["slug"]
        else:
            base = base_slug(m)
            slug, n = base, 2
            while True:
                cursor = await db.execute("SELECT 1 FROM shares WHERE slug = ?", (slug,))
                if not await cursor.fetchone():
                    break
                slug, n = f"{base}-{n}", n + 1
            await db.execute(
                "INSERT INTO shares (slug, rating_key, created_at) VALUES (?, ?, ?)",
                (slug, rating_key, time.time()),
            )
            await db.commit()
    finally:
        await db.close()

    title = _display_title(m)
    return {"slug": slug, "url": f"{public_base(request)}/s/{slug}", "title": title}


# ---------- app links ----------

def _share_prefixes() -> list[str]:
    """Paths share pages live under: direct, behind the usual /poptonium proxy prefix,
    and under PUBLIC_URL's path if that's something else."""
    prefixes = ["/s", "/poptonium/s"]
    if settings.PUBLIC_URL:
        custom = urlparse(settings.PUBLIC_URL).path.rstrip("/") + "/s"
        if custom not in prefixes:
            prefixes.append(custom)
    return prefixes


@router.get("/.well-known/apple-app-site-association")
async def apple_app_site_association():
    components = []
    for prefix in _share_prefixes():
        # Artwork stays in the browser; the page itself opens the app.
        components.append({"/": f"{prefix}/*.jpg", "exclude": True})
        components.append({"/": f"{prefix}/*"})
    return JSONResponse({"applinks": {"details": [{"appIDs": [IOS_APP_ID], "components": components}]}})


@router.get("/.well-known/assetlinks.json")
async def android_asset_links():
    extra = [f.strip().upper() for f in (settings.ANDROID_CERT_SHA256 or "").split(",") if f.strip()]
    fingerprints = list(dict.fromkeys(ANDROID_CERT_SHA256 + extra))
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": ANDROID_PACKAGE,
                   "sha256_cert_fingerprints": fingerprints},
    }])


@router.get("/share/{slug}")
async def share_lookup(slug: str):
    """What a share link points at, for an app that opened it as a Universal/App Link."""
    rating_key, m = await _shared_item(slug)
    return {"slug": slug, "rating_key": rating_key, "type": m.get("type"), "title": _display_title(m)}


# ---------- public page ----------

async def _shared_item(slug: str) -> tuple[str, dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT rating_key FROM shares WHERE slug = ?", (slug,))
        row = await cursor.fetchone()
    finally:
        await db.close()
    if not row:
        raise HTTPException(404, "Not found")
    m = await _item_metadata(row["rating_key"])
    if not m:
        raise HTTPException(404, "Not found")
    return row["rating_key"], m


def _display_title(m: dict) -> str:
    if m.get("type") == "episode":
        code = ""
        if m.get("parentIndex") is not None and m.get("index") is not None:
            code = f"S{m['parentIndex']} E{m['index']}"
        return " · ".join(p for p in (m.get("grandparentTitle"), code, m.get("title")) if p)
    year = m.get("year")
    return f"{m.get('title', '')} ({year})" if year and m.get("type") == "movie" else m.get("title", "")


def _runtime(ms) -> Optional[str]:
    if not ms:
        return None
    minutes = int(ms) // 60000
    h, mins = divmod(minutes, 60)
    return f"{h}h {mins}m" if h else f"{mins}m"


def _meta_line(m: dict) -> str:
    kind = m.get("type")
    parts: list[str] = []
    if kind == "movie":
        parts.append("Movie")
    elif kind == "show":
        parts.append("TV Series")
        seasons = m.get("childCount")
        if seasons:
            parts.append(f"{seasons} season{'s' if seasons != 1 else ''}")
    elif kind == "episode":
        parts.append("Episode")
    if m.get("year"):
        parts.append(str(m["year"]))
    genres = [g.get("tag") for g in (m.get("Genre") or [])[:2] if g.get("tag")]
    parts.extend(genres)
    if kind != "show" and _runtime(m.get("duration")):
        parts.append(_runtime(m.get("duration")))
    return " · ".join(parts)


def _artwork_path(m: dict, kind: str) -> Optional[str]:
    """Plex-relative path for the wide card image or the poster."""
    if kind == "poster":
        if m.get("type") == "episode":
            return m.get("grandparentThumb") or m.get("parentThumb") or m.get("thumb")
        return m.get("thumb")
    if m.get("type") == "episode":
        return m.get("thumb") or m.get("grandparentArt") or m.get("art")
    return m.get("art") or m.get("thumb")


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


@router.get("/s/{slug}/{kind}.jpg")
async def share_image(slug: str, kind: str):
    if kind not in _IMAGE_SIZES or not plex_configured():
        raise HTTPException(404, "Not found")
    _, m = await _shared_item(slug)
    path = _artwork_path(m, kind)
    if not path:
        raise HTTPException(404, "No artwork")
    width, height = _IMAGE_SIZES[kind]
    query = urlencode({"width": width, "height": height, "minSize": 1, "upscale": 1, "url": path})
    try:
        resp = await http_client().get(
            f"{settings.PLEX_URL}/photo/:/transcode?{query}",
            headers={"X-Plex-Token": settings.PLEX_TOKEN},
            timeout=20,
        )
    except Exception as e:
        log.warning("Share artwork %s/%s failed: %s", slug, kind, e)
        raise HTTPException(502, "Artwork unavailable")
    if resp.status_code != 200:
        raise HTTPException(502, "Artwork unavailable")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/s/{slug}", response_class=HTMLResponse)
async def share_page(slug: str, request: Request):
    try:
        rating_key, m = await _shared_item(slug)
    except HTTPException:
        return HTMLResponse(_not_found_page(), status_code=404)
    return HTMLResponse(
        render_share_page(m, rating_key, f"{public_base(request)}/s/{slug}",
                          machine_id=await plex_machine_id()),
        headers={"Cache-Control": "public, max-age=300"},
    )


_OG_TYPES = {"movie": "video.movie", "show": "video.tv_show", "episode": "video.episode"}


def render_share_page(m: dict, rating_key: str, page_url: str, machine_id: Optional[str] = None) -> str:
    e = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731
    title = _display_title(m)
    meta_line = _meta_line(m)
    summary = _truncate(m.get("summary", ""), 600)
    description = _truncate(" · ".join(p for p in (meta_line, m.get("summary")) if p), 200)
    image_url = f"{page_url}/image.jpg"
    poster_url = f"{page_url}/poster.jpg"
    app_url = f"poptonium://item/{rating_key}"
    # Chrome opens the app, or its Play listing when it isn't installed.
    app_intent = f"intent://item/{rating_key}#Intent;scheme=poptonium;package={ANDROID_PACKAGE};end"
    plex_button = ""
    if machine_id:
        plex = plex_links(rating_key, m.get("type"), machine_id)
        plex_button = (f'<a class="btn" data-ios="{e(plex["ios"])}" data-android="{e(plex["android"])}" '
                       f'href="{e(plex["desktop"])}">Open in Plex</a>')
    heading = m.get("grandparentTitle") if m.get("type") == "episode" else m.get("title")
    episode_line = ""
    if m.get("type") == "episode":
        code = f"Season {m.get('parentIndex')}, Episode {m.get('index')}" if m.get("index") is not None else ""
        episode_line = f'<p class="episode">{e(code)}{" · " if code else ""}{e(m.get("title"))}</p>'
    tagline = f'<p class="tagline">{e(m.get("tagline"))}</p>' if m.get("tagline") else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{e(title)} · Poptonium</title>
<meta name="description" content="{e(description)}">
<meta name="theme-color" content="#0E1117">
<meta name="apple-itunes-app" content="app-id={APP_STORE_ID}, app-argument={e(app_url)}">
<link rel="canonical" href="{e(page_url)}">
<meta property="og:site_name" content="Poptonium">
<meta property="og:type" content="{_OG_TYPES.get(m.get('type'), 'website')}">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(description)}">
<meta property="og:url" content="{e(page_url)}">
<meta property="og:image" content="{e(image_url)}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{e(title)}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(description)}">
<meta name="twitter:image" content="{e(image_url)}">
<style>
  :root {{ --bg:#0E1117; --bg3:#212630; --text:#F4F6FA; --dim:rgba(236,240,250,.62); --faint:rgba(236,240,250,.34);
          --accent:#46BEF5; --accent-deep:#2A8FE0; --hairline:rgba(255,255,255,.08); }}
  * {{ box-sizing:border-box; }}
  html,body {{ margin:0; background:var(--bg); color:var(--text);
    font:16px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; }}
  .hero {{ position:relative; min-height:100vh; display:flex; align-items:flex-end; }}
  .backdrop {{ position:absolute; inset:0; background:url("{e(image_url)}") center/cover no-repeat; }}
  .backdrop::after {{ content:""; position:absolute; inset:0;
    background:linear-gradient(180deg,rgba(14,17,23,.35) 0%,rgba(14,17,23,.75) 45%,var(--bg) 85%); }}
  .content {{ position:relative; width:100%; max-width:880px; margin:0 auto; padding:48px 24px 56px;
    display:flex; gap:28px; align-items:flex-end; }}
  .poster {{ width:180px; flex:none; aspect-ratio:2/3; border-radius:14px; object-fit:cover;
    box-shadow:0 18px 50px rgba(0,0,0,.6); border:1px solid var(--hairline); background:var(--bg3); }}
  h1 {{ margin:0 0 6px; font-size:34px; line-height:1.1; font-weight:800; letter-spacing:-.4px; }}
  .episode {{ margin:0 0 6px; font-size:18px; font-weight:600; }}
  .meta {{ margin:0 0 14px; color:var(--dim); font-size:14px; font-weight:600; }}
  .tagline {{ margin:0 0 10px; color:var(--dim); font-style:italic; }}
  .summary {{ margin:0 0 24px; color:var(--dim); font-size:15px; max-width:60ch; }}
  .actions {{ display:flex; flex-wrap:wrap; gap:10px; }}
  .btn {{ display:inline-flex; align-items:center; justify-content:center; padding:12px 20px; border-radius:14px;
    font-weight:700; font-size:15px; text-decoration:none; color:var(--text);
    background:rgba(255,255,255,.08); border:1px solid var(--hairline); }}
  .btn.primary {{ color:#fff; border:none; background:linear-gradient(135deg,var(--accent),var(--accent-deep)); }}
  footer {{ position:relative; text-align:center; padding:0 24px 32px; color:var(--faint); font-size:13px; }}
  footer a {{ color:var(--dim); }}
  @media (max-width:640px) {{
    .content {{ flex-direction:column; align-items:flex-start; padding-top:120px; }}
    .poster {{ width:132px; }}
    h1 {{ font-size:28px; }}
  }}
</style>
</head>
<body>
<section class="hero">
  <div class="backdrop"></div>
  <div class="content">
    <img class="poster" src="{e(poster_url)}" alt="">
    <div>
      <h1>{e(heading)}</h1>
      {episode_line}
      <p class="meta">{e(meta_line)}</p>
      {tagline}
      <p class="summary">{e(summary)}</p>
      <div class="actions">
        <a class="btn primary" data-platforms="ios android" data-android="{e(app_intent)}" href="{e(app_url)}">Open in Poptonium</a>
        {plex_button}
        <a class="btn" data-platforms="ios desktop" href="{APP_STORE_URL}">App Store</a>
        <a class="btn" data-platforms="android desktop" href="{PLAY_STORE_URL}">Google Play</a>
      </div>
    </div>
  </div>
</section>
<footer>Shared from a Plex library with <a href="{SITE_URL}">Poptonium</a></footer>
<script>
// Each button carries a link per platform; keep the ones for this device's platform
// and point them at its variant (apps on phones, Plex Web and both stores on desktop).
(function () {{
  var ua = navigator.userAgent;
  var platform = /Android/i.test(ua) ? "android"
    : (/iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) ? "ios"
    : "desktop";
  Array.prototype.forEach.call(document.querySelectorAll(".actions a"), function (a) {{
    var only = a.getAttribute("data-platforms");
    if (only && only.split(" ").indexOf(platform) < 0) {{ a.remove(); return; }}
    var href = a.getAttribute("data-" + platform);
    if (href) a.href = href;
  }});
}})();
</script>
</body>
</html>
"""


def _not_found_page() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found · Poptonium</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0E1117;color:#F4F6FA;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;text-align:center}}
a{{color:#46BEF5}}</style></head>
<body><div><h1>This link has expired</h1>
<p>The title isn't available anymore.</p><p><a href="{SITE_URL}">poptonium.app</a></p></div></body></html>
"""
