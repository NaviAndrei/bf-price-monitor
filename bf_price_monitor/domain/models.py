from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field


class Watch(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    owner: str
    query: str | None = None
    direct_url: HttpUrl | None = None
    target_price: Decimal | None = None
    drop_rule: Literal["percentage", "absolute"]
    drop_threshold: Decimal = Field(gt=0)
    track_all_time_low: bool
    seller_policy: Literal["any", "trusted"]
    cadence_minutes: int = 120
    quiet_hours_start: int | None = None
    quiet_hours_end: int | None = None
    enabled: bool = True


class CanonicalProduct(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    brand: str
    model: str
    gtin: str | None = None
    mpn: str | None = None
    sku_aliases: list[str] = Field(default_factory=list)
    category: str
    specs: dict[str, str] = Field(default_factory=dict)


class Offer(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    canonical_product_id: UUID
    retailer: str
    retailer_sku: str | None = None
    canonical_url: HttpUrl
    seller: str | None = None
    fulfillment: Literal["direct", "marketplace", "unknown"]
    currency: str = "RON"


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    offer_id: UUID
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    price: Decimal
    shipping: Decimal = Decimal("0")
    reference_price: Decimal | None = None
    in_stock: bool
    extraction_method: Literal["html", "llm", "hybrid"]
    confidence: float = Field(ge=0, le=1, default=1.0)
    content_hash: str
    run_id: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_price(self) -> Decimal:
        return self.price + self.shipping


class AlertDecision(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    policy_version: str
    watch_id: UUID
    observation_id: UUID
    verdict: Literal["alert", "skip", "defer"]
    reasons: list[str] = Field(default_factory=list)
    evidence_urls: list[HttpUrl] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DeliveryAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    alert_decision_id: UUID
    channel: str
    destination: str
    attempt_number: int = Field(ge=1)
    response_class: Literal["2xx", "4xx", "5xx", "timeout", "unknown"]
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    dedup_key: str
    final_state: Literal["delivered", "failed", "deduped"]
