"""Tests for the pure section-resolution helpers.

These drive what ends up on the Library page: which collections/libraries a
section reads, how placeholder titles render, and cross-library sort order.
"""

from app import section_resolve as sr


# ---- _collection_keys / _section_libraries (back-compat + sanitizing) -------

def test_collection_keys_multi_mixed_shapes():
    cfg = {"collection_keys": [{"key": "1", "title": "A"}, "2", {"key": "None"}, "", None]}
    assert sr._collection_keys(cfg) == ["1", "2"]


def test_collection_keys_single_backcompat():
    assert sr._collection_keys({"collection_key": "5"}) == ["5"]


def test_collection_keys_empty():
    assert sr._collection_keys({}) == []


def test_section_libraries_multi_and_backcompat():
    assert sr._section_libraries({"library_sections": ["3", 4, "None", None]}) == ["3", "4"]
    assert sr._section_libraries({"library_section": "7"}) == ["7"]
    assert sr._section_libraries({}) == []


# ---- _apply_template --------------------------------------------------------

_PICKS = {
    "directors": [{"id": "1", "title": "Nolan"}, {"id": "2", "title": "Villeneuve"}],
    "genres": [{"id": "9", "title": "Sci-Fi"}],
    "actors": [],
}


def test_apply_template_singular_and_plural():
    assert sr._apply_template("Best of {director}", _PICKS) == "Best of Nolan, Villeneuve"
    assert sr._apply_template("{directors} picks", _PICKS) == "Nolan, Villeneuve picks"
    assert sr._apply_template("{genre} classics", _PICKS) == "Sci-Fi classics"


def test_apply_template_passthrough_and_unknown():
    assert sr._apply_template(None, _PICKS) is None
    assert sr._apply_template("No placeholders here", _PICKS) == "No placeholders here"
    assert sr._apply_template("{unknown}", _PICKS) == "{unknown}"  # left intact
    assert sr._apply_template("{actor}", _PICKS) == ""             # picked, but empty list


# ---- _sort_metas ------------------------------------------------------------

def _titles(metas):
    return [m["t"] for m in metas]


def test_sort_metas_numeric_desc_missing_last():
    metas = [{"t": "a", "year": 2000}, {"t": "b", "year": 1990}, {"t": "c", "year": None}]
    assert _titles(sr._sort_metas(metas, "year:desc")) == ["a", "b", "c"]


def test_sort_metas_numeric_asc():
    metas = [{"t": "a", "year": 2000}, {"t": "b", "year": 1990}, {"t": "c"}]
    # missing (no key) always sorts last regardless of direction
    assert _titles(sr._sort_metas(metas, "year:asc")) == ["b", "a", "c"]


def test_sort_metas_string_field_case_insensitive():
    metas = [{"t": "x", "titleSort": "banana"}, {"t": "y", "titleSort": "Apple"}]
    assert _titles(sr._sort_metas(metas, "titleSort:asc")) == ["y", "x"]


def test_sort_metas_unknown_field_unchanged():
    metas = [{"t": "a", "year": 1}, {"t": "b", "year": 2}]
    assert sr._sort_metas(metas, "bogus:desc") is metas


# ---- _pick_tags -------------------------------------------------------------

def test_pick_tags_pool_normalizes_ids_and_titles():
    cfg = {"genres": [{"id": 1, "title": "Action"}, {"id": 2, "title": "Drama"}], "genres_mode": "pool"}
    out = sr._pick_tags(cfg)
    assert out["genres"] == [{"id": "1", "title": "Action"}, {"id": "2", "title": "Drama"}]


def test_pick_tags_plain_values_become_id_title():
    out = sr._pick_tags({"actors": ["5"], "actors_mode": "pool"})
    assert out["actors"] == [{"id": "5", "title": "5"}]


def test_pick_tags_drops_none_ids():
    out = sr._pick_tags({"genres": [{"id": None, "title": "x"}, {"id": "3", "title": "ok"}]})
    assert out["genres"] == [{"id": "3", "title": "ok"}]


def test_pick_tags_random_mode_picks_subset_count():
    cfg = {"genres": [{"id": str(i), "title": str(i)} for i in range(5)],
           "genres_mode": "random", "genres_pick": 2}
    out = sr._pick_tags(cfg)
    assert len(out["genres"]) == 2
    assert all(o in [{"id": str(i), "title": str(i)} for i in range(5)] for o in out["genres"])
