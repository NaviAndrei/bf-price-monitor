from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

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
