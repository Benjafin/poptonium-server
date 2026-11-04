<p align="center">
  <img src="docs/logo.png" alt="Poptonium" width="420">
</p>

<h1 align="center">Poptonium Server</h1>

Companion backend for the Poptonium Plex client. A single small container that adds:

- **Ratings** from [mdblist](https://mdblist.com) (IMDb / Rotten Tomatoes / TMDB / Metacritic / MDbList) for richer library browsing and sorting.
- **A Discover "popular" feed** refreshed nightly.
- **Custom Library sections** (rows and heroes) that shape the app's Library page.
- **In-app search and requests** through [Overseerr](https://overseerr.dev).
- **Subtitle search and download** into Plex through [OpenSubtitles](https://www.opensubtitles.com).
- **A web admin UI** at `/admin` to set everything up and manage it.

**Plex is required.** mdblist, Overseerr and OpenSubtitles are optional — leave them
unconfigured to disable that feature; the rest keeps working.

## The apps

Poptonium is a Plex client for [iOS](https://apps.apple.com/nl/app/poptonium-for-plex/id6779742766?l=en-GB)
and [Android](https://play.google.com/store/apps/details?id=games.benja.poptonium). This repo is the
optional companion backend they connect to.

The clients ship with extensive casting support: **Chromecast / Google Cast**
integration with proper remote control and the right codec profiles.

## Install

The app listens on container port **8085** and stores its data under `/data`, so bind that path.

> **Keep the host port at 8085.** On the same LAN with no reverse proxy, the client reaches the
> backend directly on port 8085. Changing the host port breaks that direct-discovery path.

No configuration is needed to start — you enter every key in the setup wizard once it's running.

### Docker Compose

```bash
docker compose up -d
```

`docker-compose.yml` maps host `8085:8085` and binds `./data:/data`. Open `http://<host>:8085/admin`
and finish setup in the wizard.

### Plain Docker

```bash
docker build -t poptonium .
docker run -d --name poptonium \
  -p 8085:8085 \
  -v /mnt/user/appdata/poptonium:/data \
  -v /mnt/user/media/Movies:/movies:ro \
  -v "/mnt/user/media/TV Shows:/tv:ro" \
  poptonium
```

The two media lines are optional (see [Cast receiver subtitles](#cast-receiver-subtitles-optional)):
use the same container paths as your Plex container.

### Unraid

Open the **Apps** tab, search for **Poptonium**, and click Install. You can leave the variables
blank, then open the WebUI (`http://<host>:8085/admin`) and enter your keys in the wizard.

The template includes an optional, read-only **Media** path for
[Cast receiver subtitles](#cast-receiver-subtitles-optional). Set its Container Path to the path
your Plex container uses for that folder, and add one mapping per Plex media path if Plex has
several (for example `/movies` and `/tv`). Leave it out if you don't cast with Poptonium.

(If you prefer to add it by hand, the same template lives at
[`templates/poptonium.xml`](templates/poptonium.xml): Docker, Add Container, Template.)

### Cast receiver subtitles (optional)

The Poptonium Cast receiver plays files directly on the Chromecast, but a Chromecast can't read
subtitle tracks embedded in the file. Plex only extracts those inside a full conversion session,
which on remote streams can re-encode video nobody watches and deliver subtitles at playback
speed. With your media mounted **read-only**, Poptonium reads the selected track straight from the
file instead (`/cast/subtitles`, ffmpeg is included in the image): the first subtitles arrive in
well under a second, ASS keeps its styling, and finished tracks are cached under `/data`. Plex
still decides and serves the video, so its bandwidth limits are unchanged.

- Mount each media folder at **the same container path Plex uses** (if Plex sees
  `/tv/Show/S01E01.mkv`, Poptonium needs `/tv` too), with `:ro`.
- If the paths can't match, set `SUBTITLE_PATH_MAP` to `plex path=container path` pairs separated
  by `;`, e.g. `/data/movies=/media/Movies;/data/tv=/media/TV Shows`.
- The dashboard's **Cast subtitles** tile shows how many of Plex's library folders are visible;
  hover it to see the ones that aren't.
- Access is checked on Plex with each viewer's own token, so users only get subtitles for items
  they can see. Without the mount everything still works: the receiver falls back to Plex.

### First run

Open `/admin` and create a single admin account — it guards the dashboard and every setting. A
**setup wizard** then walks you through entering and testing every credential right in the browser:
Plex (required) plus MDbList, Overseerr and OpenSubtitles (optional), each with a **Test connection**
button. No environment variables or restarts are needed. Once Plex tests green, a second wizard
offers to seed a set of **starter sections** adapted to your own libraries, or you can start from a
blank board. You can revisit and change any integration later under **Integrations**.

## Reverse proxy

This step is recommended but optional. On the same LAN with no proxy, the app reaches the backend
directly at `http://<plex-host>:8085` (this is why the port must stay 8085). A reverse proxy is what
makes the backend reachable from outside your LAN.

The app discovers the backend from the **Plex server connection**, so the only goal is to route the
path prefix `/poptonium/` on your existing Plex domain to this container on port 8085. No extra
domain or DNS record is needed.

Two rules:

1. **Route `/poptonium/` to the container on port 8085**, preserving the full path (no URI rewrite).
2. **Restrict `/poptonium/admin` to your LAN.** The admin UI has its own login, but it should not be
   exposed to the public internet. The app itself never calls `/admin`, so locking it down does not
   affect the client.

### SWAG / nginx

A ready-to-paste snippet is in [`swag/poptonium.subdomain.conf`](swag/poptonium.subdomain.conf).
Paste both blocks inside the `server { ... }` block of your Plex reverse-proxy conf, **above** the
main `location / { ... }` Plex block, then reload the proxy:

```nginx
# Admin dashboard: LAN-only (matched before the API block).
location ~ ^/poptonium/admin(/|$) {
    if ($lan-ip != yes) { return 404; }
    include /config/nginx/proxy.conf;
    include /config/nginx/resolver.conf;
    set $upstream_app poptonium;
    set $upstream_port 8085;
    set $upstream_proto http;
    proxy_pass $upstream_proto://$upstream_app:$upstream_port;
}

# Public API used by the app.
location /poptonium/ {
    include /config/nginx/proxy.conf;
    include /config/nginx/resolver.conf;
    set $upstream_app poptonium;
    set $upstream_port 8085;
    set $upstream_proto http;
    proxy_pass $upstream_proto://$upstream_app:$upstream_port;
}
```

`$upstream_app poptonium` works because SWAG resolves the container name on its docker network; use
a host IP instead if the proxy and the container are not on the same network.

### About `$lan-ip` (not just SWAG)

`$lan-ip` is a **SWAG-specific** variable (set to `yes` for private RFC1918 client IPs via
`/config/nginx/dbip.conf`). It does **not** exist in plain nginx or other proxies. To LAN-restrict
the admin path elsewhere, use that proxy's own access control:

- **Plain nginx**: replace the `if` line with an allow/deny list inside the admin `location`:
  ```nginx
  allow 192.168.0.0/16;
  allow 10.0.0.0/8;
  deny all;
  ```
- **Caddy**: a matcher on `remote_ip private_ranges` that `reverse_proxy`es to `poptonium:8085`, and
  a `respond 404` for everything else under `/poptonium/admin`.
- **Traefik**: an `ipWhiteList` (or `ipAllowList`) middleware with your LAN CIDRs on the
  `/poptonium/admin` router.

### Any other reverse proxy

The only requirements are: forward `/poptonium/` to `http://<container>:8085` keeping the path, and
gate `/poptonium/admin` to the LAN by whatever access-control mechanism your proxy provides.

## Configuration

Everything is configured in the admin UI — the setup wizard or the **Integrations** tab — with a
**Test connection** button for each integration and secrets masked in the interface. You never need
to touch environment variables (the one exception is the optional `SUBTITLE_PATH_MAP` below).

If you'd rather pre-fill the integrations for an automated or Unraid deploy, you can optionally set
the environment variables below instead of using the wizard; either way you can change everything in
the UI afterward. These are the credentials the wizard collects:

| Setting | Needed for | Notes |
|---------|------------|-------|
| `PLEX_URL`, `PLEX_TOKEN` | Plex (required) | Plex Media Server connection. The dashboard stays locked until Plex connects. |
| `MDBLIST_API_KEY` | Ratings + Discover feed | mdblist.com key. Without it, ratings and the popular feed are simply empty. |
| `OVERSEERR_URL`, `OVERSEERR_API_KEY` | In-app search & requests | Overseerr request/search proxy. |
| `OPENSUBTITLES_API_KEY` | Subtitle search/download | App API key from opensubtitles.com (Profile, API Consumers). |
| `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD` | Subtitle downloads | Account whose daily download quota (20/day free) is used. |
| `SUBTITLE_PATH_MAP` | Cast receiver subtitles | Only when the media is mounted at different paths than Plex uses: `plex path=container path` pairs separated by `;`. See [Cast receiver subtitles](#cast-receiver-subtitles-optional). |
| `PUBLIC_URL` | Share links | This server's internet address through your reverse proxy (e.g. `https://plex.example.com/poptonium`). Blank keeps sharing off and hides the app's Share button. Share pages are public, so if your proxy geo-blocks or LAN-restricts, exempt `/poptonium/s/` (see the SWAG snippet). |

## What the admin UI does

- **Dashboard** — health of each integration at a glance, plus on-demand controls for the scheduled
  jobs and caches.
- **Library ratings sync** — a nightly refresh of your whole library's ratings (default 03:00; change
  the hour, toggle it off, or run it now).
- **Ratings** — choose which sources show per item and how the overall rating used for sorting is
  calculated. Rating-ranked sections also weigh each title's vote counts, so a 99% from 18 votes
  doesn't outrank a 96% from 9,500; the **min votes** per source sets where that confidence
  levels off. Displayed scores and badges are unaffected — this only changes ordering.
- **Custom sections** — build the rows and heroes that appear on the app's Library page, either
  mirroring a Plex collection or filtering your library by ratings, dates, and genres. Give each a
  title, style (**Row** or **Hero**), and a spot on the page.
- **Maintenance** — clear caches and trigger scheduled jobs on demand.
