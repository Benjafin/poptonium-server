<p align="center">
  <img src="docs/logo.png" alt="Poptonium" width="420">
</p>

<h1 align="center">Poptonium Server</h1>

Companion backend for the Poptonium Plex client. A single small container that provides:

- **Ratings cache** from [mdblist](https://mdblist.com) (IMDb / Rotten Tomatoes / TMDB / Metacritic / MDbList) for fast library sorting.
- **Discover "popular" feed** built nightly from mdblist.
- **Custom Library sections** (rows and heroes) that drive the app's Library page.
- **Overseerr proxy** for in-app search and requests.
- **OpenSubtitles** search and download into Plex.
- **Web admin UI** at `/admin` for caches, scheduled jobs, config, and sections.

**Plex is required.** mdblist, Overseerr and OpenSubtitles are optional: leave their variables
blank to disable that feature, the rest keeps working. With no mdblist key the service runs fine,
it just serves no ratings and an empty popular feed.

## Install

The app listens on container port **8085** and persists everything (the SQLite cache and your
custom sections) under `/data`. The database is always `/data/poptonium.db`, so just bind `/data`.

> **Keep the host port at 8085.** When the app can't reach the backend through a reverse proxy
> (same LAN, no proxy configured) it falls back to the Plex host on a hardcoded port 8085. Changing
> the host port breaks that direct-discovery path.

### Docker Compose

```bash
cp .env.example .env   # set PLEX_URL + PLEX_TOKEN, plus any optional vars
docker compose up -d
```

`docker-compose.yml` maps host `8085:8085` and binds `./data:/data`. Open `http://<host>:8085/admin`.

### Plain Docker

```bash
docker build -t poptonium .
docker run -d --name poptonium \
  -p 8085:8085 \
  -v /mnt/user/appdata/poptonium:/data \
  -e PLEX_URL=http://<plex-host>:32400 \
  -e PLEX_TOKEN=<your-plex-token> \
  poptonium
```

### Unraid

Search **Poptonium** in Community Applications, or add the template
[`templates/poptonium.xml`](templates/poptonium.xml) manually (Docker, Add Container, Template).
Set `PLEX_URL` and `PLEX_TOKEN` (and any optional vars), then open the WebUI
(`http://<host>:8085/admin`).

### First run

Poptonium checks Plex on boot. If `PLEX_URL` / `PLEX_TOKEN` are missing or Plex is unreachable, the
admin UI shows a **Connect Plex first** screen and stays blocked until the connection works (set the
variables and restart, then press Retry). Once Plex is reachable, `/admin` prompts you to create a
single admin account that guards the dashboard and every config-changing endpoint; the client read
endpoints stay open.

## Reverse proxy

This step is recommended but optional. On the same LAN with no proxy, the app reaches the backend
directly at `http://<plex-host>:8085` (this is why the port must stay 8085). A reverse proxy is what
makes the backend reachable from outside your LAN.

The app discovers the backend from the **Plex server connection**: it probes
`<your-plex-url>/poptonium/capabilities`. So the goal is simply to route the path prefix
`/poptonium/` on your existing Plex domain to this container on port 8085. No extra domain or DNS
record is needed.

Two rules:

1. **Route `/poptonium/` to the container on port 8085**, preserving the full path (no URI rewrite,
   since the app already mounts its routes under `/poptonium`).
2. **Restrict `/poptonium/admin` to your LAN.** The admin UI has its own login, but it should not be
   exposed to the public internet. The app itself never calls `/admin`, so locking it down does not
   affect the client.

### SWAG / nginx

A ready-to-paste snippet is in [`swag/poptonium.subdomain.conf`](swag/poptonium.subdomain.conf).
Paste both blocks inside the `server { ... }` block of your Plex reverse-proxy conf, **above** the
main `location / { ... }` Plex block, then reload the proxy:

```nginx
# Admin dashboard + auth/config endpoints: LAN-only (matched before the API block).
location ~ ^/poptonium/admin(/|$) {
    if ($lan-ip != yes) { return 404; }
    include /config/nginx/proxy.conf;
    include /config/nginx/resolver.conf;
    set $upstream_app poptonium;
    set $upstream_port 8085;
    set $upstream_proto http;
    proxy_pass $upstream_proto://$upstream_app:$upstream_port;
}

# Public API (sections, ratings, capabilities, popular, overseerr, opensubtitles,
# subtitle-prefs, the Plex proxy).
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

Plex is set with environment variables; everything else is either an env var or, where noted,
configured in the admin UI. Secrets are masked and read-only in the web UI.

| Var | Required | Purpose |
|-----|----------|---------|
| `PLEX_URL`, `PLEX_TOKEN` | yes | Plex Media Server connection. The service blocks the admin UI until this is reachable. |
| `MDBLIST_API_KEY` | no | mdblist.com key: the source for all ratings plus the Discover feed. Blank disables ratings and the popular feed only. |
| `OVERSEERR_URL`, `OVERSEERR_API_KEY` | no | Overseerr request/search proxy. |
| `OPENSUBTITLES_API_KEY` | no | App API key from opensubtitles.com (Profile, API Consumers). Required for online subtitle search/download. |
| `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD` | no | Account whose daily download quota (20/day free) is used. |

### Configured in the admin UI (stored on the `/data` bind)

- **Library ratings sync**: nightly bulk-refresh of the whole library's ratings. Default is on at
  03:00; toggle it and pick the hour on the Dashboard, or run it on demand.
- **Ratings**: which sources show per item and the rating formula. Default is MDbList's own score;
  the custom preset is a weighted, optionally vote-aware average used as the canonical rating for
  sorting and section minimums.
- **Custom sections**: create **Plex Collection** sections (mirror a collection live) or **Filter**
  sections (library items matching RT/TMDB minimums, added-within / release-year windows, genres).
  Each has a title, optional subtitle, order, enabled toggle, a style (**Row** or **Hero**), and a
  placement anchor on the Library page. The app renders them from `/sections/resolved`.
- **Maintenance**: clear the ratings or popular caches and trigger scheduled jobs on demand.
