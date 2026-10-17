"""#74: unit tests for bf_price_monitor.history_merge (merge by data)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
import scrape

from bf_price_monitor.history_merge import (
    HistoryMergeError,
    dump_history_doc,
    load_history_doc,
    merge_history_docs,
    merge_product_entries,
    merge_product_entry,
)

KEY = "https://emag.ro/laptop/pd/ABC123"
OTHER = "https://flanco.ro/laptop.html"


def _obs(observed_at: str, price: float, stock: str = "in_stock") -> dict[str, Any]:
    return {
        "date": observed_at[:10],
        "observed_at": observed_at,
        "price": price,
        "stock_status": stock,
    }


def _entry(*history: dict[str, Any], **scalars: Any) -> dict[str, Any]:
    prices = [h["price"] for h in history]
    return {
        "title": "Laptop",
        "site": "emag",
        "history": list(history),
        "all_time_low": min(prices),
        "all_time_high": max(prices),
        "first_seen": history[0]["date"],
        **scalars,
    }


def _doc(**products: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "products": products}


OLD = _obs("2026-07-01T07:00:00+00:00", 3100.0)
BASE = _obs("2026-10-14T07:00:00+00:00", 2999.0)
A = _obs("2026-10-14T09:00:00+00:00", 2899.0)
B = _obs("2026-10-14T16:57:00+00:00", 2799.0)


def _observed(doc: dict[str, Any], key: str = KEY) -> list[str]:
    return [h["observed_at"] for h in doc["products"][key]["history"]]


def test_two_runs_from_the_same_base_keep_both_observations():
    base = _doc(**{KEY: _entry(BASE)})
    tip = _doc(**{KEY: _entry(BASE, A)})
    incoming = _doc(**{KEY: _entry(BASE, B)})

    merged = merge_history_docs(incoming, tip, base)

    assert _observed(merged) == [
        BASE["observed_at"],
        A["observed_at"],
        B["observed_at"],
    ]


def test_re_merging_the_same_incoming_is_idempotent():
    base = _doc(**{KEY: _entry(BASE)})
    incoming = _doc(**{KEY: _entry(BASE, B)})
    once = merge_history_docs(incoming, _doc(**{KEY: _entry(BASE, A)}), base)

    twice = merge_history_docs(incoming, once, base)

    assert twice == once


def test_without_a_base_the_merge_is_the_legacy_union():
    tip = _doc(**{KEY: _entry(BASE, A)})
    incoming = _doc(**{KEY: _entry(BASE, B)})

    merged = merge_history_docs(incoming, tip, None)

    assert merged["products"][KEY] == merge_product_entries(
        incoming["products"][KEY], tip["products"][KEY]
    )
    assert _observed(merged) == [
        BASE["observed_at"],
        A["observed_at"],
        B["observed_at"],
    ]


def test_incoming_scalar_fields_win():
    tip_entry = _entry(BASE, A, title="Old title", seller="eMAG", reference_price=1.0)
    incoming_entry = _entry(
        BASE, B, title="New title", seller=None, reference_price=2.0
    )

    merged = merge_product_entry(incoming_entry, tip_entry, _entry(BASE))

    assert merged["title"] == "New title"
    assert merged["seller"] is None
    assert merged["reference_price"] == 2.0


def test_lifetime_stats_take_min_max_across_both_sides():
    tip_entry = _entry(BASE, A, all_time_low=1500.0, first_seen="2026-09-01")
    incoming_entry = _entry(BASE, B, all_time_high=9999.0)

    merged = merge_product_entry(incoming_entry, tip_entry, _entry(BASE))

    assert merged["all_time_low"] == 1500.0
    assert merged["all_time_high"] == 9999.0
    assert merged["first_seen"] == "2026-09-01"


def test_one_sided_products_are_kept_as_is():
    tip_only = _entry(_obs("2026-10-14T09:00:00+00:00", 10.0))
    incoming_only = _entry(_obs("2026-10-14T16:57:00+00:00", 20.0), site="flanco")
    base = _doc(**{KEY: _entry(BASE)})
    tip = _doc(**{KEY: _entry(BASE), "tip-only": tip_only})
    incoming = _doc(**{KEY: _entry(BASE), OTHER: incoming_only})

    merged = merge_history_docs(incoming, tip, base)

    assert merged["products"]["tip-only"] == tip_only
    assert merged["products"][OTHER] == incoming_only
    assert list(merged["products"]) == [KEY, "tip-only", OTHER]


def test_product_in_tip_but_absent_from_incoming_is_kept():
    tip_entry = _entry(BASE, A)
    base = _doc(**{KEY: _entry(BASE), OTHER: _entry(BASE)})
    tip = _doc(**{KEY: _entry(BASE), OTHER: tip_entry})
    incoming = _doc(**{KEY: _entry(BASE, B)})

    merged = merge_history_docs(incoming, tip, base)

    assert merged["products"][OTHER] == tip_entry


def test_tip_equal_to_base_returns_incoming_unchanged_so_pruned_rows_stay_pruned():
    # The normal, race-free case: the scrape pruned OLD (older than 90 days)
    # and appended B. A plain union with the tip would put OLD back.
    # Lifetime stats survive pruning in real scrape output
    # (update_lifetime_stats), so incoming still carries OLD's high/first_seen.
    base = _doc(**{KEY: _entry(OLD, BASE)})
    incoming = _doc(
        **{KEY: _entry(BASE, B, all_time_high=3100.0, first_seen="2026-07-01")}
    )

    merged = merge_history_docs(incoming, copy.deepcopy(base), base)

    assert merged == incoming
    assert OLD["observed_at"] not in _observed(merged)


def test_three_way_keeps_tip_added_rows_while_honouring_incoming_pruning():
    base = _doc(**{KEY: _entry(OLD, BASE)})
    tip = _doc(**{KEY: _entry(OLD, BASE, A)})
    incoming = _doc(**{KEY: _entry(BASE, B)})

    merged = merge_history_docs(incoming, tip, base)

    assert _observed(merged) == [
        BASE["observed_at"],
        A["observed_at"],
        B["observed_at"],
    ]


def test_mismatched_schema_versions_refuse_to_merge():
    incoming = {"schema_version": 2, "products": {}}
    with pytest.raises(HistoryMergeError, match="schema_version"):
        merge_history_docs(incoming, _doc(), None)


def test_scrape_uses_the_extracted_merge_function():
    assert scrape._merge_product_entries is merge_product_entries


def test_unicode_round_trips_and_matches_save_history_bytes(tmp_path, monkeypatch):
    doc = _doc(**{KEY: _entry(BASE, title="Laptop ASUS Vivobook 15 – gri, ăîșț")})
    ours = tmp_path / "ours.json"
    theirs = tmp_path / "theirs.json"
    monkeypatch.setattr(scrape, "HISTORY_FILE", theirs)

    dump_history_doc(doc, ours)
    scrape.save_history(doc["products"])

    assert ours.read_bytes() == theirs.read_bytes()
    assert load_history_doc(ours.read_text(encoding="utf-8")) == doc


def test_legacy_bare_dict_is_wrapped_like_load_history():
    loaded = load_history_doc(json.dumps({KEY: _entry(BASE)}))

    assert loaded == _doc(**{KEY: _entry(BASE)})


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        json.dumps({"schema_version": 1, "products": []}),
        json.dumps({"schema_version": 1, "products": {KEY: {"title": "x"}}}),
        json.dumps({"schema_version": 1, "products": {KEY: {"history": [{"p": 1}]}}}),
    ],
)
def test_corrupt_documents_raise_history_merge_error(text):
    with pytest.raises(HistoryMergeError):
        load_history_doc(text)
