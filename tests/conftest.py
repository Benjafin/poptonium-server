"""Shared test setup.

The app reads its configuration from the environment at import time (see
``app/config.py``), so we set fake Plex/Overseerr values *before* any app module
is imported. Nothing here talks to a real service: every outbound HTTP call is
intercepted by respx in the individual tests, and the DB points at a temp file.
"""

import os
import tempfile

# --- Environment: set BEFORE importing anything under `app`. -----------------
# These hostnames are never really contacted; respx matches on them.
os.environ.setdefault("PLEX_URL", "http://plex.test")
os.environ.setdefault("PLEX_TOKEN", "admin-token")
os.environ.setdefault("OVERSEERR_URL", "http://overseerr.test")
os.environ.setdefault("OVERSEERR_API_KEY", "test-api-key")
# A throwaway on-disk SQLite file so db tests never touch /data.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "poptonium-test.db"))
# Live config now lives in a JSON file (seeded from the env above on first load);
# point it at a throwaway path so tests never touch a real /data/config.json.
os.environ.setdefault("CONFIG_PATH", os.path.join(tempfile.gettempdir(), "poptonium-test-config.json"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset the live config singleton to the env-seeded baseline before each test,
    in memory (no persist), so a value a test sets via ``monkeypatch.setitem`` — or
    an endpoint that persists — can't leak into the next test."""
    from app.config import CONFIG_KEYS, settings

    settings._values = {k: os.environ.get(k, "") for k in CONFIG_KEYS}
    yield


@pytest.fixture(autouse=True)
def _reset_caches():
    """Clear the module-level auth/user caches between tests so a cached result
    from one test can't leak into (or starve an expected HTTP call in) the next."""
    from app import client_auth, overseerr

    client_auth._token_cache.clear()
    client_auth._account_cache.clear()
    client_auth._identity_cache.clear()
    client_auth._shared_map_cache.update(expiry=0.0, by_digest={})
    overseerr._user_cache.update(expiry=0.0, by_plex_id={}, by_email={})
    yield
