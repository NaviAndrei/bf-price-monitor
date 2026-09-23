from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from bf_price_monitor.domain.models import Observation

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS canonical_products (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS offers (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES canonical_products(id),
    retailer TEXT NOT NULL,
    sku TEXT NOT NULL,
    url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(retailer, sku)
);

CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id TEXT NOT NULL REFERENCES offers(id),
    price REAL NOT NULL,
    original_price REAL,
    in_stock INTEGER NOT NULL,
    scraped_at TIMESTAMP NOT NULL,
    UNIQUE(offer_id, scraped_at)
);

CREATE INDEX IF NOT EXISTS idx_price_observations_offer_scraped
    ON price_observations(offer_id, scraped_at);
"""


def init_db(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite store with WAL mode and schema applied."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _upsert_product(
    db: sqlite3.Connection, product_id: str, title: str, category: str | None
) -> None:
    db.execute(
        """
        INSERT INTO canonical_products (id, title, category)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            category = COALESCE(excluded.category, canonical_products.category)
        """,
        (product_id, title, category),
    )


def _upsert_offer(
    db: sqlite3.Connection,
    offer_id: str,
    product_id: str,
    retailer: str,
    sku: str,
    url: str,
) -> str:
    db.execute(
        """
        INSERT INTO offers (id, product_id, retailer, sku, url, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(retailer, sku) DO UPDATE SET
            product_id = excluded.product_id,
            url = excluded.url,
            is_active = 1
        """,
        (offer_id, product_id, retailer, sku, url),
    )
    row = db.execute(
        "SELECT id FROM offers WHERE retailer = ? AND sku = ?", (retailer, sku)
    ).fetchone()
    return row["id"]


def _upsert_price_observation(
    db: sqlite3.Connection,
    offer_id: str,
    price: float,
    original_price: float | None,
    in_stock: bool,
    scraped_at: str,
) -> None:
    db.execute(
        """
        INSERT INTO price_observations
            (offer_id, price, original_price, in_stock, scraped_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(offer_id, scraped_at) DO UPDATE SET
            price = excluded.price,
            original_price = excluded.original_price,
            in_stock = excluded.in_stock
        """,
        (offer_id, price, original_price, int(in_stock), scraped_at),
    )


def record_observation(
    db: sqlite3.Connection, obs: dict[str, Any] | Observation
) -> None:
    """Atomically upsert the product/offer and record the observation. Idempotent."""
    if isinstance(obs, Observation):
        with db:
            _upsert_price_observation(
                db,
                str(obs.offer_id),
                float(obs.price),
                float(obs.reference_price) if obs.reference_price is not None else None,
                obs.in_stock,
                obs.observed_at.isoformat(),
            )
        return

    retailer = obs["retailer"]
    sku = obs["sku"]
    url = obs["url"]
    title = obs["title"]
    product_id = obs.get("product_id") or str(uuid5(NAMESPACE_URL, f"product:{title}"))
    offer_id = obs.get("offer_id") or str(
        uuid5(NAMESPACE_URL, f"offer:{retailer}:{sku}")
    )
    scraped_at = obs["scraped_at"]
    if isinstance(scraped_at, datetime):
        scraped_at = scraped_at.isoformat()

    with db:
        _upsert_product(db, product_id, title, obs.get("category"))
        resolved_offer_id = _upsert_offer(db, offer_id, product_id, retailer, sku, url)
        _upsert_price_observation(
            db,
            resolved_offer_id,
            float(obs["price"]),
            float(obs["original_price"])
            if obs.get("original_price") is not None
            else None,
            bool(obs["in_stock"]),
            scraped_at,
        )


def get_latest_price(
    db: sqlite3.Connection, retailer: str, sku: str
) -> dict[str, Any] | None:
    """Return the most recent observation for a retailer/sku offer, None if unseen."""
    row = db.execute(
        """
        SELECT o.id AS offer_id, o.retailer, o.sku, o.url,
               p.id AS product_id, p.title, p.category,
               po.price, po.original_price, po.in_stock, po.scraped_at
        FROM offers o
        JOIN canonical_products p ON p.id = o.product_id
        LEFT JOIN price_observations po ON po.offer_id = o.id
        WHERE o.retailer = ? AND o.sku = ?
        ORDER BY po.scraped_at DESC
        LIMIT 1
        """,
        (retailer, sku),
    ).fetchone()
    if row is None or row["scraped_at"] is None:
        return None
    return dict(row)


def get_price_history(
    db: sqlite3.Connection, offer_id: str, days: int = 30
) -> list[dict[str, Any]]:
    """Return observations for an offer from the last `days` days, oldest first."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = db.execute(
        """
        SELECT price, original_price, in_stock, scraped_at
        FROM price_observations
        WHERE offer_id = ? AND scraped_at >= ?
        ORDER BY scraped_at ASC
        """,
        (offer_id, cutoff),
    ).fetchall()
    return [dict(row) for row in rows]
