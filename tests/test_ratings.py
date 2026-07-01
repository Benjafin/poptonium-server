"""Tests for the rating formula and config normalization (pure logic).

These are the highest-risk functions: they collapse many per-source scores into
the one canonical 0-100 number used for sorting and section minimums, so a subtle
regression here silently mis-ranks the whole library.
"""

from app import ratings


# ---- _parse_sources ---------------------------------------------------------

def test_parse_sources_extracts_supported_and_aggregate():
    item = {
        "score": 74,  # top-level = mdblist aggregate
        "ratings": [
            {"source": "imdb", "score": 8.1, "votes": 1200},
            {"source": "tomatoes", "score": 91, "votes": 30},
            {"source": "mdblist", "score": 99},   # skipped in the list (aggregate comes from top-level)
            {"source": "bogus", "score": 50},      # unsupported → dropped
            {"source": "tmdb", "score": None},      # no score → dropped
        ],
    }
    out = ratings._parse_sources(item)
    assert out["imdb"] == {"score": 8.1, "votes": 1200}
    assert out["tomatoes"] == {"score": 91.0, "votes": 30}
    assert out["mdblist"] == {"score": 74.0, "votes": None}
    assert "bogus" not in out
    assert "tmdb" not in out


def test_parse_sources_votes_default_zero():
    out = ratings._parse_sources({"ratings": [{"source": "imdb", "score": 7}]})
    assert out["imdb"] == {"score": 7.0, "votes": 0}


# ---- effective_sources (missing-mdblist policy) -----------------------------

def _cfg(missing="average", **formula):
    return {"formula": {"missing_mdblist": missing, **formula}}


def test_effective_sources_keeps_present_mdblist():
    src = {"mdblist": {"score": 80, "votes": None}, "imdb": {"score": 90}}
    assert ratings.effective_sources(src, _cfg()) is src  # unchanged, same object


def test_effective_sources_averages_when_mdblist_missing():
    src = {"imdb": {"score": 80}, "tmdb": {"score": 70}}
    out = ratings.effective_sources(src, _cfg("average"))
    assert out["mdblist"] == {"score": 75.0, "votes": None}


def test_effective_sources_treats_zero_mdblist_as_missing():
    src = {"mdblist": {"score": 0, "votes": None}, "imdb": {"score": 60}, "tmdb": {"score": 80}}
    out = ratings.effective_sources(src, _cfg("average"))
    assert out["mdblist"]["score"] == 70.0


def test_effective_sources_zero_policy_leaves_missing():
    src = {"imdb": {"score": 80}}
    out = ratings.effective_sources(src, _cfg("zero"))
    assert "mdblist" not in out


def test_effective_sources_no_other_sources_unchanged():
    src = {"mdblist": {"score": 0, "votes": None}}
    out = ratings.effective_sources(src, _cfg("average"))
    assert out is src  # nothing to average from


# ---- compute_rating ---------------------------------------------------------

def test_compute_rating_preset_mdblist_returns_aggregate():
    src = {"mdblist": {"score": 72, "votes": None}, "imdb": {"score": 90}}
    assert ratings.compute_rating(src, _cfg(preset="mdblist")) == 72.0


def test_compute_rating_preset_mdblist_synthesizes_when_missing():
    # No aggregate, but the averaging policy fills it before returning.
    src = {"imdb": {"score": 80}, "tmdb": {"score": 60}}
    assert ratings.compute_rating(src, _cfg("average", preset="mdblist")) == 70.0


def test_compute_rating_custom_weighted_average():
    src = {"imdb": {"score": 80, "votes": 0}, "tmdb": {"score": 70, "votes": 0}}
    cfg = _cfg("zero", preset="custom", vote_aware=False, weights={"imdb": 1, "tmdb": 1})
    assert ratings.compute_rating(src, cfg) == 75.0


def test_compute_rating_custom_vote_aware_confidence():
    # conf = votes / (votes + min_votes): imdb 3000/(3000+1000)=0.75, tmdb 100/(100+300)=0.25
    src = {"imdb": {"score": 80, "votes": 3000}, "tmdb": {"score": 70, "votes": 100}}
    cfg = _cfg("zero", preset="custom", vote_aware=True,
               weights={"imdb": 1, "tmdb": 1}, min_votes={"imdb": 1000, "tmdb": 300})
    # (0.75*80 + 0.25*70) / (0.75+0.25) = 77.5
    assert ratings.compute_rating(src, cfg) == 77.5


def test_compute_rating_custom_falls_back_to_aggregate_when_no_weights():
    src = {"mdblist": {"score": 66, "votes": None}, "imdb": {"score": 80, "votes": 10}}
    cfg = _cfg("zero", preset="custom", weights={})  # imdb weight 0 → nothing usable
    assert ratings.compute_rating(src, cfg) == 66.0


def test_compute_rating_none_when_nothing_available():
    cfg = _cfg("zero", preset="mdblist")
    assert ratings.compute_rating({}, cfg) is None


# ---- display-group normalization --------------------------------------------

def test_norm_display_groups_dedups_and_sanitizes():
    groups = [
        {"visibility": "weird", "sources": ["imdb", "bogus", "tomatoes"]},  # vis→always, bogus dropped
        {"visibility": "fallback", "sources": ["imdb", "tmdb"]},            # imdb already used → only tmdb
        {"visibility": "always", "sources": ["nope"]},                      # empty after filtering → dropped
        "not-a-dict",                                                        # skipped
    ]
    out = ratings._norm_display_groups(groups)
    assert out == [
        {"visibility": "always", "sources": ["imdb", "tomatoes"]},
        {"visibility": "fallback", "sources": ["tmdb"]},
    ]


def test_flatten_groups_preserves_order_unique():
    groups = [
        {"visibility": "always", "sources": ["imdb", "tomatoes"]},
        {"visibility": "fallback", "sources": ["tmdb"]},
    ]
    assert ratings._flatten_groups(groups) == ["imdb", "tomatoes", "tmdb"]
