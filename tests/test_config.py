"""Tests for the live config singleton (app/config.py Settings).

File-only precedence: the environment seeds config.json once on first load, then
the file is the sole source of truth. Values are read as attributes and edited via
``update`` (which persists). Each test uses a fresh Settings pointed at a temp file
so it never touches the shared singleton or a real /data/config.json.
"""

import json

from app.config import Settings, mask_secret


def test_seeds_from_env_when_no_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("PLEX_URL", "http://seed.test")
    monkeypatch.setenv("MDBLIST_API_KEY", "seedkey")
    s = Settings(path=str(path)).load()
    assert s.PLEX_URL == "http://seed.test"
    assert s.MDBLIST_API_KEY == "seedkey"
    # The seed was written to disk.
    assert path.exists()
    assert json.loads(path.read_text())["PLEX_URL"] == "http://seed.test"


def test_file_wins_over_env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"PLEX_URL": "http://file.test", "PLEX_TOKEN": "filetok"}))
    monkeypatch.setenv("PLEX_URL", "http://env.test")  # ignored: file already exists
    s = Settings(path=str(path)).load()
    assert s.PLEX_URL == "http://file.test"
    assert s.PLEX_TOKEN == "filetok"


def test_update_persists_and_reads_back(tmp_path):
    path = tmp_path / "config.json"
    s = Settings(path=str(path)).load()
    s.update({"OVERSEERR_URL": "http://o.test", "OVERSEERR_API_KEY": "k"})
    assert s.OVERSEERR_URL == "http://o.test"
    # A fresh load from the same file sees the persisted values.
    s2 = Settings(path=str(path)).load()
    assert s2.OVERSEERR_API_KEY == "k"


def test_update_ignores_unknown_keys(tmp_path):
    s = Settings(path=str(tmp_path / "c.json")).load()
    s.update({"NOT_A_KEY": "x", "PLEX_URL": "http://p"})
    assert s.PLEX_URL == "http://p"
    assert "NOT_A_KEY" not in s._values


def test_as_masked(tmp_path):
    s = Settings(path=str(tmp_path / "c.json")).load()
    s.update({"PLEX_URL": "http://p", "PLEX_TOKEN": "supersecrettoken", "MDBLIST_API_KEY": ""})
    m = s.as_masked()
    assert m["PLEX_URL"] == "http://p"                 # URL shown in the clear
    assert m["PLEX_TOKEN"] != "supersecrettoken"       # secret masked
    assert m["PLEX_TOKEN_set"] is True
    assert m["MDBLIST_API_KEY_set"] is False           # empty secret -> not set
    assert "PLEX_URL_set" not in m                     # non-secret has no _set flag


def test_corrupt_file_reseeds_from_env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    monkeypatch.setenv("PLEX_TOKEN", "envtok")
    s = Settings(path=str(path)).load()
    assert s.PLEX_TOKEN == "envtok"


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("short") == "•••••"
    masked = mask_secret("abcdefghijkl")
    assert masked.startswith("abcd") and masked.endswith("ijkl")
