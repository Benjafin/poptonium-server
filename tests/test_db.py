"""Example of a DB-backed test.

Pattern: point `app.db.DB_PATH` at a fresh temp file per test via monkeypatch, so
each test gets an isolated SQLite database and the schema is built from scratch.
`get_db()` creates the tables on first connect, so there's no separate setup.
"""

import app.db as db


async def test_meta_get_set_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))

    assert await db.meta_get("missing") is None

    await db.meta_set("last_refresh", "123")
    assert await db.meta_get("last_refresh") == "123"

    # INSERT OR REPLACE semantics: setting again overwrites.
    await db.meta_set("last_refresh", "456")
    assert await db.meta_get("last_refresh") == "456"
