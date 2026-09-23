from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from bf_price_monitor.domain.models import Observation
from bf_price_monitor.storage.sqlite import (
    get_latest_price,
    get_price_history,
    init_db,
    record_observation,
)


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "history.sqlite3")
    yield conn
    conn.close()


def _obs(
    price=100.0, in_stock=True, scraped_at="2026-09-20T10:00:00+00:00", **overrides
):
    base = {
        "sku": "SKU-1",
        "title": "Laptop Lenovo V15",
        "price": price,
        "in_stock": in_stock,
        "retailer": "emag",
        "url": "https://emag.ro/laptop-lenovo-v15/pd/ABC123",
        "scraped_at": scraped_at,
        "currency": "RON",
    }
    base.update(overrides)
    return base


def test_init_db_creates_tables_idempotently(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    conn1 = init_db(db_path)
    conn1.close()
    conn2 = init_db(db_path)  # must not raise on re-init
    tables = {
        row["name"]
        for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn2.close()
    assert {"canonical_products", "offers", "price_observations"} <= tables


def test_wal_mode_is_enabled(db):
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_are_enforced(db):
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_record_observation_dict_inserts_product_offer_and_observation(db):
    record_observation(db, _obs())

    latest = get_latest_price(db, "emag", "SKU-1")
    assert latest is not None
    assert latest["price"] == 100.0
    assert latest["in_stock"] == 1
    assert latest["title"] == "Laptop Lenovo V15"


def test_record_observation_duplicate_call_is_idempotent(db):
    record_observation(db, _obs())
    record_observation(db, _obs())  # same offer + same scraped_at, must not raise

    products = db.execute("SELECT COUNT(*) FROM canonical_products").fetchone()[0]
    offers = db.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    observations = db.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
    assert (products, offers, observations) == (1, 1, 1)


def test_record_observation_updates_price_on_same_timestamp_retry(db):
    record_observation(db, _obs(price=100.0))
    record_observation(
        db, _obs(price=95.0)
    )  # retry with corrected price, same scraped_at

    latest = get_latest_price(db, "emag", "SKU-1")
    assert latest["price"] == 95.0


def test_record_observation_new_scraped_at_adds_new_row(db):
    record_observation(db, _obs(price=100.0, scraped_at="2026-09-20T10:00:00+00:00"))
    record_observation(db, _obs(price=95.0, scraped_at="2026-09-21T10:00:00+00:00"))

    count = db.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0]
    assert count == 2
    latest = get_latest_price(db, "emag", "SKU-1")
    assert latest["price"] == 95.0


def test_get_latest_price_returns_none_for_unknown_offer(db):
    assert get_latest_price(db, "emag", "does-not-exist") is None


def test_get_price_history_filters_by_days_and_orders_ascending(db):
    now = datetime.now(UTC)
    record_observation(
        db, _obs(price=120.0, scraped_at="2020-01-01T00:00:00+00:00")
    )  # outside window
    record_observation(db, _obs(price=110.0, scraped_at=now.isoformat()))

    latest = get_latest_price(db, "emag", "SKU-1")
    history = get_price_history(db, latest["offer_id"], days=30)

    assert len(history) == 1
    assert history[0]["price"] == 110.0


def test_record_observation_from_pydantic_observation_requires_existing_offer(db):
    record_observation(db, _obs())
    offer_id = get_latest_price(db, "emag", "SKU-1")["offer_id"]

    observation = Observation(
        offer_id=offer_id,
        price=Decimal("90.00"),
        in_stock=True,
        extraction_method="html",
        content_hash="hash",
        run_id="run-1",
    )
    record_observation(db, observation)

    history = get_price_history(db, offer_id, days=3650)
    assert any(row["price"] == 90.0 for row in history)


def test_record_observation_from_pydantic_observation_raises_on_unknown_offer(db):
    observation = Observation(
        offer_id=uuid4(),
        price=Decimal("90.00"),
        in_stock=True,
        extraction_method="html",
        content_hash="hash",
        run_id="run-1",
    )
    with pytest.raises(sqlite3.IntegrityError):
        record_observation(db, observation)
