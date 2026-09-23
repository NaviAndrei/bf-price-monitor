"""Independent verifier for the JSON -> SQLite history migration.

Does not import or call the migration script. Recomputes expected counts
directly from the source JSON so it cannot inherit a bug from
migrate_history_to_sqlite.py.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent

IN_STOCK_STATUSES = {"in_stock", "limited_stock", "supplier_stock"}
OUT_OF_STOCK_STATUSES = {"out_of_stock"}


def _derive_sku(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def _resolve_scraped_at(entry: dict) -> str:
    observed_at = entry.get("observed_at")
    if observed_at:
        return observed_at
    return f"{entry['date']}T00:00:00+00:00"


def _load_source(source: Path) -> dict:
    with source.open(encoding="utf-8") as f:
        return json.load(f)["products"]


def _expected_offer_scraped_ats(products: dict, retailer: str, sku: str) -> set[str]:
    """Union of resolved scraped_at values across every source product sharing this offer key."""
    result: set[str] = set()
    for url, product in products.items():
        if product["site"] != retailer or _derive_sku(url) != sku:
            continue
        for entry in product.get("history", []):
            result.add(_resolve_scraped_at(entry))
    return result


def verify(source: Path, target: Path, sample_size: int = 10, seed: int = 0) -> bool:
    ok = True
    products = _load_source(source)

    # 1/2: raw observation counts
    source_count = sum(len(p.get("history", [])) for p in products.values())
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    target_count = conn.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]

    # Independently recompute the expected count by deduplicating on
    # (retailer, sku, resolved scraped_at) — two source products can share an
    # offer key (see report) and collapse duplicate same-instant entries.
    dedup_keys: set[tuple[str, str, str]] = set()
    for url, product in products.items():
        retailer = product["site"]
        sku = _derive_sku(url)
        for entry in product.get("history", []):
            dedup_keys.add((retailer, sku, _resolve_scraped_at(entry)))
    expected_count = len(dedup_keys)

    print(f"Source raw observations (sum of len(history)): {source_count}")
    print(f"Target price_observations row count: {target_count}")
    print(
        f"Expected row count after (retailer, sku, scraped_at) dedup: {expected_count}"
    )
    if target_count == expected_count:
        print(
            f"PASS: target matches deduplicated expectation (delta from raw source: "
            f"{source_count - target_count})"
        )
    else:
        print(f"FAIL: target count {target_count} != expected {expected_count}")
        ok = False

    # integrity / foreign key checks
    integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
    integrity_ok = len(integrity_rows) == 1 and integrity_rows[0][0] == "ok"
    print(f"PRAGMA integrity_check: {[dict(r) for r in integrity_rows]}")
    if not integrity_ok:
        ok = False

    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    print(
        f"PRAGMA foreign_key_check: {[dict(r) for r in fk_rows] if fk_rows else 'no violations'}"
    )
    if fk_rows:
        ok = False

    # sample verification
    rng = random.Random(seed)
    sample_urls = rng.sample(list(products.keys()), min(sample_size, len(products)))
    print(f"\nSample verification ({len(sample_urls)} products):")
    for url in sample_urls:
        product = products[url]
        retailer = product["site"]
        sku = _derive_sku(url)
        offer_row = conn.execute(
            "SELECT id FROM offers WHERE retailer = ? AND sku = ?", (retailer, sku)
        ).fetchone()
        if offer_row is None:
            print(f"  FAIL: no offer found for {retailer}/{sku} ({url[:60]})")
            ok = False
            continue
        offer_id = offer_row["id"]
        actual = conn.execute(
            "SELECT COUNT(*) FROM price_observations WHERE offer_id = ?", (offer_id,)
        ).fetchone()[0]
        expected = len(_expected_offer_scraped_ats(products, retailer, sku))
        status = "OK" if actual == expected else "FAIL"
        if status == "FAIL":
            ok = False
        print(
            f"  {status}: {retailer}/{sku} -> expected {expected} observations, "
            f"found {actual}"
        )

    conn.close()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=REPO_ROOT / "data" / "price_history.json"
    )
    parser.add_argument(
        "--target", type=Path, default=REPO_ROOT / "data" / "price_history.db"
    )
    parser.add_argument("--sample-size", type=int, default=10)
    args = parser.parse_args()

    passed = verify(args.source, args.target, args.sample_size)
    print("\nRESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
