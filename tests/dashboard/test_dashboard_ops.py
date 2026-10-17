"""Slice B: ids, known sites, operations (pure) and the on-disk store."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import scrape

from bf_price_monitor.dashboard import ids, operations, sites, store
from bf_price_monitor.dashboard.operations import (
    ALREADY_APPLIED,
    APPLIED,
    Conflict,
    InvalidOperation,
)

# Every field the Phase 1 form never shows. An edit must carry these through
# untouched, whatever else changes (user note: cadence_minutes included).
HIDDEN: dict[str, Any] = {
    "owner": "someone-else",
    "track_all_time_low": False,
    "seller_policy": "trusted",
    "cadence_minutes": 45,
    "cooldown_hours": 6,
    "atl_policy": "aggressive",
    "drop_rule": "absolute",
    "drop_threshold": 12.5,
}
# Not valid against the current schema (additionalProperties: false), but the
# pure editor must still never drop what it does not understand.
UNKNOWN: dict[str, Any] = {
    "quiet_hours_start": 23,
    "mystery_key": {"nested": [1, 2, {"deep": True}]},
}


def _watch(site: str, query: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": ids.watch_id(site, query),
        "site": site,
        "query": query,
        "enabled": True,
        "channels": ["telegram", "ntfy"],
        **extra,
    }


def _doc(*watches: dict[str, Any]) -> dict[str, Any]:
    return {"watches": [copy.deepcopy(w) for w in watches]}


def _rich(site: str, query: str, **extra: Any) -> dict[str, Any]:
    return _watch(site, query, target_price=2500.0, **HIDDEN, **extra)


# --- ids and sites ---------------------------------------------------------


def test_watch_id_matches_migration_script():
    assert (
        ids.watch_id("emag", "laptop lenovo v15")
        == "babebbc9-2a56-5d0c-b57c-9ca8ad44d7b4"
    )


def test_known_sites_match_non_placeholder_scrapers():
    real = {
        name
        for name, fn in scrape.SCRAPERS.items()
        if not scrape.is_placeholder_scraper(fn)
    }
    assert set(sites.KNOWN_SITES) == real | {scrape.FANOUT_SITE}


def test_duplicate_key_ignores_case_and_whitespace():
    assert ids.duplicate_key("emag", "  Laptop   LENOVO v15 ") == ids.duplicate_key(
        "emag", "laptop lenovo v15"
    )
    assert ids.duplicate_key("emag", "x") != ids.duplicate_key("flanco", "x")


# --- add -------------------------------------------------------------------


def test_add_appends_a_watch_with_defaults_and_derived_id():
    op = operations.make_add("pcgarage", "monitor 27", target_price=900)
    status, new = operations.apply(_doc(_watch("emag", "a")), op)
    assert status == APPLIED
    added = new["watches"][-1]
    assert added["id"] == ids.watch_id("pcgarage", "monitor 27")
    assert added["site"] == "pcgarage"
    assert added["target_price"] == 900
    assert added["enabled"] is True
    assert added["cadence_minutes"] == 120
    assert len(new["watches"]) == 2


def test_apply_never_mutates_its_input():
    doc = _doc(_rich("emag", "a"))
    before = copy.deepcopy(doc)
    operations.apply(doc, operations.make_add("emag", "b"))
    operations.apply(doc, operations.make_set_enabled(doc["watches"][0], False))
    assert doc == before


def test_add_rejects_duplicate_id_with_different_content():
    existing = _watch("emag", "a", target_price=10.0)
    op = operations.make_add("emag", "a", target_price=99)
    with pytest.raises(Conflict):
        operations.apply(_doc(existing), op)


def test_add_rejects_near_duplicate_query():
    doc = _doc(_watch("emag", "laptop lenovo v15"))
    op = operations.make_add("emag", "Laptop  Lenovo V15")
    with pytest.raises(InvalidOperation, match="already watching"):
        operations.apply(doc, op)


def test_add_same_query_on_another_site_is_allowed():
    doc = _doc(_watch("emag", "a"))
    status, new = operations.apply(doc, operations.make_add("flanco", "a"))
    assert status == APPLIED and len(new["watches"]) == 2


def test_add_of_a_watch_already_present_is_a_noop():
    op = operations.make_add("emag", "a")
    _, once = operations.apply(_doc(), op)
    status, twice = operations.apply(once, op)
    assert status == ALREADY_APPLIED
    assert twice == once


# --- update / set_enabled / delete -----------------------------------------


def test_update_changes_only_the_edited_fields():
    current = _rich("emag", "a")
    doc = _doc(current)
    op = operations.make_update(current, {"target_price": 2000, "min_drop_percent": 5})
    status, new = operations.apply(doc, op)
    assert status == APPLIED
    w = new["watches"][0]
    assert w["target_price"] == 2000 and w["min_drop_percent"] == 5
    assert w["channels"] == current["channels"]


def test_update_with_none_removes_the_threshold():
    current = _rich("emag", "a")
    op = operations.make_update(current, {"target_price": None})
    _, new = operations.apply(_doc(current), op)
    assert "target_price" not in new["watches"][0]


def test_update_cannot_change_site_or_query():
    current = _watch("emag", "a")
    for forbidden in ("site", "query", "id", "owner", "cadence_minutes", "enabled"):
        with pytest.raises(InvalidOperation):
            operations.make_update(current, {forbidden: "x"})


@pytest.mark.parametrize("kind", ["update", "set_enabled_off", "delete"])
def test_edits_round_trip_every_field_the_form_does_not_show(kind: str):
    """update, set_enabled and delete never lose cadence_minutes, track_all_time_low,
    seller_policy, owner, atl_policy (and the other hidden fields), nor any
    unknown key, on the edited watch or on its neighbours."""
    target = _rich("emag", "target", **UNKNOWN)
    other = _rich("flanco", "neighbour", **UNKNOWN)
    doc = _doc(target, other)
    if kind == "update":
        op = operations.make_update(target, {"min_drop_percent": 7.5})
    elif kind == "set_enabled_off":
        op = operations.make_set_enabled(target, False)
    else:
        op = operations.make_delete(target)
    status, new = operations.apply(doc, op)
    assert status == APPLIED

    # The neighbour is byte-for-byte identical, key order included.
    neighbour = next(w for w in new["watches"] if w["id"] == other["id"])
    assert json.dumps(neighbour) == json.dumps(other)

    if kind == "delete":
        assert all(w["id"] != target["id"] for w in new["watches"])
        return
    edited = next(w for w in new["watches"] if w["id"] == target["id"])
    for key, value in {**HIDDEN, **UNKNOWN}.items():
        assert edited[key] == value, key
    assert edited["channels"] == target["channels"]
    assert edited["target_price"] == target["target_price"]


def test_update_after_disable_and_enable_still_round_trips_everything():
    target = _rich("emag", "target", **UNKNOWN)
    doc = _doc(target)
    _, off = operations.apply(doc, operations.make_set_enabled(target, False))
    off_watch = off["watches"][0]
    _, on = operations.apply(off, operations.make_set_enabled(off_watch, True))
    assert on["watches"][0] == target


def test_op_already_applied_is_noop():
    current = _watch("emag", "a")
    op = operations.make_set_enabled(current, False)
    _, once = operations.apply(_doc(current), op)
    status, twice = operations.apply(once, op)
    assert status == ALREADY_APPLIED
    assert twice == once


def test_delete_of_an_already_deleted_watch_is_a_noop():
    gone = _watch("emag", "gone")
    keep = _watch("emag", "keep")
    status, new = operations.apply(_doc(keep), operations.make_delete(gone))
    assert status == ALREADY_APPLIED
    assert new == _doc(keep)


def test_op_on_changed_watch_is_conflict():
    seen = _watch("emag", "a", target_price=100.0)
    changed = _watch("emag", "a", target_price=111.0)
    op = operations.make_update(seen, {"min_drop_percent": 3})
    with pytest.raises(Conflict, match="changed"):
        operations.apply(_doc(changed), op)


def test_op_on_a_missing_watch_is_conflict():
    seen = _watch("emag", "a")
    with pytest.raises(Conflict, match="no longer exists"):
        operations.apply(
            _doc(_watch("emag", "b")), operations.make_set_enabled(seen, False)
        )


def test_delete_conflicts_when_the_watch_changed_since_it_was_shown():
    seen = _watch("emag", "a")
    changed = _watch("emag", "a", target_price=5.0)
    with pytest.raises(Conflict):
        operations.apply(_doc(changed), operations.make_delete(seen))


# --- input validation ------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["", "   ", "a" * 201, "line\nbreak", "tab\there", "nul\x00byte", "del\x7f"],
)
def test_query_length_and_control_chars_rejected(query: str):
    with pytest.raises(InvalidOperation):
        operations.make_add("emag", query)


@pytest.mark.parametrize(
    "price", [0, -1, -0.01, float("nan"), float("inf"), True, "10"]
)
def test_price_must_be_positive(price: Any):
    with pytest.raises(InvalidOperation):
        operations.make_add("emag", "a", target_price=price)
    with pytest.raises(InvalidOperation):
        operations.make_update(_watch("emag", "a"), {"target_price": price})


def test_min_drop_percent_must_be_in_range():
    for bad in (0, -5, 100.5):
        with pytest.raises(InvalidOperation):
            operations.make_add("emag", "a", min_drop_percent=bad)
    operations.make_add("emag", "a", min_drop_percent=100)


def test_unknown_channel_rejected():
    with pytest.raises(InvalidOperation):
        operations.make_add("emag", "a", channels=["telegram", "carrier-pigeon"])
    with pytest.raises(InvalidOperation):
        operations.make_add("emag", "a", channels=[])
    with pytest.raises(InvalidOperation):
        operations.make_add("emag", "a", channels=["ntfy", "ntfy"])


def test_unknown_site_rejected_and_altex_is_not_offered():
    with pytest.raises(InvalidOperation):
        operations.make_add("altex", "a")
    with pytest.raises(InvalidOperation):
        operations.make_add("ebay", "a")
    operations.make_add("all", "a")


def test_hand_edited_journal_op_is_revalidated_on_apply():
    op = operations.make_add("emag", "a")
    op["watch"]["target_price"] = -3
    with pytest.raises(InvalidOperation):
        operations.apply(_doc(), op)
    with pytest.raises(InvalidOperation):
        operations.apply(_doc(), {"op": "explode"})


def test_describe_builds_the_commit_subject():
    w = _watch("emag", "laptop lenovo v15")
    assert (
        operations.describe(operations.make_add("emag", "laptop lenovo v15"))
        == 'chore(watchlist): add emag "laptop lenovo v15"'
    )
    assert operations.describe(operations.make_update(w, {"target_price": 1})) == (
        'chore(watchlist): update emag "laptop lenovo v15"'
    )
    assert operations.describe(operations.make_set_enabled(w, False)) == (
        'chore(watchlist): disable emag "laptop lenovo v15"'
    )
    assert operations.describe(operations.make_set_enabled(w, True)) == (
        'chore(watchlist): enable emag "laptop lenovo v15"'
    )
    assert operations.describe(operations.make_delete(w)) == (
        'chore(watchlist): remove emag "laptop lenovo v15"'
    )


# --- store -----------------------------------------------------------------


def test_invalid_result_never_written(tmp_path: Path):
    path = tmp_path / "watchlist.json"
    good = _doc(_watch("emag", "a"))
    store.save(path, good)
    before = path.read_bytes()
    bad = _doc(_watch("emag", "a", target_price=-1))
    with pytest.raises(store.StoreError):
        store.save(path, bad)
    assert path.read_bytes() == before


def test_deleting_the_last_watch_is_refused_by_the_schema(tmp_path: Path):
    only = _watch("emag", "a")
    _, empty = operations.apply(_doc(only), operations.make_delete(only))
    with pytest.raises(store.StoreError):
        store.save(tmp_path / "w.json", empty)


def test_store_round_trips_every_schema_field_through_the_file(tmp_path: Path):
    target = _rich("emag", "target")
    other = _rich("flanco", "neighbour", min_drop_percent=4.0)
    path = tmp_path / "w.json"
    store.save(path, _doc(target, other))
    loaded = store.load(path)
    _, new = operations.apply(
        loaded, operations.make_set_enabled(loaded["watches"][0], False)
    )
    store.save(path, new)
    again = store.load(path)
    for key, value in HIDDEN.items():
        assert again["watches"][0][key] == value, key
    assert again["watches"][1] == other
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert b"\r\n" not in path.read_bytes()


def test_store_refuses_unknown_keys_instead_of_dropping_them(tmp_path: Path):
    path = tmp_path / "w.json"
    store.save(path, _doc(_watch("emag", "a")))
    before = path.read_bytes()
    with pytest.raises(store.StoreError):
        store.save(path, _doc(_watch("emag", "a", mystery_key=1)))
    assert path.read_bytes() == before


def test_store_rejects_the_legacy_flat_array_shape(tmp_path: Path):
    path = tmp_path / "w.json"
    path.write_text('[{"site": "emag", "query": "a"}]', encoding="utf-8")
    with pytest.raises(store.StoreError, match="legacy"):
        store.load(path)


def test_store_load_reports_unparseable_json(tmp_path: Path):
    path = tmp_path / "w.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(store.StoreError):
        store.load(path)
