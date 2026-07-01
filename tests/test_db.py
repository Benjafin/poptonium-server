"""Example of a DB-backed test.

Pattern: point `app.db.DB_PATH` at a fresh temp file per test via monkeypatch, so
each test gets an isolated SQLite database and the schema is built from scratch.
`get_db()` creates the tables on first connect, so there's no separate setup.
"""

import aiosqlite

import app.db as db


async def test_get_db_migrates_legacy_schema(tmp_path, monkeypatch):
    """get_db() upgrades an older DB: drops a stale popular_items (with the removed
    rt_critic_score column) and adds the sections.position column."""
    path = str(tmp_path / "legacy.db")
    conn = await aiosqlite.connect(path)
    await conn.execute("CREATE TABLE popular_items (id INTEGER PRIMARY KEY, rt_critic_score INTEGER)")
    await conn.execute(
        "CREATE TABLE sections (id INTEGER PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL, "
        "style TEXT NOT NULL DEFAULT 'row', sort_order INTEGER NOT NULL DEFAULT 0, "
        "enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}', "
        "created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0)"
    )
    await conn.commit()
    await conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    conn2 = await db.get_db()
    try:
        cur = await conn2.execute("PRAGMA table_info(popular_items)")
        pcols = {r[1] for r in await cur.fetchall()}
        assert "rt_critic_score" not in pcols   # stale table was dropped + recreated
        assert "imdb_id" in pcols                # new schema in place

        cur = await conn2.execute("PRAGMA table_info(sections)")
        scols = {r[1] for r in await cur.fetchall()}
        assert "position" in scols               # column added by migration
    finally:
        await conn2.close()


async def test_meta_get_set_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))

    assert await db.meta_get("missing") is None

    await db.meta_set("last_refresh", "123")
    assert await db.meta_get("last_refresh") == "123"

    # INSERT OR REPLACE semantics: setting again overwrites.
    await db.meta_set("last_refresh", "456")
    assert await db.meta_get("last_refresh") == "456"
