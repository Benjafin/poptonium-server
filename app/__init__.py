"""poptonium application package.

The service is split into single-responsibility modules:

- ``config``        — environment configuration, constants, logging.
- ``db``            — SQLite connection, schema, and the generic ``meta`` KV store.
- ``http_client``   — the shared keep-alive httpx client.
- ``scheduler``     — holder for the process-wide APScheduler instance.
- ``ratings``       — mdblist fetching, the rating formula, and library sync.
- ``plex``          — Plex HTTP helpers and item mapping.
- ``popular``       — the Discover/popular feed.
- ``overseerr``     — Overseerr request proxy.
- ``opensubtitles`` — OpenSubtitles search + download-to-Plex.
- ``plugins``       — generic integration-plugin host.
- ``sections``      — custom Library sections (resolution + CRUD).
- ``subtitle_prefs``— per-series subtitle preferences.
- ``capabilities``  — client capability discovery.
- ``admin``         — admin status, Plex helpers, jobs, and the web UI.
- ``server``        — the FastAPI app wiring everything together.
"""
