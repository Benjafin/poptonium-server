"""SQLite connection, schema bootstrap, and the generic ``meta`` key/value store.

``get_db`` opens a fresh short-lived connection (WAL + busy timeout) and ensures
the schema exists, so every caller can ``await get_db()`` without ordering
concerns. Callers own closing the connection (``try/finally: await db.close()``).
"""

from typing import Optional

import aiosqlite

from .config import DB_PATH


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # WAL + a busy timeout so concurrent jobs (popular refresh, library sync,
    # section resolution) wait for the write lock instead of erroring out.
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=15000")
    # Universal rating cache: one row per title, keyed by TMDB id + media type.
    # `ratings_json` holds {source: {score, votes}} for SUPPORTED_SOURCES.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mdblist_ratings (
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            imdb_id TEXT,
            mdblist_score INTEGER,
            ratings_json TEXT NOT NULL DEFAULT '{}',
            fetched_at REAL NOT NULL,
            PRIMARY KEY (tmdb_id, media_type)
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mdblist_ratings_imdb ON mdblist_ratings(imdb_id)"
    )
    # Discover/popular feed metadata. Scores now live in mdblist_ratings (joined
    # by tmdb_id); drop the stale per-score columns from older DBs first.
    cursor = await db.execute("PRAGMA table_info(popular_items)")
    pcols = {r[1] for r in await cursor.fetchall()}
    if pcols and "rt_critic_score" in pcols:
        await db.execute("DROP TABLE popular_items")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS popular_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT NOT NULL,
            tmdb_id INTEGER,
            title TEXT NOT NULL,
            year INTEGER,
            media_type TEXT NOT NULL,
            poster_url TEXT,
            description TEXT,
            certification TEXT,
            rank INTEGER,
            fetched_at REAL NOT NULL,
            UNIQUE(imdb_id, media_type)
        )
    """)
    # Registered integration plugins (separate sidecar services implementing the
    # manifest contract). We proxy /plugins/{id}/* to base_url and render their
    # settings generically from the cached manifest.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS plugins (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            manifest_json TEXT NOT NULL DEFAULT '{}',
            added_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    # User-defined custom sections that drive the app's Library page.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subtitle TEXT,
            type TEXT NOT NULL,
            style TEXT NOT NULL DEFAULT 'row',
            position TEXT NOT NULL DEFAULT 'top',
            sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            config TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    # Generic key/value store for app-level metadata (e.g. last refresh times).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Per-series subtitle language preference, per Plex user. Plex itself has no
    # per-show subtitle setting; the client supplies the account id + show ratingKey.
    # language NULL = subtitles explicitly off; no row = no preference (use default).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_prefs (
            user_id TEXT NOT NULL,
            series_key TEXT NOT NULL,
            language TEXT,
            updated_at REAL,
            PRIMARY KEY (user_id, series_key)
        )
    """)
    # Migrate: add sections.position for existing DBs (placement relative to the
    # built-in All/Movies/TV shelves).
    cursor = await db.execute("PRAGMA table_info(sections)")
    scols = {r[1] for r in await cursor.fetchall()}
    if "position" not in scols:
        await db.execute("ALTER TABLE sections ADD COLUMN position TEXT NOT NULL DEFAULT 'top'")
    await db.commit()
    return db


async def meta_get(key: str) -> Optional[str]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else None
    finally:
        await db.close()


async def meta_set(key: str, value: str):
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()
    finally:
        await db.close()
