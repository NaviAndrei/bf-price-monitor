"""Read-only dataset assembly for the anomaly pilot.

Every feature is causal: a row is built only from the offer's observations
at or before its own timestamp, so a chronological split can't leak the
future into training. Labels come only from real Telegram feedback
(alert_feedback, #37); nothing here invents or infers a label.
"""

from __future__ import annotations

import hashlib
import sqlite3
import statistics
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bf_price_monitor.storage.feedback import list_feedback

# Explicit label policy. Price-anomaly relevance is only defined for
# labels that judge the price itself; identity problems (wrong_product,
# duplicate) say nothing about whether the price moved abnormally.
LABEL_RELEVANCE: Mapping[str, int | None] = {
    "useful": 1,
    "purchased": 1,
    "fake_discount": 0,
    "wrong_price": 0,
    "wrong_product": None,
    "duplicate": None,
}


class InsufficientDataError(ValueError):
    pass


@dataclass(frozen=True)
class Observation:
    offer_id: str
    retailer: str
    url: str
    price: float
    scraped_at: datetime


@dataclass
class FeatureRow:
    offer_id: str
    retailer: str
    scraped_at: datetime
    price: float
    features: dict[str, float]
    # The offer's prices up to and including this row, for PELT.
    history: tuple[float, ...]
    decision_ids: tuple[str, ...]
    deterministic_alert: bool = False
    label: int | None = None


@dataclass(frozen=True)
class Split:
    train: list[FeatureRow]
    validation: list[FeatureRow]
    test: list[FeatureRow]
    validation_start: datetime
    test_start: datetime


@dataclass
class LabelSet:
    by_alert: dict[str, int] = field(default_factory=dict)
    raw_counts: dict[str, int] = field(default_factory=dict)
    excluded_by_policy: int = 0
    tied_alerts: int = 0
    matched_alerts: int = 0
    unmatched_alerts: int = 0


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    # mode=ro: the pilot must never migrate or write the monitor's database.
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def load_observations(conn: sqlite3.Connection) -> list[Observation]:
    rows = conn.execute(
        "SELECT o.id AS offer_id, o.retailer, o.url, p.price, p.scraped_at "
        "FROM price_observations p JOIN offers o ON o.id = p.offer_id"
    ).fetchall()
    observations = [
        Observation(
            offer_id=row["offer_id"],
            retailer=row["retailer"],
            url=row["url"],
            price=float(row["price"]),
            scraped_at=_parse_utc(row["scraped_at"]),
        )
        for row in rows
    ]
    observations.sort(key=lambda o: (o.offer_id, o.scraped_at))
    return observations


def alert_decision_ids(url: str, price: float, retailer: str) -> tuple[str, ...]:
    """AlertDecision ids notify.py would give this observation.

    Mirrors notify._deal_event_id(_deal_dedup_key(alert)); a test pins the
    two together. The integer spelling is also tried because the alert's
    price went through JSON, where 1299.0 and 1299 can both occur.
    """
    spellings = [str(price)]
    if price.is_integer():
        spellings.append(str(int(price)))
    ids = []
    for spelled in spellings:
        dedup = hashlib.sha256(f"{url}:{spelled}:{retailer}".encode()).hexdigest()
        ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"alert-decision:{dedup}")))
    return tuple(ids)


def _median_or_none(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def build_feature_rows(observations: Iterable[Observation]) -> list[FeatureRow]:
    by_offer: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        by_offer[obs.offer_id].append(obs)

    rows: list[FeatureRow] = []
    for series in by_offer.values():
        series.sort(key=lambda o: o.scraped_at)
        prices = [o.price for o in series]
        last_change_at = series[0].scraped_at
        for i in range(1, len(series)):
            obs = series[i]
            # Latest price change strictly before this row.
            if i >= 2 and prices[i - 1] != prices[i - 2]:
                last_change_at = series[i - 1].scraped_at
            prev = prices[i - 1]
            now = obs.scraped_at
            window30 = [
                o.price for o in series[:i] if o.scraped_at >= now - timedelta(days=30)
            ]
            window7 = [
                o.price
                for o in series[: i + 1]
                if o.scraped_at >= now - timedelta(days=7)
            ]
            median30 = _median_or_none(window30)
            mean7 = statistics.fmean(window7)
            features = {
                "drop_pct": (prev - obs.price) / prev * 100 if prev > 0 else 0.0,
                "relative_to_30d_median": (obs.price / median30 - 1)
                if median30
                else 0.0,
                "volatility_score_7d": statistics.pstdev(window7) / mean7
                if len(window7) >= 2 and mean7 > 0
                else 0.0,
                "hour_of_day": float(now.astimezone(UTC).hour),
                "days_since_last_change": (now - last_change_at).total_seconds()
                / 86400,
            }
            rows.append(
                FeatureRow(
                    offer_id=obs.offer_id,
                    retailer=obs.retailer,
                    scraped_at=now,
                    price=obs.price,
                    features=features,
                    history=tuple(prices[: i + 1]),
                    decision_ids=alert_decision_ids(obs.url, obs.price, obs.retailer),
                )
            )
    rows.sort(key=lambda r: (r.scraped_at, r.offer_id))
    return rows


def load_decided_alert_ids(conn: sqlite3.Connection) -> set[str]:
    """AlertDecision ids the deterministic policy actually sent (#55 trail)."""
    if not _table_exists(conn, "delivery_attempts"):
        return set()
    rows = conn.execute("SELECT DISTINCT alert_decision_id FROM delivery_attempts")
    return {row[0] for row in rows}


def load_labels(conn: sqlite3.Connection) -> LabelSet:
    labels = LabelSet()
    if not _table_exists(conn, "alert_feedback_current"):
        return labels
    votes: dict[str, Counter[int]] = defaultdict(Counter)
    raw: Counter[str] = Counter()
    for item in list_feedback(conn, current_only=True):
        raw[item["label"]] += 1
        relevance = LABEL_RELEVANCE.get(item["label"])
        if relevance is None:
            labels.excluded_by_policy += 1
            continue
        votes[item["alert_decision_id"]][relevance] += 1
    labels.raw_counts = dict(sorted(raw.items()))
    for alert_id, counter in votes.items():
        ranked = counter.most_common()
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            labels.tied_alerts += 1
            continue
        labels.by_alert[alert_id] = ranked[0][0]
    return labels


def attach_outcomes(
    rows: Sequence[FeatureRow], decided: set[str], labels: LabelSet
) -> None:
    matched: set[str] = set()
    for row in rows:
        row.deterministic_alert = any(i in decided for i in row.decision_ids)
        for alert_id in row.decision_ids:
            if alert_id in labels.by_alert:
                row.label = labels.by_alert[alert_id]
                matched.add(alert_id)
                break
    labels.matched_alerts = len(matched)
    labels.unmatched_alerts = len(labels.by_alert) - len(matched)


def chronological_split(
    rows: Sequence[FeatureRow], train_fraction: float, validation_fraction: float
) -> Split:
    ordered = sorted(rows, key=lambda r: (r.scraped_at, r.offer_id))
    n = len(ordered)
    if n < 3:
        raise InsufficientDataError(f"need at least 3 feature rows, have {n}")
    # Boundaries are timestamps, so rows sharing a timestamp never straddle
    # two splits.
    validation_start = ordered[min(int(n * train_fraction), n - 1)].scraped_at
    test_start = ordered[
        min(int(n * (train_fraction + validation_fraction)), n - 1)
    ].scraped_at
    train = [r for r in ordered if r.scraped_at < validation_start]
    validation = [r for r in ordered if validation_start <= r.scraped_at < test_start]
    test = [r for r in ordered if r.scraped_at >= test_start]
    if not (train and validation and test):
        raise InsufficientDataError(
            "chronological split left an empty partition "
            f"(train={len(train)}, validation={len(validation)}, test={len(test)})"
        )
    return Split(train, validation, test, validation_start, test_start)


def data_fingerprint(
    observations: Sequence[Observation], decided: set[str], labels: LabelSet
) -> str:
    digest = hashlib.sha256()
    for obs in sorted(observations, key=lambda o: (o.offer_id, o.scraped_at)):
        digest.update(
            f"{obs.offer_id}|{obs.scraped_at.isoformat()}|{obs.price!r}\n".encode()
        )
    for alert_id in sorted(decided):
        digest.update(f"decided|{alert_id}\n".encode())
    for alert_id, label in sorted(labels.by_alert.items()):
        digest.update(f"label|{alert_id}|{label}\n".encode())
    return "sha256:" + digest.hexdigest()


def data_profile(
    observations: Sequence[Observation], rows: Sequence[FeatureRow]
) -> dict[str, Any]:
    if not observations:
        return {"observations": 0, "offers": 0, "feature_rows": 0}
    times = [o.scraped_at for o in observations]
    midnight = sum(
        1 for t in times if (t.hour, t.minute, t.second, t.microsecond) == (0, 0, 0, 0)
    )
    return {
        "observations": len(observations),
        "offers": len({o.offer_id for o in observations}),
        "feature_rows": len(rows),
        "retailers": dict(sorted(Counter(o.retailer for o in observations).items())),
        "first_observation_utc": min(times).isoformat(),
        "last_observation_utc": max(times).isoformat(),
        # Migrated JSON history is dated, not timed (00:00:00 UTC), which
        # makes hour_of_day an artifact for those rows.
        "midnight_timestamp_fraction": round(midnight / len(times), 4),
    }
