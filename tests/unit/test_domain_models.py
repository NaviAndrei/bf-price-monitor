from __future__ import annotations

import hashlib
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from pydantic import ValidationError

from bf_price_monitor.domain import (
    AlertDecision,
    CanonicalProduct,
    DeliveryAttempt,
    Observation,
    Offer,
    Watch,
)


def test_watch_happy_path():
    watch = Watch(
        owner="ivan",
        query="laptop lenovo v15",
        drop_rule="percentage",
        drop_threshold=Decimal("5"),
        track_all_time_low=True,
        seller_policy="any",
    )
    assert watch.enabled is True
    assert watch.cadence_minutes == 120


def test_watch_negative_drop_threshold_raises():
    with pytest.raises(ValidationError):
        Watch(
            owner="ivan",
            query="laptop lenovo v15",
            drop_rule="percentage",
            drop_threshold=Decimal("-5"),
            track_all_time_low=True,
            seller_policy="any",
        )


def test_canonical_product_happy_path():
    product = CanonicalProduct(
        brand="Lenovo",
        model="V15 G4 AMN",
        category="laptop",
    )
    assert product.sku_aliases == []
    assert product.specs == {}


def test_offer_happy_path():
    offer = Offer(
        canonical_product_id=uuid4(),
        retailer="emag",
        canonical_url="https://www.emag.ro/some-product/pd/ABC123/",
        fulfillment="direct",
    )
    assert offer.currency == "RON"


def test_observation_total_price_is_price_plus_shipping():
    observation = Observation(
        offer_id=uuid4(),
        price=Decimal("100.50"),
        shipping=Decimal("15.25"),
        in_stock=True,
        extraction_method="html",
        confidence=0.9,
        content_hash="abc123",
        run_id="run-1",
    )
    assert observation.total_price == Decimal("115.75")


def test_alert_decision_happy_path():
    decision = AlertDecision(
        policy_version="v1",
        watch_id=uuid4(),
        observation_id=uuid4(),
        verdict="alert",
        reasons=["price dropped below target"],
    )
    assert decision.verdict == "alert"


def test_observation_from_legacy_scrape_dict_round_trips():
    # Mirrors the dict scripts/scrape.py builds for a real listing result
    # (see the SCRAPERS functions' return shape) plus the derived fields it
    # adds at the Observation.model_validate call site.
    legacy_result = {
        "title": "Laptop Lenovo V15 G4 AMN",
        "price": 2499.99,
        "url": "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/",
        "stock_status": "in_stock",
    }
    observation = Observation.model_validate(
        {
            "offer_id": uuid5(NAMESPACE_URL, legacy_result["url"]),
            "price": Decimal(str(legacy_result["price"])),
            "shipping": Decimal("0"),
            "reference_price": None,
            "in_stock": legacy_result["stock_status"] != "out_of_stock",
            "extraction_method": "html",
            "content_hash": hashlib.sha256(
                f"{legacy_result['title']}|{legacy_result['price']}|"
                f"{legacy_result['stock_status']}".encode()
            ).hexdigest(),
            "run_id": "run-1",
        }
    )
    assert observation.total_price == Decimal("2499.99")
    assert observation.in_stock is True


def test_alert_decision_from_legacy_alert_dict_round_trips():
    # Mirrors the alert dict scripts/analyze.py writes to formatted_alerts.json
    # (site/query/url from scrape.py's alerts.append, rule_verdict from
    # evaluate_omnibus_rule) plus the derived fields scripts/notify.py adds
    # at the AlertDecision.model_validate call site.
    legacy_alert = {
        "site": "emag",
        "query": "laptop lenovo v15",
        "url": "https://www.emag.ro/laptop-lenovo-v15/pd/ABC123/",
        "rule_verdict": "GENUINE_DEAL",
    }
    decision = AlertDecision.model_validate(
        {
            "policy_version": "omnibus-v1",
            "watch_id": uuid5(
                NAMESPACE_URL, f"{legacy_alert['site']}:{legacy_alert['query']}"
            ),
            "observation_id": uuid5(NAMESPACE_URL, legacy_alert["url"]),
            "verdict": "alert",
            "reasons": [legacy_alert["rule_verdict"]],
            "evidence_urls": [legacy_alert["url"]],
        }
    )
    assert decision.verdict == "alert"
    assert decision.reasons == ["GENUINE_DEAL"]


def test_delivery_attempt_happy_path():
    attempt = DeliveryAttempt(
        alert_decision_id=uuid4(),
        channel="telegram",
        destination="chat-123",
        attempt_number=1,
        response_class="2xx",
        dedup_key="dedup-1",
        final_state="delivered",
    )
    assert attempt.final_state == "delivered"
