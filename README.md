<p align="center">
  <img src="docs/logo.png" alt="Poptonium" width="420">
</p>

<h1 align="center">Poptonium Server</h1>

<p align="center">
  The optional companion server for <a href="https://poptonium.app">Poptonium</a>, a Plex client for
  <a href="https://apps.apple.com/nl/app/poptonium-for-plex/id6779742766?l=en-GB">iOS</a> and
  <a href="https://play.google.com/store/apps/details?id=games.benja.poptonium">Android</a>.
</p>

One small container with a web admin UI. It powers:

- **Curated library**: rows and hero banners built from Plex collections or rating, date and genre filters.
- **Ratings**: IMDb, Rotten Tomatoes, TMDB and Metacritic, with the display and weighting you choose.
- **Subtitles**: viewers search and download in-app through your OpenSubtitles connection; styled subtitles on the Poptonium Cast receiver.
- **Popular feed**: a nightly-refreshed Discover feed.
- **Search and requests**: through Overseerr.
- **Share links**: share a movie, show or episode as a public link with a rich preview.

Without the server, the app is a standard Plex client with full Cast support. Feature details, guides and support: **[poptonium.app](https://poptonium.app)**.

## Install

Only Plex is required. Each integration is optional, and leaving one out disables just that feature.

| | |
|---|---|
| Image | `ghcr.io/benjafin/poptonium:latest` (stable) or `:dev` |
| Port | `8085`. **Keep the host port at 8085**: without a reverse proxy the app finds the server on the Plex host at 8085. |
| Data | `/data` (config, database, caches) |

**Unraid**: in **Apps**, search for **Poptonium** and click Install.

**Docker**:

```bash
docker run -d --name poptonium --restart unless-stopped \
  -p 8085:8085 \
  -v /path/to/appdata/poptonium:/data \
  ghcr.io/benjafin/poptonium:latest
```

**Compose**: `docker compose up -d` with the included [`docker-compose.yml`](docker-compose.yml). It builds from source.

## Set up

Open `http://<host>:8085/admin`, create the admin account, and follow the setup wizard. It tests each connection as you go:

| Integration | Enables | You need |
|---|---|---|
| **Plex** (required) | Everything | Server URL (e.g. `http://plex:32400`) and token |
| MDbList | Ratings, popular feed | [mdblist.com](https://mdblist.com) API key |
| Overseerr | Search and requests | URL and API key |
| OpenSubtitles | Subtitle search and download | App API key (Profile > API Consumers) plus an account; its quota (20/day free) is used |

Next, choose starter sections based on your libraries, or start from a blank board. You can change everything later under **Integrations**, **Ratings** and **Sections**.

## Optional

### Reverse proxy (access outside your LAN)

The app discovers the server through your Plex connection, so you don't need a new domain. On your existing Plex proxy:

1. Route `/poptonium/` to `http://poptonium:8085`, keeping the full path (no rewrite).
2. Restrict `/poptonium/admin` to your LAN. The app never calls it.

SWAG/nginx: paste both blocks from [`swag/poptonium.subdomain.conf`](swag/poptonium.subdomain.conf) above the main `location /` block. `$lan-ip` exists only in SWAG. On other proxies, use their own access control instead: `allow`/`deny` in nginx, `remote_ip private_ranges` in Caddy, an `ipAllowList` middleware in Traefik.

### Share links

Under **Integrations > Share links**, set the Public URL (or `PUBLIC_URL`) to the server's public address, e.g. `https://plex.example.com/poptonium`. Leave it blank to hide the app's Share button. Share pages are public, so if your proxy geo-blocks or LAN-restricts requests, exempt `/poptonium/s/`.

### Fast Cast subtitles

A Chromecast can't read subtitle tracks embedded in a file, and having Plex extract them is slow. Mount your media **read-only at the same container paths Plex uses** and the server reads the tracks straight from the file: they arrive in under a second and ASS keeps its styling.

```bash
  -v /mnt/user/media/Movies:/movies:ro \
  -v "/mnt/user/media/TV Shows:/tv:ro" \
```

If the paths can't match, set `SUBTITLE_PATH_MAP` to `plex path=container path` pairs separated by `;`, for example `/data/movies=/media/Movies;/data/tv=/media/TV Shows`. The dashboard's **Cast subtitles** tile shows which Plex folders are visible. Without the mount, subtitles still come through Plex.

### Environment variables

The wizard is all you need. If you want to pre-fill a deploy, these variables seed `/data/config.json` **on first boot only**. After that, the admin UI is the source of truth.

| Variable | Purpose |
|---|---|
| `PLEX_URL`, `PLEX_TOKEN` | Plex connection |
| `MDBLIST_API_KEY` | Ratings and popular feed |
| `OVERSEERR_URL`, `OVERSEERR_API_KEY` | Search and requests |
| `OPENSUBTITLES_API_KEY`, `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD` | Subtitles |
| `PUBLIC_URL` | Share links |
| `SUBTITLE_PATH_MAP` | Cast subtitle path mapping (read on every start) |
| `TZ` | Timezone for the nightly ratings sync (default 03:00) |

## License

[MIT](LICENSE)
