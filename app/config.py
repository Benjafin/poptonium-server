"""Environment configuration, service-wide constants, and logging setup."""

import logging
import os

# Persistent data always lives on the /data bind; the DB filename is fixed.
# Overridable via DB_PATH only so tests can point at a temp file; prod leaves it.
DB_PATH = os.environ.get("DB_PATH", "/data/poptonium.db")

# Optional. With no key, ratings and the Discover popular feed are disabled; the
# rest of the service keeps working.
MDBLIST_API_KEY = os.environ.get("MDBLIST_API_KEY", "")
MDBLIST_BASE = "https://api.mdblist.com"
RATINGS_MAX_AGE = 14 * 86400  # consider a cached rating stale after 14 days
# Nightly library-ratings sync is configured in the dashboard (meta key
# "ratings_sync"), defaulting to enabled at 03:00. No env var.

OVERSEERR_URL = os.environ.get("OVERSEERR_URL", "")
OVERSEERR_API_KEY = os.environ.get("OVERSEERR_API_KEY", "")

PLEX_URL = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

# plex.tv account endpoint — resolves which account owns a given Plex token.
PLEX_TV_USER_URL = "https://plex.tv/api/v2/user"

# OpenSubtitles (api.opensubtitles.com). One app-level API key + one shared account
# (its daily download quota is shared by everyone). API key is created under the
# account's "API Consumers" page. The User-Agent identifying the app is fixed.
OPENSUBTITLES_API_KEY = os.environ.get("OPENSUBTITLES_API_KEY", "")
OPENSUBTITLES_USERNAME = os.environ.get("OPENSUBTITLES_USERNAME", "")
OPENSUBTITLES_PASSWORD = os.environ.get("OPENSUBTITLES_PASSWORD", "")
OPENSUBTITLES_USER_AGENT = "Poptonium"
OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"

SERVICE_VERSION = "1.0.0"

# Version of the section-rendering contract between this backend and the app.
# The backend tags each resolved section with the min app version that can render
# it (derived from its type/style below, never stored or user-set); a client whose
# own section-schema version is lower skips that section rather than mis-rendering
# it. Bump this whenever a new section type/style ships that older apps can't draw.
SECTION_SCHEMA_VERSION = "1.1.0"

# The min app version required to render a section, keyed by its type and its
# style. A section's floor is the highest of the two. Everything we support today
# renders on 1.0.0. When a NEW type/style is NOT backward-compatible, hardcode its
# higher floor here (and bump SECTION_SCHEMA_VERSION + the app's ClientSchema):
# old apps then skip those sections automatically. This is internal/invisible to
# the admin; it is not a per-section field.
SECTION_TYPE_MIN_VERSION = {
    "plex_collection": "1.0.0",
    "filter": "1.0.0",
    "sessions": "1.0.0",
    "history": "1.0.0",
}
SECTION_STYLE_MIN_VERSION = {
    "row": "1.0.0",
    "hero": "1.0.0",
    "bento": "1.0.0",
}
# Opt-in config FEATURES that need a newer client than the section's type/style
# alone (e.g. a filter section can render on 1.0.0, but not once it emits individual
# episode cards). Keyed by the cfg flag that enables the feature; a section using
# one is stamped with the higher floor so older apps skip it instead of mis-drawing.
SECTION_FEATURE_MIN_VERSION = {
    "episode_items": "1.1.0",   # lists individual episodes, not whole shows
}


def _version_key(v: str):
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def section_min_version(section_type: str, style: str, config: dict = None) -> str:
    """Min app version that can render a section of this type+style, raised by any
    opt-in config feature it uses. Unknown type/style defaults to the current schema
    version, so a future type added in code without an explicit entry still fails
    safe (old apps skip it)."""
    floors = [
        SECTION_TYPE_MIN_VERSION.get(section_type, SECTION_SCHEMA_VERSION),
        SECTION_STYLE_MIN_VERSION.get(style, SECTION_SCHEMA_VERSION),
    ]
    for flag, floor in SECTION_FEATURE_MIN_VERSION.items():
        if config and config.get(flag):
            floors.append(floor)
    return max(floors, key=_version_key)


def version_gte(a: str, b: str) -> bool:
    """Dotted-version >= compare, zero-padding missing components ("1.1" == "1.1.0")."""
    pa = [int(x) if x.isdigit() else 0 for x in (a or "0").split(".")]
    pb = [int(x) if x.isdigit() else 0 for x in (b or "0").split(".")]
    n = max(len(pa), len(pb))
    return pa + [0] * (n - len(pa)) >= pb + [0] * (n - len(pb))

# Rating sources we support, in canonical id form. mdblist keys: tomatoes=RT
# critic, popcorn=RT audience; "mdblist" is the item-level aggregate score.
SUPPORTED_SOURCES = ["mdblist", "imdb", "tomatoes", "popcorn", "tmdb", "metacritic"]

DEFAULT_RATING_CONFIG = {
    "display_sources": ["mdblist", "imdb", "tomatoes", "popcorn", "tmdb", "metacritic"],
    # Ordered badge GROUPS. Each group has a visibility:
    #   "always":  its present (non-empty/non-zero) sources are always shown.
    #   "fallback": only shown when EVERY "always" source is missing; fallback
    #                groups are tried in order until one has a present source.
    # `display_sources` is kept as the flattened union for older app clients.
    "display_groups": [
        {"visibility": "always", "sources": ["mdblist", "imdb", "tomatoes", "popcorn", "tmdb", "metacritic"]},
    ],
    "formula": {
        "preset": "mdblist",  # or "custom"
        "weights": {"imdb": 1, "tomatoes": 1, "popcorn": 1, "tmdb": 1, "metacritic": 1},
        "vote_aware": True,
        "min_votes": {"imdb": 1000, "tomatoes": 20, "popcorn": 50, "tmdb": 300, "metacritic": 10},
        # How to handle items mdblist gives no aggregate score for:
        # "average" = synthesize the MDbList score as the mean of available
        # sources; "zero" = leave it out (the app hides the chip).
        "missing_mdblist": "average",
    },
}

# Plex `type` ints for the media kinds we filter on.
PLEX_TYPE = {"movie": 1, "show": 2}

log = logging.getLogger("poptonium")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
