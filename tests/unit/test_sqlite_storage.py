from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bf_price_monitor.domain.models import DeliveryAttempt, Observation
from bf_price_monitor.storage.sqlite import (
    get_latest_price,
    get_price_history,
    init_db,
    record_delivery_attempt,
    record_delivery_attempt_new_cycle,
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


# --- T-47 (#66): durable cross-retailer identity links -----------------------

PCG_URL = "https://pcgarage.ro/notebook-laptop/asus/156-vivobook-15-x1504ma-fhd"
FLANCO_URL = "https://flanco.ro/laptop-asus-vivobook-15-x1504ma-bq200.html"
# What the pcgarage/flanco adapters actually derive from those URLs (last
# path segment, lowercased) -- kept consistent with the stored offer's sku
# since production always stores exactly this value (scripts/scrape.py's
# offer_sku()), and _resolve_product_id's sku_exact path depends on it.
PCG_SKU = "156-vivobook-15-x1504ma-fhd"
FLANCO_SKU = "laptop-asus-vivobook-15-x1504ma-bq200.html"

# Real titles from tests/unit/test_identity.py's fixtures: identical model
# code and configuration, scores 0.8143 ("medium", below HIGH_CONFIDENCE).
PCG_X1504MA = (
    "Laptop ASUS 15.6'' Vivobook 15 X1504MA, FHD, Procesor Intel® Core™ 3 304 "
    "(6M Cache, up to 4.30 GHz), 8GB DDR5, 512GB SSD, Intel Graphics, No OS, Blue"
)
FLANCO_X1504MA_MEDIUM = (
    'Laptop Asus Vivobook 15 X1504MA-BQ200, 15.6" FHD, Intel Core 3 304, 8GB, '
    "512GB SSD, Free Dos, Quiet Blue"
)
# Case-folded variant of PCG_X1504MA: normalize_title() folds case, so the
# token set -- and therefore the match score -- is identical (1.0, "high"),
# but the raw string differs, so it doesn't collide with PCG_X1504MA's own
# uuid5(title) hash by coincidence. No real fixture pair reaches
# HIGH_CONFIDENCE yet (see docs/DECISIONS.md); this exercises that path
# deliberately.
FLANCO_X1504MA_HIGH = PCG_X1504MA.upper()


def test_high_confidence_cross_retailer_match_merges_into_one_product_row(db):
    record_observation(
        db, _obs(sku=PCG_SKU, retailer="pcgarage", url=PCG_URL, title=PCG_X1504MA)
    )
    record_observation(
        db,
        _obs(
            sku=FLANCO_SKU,
            retailer="flanco",
            url=FLANCO_URL,
            title=FLANCO_X1504MA_HIGH,
        ),
    )

    assert db.execute("SELECT COUNT(*) FROM canonical_products").fetchone()[0] == 1
    pcg_product = get_latest_price(db, "pcgarage", PCG_SKU)["product_id"]
    flanco_product = get_latest_price(db, "flanco", FLANCO_SKU)["product_id"]
    assert pcg_product == flanco_product

    links = db.execute("SELECT * FROM cross_retailer_links").fetchall()
    assert len(links) == 1
    assert links[0]["method"] == "fuzzy_title"
    assert links[0]["linked_product_id"] == pcg_product

    remaps = db.execute("SELECT * FROM product_id_remaps").fetchall()
    assert len(remaps) == 1
    assert remaps[0]["new_product_id"] == pcg_product


def test_medium_confidence_cross_retailer_match_links_without_merging(db):
    record_observation(
        db, _obs(sku=PCG_SKU, retailer="pcgarage", url=PCG_URL, title=PCG_X1504MA)
    )
    record_observation(
        db,
        _obs(
            sku=FLANCO_SKU,
            retailer="flanco",
            url=FLANCO_URL,
            title=FLANCO_X1504MA_MEDIUM,
        ),
    )

    # Below HIGH_CONFIDENCE: the link is recorded, but each retailer keeps
    # its own canonical_products row -- no merge on a medium-confidence guess.
    assert db.execute("SELECT COUNT(*) FROM canonical_products").fetchone()[0] == 2
    pcg_product = get_latest_price(db, "pcgarage", PCG_SKU)["product_id"]
    flanco_product = get_latest_price(db, "flanco", FLANCO_SKU)["product_id"]
    assert pcg_product != flanco_product

    links = db.execute("SELECT * FROM cross_retailer_links").fetchall()
    assert len(links) == 1
    assert links[0]["method"] == "fuzzy_title"
    assert links[0]["confidence"] < 0.85
    assert links[0]["linked_product_id"] == pcg_product

    assert db.execute("SELECT COUNT(*) FROM product_id_remaps").fetchone()[0] == 0


def test_cross_retailer_link_is_durable_across_a_reopened_connection(tmp_path):
    db_path = tmp_path / "durable.sqlite3"
    conn = init_db(db_path)
    record_observation(
        conn,
        _obs(sku=PCG_SKU, retailer="pcgarage", url=PCG_URL, title=PCG_X1504MA),
    )
    record_observation(
        conn,
        _obs(
            sku=FLANCO_SKU,
            retailer="flanco",
            url=FLANCO_URL,
            title=FLANCO_X1504MA_HIGH,
        ),
    )
    conn.close()

    reopened = init_db(db_path)
    try:
        links = reopened.execute("SELECT * FROM cross_retailer_links").fetchall()
        assert len(links) == 1
        products = reopened.execute(
            "SELECT COUNT(*) FROM canonical_products"
        ).fetchone()[0]
        assert products == 1
    finally:
        reopened.close()


def test_sku_exact_match_keeps_product_id_stable_across_title_text_edits(db):
    # sku must match what the emag adapter derives from url (the id after
    # /pd/), since that's what _resolve_product_id's own extraction uses --
    # not the stored sku field, which record_observation never re-derives.
    emag_sku = "SKU1AAAAAA"
    emag_url = f"https://emag.ro/laptop-original/pd/{emag_sku}"
    record_observation(
        db,
        _obs(sku=emag_sku, retailer="emag", url=emag_url, title="Laptop Original"),
    )
    first_product_id = get_latest_price(db, "emag", emag_sku)["product_id"]

    # Retailer edits the listed title text; same retailer+sku is still the
    # same offer, so its product_id must not churn on the next scrape.
    record_observation(
        db,
        _obs(
            sku=emag_sku,
            retailer="emag",
            url=emag_url,
            title="Laptop Original (Updated Listing Text)",
            scraped_at="2026-09-21T10:00:00+00:00",
        ),
    )
    second_product_id = get_latest_price(db, "emag", emag_sku)["product_id"]

    assert second_product_id == first_product_id
    assert db.execute("SELECT COUNT(*) FROM canonical_products").fetchone()[0] == 1


# --- T-37b (#55): delivery_attempts audit table ------------------------------


def _attempt(**overrides):
    base = {
        "alert_decision_id": uuid4(),
        "channel": "telegram",
        "destination": "dest-ref-1",
        "attempt_number": 1,
        "response_class": "2xx",
        "dedup_key": "dedup-1",
        "final_state": "delivered",
    }
    base.update(overrides)
    return DeliveryAttempt(**base)


def test_init_db_creates_delivery_attempts_table_idempotently(tmp_path):
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
    assert "delivery_attempts" in tables


def test_record_delivery_attempt_inserts_one_row(db):
    attempt = _attempt()
    record_delivery_attempt(db, attempt)

    rows = db.execute("SELECT * FROM delivery_attempts").fetchall()
    assert len(rows) == 1
    assert rows[0]["alert_decision_id"] == str(attempt.alert_decision_id)
    assert rows[0]["final_state"] == "delivered"
    assert rows[0]["destination_ref"] == "dest-ref-1"


def test_record_delivery_attempt_same_attempt_number_is_idempotent(db):
    decision_id = uuid4()
    record_delivery_attempt(
        db, _attempt(alert_decision_id=decision_id, final_state="failed")
    )
    # Replay of the same logical attempt (e.g. a re-run of the notify step)
    # must update in place, not append a duplicate audit row.
    record_delivery_attempt(
        db, _attempt(alert_decision_id=decision_id, final_state="delivered")
    )

    rows = db.execute(
        "SELECT * FROM delivery_attempts WHERE alert_decision_id = ?",
        (str(decision_id),),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["final_state"] == "delivered"


def test_record_delivery_attempt_new_attempt_number_adds_new_row(db):
    decision_id = uuid4()
    record_delivery_attempt(
        db, _attempt(alert_decision_id=decision_id, attempt_number=1)
    )
    record_delivery_attempt(
        db, _attempt(alert_decision_id=decision_id, attempt_number=2)
    )

    rows = db.execute(
        "SELECT * FROM delivery_attempts WHERE alert_decision_id = ?",
        (str(decision_id),),
    ).fetchall()
    assert len(rows) == 2


def test_record_delivery_attempt_accepts_none_dedup_key_for_health_alerts(db):
    record_delivery_attempt(db, _attempt(dedup_key=None))

    row = db.execute("SELECT * FROM delivery_attempts").fetchone()
    assert row["dedup_key"] is None


def test_record_delivery_attempt_rejects_invalid_literal_before_insert(db):
    with pytest.raises(ValidationError):
        record_delivery_attempt(
            db,
            {
                "alert_decision_id": str(uuid4()),
                "channel": "telegram",
                "destination": "dest-ref-1",
                "attempt_number": 1,
                "response_class": "not-a-real-class",
                "dedup_key": "dedup-1",
                "final_state": "delivered",
            },
        )
    assert db.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0] == 0


def test_delivery_attempt_lookup_by_dedup_key(db):
    record_delivery_attempt(db, _attempt(dedup_key="find-me"))
    record_delivery_attempt(
        db, _attempt(alert_decision_id=uuid4(), dedup_key="other-key")
    )

    rows = db.execute(
        "SELECT * FROM delivery_attempts WHERE dedup_key = ?", ("find-me",)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == "find-me"


def _new_cycle_attempt(**overrides):
    base = {
        "alert_decision_id": uuid4(),
        "channel": "telegram",
        "destination": "dest-ref-1",
        "response_class": "2xx",
        "dedup_key": "dedup-1",
        "final_state": "delivered",
    }
    base.update(overrides)
    return base


def test_record_delivery_attempt_new_cycle_allocates_sequential_numbers(db):
    decision_id = uuid4()
    first = record_delivery_attempt_new_cycle(
        db, _new_cycle_attempt(alert_decision_id=decision_id)
    )
    second = record_delivery_attempt_new_cycle(
        db, _new_cycle_attempt(alert_decision_id=decision_id, final_state="failed")
    )

    assert (first, second) == (1, 2)
    rows = db.execute(
        "SELECT attempt_number, final_state FROM delivery_attempts "
        "WHERE alert_decision_id = ? ORDER BY attempt_number",
        (str(decision_id),),
    ).fetchall()
    assert [r["attempt_number"] for r in rows] == [1, 2]
    assert [r["final_state"] for r in rows] == ["delivered", "failed"]


def test_record_delivery_attempt_new_cycle_is_independent_per_decision(db):
    first_id = uuid4()
    second_id = uuid4()
    record_delivery_attempt_new_cycle(
        db, _new_cycle_attempt(alert_decision_id=first_id)
    )
    number = record_delivery_attempt_new_cycle(
        db, _new_cycle_attempt(alert_decision_id=second_id)
    )
    assert number == 1  # a different alert_decision_id starts its own sequence


def test_record_delivery_attempt_new_cycle_allocates_atomically_under_race(tmp_path):
    # Real concurrency, not sequential calls: two separate connections to the
    # same on-disk database race to allocate an attempt_number for the same
    # alert_decision_id. A prior implementation did a standalone
    # SELECT MAX(...) in Python before its own INSERT, leaving a window where
    # both threads could read the same MAX and either collide on the
    # UNIQUE(alert_decision_id, attempt_number) constraint or silently
    # duplicate a number. record_delivery_attempt_new_cycle instead computes
    # MAX+1 inside the INSERT ... SELECT itself, so SQLite's write lock
    # serializes the two calls and each sees the other's committed row.
    db_path = tmp_path / "race.sqlite3"
    decision_id = uuid4()
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    # Create the file and switch it to WAL mode once, up front. That one-time
    # mode switch needs its own exclusive lock; racing it across both threads
    # would test SQLite's file-creation locking, not our allocation logic.
    init_db(db_path).close()

    def _allocate(key):
        # Each thread opens its own connection: sqlite3 connections are only
        # usable from the thread that created them (check_same_thread), and
        # opening per-thread is also what two separate live processes would
        # actually look like from SQLite's point of view.
        conn = init_db(db_path)
        try:
            barrier.wait(timeout=5)
            results[key] = record_delivery_attempt_new_cycle(
                conn, _new_cycle_attempt(alert_decision_id=decision_id)
            )
        except BaseException as exc:  # captured; asserted on the main thread
            errors.append(exc)
        finally:
            conn.close()

    t_a = threading.Thread(target=_allocate, args=("a",))
    t_b = threading.Thread(target=_allocate, args=("b",))
    t_a.start()
    t_b.start()
    t_a.join(timeout=5)
    t_b.join(timeout=5)

    assert errors == []
    assert {results["a"], results["b"]} == {1, 2}

    conn = init_db(db_path)
    try:
        rows = conn.execute(
            "SELECT attempt_number FROM delivery_attempts WHERE alert_decision_id = ?",
            (str(decision_id),),
        ).fetchall()
    finally:
        conn.close()
    assert sorted(r["attempt_number"] for r in rows) == [1, 2]
