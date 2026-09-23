"""Migrate data/price_history.json into the SQLite store from T-19.

Read-only against the source JSON. Safe to re-run: record_observation()
upserts on (retailer, sku) and (offer_id, scraped_at), so migrating twice
produces the same row counts.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bf_price_monitor.storage.sqlite import init_db, record_observation  # noqa: E402

IN_STOCK_STATUSES = {"in_stock", "limited_stock", "supplier_stock"}
OUT_OF_STOCK_STATUSES = {"out_of_stock"}


def _derive_sku(url: str) -> str:
    """Last non-empty path segment of the product URL, e.g. '.../pd/DX1TPW3BM' -> 'DX1TPW3BM'."""
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def _resolve_scraped_at(entry: dict) -> str:
    """Prefer the full observed_at timestamp; fall back to midnight UTC of date."""
    observed_at = entry.get("observed_at")
    if observed_at:
        return observed_at
    return f"{entry['date']}T00:00:00+00:00"


def _resolve_in_stock(entry: dict) -> bool:
    """Missing stock_status predates stock tracking (2026-09-06/07) -> assume available."""
    status = entry.get("stock_status")
    if status is None:
        return True
    if status in IN_STOCK_STATUSES:
        return True
    if status in OUT_OF_STOCK_STATUSES:
        return False
    raise ValueError(f"unknown stock_status value: {status!r}")


def migrate(source: Path, target: Path, dry_run: bool) -> dict:
    with source.open(encoding="utf-8") as f:
        data = json.load(f)
    products = data["products"]

    products_processed = 0
    observations_inserted = 0
    skipped: list[tuple[str, str]] = []

    db = None if dry_run else init_db(target)

    start = time.monotonic()
    try:
        for url, product in products.items():
            try:
                retailer = product["site"]
                title = product["title"]
                sku = _derive_sku(url)
                for entry in product.get("history", []):
                    observations_inserted += 1
                    if dry_run:
                        continue
                    obs = {
                        "sku": sku,
                        "title": title,
                        "price": entry["price"],
                        "in_stock": _resolve_in_stock(entry),
                        "retailer": retailer,
                        "url": url,
                        "scraped_at": _resolve_scraped_at(entry),
                    }
                    record_observation(db, obs)
                products_processed += 1
            except Exception as exc:  # noqa: BLE001 - one bad product must not abort the run
                skipped.append((url, str(exc)))
    finally:
        if db is not None:
            db.close()

    elapsed = time.monotonic() - start
    return {
        "products_processed": products_processed,
        "observations_inserted": observations_inserted,
        "skipped": skipped,
        "elapsed_seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=REPO_ROOT / "data" / "price_history.json"
    )
    parser.add_argument(
        "--target", type=Path, default=REPO_ROOT / "data" / "price_history.db"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    summary = migrate(args.source, args.target, args.dry_run)

    print(f"{'[dry-run] ' if args.dry_run else ''}Migration summary:")
    print(f"  products processed: {summary['products_processed']}")
    print(f"  observations inserted: {summary['observations_inserted']}")
    print(f"  products skipped: {len(summary['skipped'])}")
    for url, reason in summary["skipped"]:
        print(f"    - {url}: {reason}")
    print(f"  elapsed: {summary['elapsed_seconds']:.2f}s")

    return 1 if summary["skipped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
