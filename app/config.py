"""Environment configuration, service-wide constants, and logging setup.

Integration credentials (Plex, MDbList, Overseerr, OpenSubtitles) are NOT plain
env constants anymore: they live in the ``settings`` singleton at the bottom of
this module, backed by a JSON file on the /data bind (``CONFIG_PATH``). The
environment only *seeds* that file on first boot; thereafter the file is the sole
source of truth, so the values are editable in the admin UI and apply live
(consumers read ``settings.PLEX_URL`` etc., never a captured constant).
"""

import json
import logging
import os

# Persistent data always lives on the /data bind; the DB filename is fixed.
# Overridable via DB_PATH only so tests can point at a temp file; prod leaves it.
DB_PATH = os.environ.get("DB_PATH", "/data/poptonium.db")

# The live integration config file. Defaults next to the DB on the /data bind.
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(DB_PATH) or ".", "config.json"))

MDBLIST_BASE = "https://api.mdblist.com"
RATINGS_MAX_AGE = 14 * 86400  # consider a cached rating stale after 14 days
# Nightly library-ratings sync is configured in the dashboard (meta key
# "ratings_sync"), defaulting to enabled at 03:00. No env var.

# plex.tv account endpoint — resolves which account owns a given Plex token.
PLEX_TV_USER_URL = "https://plex.tv/api/v2/user"

# OpenSubtitles (api.opensubtitles.com). One app-level API key + one shared account
# (its daily download quota is shared by everyone). API key is created under the
# account's "API Consumers" page. The User-Agent identifying the app is fixed.
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


def degrade_config(config: dict, client_schema: str) -> dict:
    """A copy of `config` with any opt-in feature the requesting client is too old to
    render turned off, so the section is served (and version-stamped) in its supported
    form rather than skipped. Shared by section resolution and the shells listing."""
    out = dict(config or {})
    for flag, floor in SECTION_FEATURE_MIN_VERSION.items():
        if out.get(flag) and not version_gte(client_schema, floor):
            out[flag] = False
    return out

# Rating sources we support, in canonical id form. mdblist keys: tomatoes=RT
# critic, popcorn=RT audience; "mdblist" is the item-level aggregate score.
SUPPORTED_SOURCES = ["mdblist", "imdb", "tomatoes", "popcorn", "tmdb", "metacritic"]

DEFAULT_RATING_CONFIG = {
    # Ordered badge GROUPS. Each group has a visibility:
    #   "always":  its present (non-empty/non-zero) sources are always shown.
    #   "fallback": only shown when EVERY "always" source is missing; fallback
    #                groups are tried in order until one has a present source.
    # `display_sources` is kept as the flattened union for older app clients.
    # Default mirrors a curated real-world setup: Rotten Tomatoes critic + audience
    # always, then Metacritic -> IMDb -> TMDB as fallbacks. The mdblist aggregate is
    # used for ranking/formula but not shown as its own badge.
    "display_sources": ["tomatoes", "popcorn", "metacritic", "imdb", "tmdb"],
    "display_groups": [
        {"visibility": "always", "sources": ["tomatoes", "popcorn"]},
        {"visibility": "fallback", "sources": ["metacritic"]},
        {"visibility": "fallback", "sources": ["imdb"]},
        {"visibility": "fallback", "sources": ["tmdb"]},
    ],
    "formula": {
        "preset": "mdblist",  # or "custom"
        "weights": {"imdb": 1, "tomatoes": 1, "popcorn": 1, "tmdb": 1, "metacritic": 1},
        "vote_aware": True,
        # Half-confidence point per source: a title with exactly `m` votes counts
        # half as much as one with infinite votes. Calibrated against observed
        # mdblist vote scales, which differ by orders of magnitude per source
        # (IMDb runs 10k-500k, RT critic 40-500, Metacritic 8-65). Used both by
        # the vote_aware custom formula and by ranking shrinkage (`rank_score`).
        "min_votes": {"imdb": 50000, "tomatoes": 100, "popcorn": 800, "tmdb": 500, "metacritic": 25},
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


# ---------- Live integration config (settings singleton) ----------

# The ordered set of persisted integration keys. URLs/usernames are shown in the
# clear in the admin UI; the rest are masked (SECRET_KEYS).
CONFIG_KEYS = (
    "PLEX_URL", "PLEX_TOKEN",
    "MDBLIST_API_KEY",
    "OVERSEERR_URL", "OVERSEERR_API_KEY",
    "OPENSUBTITLES_API_KEY", "OPENSUBTITLES_USERNAME", "OPENSUBTITLES_PASSWORD",
)
SECRET_KEYS = frozenset({
    "PLEX_TOKEN", "MDBLIST_API_KEY", "OVERSEERR_API_KEY",
    "OPENSUBTITLES_API_KEY", "OPENSUBTITLES_PASSWORD",
})


def mask_secret(val: str) -> str:
    """Short, non-reversible preview of a secret for display in the admin UI."""
    if not val:
        return ""
    if len(val) <= 8:
        return "•" * len(val)
    return f"{val[:4]}…{val[-4:]}"


class Settings:
    """Live integration credentials, persisted to ``CONFIG_PATH`` on the /data bind.

    File-only precedence: on first boot (no file) the values are seeded once from
    the environment so an env-configured instance keeps working, then the file is
    the sole source of truth. Consumers read attributes (``settings.PLEX_URL``), so
    an in-app edit via ``update`` propagates everywhere with no restart.
    """

    def __init__(self, path: str = None):
        self._path = path or CONFIG_PATH
        self._values = {k: "" for k in CONFIG_KEYS}

    def load(self) -> "Settings":
        data = None
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                log.warning("Config file %s unreadable; reseeding from environment", self._path)
        if data is None:
            # First boot (or a corrupt file): seed once from the environment.
            data = {k: os.environ.get(k, "") for k in CONFIG_KEYS}
            self._values = {k: str(data.get(k) or "") for k in CONFIG_KEYS}
            self._persist()
        else:
            self._values = {k: str(data.get(k) or "") for k in CONFIG_KEYS}
        return self

    def _persist(self):
        try:
            d = os.path.dirname(self._path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._values, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            log.error("Failed to persist config to %s: %s", self._path, e)

    def update(self, patch: dict) -> dict:
        """Merge known keys from `patch` (ignoring unknown ones), persist, and return
        the new value map. A key set to None/"" clears it."""
        for k, v in (patch or {}).items():
            if k in self._values:
                self._values[k] = "" if v is None else str(v)
        self._persist()
        return dict(self._values)

    def as_masked(self) -> dict:
        """Config for the admin UI: URLs/usernames in the clear; secrets masked and
        accompanied by a ``<KEY>_set`` boolean (mirrors the plugin settings shape)."""
        out = {}
        for k in CONFIG_KEYS:
            v = self._values.get(k, "")
            if k in SECRET_KEYS:
                out[k] = mask_secret(v)
                out[f"{k}_set"] = bool(v)
            else:
                out[k] = v
        return out

    def __getattr__(self, name):
        # Only reached when `name` isn't a real attribute/method — i.e. the config keys.
        values = self.__dict__.get("_values")
        if values is not None and name in values:
            return values[name]
        raise AttributeError(name)


settings = Settings()
settings.load()
