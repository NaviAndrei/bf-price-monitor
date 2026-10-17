"""Merge two copies of data/price_history.json by data, never by text (#74).

Two monitor runs that start from the same commit each append one observation
per product to the same JSON arrays, so git sees overlapping edits it cannot
merge. This module merges the documents instead:

* **Observations merge three ways.** Given the commit the incoming scrape
  started from (``base``), a row is kept when the tip has it and the incoming
  run did not remove it, or when the incoming run added it. That keeps the
  other run's new rows *and* honours the incoming run's pruning
  (``prune_history`` in scripts/scrape.py), which a plain union would undo.
  Without a base this degrades to :func:`merge_product_entries`, the union
  scrape.py has always used for duplicate keys.
* **Products are a union.** The scrape never deletes a product, so a product
  present on one side only is kept as it is.
* **The incoming entry wins scalar fields** (title, seller, reference_price,
  ...); lifetime stats take min/max across both sides.

Stdlib only: scripts/persist_history.py imports this from the runner's bare
system Python, which has none of the project's dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HISTORY_SCHEMA_VERSION = 1

Entry = dict[str, Any]
Doc = dict[str, Any]
_Signature = tuple[Any, Any, Any, Any]


class HistoryMergeError(ValueError):
    """A history document is unreadable or cannot be merged safely."""


def _signature(h: Entry) -> _Signature:
    return (h["date"], h.get("observed_at"), h["price"], h["stock_status"])


def merge_product_entries(a: Entry, b: Entry) -> Entry:
    """Union of two entries for one product; ``a``'s scalar fields win.

    Moved verbatim from scripts/scrape.py (``_merge_product_entries``), which
    still uses it for keys that canonicalize to the same URL.
    """
    merged = dict(a)
    history = list(a.get("history", []))
    seen = {
        (h["date"], h.get("observed_at"), h["price"], h["stock_status"])
        for h in history
    }
    for h in b.get("history", []):
        signature = (h["date"], h.get("observed_at"), h["price"], h["stock_status"])
        if signature not in seen:
            history.append(h)
            seen.add(signature)
    history.sort(key=lambda h: (h["date"], h.get("observed_at") or ""))
    merged["history"] = history

    lows = [e["all_time_low"] for e in (a, b) if "all_time_low" in e]
    if lows:
        merged["all_time_low"] = min(lows)
    highs = [e["all_time_high"] for e in (a, b) if "all_time_high" in e]
    if highs:
        merged["all_time_high"] = max(highs)
    first_seens = [e["first_seen"] for e in (a, b) if "first_seen" in e]
    if first_seens:
        merged["first_seen"] = min(first_seens)
    return merged


def merge_product_entry(
    incoming: Entry, tip: Entry | None, base: Entry | None
) -> Entry:
    """Three-way merge of one product's entry; see the module docstring."""
    if tip is None:
        return incoming
    if base is None:
        return merge_product_entries(incoming, tip)
    base_sigs = {_signature(h) for h in base.get("history", [])}
    incoming_sigs = {_signature(h) for h in incoming.get("history", [])}
    removed_by_incoming = base_sigs - incoming_sigs
    kept_from_tip = [
        h for h in tip.get("history", []) if _signature(h) not in removed_by_incoming
    ]
    added_by_incoming = [
        h for h in incoming.get("history", []) if _signature(h) not in base_sigs
    ]
    return merge_product_entries(
        {**incoming, "history": added_by_incoming},
        {**tip, "history": kept_from_tip},
    )


def merge_history_docs(incoming: Doc, tip: Doc, base: Doc | None) -> Doc:
    """Merge whole documents; tip product order first, then new products."""
    versions = {incoming["schema_version"], tip["schema_version"]}
    if len(versions) != 1:
        raise HistoryMergeError(
            f"schema_version differs between incoming and tip: {sorted(versions)}"
        )
    incoming_products: dict[str, Entry] = incoming["products"]
    tip_products: dict[str, Entry] = tip["products"]
    base_products: dict[str, Entry] | None = None if base is None else base["products"]

    merged: dict[str, Entry] = {}
    for key, tip_entry in tip_products.items():
        if key not in incoming_products:
            merged[key] = tip_entry
            continue
        base_entry = None if base_products is None else base_products.get(key)
        merged[key] = merge_product_entry(incoming_products[key], tip_entry, base_entry)
    for key, incoming_entry in incoming_products.items():
        if key not in merged:
            merged[key] = incoming_entry
    return {"schema_version": versions.pop(), "products": merged}


def load_history_doc(text: str) -> Doc:
    """Parse and shape-check a history document.

    A pre-versioning file (a bare ``{url: entry}`` dict) is wrapped the same
    way scrape.load_history() does. Raises HistoryMergeError instead of a
    KeyError deep inside a merge.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HistoryMergeError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise HistoryMergeError("top level must be a JSON object")
    if "schema_version" not in data:
        data = {"schema_version": HISTORY_SCHEMA_VERSION, "products": data}
    products = data.get("products")
    if not isinstance(products, dict):
        raise HistoryMergeError("'products' must be a JSON object")
    for key, entry in products.items():
        history = entry.get("history") if isinstance(entry, dict) else None
        if not isinstance(history, list):
            raise HistoryMergeError(f"product {key!r} has no 'history' list")
        for row in history:
            if not isinstance(row, dict) or not {"date", "price", "stock_status"} <= (
                row.keys()
            ):
                raise HistoryMergeError(f"product {key!r} has a malformed observation")
    return {"schema_version": data["schema_version"], "products": products}


def dump_history_doc(doc: Doc, path: Path) -> None:
    """Write exactly what scrape.save_history() writes for the same products."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"schema_version": doc["schema_version"], "products": doc["products"]},
            f,
            indent=2,
            ensure_ascii=False,
        )
